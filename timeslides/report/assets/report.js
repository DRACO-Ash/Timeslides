/* Report shell behaviour: data-mode and reference selection, per-source
 * toggling, object isolation and tab switching.
 *
 * Lifted unchanged from the f-string inside render_report() in
 * LEO_Waterfall_Phase_Offset.py, with one change: the two values that used to
 * be interpolated by Python (GROUPS and SRCLBL) are now read from a JSON
 * script block. Nothing else about this file differs from the original, and
 * tests/test_report.py asserts the rendered output still contains every
 * behaviour this file provides.
 *
 * The reason for the move is that JavaScript held inside a Python string
 * literal is invisible to the SonarQube quality gate, to linting and to
 * coverage. As a real .js file it is analysed like any other source.
 */
const REPORT_DATA = JSON.parse(document.getElementById("report-data").textContent);
const GROUPS = REPORT_DATA.groups;
const SRCLBL = REPORT_DATA.srclbl;
const ST = {};
GROUPS.forEach(g => {
  ST[g.id] = { sources:new Set(g.present), hidden:new Set(),
                mode:g.defaultMode, ref:String(g.defaultRef), manual:[], horizon:null,
                datum:"rolling" };
  g.cards = []; g.vkms = 1;
});

/* The counterpart to esc() on the server. Object names come from a UDL tenant
 * and group names from the picker, and both reach innerHTML templates below, so
 * neither can be interpolated raw. */
function esc(v){
  return String(v ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"
  })[c]);
}

function fmt(n, dp=1){ const s = n>=0 ? "+" : ""; return s + n.toFixed(dp); }
function km(sec, v){ return Math.abs(sec)*v; }
function panelEl(id){ return document.querySelector('.panel[data-panel="'+id+'"]'); }
/* The dataset's own traces, by index. Plotly cycles a short update array
   across every trace on the plot, so without this a restyle would also rewrite
   a manual state's trace with the first object's data or visibility. */
function ownTraces(g){ return g.traces.map((t, i) => i); }

function refOptions(g){
  const st = ST[g.id];
  const md = g.modeData[st.mode];
  const sel = document.getElementById("refsel-"+g.id);
  if(!sel) return;
  sel.innerHTML = md.refs.map(r =>
    `<option value="${r.norad}"${String(r.norad)===st.ref?" selected":""}>${esc(r.name)} · ${r.norad}</option>`
  ).join("");
}

/* The dataset for the chosen datum. The server measures every point twice:
 * against the rolling datum (the main reading) and against the latest element
 * set alone (`alt`). Only the offsets, cards and Look-ahead differ, so "Latest
 * only" overlays those three and shares everything else. */
function dataset(g){
  const st = ST[g.id];
  const rd = g.modeData[st.mode].refData[st.ref];
  return st.datum === "latest" && rd.alt ? {...rd, ...rd.alt} : rd;
}

function applyData(g){
  const st = ST[g.id];
  const md = g.modeData[st.mode];
  if(!md.refData[st.ref]) st.ref = String(md.defaultRef);   // ref absent in this mode
  const rd = dataset(g);
  g.cards = rd.cards; g.vkms = rd.vkms;
  if(window.Plotly)
    Plotly.restyle(g.divId, {x: rd.x, y: rd.y, "marker.color": rd.colours}, ownTraces(g));
  const refc = rd.cards.find(c => c.is_ref);
  const rm = document.getElementById("refmeta-"+g.id);
  if(refc && rm) rm.textContent = refc.name + " · " + refc.norad;
  const mm = document.getElementById("modemeta-"+g.id);
  if(mm) mm.textContent = st.mode;
  const sel = document.getElementById("refsel-"+g.id);
  if(sel && sel.value !== st.ref) sel.value = st.ref;
  panelEl(g.id).querySelectorAll(".modeseg button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode===st.mode));
  buildCards(g); buildRelative(g); apply(g); replotManual(g); drawAhead(g); drawDatum(g);
}

function setRef(g, refNo){ ST[g.id].ref = String(refNo); applyData(g); }
function setMode(g, mode){ ST[g.id].mode = mode; refOptions(g); applyData(g); }

/* Card fragments, each named rather than nested inside the template. The
 * kilometre line used to be a ternary inside a ternary, which is hard to read
 * and hard to change without breaking. Same output. */
function cardClasses(c){
  const classes = ["card"];
  if(c.is_ref) classes.push("ref");
  if(c.absent) classes.push("nodata");
  return classes.join(" ");
}

function cardBadge(c){
  return c.is_ref
    ? '<span class="refchip">REF</span>'
    : '<span class="eye">SHOWN</span>';
}

function cardCounts(c){
  const parts = Object.entries(c.counts)
    .filter(entry => entry[1] > 0)
    .map(entry => SRCLBL[entry[0]] + " " + entry[1]);
  return parts.join(" \u00b7 ") || "no data";
}

function cardKmLine(c, vkms){
  if(c.absent) return "no data in this mode";
  if(c.is_ref) return "reference datum &mdash; all offsets measured from here";
  const sense = c.current < 0 ? "trails" : "leads";
  return sense + " reference by <b>~" + km(c.current, vkms).toFixed(0)
       + " km</b> along-track";
}

/* Set by the server when the track changed rate in the window: the drift
   shown is then the rate since, and this says what it was before. */
function driftNote(c){
  return c.driftNote ? '<br><span class="driftnote">' + esc(c.driftNote) + "</span>" : "";
}

function cardMarkup(c, vkms){
  const offset = c.absent ? "&mdash;" : fmt(c.current);
  const unit = c.absent ? "" : "s";
  const drift = c.absent ? "&mdash;" : fmt(c.drift);
  return `
    <div class="${cardClasses(c)}" data-obj="${c.norad}" style="--c:${c.colour}"
         role="button" tabindex="0" aria-label="${esc(c.name)} ${c.norad}, tap to isolate">
      <div class="top">
        <div><div class="nm">${esc(c.name)}</div><div class="id">NORAD ${c.norad}</div></div>
        ${cardBadge(c)}
      </div>
      <div class="readout">
        <div class="big" style="color:${c.colour}">${offset}<span class="u">${unit}</span></div>
        <div class="sub">drift <b>${drift}</b> s/day${driftNote(c)}<br>${cardCounts(c)}</div>
      </div>
      <div class="kmline">${cardKmLine(c, vkms)}</div>
    </div>`;
}

function buildCards(g){
  const wrap = document.getElementById("cards-"+g.id);
  wrap.innerHTML = g.cards.map(c => cardMarkup(c, g.vkms)).join("");
  wrap.querySelectorAll(".card").forEach(el => {
    el.addEventListener("click", () => toggleObj(g, Number(el.dataset.obj)));
    el.addEventListener("keydown", e => {
      if(e.key==="Enter"||e.key===" "){ e.preventDefault(); toggleObj(g, Number(el.dataset.obj)); }
    });
  });
}

/* Relative motion between a pair of objects. Split out of buildRelative,
 * which measured 20 on cognitive complexity against a cap of 15, and the
 * arrow was a chained ternary. Same states, same thresholds, same output. */
const PAIR_LINK = {
  CLOSING: "&rarr;&larr;",
  SEPARATING: "&larr;&nbsp;&rarr;"
};

function pairState(gap, rel){
  const rate = Math.abs(rel);
  if(Math.abs(gap) < 1) return { state: "ALIGNED", cls: "rel-al", eta: "" };
  if(rate < 0.05) return { state: "STEADY", cls: "rel-st", eta: "" };
  if(Math.sign(gap) === -Math.sign(rel)){
    return {
      state: "CLOSING",
      cls: "rel-cl",
      eta: " \u00b7 ~" + (Math.abs(gap)/rate).toFixed(0) + " d to align"
    };
  }
  return { state: "SEPARATING", cls: "rel-sp", eta: "" };
}

function pairRow(a, b, vkms){
  const gap = a.current - b.current;
  const rel = a.drift - b.drift;
  const rate = Math.abs(rel);
  const outcome = pairState(gap, rel);
  const link = PAIR_LINK[outcome.state] || "&mdash;";
  const rateStr = rate >= 0.05 ? rate.toFixed(1) + " s/day \u00b7 " : "";
  const gapStr = Math.abs(gap).toFixed(0);
  const kmStr = km(gap, vkms).toFixed(0);
  return `<div class="rel">
      <div class="rel-h"><span class="rdot" style="background:${a.colour}"></span>${esc(a.name)}
        <span class="rlink">${link}</span>
        <span class="rdot" style="background:${b.colour}"></span>${esc(b.name)}</div>
      <div class="rel-b"><span class="rbadge ${outcome.cls}">${outcome.state}</span>
        ${rateStr}gap ${gapStr} s (~${kmStr} km)${outcome.eta}</div>
    </div>`;
}

function buildRelative(g){
  const box = document.getElementById("rel-"+g.id);
  const rows = [];
  for(let i = 0; i < g.cards.length; i++){
    for(let j = i + 1; j < g.cards.length; j++){
      rows.push(pairRow(g.cards[i], g.cards[j], g.vkms));
    }
  }
  box.innerHTML = rows.length
    ? rows.join("")
    : '<p class="hint">Add two or more objects to see relative motion.</p>';
}

function toggleObj(g, norad){
  const h = ST[g.id].hidden;
  if(h.has(norad)) h.delete(norad); else h.add(norad);
  apply(g);
}

function apply(g){
  const st = ST[g.id];
  const vis = g.traces.map(t => st.sources.has(t.source) && !st.hidden.has(t.obj));
  if(window.Plotly) Plotly.restyle(g.divId, {visible: vis}, ownTraces(g));
  aheadVisibility(g);
  const panel = panelEl(g.id);
  panel.querySelectorAll(".srcseg .srcchip").forEach(b =>
    b.classList.toggle("active", st.sources.has(b.dataset.src)));
  panel.querySelectorAll(".cards .card").forEach(el => {
    const off = st.hidden.has(Number(el.dataset.obj));
    el.classList.toggle("off", off);
    const eye = el.querySelector(".eye");
    if(eye) eye.textContent = off ? "HIDDEN" : "SHOWN";
  });
}

/* Manual states. A hand-entered state is not UDL data, so it never joins the
 * datasets above: the service measures it against this panel's own reference
 * element set and returns a finished trace and rail row, which are added
 * alongside. Held in memory only. A reload or a new render drops it, and a
 * change of reference or mode measures it again against the new datum. */
/* The service refuses with a sentence in detail; anything else that comes
   back is reported as a plain refusal rather than as "[object Object]". */
function refusal(data){
  const said = typeof data.detail === "string" ? data.detail : "refused";
  return new Error(said);
}

function manualRequest(g, body){
  const st = ST[g.id];
  const md = g.modeData[st.mode];
  const tle = md.refData[st.ref].tle;
  // Measured against the datum the plot is showing: the reference's chain
  // under the rolling datum, its latest element set alone otherwise.
  const datum = st.datum === "rolling" ? (md.chains?.[st.ref] ?? []) : [];
  return fetch("/api/manual-state", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({...body, tle, datum, invert: g.invert})
  }).then(res => res.json().then(data => res.ok ? data : Promise.reject(refusal(data))));
}

function plotManual(g, body){
  const msg = document.getElementById("manmsg-"+g.id);
  return manualRequest(g, body).then(m => {
    ST[g.id].manual.push({uid: m.trace.uid, body});
    if(window.Plotly) Plotly.addTraces(g.divId, m.trace);
    document.getElementById("manlist-"+g.id).insertAdjacentHTML("beforeend", m.row);
    msg.textContent = "";
  }, err => { msg.textContent = "Not plotted: " + err.message; });
}

function removeManual(g, uid){
  ST[g.id].manual = ST[g.id].manual.filter(m => m.uid !== uid);
  const at = (document.getElementById(g.divId).data || []).findIndex(t => t.uid === uid);
  if(at >= 0) Plotly.deleteTraces(g.divId, at);
  document.querySelector('#manlist-'+g.id+' [data-uid="'+uid+'"]')?.remove();
}

function replotManual(g){
  const held = ST[g.id].manual.slice();
  held.forEach(m => removeManual(g, m.uid));
  held.forEach(m => plotManual(g, m.body));
}

/* The Look-ahead. The server builds every horizon's view, traces, shapes,
 * annotations and an escaped event list (report/lookahead.py); this picks the
 * one for the chosen horizon and draws it, and nothing else. */
function aheadHorizon(g){
  const st = ST[g.id];
  const ahead = dataset(g).ahead;
  return ahead ? (st.horizon ?? ahead.default) : null;
}

function aheadView(g){
  const ahead = dataset(g).ahead;
  return ahead ? ahead.views[aheadHorizon(g)] : null;
}

function drawAhead(g){
  const v = aheadView(g);
  const list = document.getElementById("ahead-"+g.id);
  if(list) list.innerHTML = v ? v.items : "";
  panelEl(g.id).querySelectorAll(".horizon button").forEach(b =>
    b.classList.toggle("active", b.dataset.h === aheadHorizon(g)));
  if(!window.Plotly) return;
  const gd = document.getElementById(g.divId);
  const old = (gd.data || []).map((t, i) => String(t.uid).startsWith("ahead-") ? i : -1)
    .filter(i => i >= 0);
  if(old.length) Plotly.deleteTraces(g.divId, old);
  if(v?.traces.length) Plotly.addTraces(g.divId, v.traces);
  const shapes = (v ? v.shapes : []).concat(datumShapes(g));
  Plotly.relayout(g.divId, {shapes, annotations: v ? v.annotations : []});
  aheadVisibility(g);
}

/* The datum. Ticks on the plot's left edge mark each reference element-set
 * epoch under the rolling datum, so a gap in the datum is visible where it
 * falls. The faint line is the reference against its own latest element set:
 * under a rolling datum the reference sits at zero through its own burn, and
 * this is what keeps that burn visible. */
const DATUM_GREEN = "#27AE60";

function datumShapes(g){
  const st = ST[g.id];
  const d = g.modeData[st.mode].refData[st.ref].datum;
  if(st.datum !== "rolling" || !d || d.count < 2) return [];
  return d.epochs.map(y => ({type: "line", name: "datum-epoch", xref: "paper", x0: 0, x1: 0.012,
    yref: "y", y0: y, y1: y, line: {color: DATUM_GREEN, width: 2}}));
}

function datumLabel(st, d){
  if(st.datum === "latest") return "Latest element set only";
  const sets = d.count === 1 ? "1 element set, the same as latest" : d.count + " element sets";
  return "Rolling · " + sets;
}

function datumCheck(d){
  const c = d.check;
  if(!c?.n) return "No independent check: the reference has no state vectors in this window.";
  let text = "Check: the reference's own " + c.n + " state vectors sit " +
    c.rolling.toFixed(2) + " s RMS from zero against the rolling datum, " +
    c.latest.toFixed(2) + " s against the latest element set alone.";
  if(d.gapHours > 24) text += " Longest gap between reference element sets: " + d.gapHours + " h.";
  return text;
}

function referenceTrace(g, rd){
  const ref = Number(ST[g.id].ref);
  const pts = [];
  g.traces.forEach((t, i) => {
    if(t.obj !== ref) return;
    rd.alt.x[i].forEach((x, k) => pts.push([rd.y[i][k], x]));
  });
  pts.sort((a, b) => a[0].localeCompare(b[0]));
  return {type: "scatter", mode: "lines", uid: "datum-ref", name: "reference vs latest",
    x: pts.map(p => p[1]), y: pts.map(p => p[0]), showlegend: false, hoverinfo: "skip",
    opacity: 0.5, line: {color: "#C67C00", width: 1, dash: "dot"}};
}

function drawDatum(g){
  const st = ST[g.id];
  const rd = g.modeData[st.mode].refData[st.ref];
  const d = rd.datum;
  panelEl(g.id).querySelectorAll(".datumseg button").forEach(b =>
    b.classList.toggle("active", b.dataset.datum === st.datum));
  const meta = document.getElementById("datummeta-"+g.id);
  if(meta && d) meta.textContent = datumLabel(st, d);
  const check = document.getElementById("datumcheck-"+g.id);
  if(check && d) check.textContent = datumCheck(d);
  if(!window.Plotly) return;
  const gd = document.getElementById(g.divId);
  const old = (gd.data || []).findIndex(t => t.uid === "datum-ref");
  if(old >= 0) Plotly.deleteTraces(g.divId, old);
  if(st.datum === "rolling" && rd.alt && d?.count > 1) Plotly.addTraces(g.divId, referenceTrace(g, rd));
}

/* An isolated object's forecast goes with it. Only the projected line and
   band are per object; the pass and rate-change markers stay. */
function aheadVisibility(g){
  if(!window.Plotly) return;
  const hidden = ST[g.id].hidden;
  const idx = [], vis = [];
  (document.getElementById(g.divId).data || []).forEach((t, i) => {
    const m = /^ahead-(?:line|band)-(\d+)$/.exec(String(t.uid));
    if(m){ idx.push(i); vis.push(!hidden.has(Number(m[1]))); }
  });
  if(idx.length) Plotly.restyle(g.divId, {visible: vis}, idx);
}

function showTab(id){
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", Number(p.dataset.panel)===id));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", Number(t.dataset.tab)===id));
  const g = GROUPS.find(x => x.id===id);
  if(g && window.Plotly) Plotly.Plots.resize(g.divId);
}

function init(){
  GROUPS.forEach(g => {
    refOptions(g);
    panelEl(g.id).querySelectorAll(".srcseg .srcchip").forEach(b =>
      b.addEventListener("click", () => {
        const st = ST[g.id];
        if(st.sources.has(b.dataset.src)) st.sources.delete(b.dataset.src);
        else st.sources.add(b.dataset.src);
        apply(g);
      }));
    panelEl(g.id).querySelectorAll(".modeseg button").forEach(b =>
      b.addEventListener("click", () => setMode(g, b.dataset.mode)));
    const sel = document.getElementById("refsel-"+g.id);
    if(sel) sel.addEventListener("change", () => setRef(g, sel.value));
    const form = document.getElementById("manform-"+g.id);
    if(form) form.querySelector(".mango").addEventListener("click", () =>
      plotManual(g, Object.fromEntries(new FormData(form))));
    panelEl(g.id).querySelectorAll(".horizon button").forEach(b =>
      b.addEventListener("click", () => { ST[g.id].horizon = b.dataset.h; drawAhead(g); }));
    panelEl(g.id).querySelectorAll(".datumseg button").forEach(b =>
      b.addEventListener("click", () => { ST[g.id].datum = b.dataset.datum; applyData(g); }));
    const list = document.getElementById("manlist-"+g.id);
    if(list) list.addEventListener("click", e => {
      const row = e.target.closest(".manx") ? e.target.closest(".manrow") : null;
      if(row) removeManual(g, row.dataset.uid);
    });
    applyData(g);
  });
  document.querySelectorAll(".tab").forEach(t =>
    t.addEventListener("click", () => showTab(Number(t.dataset.tab))));
  if(GROUPS.length) showTab(GROUPS[0].id);
}
window.addEventListener("load", init);