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
from ..models import SRC_LABEL, SRC_ORDER, SRC_SHAPE, SRC_SYMBOL
from ..physics import compute_series, propagate, reference_satrec

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
            .replace(" ", "\\u2028")
            .replace(" ", "\\u2029"))


@functools.lru_cache(maxsize=None)
def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
#  Series statistics and colours
# --------------------------------------------------------------------------- #
def _series_stats(series):
    """current offset, drift rate (s/day) via linear fit, and count."""
    if not series:
        return None
    ts = [e for e, _ in series]
    ys = [o for _, o in series]
    if len(ys) > 1:
        t0 = ts[0]
        xs = np.array([(t - t0).total_seconds() for t in ts])
        slope = float(np.polyfit(xs, np.array(ys), 1)[0]) * 86400.0
    else:
        slope = 0.0
    return dict(current=float(ys[-1]), drift=slope, n=len(ys))


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
def _object_traces(obj, sat_no, name, present, series):
    """The per-source trace arrays and the summary card for one object."""
    xs, ys, counts, headline = [], [], {}, None
    for key in present:
        ser = series.get(key, [])
        xs.append([o for _, o in ser])
        ys.append([e.isoformat() for e, _ in ser])
        counts[key] = len(ser)
        if headline is None and ser:
            headline = _series_stats(ser)
    primary = headline or dict(current=0.0, drift=0.0, n=0)
    card = dict(norad=sat_no, name=name, current=primary["current"],
                drift=primary["drift"], counts=counts,
                absent=all(v == 0 for v in counts.values()))
    return xs, ys, card


def _dataset(objects_by_sat, sat_order, present, names, ref_no, ref_epoch, invert, window):
    """One (mode, reference) dataset with a FIXED layout: for each sat, one trace
    per source key in `present` (state providers then Space-Track). Missing data
    yields empty arrays so trace indices stay stable for restyle."""
    ref_sat = reference_satrec(list(objects_by_sat.values()), ref_no, ref_epoch)
    colour = _sat_colours(sat_order, ref_no)
    xs, ys, colours, cards = [], [], [], []
    for s in sat_order:
        obj = objects_by_sat.get(s)
        nm = names.get(s, f"OBJECT {s}")
        series = compute_series(obj, ref_sat, invert) if obj else {}
        obj_xs, obj_ys, card = _object_traces(obj, s, nm, present, series)
        xs.extend(obj_xs)
        ys.extend(obj_ys)
        colours.extend([colour[s]] * len(present))
        card.update(colour=colour[s], is_ref=(s == ref_no))
        cards.append(card)
    _r0, v0 = propagate(ref_sat, window[0])
    return dict(x=xs, y=ys, colours=colours, cards=cards,
                vkms=round(float(np.linalg.norm(v0)), 3))


# --------------------------------------------------------------------------- #
#  Plotly figure
# --------------------------------------------------------------------------- #
def _marker(key, colour):
    symbol = SRC_SYMBOL.get(key, "circle")
    is_open = symbol.endswith("-open")
    return dict(symbol=symbol, size=(8 if is_open else 7), color=colour,
                opacity=0.95,
                line=dict(width=1.4 if is_open else 0, color=colour))


def _layout():
    return dict(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#8FA0BE", family="ui-monospace,'SF Mono',Menlo,Consolas,monospace",
                  size=12),
        showlegend=False, autosize=True, margin=dict(l=64, r=24, t=16, b=52),
        hoverlabel=dict(bgcolor="#111a30", bordercolor="#385FAF",
                        font=dict(color="#E6ECF5",
                                  family="ui-monospace,Menlo,monospace", size=12)),
        xaxis=dict(title=dict(text="PHASE OFFSET  ·  seconds along-track",
                              font=dict(size=11, color="#6C7C9C")),
                   gridcolor="rgba(115,155,207,0.10)", zeroline=True,
                   zerolinecolor="rgba(198,124,0,0.55)", zerolinewidth=1.5,
                   tickfont=dict(size=11), showline=False),
        yaxis=dict(title=dict(text="EPOCH (UTC)", font=dict(size=11, color="#6C7C9C")),
                   autorange="reversed", gridcolor="rgba(115,155,207,0.10)",
                   tickfont=dict(size=11), showline=False),
        dragmode="pan",
    )


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
            # escaped here as well as at the markup sites.
            fig.add_trace(go.Scatter(
                x=ds["x"][idx], y=ds["y"][idx], mode="markers",
                name=f"{s} {key}", marker=_marker(key, colr),
                hovertemplate=(f"<b>{esc(nm)}</b> · {s}<br>{esc(SRC_LABEL.get(key, key))}<br>"
                               "%{y|%d %b %H:%M}Z<br>offset %{x:.1f} s<extra></extra>")))
            traces.append(dict(obj=s, source=key))
    fig.update_layout(**_layout())
    plot_div = fig.to_html(full_html=False, include_plotlyjs=(True if first else False),
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
            if key == "spacetrack" and obj.elsets:
                return True
            if key != "spacetrack" and obj.state_series.get(key):
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
        refs.append(dict(norad=s, name=names.get(s, f"OBJECT {s}")))
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
        out[mlabel] = dict(refData=ref_sets, refs=refs, defaultRef=dref)
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
    present_meta = [dict(key=k, label=SRC_LABEL.get(k, k),
                         shape=SRC_SHAPE.get(SRC_SYMBOL.get(k, "circle"), "mk-circle"))
                    for k in present]

    return dict(
        id=panel_id, name=name, div_id=div_id, plot_div=plot_div, traces=traces,
        present=present, presentMeta=present_meta,
        modeData=mode_data, modeOrder=[m for m in mode_order if m in mode_data],
        defaultMode=first_mode, defaultRef=md0["defaultRef"], v_kms=seed["vkms"],
        window_start=window[0].strftime("%d %b %Y %H:%MZ"),
        window_end=window[1].strftime("%d %b %Y %H:%MZ"),
        source=" · ".join(SRC_LABEL.get(k, k) for k in present),
    )


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
    groups = [dict(id=p["id"], divId=p["div_id"], traces=p["traces"],
                   present=p["present"], modeData=p["modeData"],
                   modeOrder=p["modeOrder"], defaultMode=p["defaultMode"],
                   defaultRef=p["defaultRef"])
              for p in panels]
    return json_for_html(dict(groups=groups, srclbl=SRC_LABEL))


def render_report(panels, classification: str, generated: dt.datetime | None = None) -> str:
    """Assemble the multi-group tabbed report into one self-contained HTML string."""
    if not panels:
        raise ComputeError("nothing to render: no panels were built")
    when = generated or dt.datetime.now(dt.timezone.utc)
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
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
