"""Report rendering: the Plotly waterfall inside the mission-control shell.

Lifted from LEO_Waterfall_Phase_Offset.py. The figure, the layout, the shell
markup, the colours and the client-side behaviour are all as they were. Four
things changed, all of them forced by the move from a local script to a
multi-user service:

1. The CSS and the client-side JavaScript now live in assets/report.css and
   assets/report.js instead of a Python string literal, and are inlined at
   render time. The generated file is byte-for-byte equivalent in structure and
   still fully self-contained. The point is that code held in a Python string
   is invisible to the quality gate, to linting and to coverage; as real asset
   files they are analysed like any other source.

2. Every reflection site is escaped. In the original, group names were typed by
   the operator into a local ini file and object names came from a Space-Track
   catalogue lookup, so both were effectively trusted. Now group names arrive
   over HTTP from the picker and object names come from a UDL tenant, so both
   are untrusted input reaching an HTML sink. esc() is applied server-side and
   its counterpart in report.js client-side. Well-formed names render exactly
   as before.

3. render_report returns the HTML rather than writing it to a path, because the
   caller is now a request handler that may never touch a filesystem.
   write_report() is the thin wrapper for the cases that do.

4. SystemExit became ComputeError, so a group with no usable data returns a
   status code instead of killing the worker.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
from html import escape
from pathlib import Path

import numpy as np

from ..errors import ComputeError
from ..models import (ELSET_KEY, SRC_LABEL, SRC_ORDER, SRC_SHAPE, SRC_SYMBOL,
                      UNATTRIBUTED)
from ..physics import compute_series, propagate, reference_satrec
from ..quality import summarise
from ..storage import VolumeWriter

ASSETS = Path(__file__).parent / "assets"

REF_COLOUR = "#C67C00"                                    # copper-amber, reference only
COOL_PALETTE = ["#4C9BE8", "#27AE60", "#E0508A", "#9B8CFF", "#22C1C3", "#F1C40F",
                "#5DD39E", "#FF8C6B", "#7FB2FF", "#D98CFF"]


# --------------------------------------------------------------------------- #
#  Escaping
# --------------------------------------------------------------------------- #
def esc(value) -> str:
    """The single HTML escaper. Neutralises & < > " ' so an object or group name
    cannot break out of an attribute or a text node."""
    return escape(str(value), quote=True).replace("'", "&#x27;")


def json_for_html(obj) -> str:
    """JSON safe to embed in a <script> block.

    json.dumps alone is not enough: a group named ``</script><script>`` would
    close the block and everything after it would be parsed as markup. The
    line and paragraph separators are escaped too because they are literal
    line terminators in JavaScript.
    """
    return (json.dumps(obj)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


@functools.cache
def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
#  Series statistics and colours
# --------------------------------------------------------------------------- #
def _series_stats(series):
    """current offset, drift rate (s/day) via linear fit, and count."""
    if not series:
        return None
    ts = [point[0] for point in series]
    ys = [point[1] for point in series]
    if len(ys) > 1:
        t0 = ts[0]
        xs = np.array([(t - t0).total_seconds() for t in ts])
        slope = float(np.polyfit(xs, np.array(ys), 1)[0]) * 86400.0
    else:
        slope = 0.0
    return {"current": float(ys[-1]), "drift": slope, "n": len(ys)}


def _sat_colours(sat_order, ref_no):
    """Fixed colour per satellite for a given reference (reference = copper)."""
    out, ci = {}, 0
    for s in sat_order:
        if s == ref_no:
            out[s] = REF_COLOUR
        else:
            out[s] = COOL_PALETTE[ci % len(COOL_PALETTE)]
            ci += 1
    return out


# --------------------------------------------------------------------------- #
#  Datasets
# --------------------------------------------------------------------------- #
def _source_line(key: str) -> str:
    """The provenance fragment of a point's tooltip.

    Only the element-set series gets one. Its query is not filtered by
    provider, so who produced a given point is a property of that point; the
    state-vector series are queried per provider, and repeating the provider
    name that is already on the line above would be noise.
    """
    # A literal middot, not the HTML entity. Plotly's hovertemplate decodes
    # only the basic entities (&amp;, &lt;, &gt;) and leaves named ones alone,
    # so "&middot;" appeared on screen verbatim. Verified by hovering a real
    # point in a browser, which is the only thing that shows it.
    #
    # The escaping of the values interpolated into this template is still
    # correct, and safe: Plotly parses its pseudo-HTML tags BEFORE decoding
    # entities, so an escaped "&lt;script&gt;" is displayed as inert text
    # rather than becoming a tag. Also verified rather than assumed.
    return " \u00b7 %{customdata}" if key == ELSET_KEY else ""


def _object_traces(sat_no, name, present, series):
    """The per-source trace arrays, per-point provenance, and the summary card.

    `srcs` is the originator of each individual point, which the plot shows in
    the tooltip. It matters most for the element-set series, whose query is not
    filtered by provider, so the series label alone does not say who produced
    any given point.
    """
    xs, ys, srcs, counts, headline = [], [], [], {}, None
    for key in present:
        ser = series.get(key, [])
        xs.append([point[1] for point in ser])
        ys.append([point[0].isoformat() for point in ser])
        # Escaped here, at the point it enters the dataset. These strings come
        # from a UDL tenant and end up in a Plotly hovertemplate, which renders
        # as HTML, so this is a reflection site like any other.
        srcs.append([esc(point[2] or UNATTRIBUTED) for point in ser])
        counts[key] = len(ser)
        if headline is None and ser:
            headline = _series_stats(ser)
    primary = headline or {"current": 0.0, "drift": 0.0, "n": 0}
    card = {
        "norad": sat_no,
        "name": name,
        "current": primary["current"],
        "drift": primary["drift"],
        "counts": counts,
        "absent": all(v == 0 for v in counts.values())}
    return xs, ys, srcs, card


def _dataset(objects_by_sat, sat_order, present, names, ref_no, ref_epoch, invert, window):
    """One (mode, reference) dataset with a FIXED layout: for each sat, one trace
    per source key in `present` (state providers, then the element-set series).
    Missing data yields empty arrays so trace indices stay stable for restyle."""
    ref_sat = reference_satrec(list(objects_by_sat.values()), ref_no, ref_epoch)
    colour = _sat_colours(sat_order, ref_no)
    xs, ys, srcs, colours, cards = [], [], [], [], []
    for s in sat_order:
        obj = objects_by_sat.get(s)
        nm = names.get(s, f"OBJECT {s}")
        series = compute_series(obj, ref_sat, invert) if obj else {}
        obj_xs, obj_ys, obj_srcs, card = _object_traces(s, nm, present, series)
        xs.extend(obj_xs)
        ys.extend(obj_ys)
        srcs.extend(obj_srcs)
        colours.extend([colour[s]] * len(present))
        card.update(colour=colour[s], is_ref=(s == ref_no))
        cards.append(card)
    _r0, v0 = propagate(ref_sat, window[0])
    return {
        "x": xs,
        "y": ys,
        "srcs": srcs,
        "colours": colours,
        "cards": cards,
        "vkms": round(float(np.linalg.norm(v0)), 3)}


# --------------------------------------------------------------------------- #
#  Plotly figure
# --------------------------------------------------------------------------- #
def _marker(key, colour):
    symbol = SRC_SYMBOL.get(key, "circle")
    is_open = symbol.endswith("-open")
    return {
        "symbol": symbol,
        "size": 8 if is_open else 7,
        "color": colour,
        "opacity": 0.95,
        "line": {"width": 1.4 if is_open else 0, "color": colour}}


def _layout():
    """The figure layout. Values as the original set them; only the dict
    literals are formatted differently."""
    # Not shared between the two axes: plotly holds what it is given, and a
    # dict reused across both would let a mutation of one change the other.
    grid = "rgba(115,155,207,0.10)"
    mono = "ui-monospace,Menlo,monospace"
    return {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {
            "color": "#8FA0BE",
            "family": "ui-monospace,'SF Mono',Menlo,Consolas,monospace",
            "size": 12,
        },
        "showlegend": False,
        "autosize": True,
        "margin": {"l": 64, "r": 24, "t": 16, "b": 52},
        "hoverlabel": {
            "bgcolor": "#111a30",
            "bordercolor": "#385FAF",
            "font": {"color": "#E6ECF5", "family": mono, "size": 12},
        },
        "xaxis": {
            "title": {"text": "PHASE OFFSET  \u00b7  seconds along-track",
                      "font": {"size": 11, "color": "#6C7C9C"}},
            "gridcolor": grid,
            "zeroline": True,
            "zerolinecolor": "rgba(198,124,0,0.55)",
            "zerolinewidth": 1.5,
            "tickfont": {"size": 11},
            "showline": False,
        },
        "yaxis": {
            "title": {"text": "EPOCH (UTC)",
                      "font": {"size": 11, "color": "#6C7C9C"}},
            "autorange": "reversed",
            "gridcolor": grid,
            "tickfont": {"size": 11},
            "showline": False,
        },
        "dragmode": "pan",
    }


def _figure(sat_order, present, names, ds, div_id, first):
    """Build the Plotly figure with a fixed len(present)-traces-per-object layout,
    seeded from dataset ds. Shape encodes source; colour encodes object."""
    import plotly.graph_objects as go
    fig = go.Figure()
    traces = []
    npresent = len(present)
    for k, s in enumerate(sat_order):
        nm = names.get(s, f"OBJECT {s}")
        for j, key in enumerate(present):
            idx = k * npresent + j
            colr = ds["colours"][idx]
            # Plotly renders hovertemplate as HTML, so the object name is
            # escaped here as well as at the markup sites. The per-point source
            # comes through customdata and is escaped when the dataset is
            # built, because it is a string from a UDL tenant.
            fig.add_trace(go.Scatter(
                x=ds["x"][idx], y=ds["y"][idx], mode="markers",
                customdata=ds["srcs"][idx],
                name=f"{s} {key}", marker=_marker(key, colr),
                hovertemplate=(f"<b>{esc(nm)}</b> · {s}<br>"
                               f"{esc(SRC_LABEL.get(key, key))}"
                               f"{_source_line(key)}<br>"
                               "%{y|%d %b %H:%M}Z<br>offset %{x:.1f} s<extra></extra>")))
            traces.append({"obj": s, "source": key})
    fig.update_layout(**_layout())
    plot_div = fig.to_html(full_html=False, include_plotlyjs=bool(first),
                           div_id=div_id, default_width="100%", default_height="100%",
                           config={"displaylogo": False, "responsive": True,
                                   "scrollZoom": True,
                                   "modeBarButtonsToRemove": ["select2d", "lasso2d"]})
    return plot_div, traces


# --------------------------------------------------------------------------- #
#  Panels
# --------------------------------------------------------------------------- #
def _has_source(objects_by_mode, key):
    """True when any object in any mode carries data for this source key."""
    for by_sat in objects_by_mode.values():
        for obj in by_sat.values():
            if key == ELSET_KEY and obj.elsets:
                return True
            if key != ELSET_KEY and obj.state_series.get(key):
                return True
    return False


def _present_sources(objects_by_mode, name):
    """Source keys carrying any data anywhere in this panel, canonical order."""
    present = [key for key in SRC_ORDER if _has_source(objects_by_mode, key)]
    if not present:
        raise ComputeError(f"[{name}] no state or TLE data in any source.")
    return present


def _mode_dataset(by_sat, sat_order, present, names, ref_epoch, invert, window):
    """Every viable reference anchor for one data mode."""
    refs, ref_sets = [], {}
    for s in sat_order:
        if s not in by_sat or not by_sat[s].elsets:
            continue  # need a TLE to anchor a reference
        try:
            ds = _dataset(by_sat, sat_order, present, names, s, ref_epoch, invert, window)
        except ComputeError:
            continue
        ref_sets[str(s)] = ds
        refs.append({"norad": s, "name": names.get(s, f"OBJECT {s}")})
    return refs, ref_sets


def _mode_data(objects_by_mode, mode_order, sat_order, present, names,
               default_ref, ref_epoch, invert, window, name):
    """Precompute a dataset per (mode, reference) so mode, reference and
    per-source visibility are all selectable client-side."""
    out = {}
    for mlabel in mode_order:
        refs, ref_sets = _mode_dataset(objects_by_mode.get(mlabel, {}), sat_order,
                                       present, names, ref_epoch, invert, window)
        if not ref_sets:
            continue
        dref = default_ref if str(default_ref) in ref_sets else refs[0]["norad"]
        out[mlabel] = {"refData": ref_sets, "refs": refs, "defaultRef": dref}
    if not out:
        raise ComputeError(f"[{name}] no usable data in any requested mode.")
    return out


def build_panel(panel_id, name, sat_order, names, objects_by_mode, mode_order,
                default_ref, invert, window, ref_epoch, first):
    """Build one group panel across all fetched data modes.

    The original took a `tle_source` argument to choose between Space-Track and
    the UDL. Element sets now always come from the UDL, so the parameter said
    nothing and was never read.
    """
    div_id = f"waterfall_{panel_id}"
    present = _present_sources(objects_by_mode, name)
    mode_data = _mode_data(objects_by_mode, mode_order, sat_order, present, names,
                           default_ref, ref_epoch, invert, window, name)

    first_mode = next(m for m in mode_order if m in mode_data)
    md0 = mode_data[first_mode]
    seed = md0["refData"][str(md0["defaultRef"])]
    plot_div, traces = _figure(sat_order, present, names, seed, div_id, first)
    present_meta = [{
        "key": k,
        "label": SRC_LABEL.get(k, k),
        "shape": SRC_SHAPE.get(SRC_SYMBOL.get(k, "circle"), "mk-circle")}
                    for k in present]

    return {
        "id": panel_id,
        "name": name,
        "div_id": div_id,
        "quality": _quality_findings(objects_by_mode),
        "plot_div": plot_div,
        "traces": traces,
        "present": present,
        "presentMeta": present_meta,
        "modeData": mode_data,
        "modeOrder": [m for m in mode_order if m in mode_data],
        "defaultMode": first_mode,
        "defaultRef": md0["defaultRef"],
        "v_kms": seed["vkms"],
        "window_start": window[0].strftime("%d %b %Y %H:%MZ"),
        "window_end": window[1].strftime("%d %b %Y %H:%MZ"),
        "source": " · ".join(SRC_LABEL.get(k, k) for k in present)}


# --------------------------------------------------------------------------- #
#  Shell markup
# --------------------------------------------------------------------------- #
def _mode_segment(p) -> str:
    """The REAL / SIM / TEST / EXERCISE selector, shown only when more than one
    data mode was fetched."""
    if len(p["modeOrder"]) <= 1:
        return ""
    buttons = "".join(f"<button data-mode='{esc(m)}'>{esc(m)}</button>"
                      for m in p["modeOrder"])
    return ("<div><h2>Data mode &mdash; REAL / SIM / TEST / EXERCISE</h2>"
            f"<div class='seg modeseg' data-group='{p['id']}'>{buttons}</div></div>")


def _source_chips(p) -> str:
    return "".join(
        f'<button class="srcchip active" data-src="{esc(m["key"])}">'
        f'<span class="mk {esc(m["shape"])}"></span>{esc(m["label"])}</button>'
        for m in p["presentMeta"])


def _quality_findings(objects_by_mode) -> dict:
    """Gather the ingestion findings for one panel's objects.

    Read off the objects rather than passed in, because that is where the
    fetcher put them and an extra argument is an extra thing to forget.
    """
    findings = []
    for by_sat in objects_by_mode.values():
        for obj in by_sat.values():
            if obj is not None:
                findings.extend(obj.findings)
    return summarise(findings)


def _conflict_line(finding: dict) -> str:
    """One source's same-epoch disagreement, in the terms an analyst needs.

    Which epochs, how the winner was chosen, and whether that choice meant
    anything. A conflict resolved arbitrarily is a different statement from one
    resolved by creation time, and reading the chart depends on knowing which.
    """
    conflicts = finding["conflicts"]
    epochs = ", ".join(esc(c["epoch"]) for c in conflicts[:3])
    if len(conflicts) > 3:
        epochs += f" and {len(conflicts) - 3} more"
    arbitrary = sum(1 for c in conflicts if c["arbitrary"])
    # Covers both undecidable cases: no creation stamp at all, and two stamps
    # that are identical. Naming only the first would be wrong for the second.
    how = ("resolved by the feed's creation stamp" if arbitrary == 0
           else "nothing in the feed distinguishes them, so the one plotted is "
                "whichever arrived first")
    return (f'<li><b>{esc(finding["source"])}</b> sent {len(conflicts)} '
            f"disagreeing report{'s' if len(conflicts) != 1 else ''} at the "
            f"same epoch ({epochs}). One of each pair is plotted and the other "
            f"is not: {how}.</li>")


def _quality_band(q: dict) -> str:
    """The data-quality band, shown only when there is something to say.

    Duplication is invisible on the chart by nature: two reports at one epoch
    overplot, so the picture looks identical whether a source sent one or five.
    A conflict is worse, because which record is plotted changes the reading.
    Neither belongs only in the pod log.
    """
    if not q["findings"]:
        return ""
    conflicting = [f for f in q["findings"] if f["conflicts"]]
    kind = "dq-alert" if conflicting else "dq-note"
    head = ("Same-epoch disagreement" if conflicting
            else "Duplicate reports collapsed")
    body = "".join(_conflict_line(f) for f in conflicting)
    if q["duplicates"]:
        sources = ", ".join(esc(f["source"]) for f in q["findings"]
                            if f["duplicates"])
        body += (f"<li>{q['duplicates']} identical report"
                 f"{'s' if q['duplicates'] != 1 else ''} arrived more than "
                 f"once ({sources}) and were collapsed. The chart is "
                 f"unaffected; the count says something about the feed.</li>")
    return (f'<div><h2>Data quality</h2><div class="dq {kind}" role="alert">'
            f"<b>{head}.</b><ul>{body}</ul></div></div>")


def _panel_hint(p) -> str:
    v = p["v_kms"]
    return (
        '<p class="hint"><b style="color:var(--muted)">Offset (s)</b> is along-track '
        "timing: how far apart along the orbit, expressed as travel time. &minus;120 s "
        "means the object passes a given point about 120 s after the reference &mdash; "
        f"trailing by roughly 120 s, near {v} km/s that is about {round(v)} km per second "
        'of offset. <b style="color:var(--muted)">Drift (s/day)</b> is how fast that gap '
        'is changing. <b style="color:var(--muted)">Closing / separating</b> combines the '
        "two: it is along-track timing only, not a conjunction &mdash; radial and "
        "cross-track separation are not shown here. Drag to pan, scroll to zoom, "
        "double-click to reset.</p>")


def _panel_section(p, active) -> str:
    cls = "panel active" if active else "panel"
    name = esc(p["name"])
    return f"""
<section class="{cls}" data-panel="{p['id']}">
  <div class="panelmeta">
    <div class="pmname">{name}</div>
    <div class="meta">
      <div><div class="k">Reference</div><div class="v copper" id="refmeta-{p['id']}"></div></div>
      <div><div class="k">Data mode</div><div class="v" id="modemeta-{p['id']}"></div></div>
      <div><div class="k">Window</div><div class="v">{esc(p['window_start'])} &rarr; {esc(p['window_end'])}</div></div>
      <div><div class="k">Sources</div><div class="v">{esc(p['source'])}</div></div>
    </div>
  </div>
  <div class="stripe"></div>
  <main>
    <aside class="rail">
      {_mode_segment(p)}
      <div>
        <h2>Reference &mdash; re-anchor the waterfall</h2>
        <select class="refsel" data-group="{p['id']}" id="refsel-{p['id']}"
                aria-label="Reference object for {name}"></select>
      </div>
      <div>
        <h2>Data sources &mdash; toggle (shape = source)</h2>
        <div class="srcseg" data-group="{p['id']}">
          {_source_chips(p)}
        </div>
      </div>
      {_quality_band(p["quality"])}
      <div>
        <h2>Objects &mdash; tap to isolate</h2>
        <div class="cards" id="cards-{p['id']}"></div>
      </div>
      <div>
        <h2>Relative motion &mdash; object to object</h2>
        <div id="rel-{p['id']}"></div>
      </div>
      {_panel_hint(p)}
    </aside>
    <div class="plotwrap">{p['plot_div']}</div>
  </main>
</section>"""


def _tab_buttons(panels) -> str:
    return "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-tab="{p["id"]}">'
        f'{esc(p["name"])}<span class="cnt">{len(p["traces"]) // 2}</span></button>'
        for i, p in enumerate(panels))


def _report_payload(panels) -> str:
    """The data the client-side code reads, as an escaped JSON block."""
    groups = [{
        "id": p["id"],
        "divId": p["div_id"],
        "traces": p["traces"],
        "present": p["present"],
        "modeData": p["modeData"],
        "modeOrder": p["modeOrder"],
        "defaultMode": p["defaultMode"],
        "defaultRef": p["defaultRef"]}
              for p in panels]
    return json_for_html({"groups": groups, "srclbl": SRC_LABEL})


def render_report(panels, classification: str, generated: dt.datetime | None = None) -> str:
    """Assemble the multi-group tabbed report into one self-contained HTML string."""
    if not panels:
        raise ComputeError("nothing to render: no panels were built")
    when = generated or dt.datetime.now(dt.UTC)
    stamp = when.strftime("%d %b %Y %H:%M:%SZ")
    plural = "S" if len(panels) != 1 else ""
    body = f"""
<div class="app">
  <div class="classif">{esc(classification.upper())}</div>
  <header>
    <div class="brand">
      <p class="eyebrow">Bluestaq Limited · Space Domain Awareness</p>
      <h1>Phase Offset Waterfall <b>&mdash; relative along-track drift</b></h1>
    </div>
    <div class="gen">GENERATED {esc(stamp)}</div>
  </header>
  <nav class="tabs">{_tab_buttons(panels)}</nav>
  <div class="panels">{"".join(_panel_section(p, i == 0) for i, p in enumerate(panels))}</div>
  <div class="foot">
    <span>BLUESTAQ LIMITED &nbsp;·&nbsp; MISSION CRITICAL SOLUTIONS</span>
    <span class="r"><span>{len(panels)} GROUP{plural}</span></span>
  </div>
</div>
<script id="report-data" type="application/json">{_report_payload(panels)}</script>
<script>
{_asset("report.js")}
</script>
"""
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Phase Offset Waterfall</title><style>"
            + _asset("report.css") + "</style></head><body>"
            + body + "</body></html>")


def write_report(html: str, out_path) -> Path:
    """Write a report to a path, for the command-line entry point.

    Through the shared writer like every other write in the application, so a
    local run on an unusual filesystem behaves the same way the service does.
    """
    path = Path(out_path)
    VolumeWriter().write(path, html)
    return path
