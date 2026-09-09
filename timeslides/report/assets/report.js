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
                mode:g.defaultMode, ref:String(g.defaultRef) };
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

function refOptions(g){
  const st = ST[g.id];
  const md = g.modeData[st.mode];
  const sel = document.getElementById("refsel-"+g.id);
  if(!sel) return;
  sel.innerHTML = md.refs.map(r =>
    `<option value="${r.norad}"${String(r.norad)===st.ref?" selected":""}>${esc(r.name)} · ${r.norad}</option>`
  ).join("");
}

function applyData(g){
  const st = ST[g.id];
  const md = g.modeData[st.mode];
  if(!md.refData[st.ref]) st.ref = String(md.defaultRef);   // ref absent in this mode
  const rd = md.refData[st.ref];
  g.cards = rd.cards; g.vkms = rd.vkms;
  if(window.Plotly)
    Plotly.restyle(g.divId, {x: rd.x, y: rd.y, "marker.color": rd.colours});
  const refc = rd.cards.find(c => c.is_ref);
  const rm = document.getElementById("refmeta-"+g.id);
  if(refc && rm) rm.textContent = refc.name + " · " + refc.norad;
  const mm = document.getElementById("modemeta-"+g.id);
  if(mm) mm.textContent = st.mode;
  const sel = document.getElementById("refsel-"+g.id);
  if(sel && sel.value !== st.ref) sel.value = st.ref;
  panelEl(g.id).querySelectorAll(".modeseg button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode===st.mode));
  buildCards(g); buildRelative(g); apply(g);
}

function setRef(g, refNo){ ST[g.id].ref = String(refNo); applyData(g); }
function setMode(g, mode){ ST[g.id].mode = mode; refOptions(g); applyData(g); }

function buildCards(g){
  const wrap = document.getElementById("cards-"+g.id);
  wrap.innerHTML = g.cards.map(c => `
    <div class="card ${c.is_ref?'ref':''} ${c.absent?'nodata':''}" data-obj="${c.norad}" style="--c:${c.colour}"
         role="button" tabindex="0" aria-label="${esc(c.name)} ${c.norad}, tap to isolate">
      <div class="top">
        <div><div class="nm">${esc(c.name)}</div><div class="id">NORAD ${c.norad}</div></div>
        ${c.is_ref ? '<span class="refchip">REF</span>' : '<span class="eye">SHOWN</span>'}
      </div>
      <div class="readout">
        <div class="big" style="color:${c.colour}">${c.absent?'&mdash;':fmt(c.current)}<span class="u">${c.absent?'':'s'}</span></div>
        <div class="sub">drift <b>${c.absent?'&mdash;':fmt(c.drift)}</b> s/day<br>${
          Object.entries(c.counts).filter(e=>e[1]>0).map(e=>SRCLBL[e[0]]+' '+e[1]).join(' · ') || 'no data'
        }</div>
      </div>
      <div class="kmline">${c.absent
        ? 'no data in this mode'
        : (c.is_ref
          ? 'reference datum &mdash; all offsets measured from here'
          : (c.current<0?'trails':'leads') + ' reference by <b>~'+km(c.current,g.vkms).toFixed(0)+' km</b> along-track')}</div>
    </div>`).join("");
  wrap.querySelectorAll(".card").forEach(el => {
    el.addEventListener("click", () => toggleObj(g, +el.dataset.obj));
    el.addEventListener("keydown", e => {
      if(e.key==="Enter"||e.key===" "){ e.preventDefault(); toggleObj(g, +el.dataset.obj); }
    });
  });
}

function buildRelative(g){
  const box = document.getElementById("rel-"+g.id);
  const rows = [];
  for(let i=0;i<g.cards.length;i++) for(let j=i+1;j<g.cards.length;j++){
    const a=g.cards[i], b=g.cards[j];
    const gap = a.current - b.current;
    const rel = a.drift - b.drift;
    const rate = Math.abs(rel);
    let state, cls, eta="";
    if(Math.abs(gap) < 1){ state="ALIGNED"; cls="rel-al"; }
    else if(rate < 0.05){ state="STEADY"; cls="rel-st"; }
    else if(Math.sign(gap) === -Math.sign(rel)){
      state="CLOSING"; cls="rel-cl"; eta = " · ~"+(Math.abs(gap)/rate).toFixed(0)+" d to align";
    } else { state="SEPARATING"; cls="rel-sp"; }
    const link = state==="CLOSING" ? "&rarr;&larr;" : state==="SEPARATING" ? "&larr;&nbsp;&rarr;" : "&mdash;";
    const rateStr = rate>=0.05 ? rate.toFixed(1)+" s/day · " : "";
    rows.push(`<div class="rel">
      <div class="rel-h"><span class="rdot" style="background:${a.colour}"></span>${esc(a.name)}
        <span class="rlink">${link}</span>
        <span class="rdot" style="background:${b.colour}"></span>${esc(b.name)}</div>
      <div class="rel-b"><span class="rbadge ${cls}">${state}</span>
        ${rateStr}gap ${Math.abs(gap).toFixed(0)} s (~${km(gap,g.vkms).toFixed(0)} km)${eta}</div>
    </div>`);
  }
  box.innerHTML = rows.length ? rows.join("") :
    '<p class="hint">Add two or more objects to see relative motion.</p>';
}

function toggleObj(g, norad){
  const h = ST[g.id].hidden;
  if(h.has(norad)) h.delete(norad); else h.add(norad);
  apply(g);
}

function apply(g){
  const st = ST[g.id];
  const vis = g.traces.map(t => st.sources.has(t.source) && !st.hidden.has(t.obj));
  if(window.Plotly) Plotly.restyle(g.divId, {visible: vis});
  const panel = panelEl(g.id);
  panel.querySelectorAll(".srcseg .srcchip").forEach(b =>
    b.classList.toggle("active", st.sources.has(b.dataset.src)));
  panel.querySelectorAll(".cards .card").forEach(el => {
    const off = st.hidden.has(+el.dataset.obj);
    el.classList.toggle("off", off);
    const eye = el.querySelector(".eye");
    if(eye) eye.textContent = off ? "HIDDEN" : "SHOWN";
  });
}

function showTab(id){
  document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", +p.dataset.panel===id));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", +t.dataset.tab===id));
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
    applyData(g);
  });
  document.querySelectorAll(".tab").forEach(t =>
    t.addEventListener("click", () => showTab(+t.dataset.tab)));
  if(GROUPS.length) showTab(GROUPS[0].id);
}
window.addEventListener("load", init);