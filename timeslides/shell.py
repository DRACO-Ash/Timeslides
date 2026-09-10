"""The page served at /: the report's own chrome, with a Configure tab.

You asked for the picker inside the report shell rather than on a page of its
own, so this reuses report.css wholesale. The classification banner, the
header, the tab bar, the rail, the card styling and the footer are the same
furniture as the waterfall, and the Configure tab sits in that tab bar next to
the report.

The generated report itself is not modified. It cannot be: configuration has to
exist before there is a report to put a tab into. So the Report tab holds the
generated document in a frame, exactly as render_report produced it, and the
shell's tab bar wraps both. That keeps the waterfall page byte-for-byte the
artefact it always was while presenting as one application.

Nothing here calls the UDL. This page is also the platform's readiness target
(port 8080, path /), and a probe that depends on an upstream turns somebody
else's outage into a restart loop of our own.
"""

from __future__ import annotations

from .models import DATA_MODES, ELSET_LABEL, SRC_SHAPE, STATE_SOURCES
from .report.builder import _asset, esc, json_for_html

# Inlined so the page makes no external request at all. Without an icon the
# browser asks for /favicon.ico unprompted and logs a 404 on every page load,
# which is noise in the access log and a failure in the smoke test.
FAVICON = (
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNiIgZmlsbD0iIzE2MjY0NiIvPjxsaW5lIHgxPSIxNiIgeTE9IjQiIHgyPSIxNiIgeTI9IjI4IiBzdHJva2U9IiNDNjdDMDAiIHN0cm9rZS13aWR0aD0iMiIvPjxjaXJjbGUgY3g9IjE2IiBjeT0iOCIgcj0iMi42IiBmaWxsPSIjQzY3QzAwIi8+PGNpcmNsZSBjeD0iMTAiIGN5PSIxNSIgcj0iMi42IiBmaWxsPSIjNEM5QkU4Ii8+PGNpcmNsZSBjeD0iMjMiIGN5PSIyMiIgcj0iMi42IiBmaWxsPSIjMjdBRTYwIi8+PC9zdmc+"
)


def _mode_chips() -> str:
    """REAL is on by default; the rest are opt-in, as the CLI default was."""
    return "".join(
        f'<button class="srcchip{" active" if mode == "REAL" else ""}" '
        f'data-mode="{esc(mode)}">{esc(mode)}</button>'
        for mode in DATA_MODES)


def _source_chips() -> str:
    """The state-vector providers, with the marker shape the plot will use.

    Rendered server-side from STATE_SOURCES so this list cannot drift out of
    step with what the report legend shows. That drift is exactly the bug this
    replaced: the tab offered state providers only while the report legend also
    carried the element-set series, so the two never matched.
    """
    return "".join(
        f'<button class="srcchip active" data-source="{esc(s["key"])}" '
        f'title="UDL source {esc(s["udl_source"])}">'
        f'<span class="mk {esc(SRC_SHAPE.get(s["symbol"], "mk-circle"))}"></span>'
        f'{esc(s["label"])}</button>'
        for s in STATE_SOURCES)


def _configure_panel(storage=None) -> str:
    return f"""
<section class="panel cfg active" data-panel="cfg">
  <div class="cfgwrap">
    <aside class="rail">
      <div>
        <h2>Catalogue &mdash; search the UDL</h2>
        <div class="search">
          <input class="inp" id="q" type="search" maxlength="120"
                 placeholder="name or NORAD number"
                 aria-label="Search the UDL on-orbit catalogue">
          <button class="btn" id="searchbtn">Search</button>
        </div>
        <div id="searchmsg"></div>
        <div class="rows" id="hits"></div>
      </div>
    </aside>
    <div class="cfgmain">
      {_storage_banner(storage)}
      <section>
        <h2>Group being built <span class="gm" id="editing"></span></h2>
        <div class="search">
          <input class="inp" id="gname" type="text" maxlength="80"
                 placeholder="group name" aria-label="Group name">
          <button class="btn" id="savebtn" disabled>Save group</button>
          <button class="btn ghost" id="cancelbtn" hidden>Cancel</button>
        </div>
        <div id="savemsg"></div>
        <div class="rows" id="picked"></div>
        <p class="hint">The reference object is the waterfall anchor: every other
        offset in the group is measured from it, and it sits near zero against
        itself. It must be one of the group's own objects.</p>
      </section>

      <section>
        <h2>Saved groups</h2>
        <div id="groupsmsg"></div>
        <div id="groups"></div>
      </section>

      <section>
        <h2>Render</h2>
        <div class="ctrls">
          <div class="ctrl">
            <label for="days">Window (days)</label>
            <input class="inp" id="days" type="number" min="1" max="90" value="7">
          </div>
          <div class="ctrl">
            <label>Data mode</label>
            <div class="srcseg">{_mode_chips()}</div>
          </div>
          <div class="ctrl wide">
            <label>State providers
              <button class="btn sm ghost" id="probebtn">check availability</button>
            </label>
            <div class="srcseg">{_source_chips()}</div>
            <div id="probemsg" role="status" aria-live="polite"
                 aria-busy="false"></div>
          </div>
          <div class="ctrl">
            <label>Sign</label>
            <div class="srcseg"><button class="srcchip" id="invert">Invert</button></div>
          </div>
          <button class="btn" id="runbtn" disabled>Render report</button>
        </div>
        <div id="runmsg"></div>
        <p class="note" id="groupcount"></p>
        <p class="hint">Shape encodes the source and colour encodes the object.
        Filled shapes are measured state vectors from a provider. The one open
        shape is <b style="color:var(--muted)">{esc(ELSET_LABEL)}</b>, which is
        every two-line element set in the window propagated to its own epoch;
        it is always plotted, because it is also where the reference orbit
        comes from. A provider with no data in the window simply does not
        appear, so use <b style="color:var(--muted)">check availability</b> if
        one is missing that you expected.</p>
      </section>
    </div>
  </div>
</section>"""


def _report_panel() -> str:
    return """
<section class="panel rep" data-panel="rep">
  <div class="empty" id="repempty">
    <p>No report yet. Choose a window and the providers you want on the
    Configure tab, then render. The waterfall opens here, exactly as it is
    produced for download.</p>
  </div>
  <iframe class="repframe" id="repframe" hidden title="Phase offset waterfall"
          sandbox="allow-scripts allow-same-origin"></iframe>
</section>"""


def _storage_banner(storage) -> str:
    """A standing warning when the group store is running from memory.

    The application still works in that state: groups can be built, saved,
    edited and rendered. What it cannot do is survive a restart. Saying so
    plainly beats both silence and the earlier behaviour, which was to refuse
    every save and read as a broken application.
    """
    if not storage or storage.get("writable"):
        return ""
    return ('<div class="err storagewarn" role="alert">'
            '<b>Groups are being kept in memory, not saved.</b> '
            "Everything here works, but any group you build will be lost when "
            "the pod restarts. To make groups persist, enable the persistent "
            "storage add-on for this app and mount it at "
            + esc(str(storage.get("path", "the configured storage path")))
            + '. <span style="color:var(--muted)">Reported by the volume: '
            + esc(str(storage.get("detail", "not writable")))
            + "</span></div>")


def render_shell(classification: str, demo: bool = False, storage=None) -> str:
    """The application page. Self-contained; no external requests."""
    banner = esc(classification.upper())
    mode_note = ' &middot; <b style="color:var(--copper)">DEMO DATA</b>' if demo else ""
    payload = json_for_html({"demo": bool(demo), "classification": classification,
                             "storageWritable": bool((storage or {}).get("writable", True))})
    body = f"""
<div class="app">
  <div class="classif">{banner}</div>
  <header>
    <div class="brand">
      <p class="eyebrow">Bluestaq Limited &middot; Space Domain Awareness</p>
      <h1>Phase Offset Waterfall <b>&mdash; relative along-track drift</b></h1>
    </div>
    <div class="gen">TIMESLIDES{mode_note}</div>
  </header>
  <nav class="tabs">
    <button class="tab active" data-tab="cfg">Configure</button>
    <button class="tab" data-tab="rep">Report</button>
  </nav>
  <div class="panels">{_configure_panel(storage)}{_report_panel()}</div>
  <div class="foot">
    <span>BLUESTAQ LIMITED &nbsp;&middot;&nbsp; MISSION CRITICAL SOLUTIONS</span>
    <span class="r"><span>UDL</span></span>
  </div>
</div>
<script id="shell-data" type="application/json">{payload}</script>
<script>
{_asset("picker.js")}
</script>
"""
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f'<link rel="icon" href="data:image/svg+xml;base64,{FAVICON}">'
            "<title>Timeslides &mdash; Phase Offset Waterfall</title><style>"
            + _asset("report.css") + _asset("shell.css")
            + "</style></head><body>" + body + "</body></html>")
