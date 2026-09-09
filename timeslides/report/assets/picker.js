/* The Configure tab: build satellite groups from UDL catalogue selections,
 * then render them.
 *
 * Every name shown here comes from a UDL tenant or from a group somebody else
 * created, so nothing is interpolated into markup without esc().
 */
"use strict";

const CFG = JSON.parse(document.getElementById("shell-data").textContent);

const S = {
  rev: 0,
  groups: [],
  hits: [],          /* catalogue results */
  picked: [],        /* {satNo, name} in the group being built */
  reference: null,
  editing: null,     /* group id when editing an existing group */
  name: "",
  run: null,         /* the job we are polling */
  reportUrl: null,
  poll: null
};

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"
  })[c]);
}

function el(id) { return document.getElementById(id); }

function show(target, message, isError) {
  const box = el(target);
  if (!box) return;
  box.innerHTML = message ? `<div class="${isError ? "err" : "note"}">${esc(message)}</div>` : "";
}

/* --- transport ---------------------------------------------------------- */
async function api(path, options) {
  const res = await fetch(path, Object.assign({
    headers: { "Content-Type": "application/json" }
  }, options || {}));
  let body = null;
  try { body = await res.json(); } catch (_) { /* empty or non-JSON */ }
  if (!res.ok) {
    const detail = (body && (body.detail || body.error)) || `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return body;
}

/* --- catalogue ---------------------------------------------------------- */
async function search() {
  const q = el("q").value.trim();
  if (!q) { S.hits = []; return drawHits(); }
  el("searchbtn").disabled = true;
  show("searchmsg", "");
  try {
    const body = await api(`/api/catalogue?q=${encodeURIComponent(q)}&limit=60`);
    S.hits = body.results || [];
    if (!S.hits.length) show("searchmsg", `Nothing in the catalogue matches "${q}".`);
  } catch (err) {
    S.hits = [];
    show("searchmsg", `Catalogue search failed: ${err.message}`, true);
  } finally {
    el("searchbtn").disabled = false;
    drawHits();
  }
}

function drawHits() {
  const picked = new Set(S.picked.map(p => p.satNo));
  el("hits").innerHTML = S.hits.map(r => `
    <div class="row ${picked.has(r.satNo) ? "picked" : ""}">
      <span class="rn" title="${esc(r.name)}">${esc(r.name)}</span>
      <span class="rid">${r.satNo}</span>
      <button class="btn sm ${picked.has(r.satNo) ? "ghost" : ""}"
              data-add="${r.satNo}" ${picked.has(r.satNo) ? "disabled" : ""}>
        ${picked.has(r.satNo) ? "added" : "add"}
      </button>
    </div>`).join("") || '<p class="note">Search the catalogue to add objects.</p>';
}

/* --- the group being built --------------------------------------------- */
function addPick(satNo) {
  if (S.picked.some(p => p.satNo === satNo)) return;
  const hit = S.hits.find(r => r.satNo === satNo);
  S.picked.push({ satNo, name: hit ? hit.name : `OBJECT ${satNo}` });
  if (S.reference === null) S.reference = satNo;
  drawHits();
  drawPicked();
}

function removePick(satNo) {
  S.picked = S.picked.filter(p => p.satNo !== satNo);
  if (S.reference === satNo) S.reference = S.picked.length ? S.picked[0].satNo : null;
  drawHits();
  drawPicked();
}

function drawPicked() {
  el("picked").innerHTML = S.picked.map(p => `
    <div class="row ${p.satNo === S.reference ? "isref" : ""}">
      <span class="rn" title="${esc(p.name)}">${esc(p.name)}</span>
      <span class="rid">${p.satNo}</span>
      <button class="btn sm ghost" data-ref="${p.satNo}"
              ${p.satNo === S.reference ? "disabled" : ""}>
        ${p.satNo === S.reference ? "reference" : "make ref"}
      </button>
      <button class="btn sm warn" data-del="${p.satNo}">remove</button>
    </div>`).join("") ||
    '<p class="note">No objects yet. A group needs at least two.</p>';
  el("savebtn").disabled = S.picked.length < 2 || !el("gname").value.trim();
  el("editing").textContent = S.editing ? "editing an existing group" : "";
  el("cancelbtn").hidden = !S.editing;
}

async function saveGroup() {
  const payload = {
    name: el("gname").value.trim(),
    sats: S.picked.map(p => p.satNo),
    reference: S.reference,
    rev: S.rev
  };
  el("savebtn").disabled = true;
  show("savemsg", "");
  try {
    if (S.editing) {
      await api(`/api/groups/${encodeURIComponent(S.editing)}`,
                { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/groups", { method: "POST", body: JSON.stringify(payload) });
    }
    resetEditor();
    await loadGroups();
  } catch (err) {
    show("savemsg", err.message, true);
  } finally {
    drawPicked();
  }
}

function resetEditor() {
  S.picked = [];
  S.reference = null;
  S.editing = null;
  el("gname").value = "";
  show("savemsg", "");
  drawPicked();
  drawHits();
}

function editGroup(id) {
  const g = S.groups.find(x => x.id === id);
  if (!g) return;
  S.editing = id;
  S.reference = g.reference;
  el("gname").value = g.name;
  S.picked = g.sats.map(n => ({ satNo: n, name: nameFor(g, n) }));
  drawPicked();
  drawHits();
  el("gname").focus();
}

function nameFor(group, satNo) {
  const hit = S.hits.find(r => r.satNo === satNo);
  if (hit) return hit.name;
  const cached = (group.names || {})[satNo];
  return cached || `OBJECT ${satNo}`;
}

/* --- group list --------------------------------------------------------- */
async function loadGroups() {
  try {
    const body = await api("/api/groups");
    S.rev = body.rev;
    S.groups = body.groups || [];
    show("groupsmsg", "");
  } catch (err) {
    show("groupsmsg", `Could not load groups: ${err.message}`, true);
  }
  drawGroups();
}

function drawGroups() {
  el("groups").innerHTML = S.groups.map(g => `
    <div class="grp">
      <div class="gh">
        <span class="gn">${esc(g.name)}</span>
        <span class="gm">${g.sats.length} objects</span>
        <button class="btn sm ghost" data-edit="${esc(g.id)}">edit</button>
        <button class="btn sm warn" data-archive="${esc(g.id)}">archive</button>
      </div>
      <div class="chips">${g.sats.map(n =>
        `<span class="chip ${n === g.reference ? "ref" : ""}">${n}${
          n === g.reference ? " · ref" : ""}</span>`).join("")}</div>
    </div>`).join("") ||
    '<p class="note">No groups yet. Build one from the catalogue on the left.</p>';
  el("runbtn").disabled = S.groups.length === 0;
  el("groupcount").textContent = S.groups.length
    ? `${S.groups.length} group${S.groups.length === 1 ? "" : "s"} will be rendered`
    : "";
}

async function archiveGroup(id) {
  const g = S.groups.find(x => x.id === id);
  if (g && !window.confirm(`Archive "${g.name}"? It can be restored later.`)) return;
  try {
    await api(`/api/groups/${encodeURIComponent(id)}?rev=${S.rev}`, { method: "DELETE" });
    if (S.editing === id) resetEditor();
    await loadGroups();
  } catch (err) {
    show("groupsmsg", err.message, true);
  }
}

/* --- provider availability ---------------------------------------------- */
/* The UDL source strings are names, not values read back from a tenant, so a
 * provider that is spelled differently returns nothing and never appears in a
 * report. This asks the UDL for one record per provider and says which
 * answered, rather than leaving it to be discovered by absence. */
async function probeSources() {
  const btn = el("probebtn");
  const msg = el("probemsg");
  btn.disabled = true;
  /* aria-busy marks the region as updating. It is the correct signal for a
   * live region mid-update, and it also gives anything watching the page a
   * real settle condition: the spinner and the result both render as .note,
   * so without it there is nothing in the DOM that distinguishes "asking" from
   * "answered". */
  msg.setAttribute("aria-busy", "true");
  msg.innerHTML = '<div class="note"><span class="spin"></span>asking the UDL</div>';
  try {
    const body = await api("/api/sources/probe?days=7");
    const gone = [];
    body.results.forEach(r => {
      const chip = document.querySelector(`[data-source="${CSS.escape(r.key)}"]`);
      if (!chip) return;
      chip.classList.toggle("confirmed", r.available);
      chip.classList.toggle("gone", !r.available);
      chip.title = r.available
        ? `UDL source ${r.udlSource}: answered`
        : `UDL source ${r.udlSource}: no data${r.error ? " - " + r.error : ""}`;
      if (!r.available) gone.push(r.label);
    });
    const subject = body.satNo ? ` (probed with NORAD ${body.satNo})` : "";
    if (body.demo) {
      show("probemsg", `Demo mode: all providers are synthetic${subject}.`);
    } else if (gone.length) {
      show("probemsg",
           `No data from ${gone.join(", ")}${subject}. Either the provider does `
           + `not cover this object in the last 7 days, or its UDL source name `
           + `differs on this tenant. Hover a chip for the detail.`);
    } else {
      el("probemsg").innerHTML =
        `<div class="note ok">Every provider answered${esc(subject)}.</div>`;
    }
  } catch (err) {
    show("probemsg", err.message, true);
  } finally {
    btn.disabled = false;
    msg.setAttribute("aria-busy", "false");
  }
}

/* --- runs --------------------------------------------------------------- */
function selectedModes() {
  return Array.from(document.querySelectorAll("[data-mode].active"))
    .map(b => b.dataset.mode);
}

function selectedSources() {
  return Array.from(document.querySelectorAll("[data-source].active"))
    .map(b => b.dataset.source);
}

async function startRun() {
  const body = {
    groupIds: [],
    days: Math.max(1, Math.min(90, parseInt(el("days").value, 10) || 7)),
    modes: selectedModes(),
    sources: selectedSources(),
    invert: el("invert").classList.contains("active")
  };
  if (!body.modes.length) return show("runmsg", "Select at least one data mode.", true);
  if (!body.sources.length) return show("runmsg", "Select at least one provider.", true);
  el("runbtn").disabled = true;
  show("runmsg", "");
  try {
    const job = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
    S.run = job;
    if (job.joined) show("runmsg", "Joined a render already in progress.");
    pollRun();
  } catch (err) {
    show("runmsg", err.message, true);
    el("runbtn").disabled = false;
  }
}

function pollRun() {
  if (S.poll) clearTimeout(S.poll);
  const tick = async () => {
    try {
      const job = await api(`/api/runs/${encodeURIComponent(S.run.id)}`);
      S.run = job;
      drawRun();
      if (job.status === "queued" || job.status === "running") {
        S.poll = setTimeout(tick, 1500);
      } else {
        el("runbtn").disabled = S.groups.length === 0;
        if (job.status === "done") openReport(job.reportUrl);
      }
    } catch (err) {
      show("runmsg", `Lost track of the run: ${err.message}`, true);
      el("runbtn").disabled = false;
    }
  };
  tick();
}

function drawRun() {
  const j = S.run;
  if (!j) return;
  if (j.status === "failed") {
    return show("runmsg", j.error || "The render failed.", true);
  }
  if (j.status === "done") {
    return show("runmsg", "Report ready. Opening the Report tab.");
  }
  const p = j.progress || {};
  const detail = p.total ? ` ${p.done}/${p.total}${p.current ? ` · ${p.current}` : ""}` : "";
  el("runmsg").innerHTML =
    `<div class="note"><span class="spin"></span>${esc(j.status)}${esc(detail)}</div>`;
}

function openReport(url) {
  S.reportUrl = url;
  const frame = el("repframe");
  const empty = el("repempty");
  frame.src = url;
  frame.hidden = false;
  empty.hidden = true;
  showTab("rep");
}

/* --- tabs and wiring ---------------------------------------------------- */
function showTab(which) {
  document.querySelectorAll(".panel").forEach(p =>
    p.classList.toggle("active", p.dataset.panel === which));
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === which));
  /* The embedded report brings its own banner, header and footer, so the
   * shell stands its own down rather than showing each of them twice. Only
   * once a report is actually loaded: the empty-state placeholder still wants
   * the surrounding furniture. */
  document.body.classList.toggle("showing-report",
                                 which === "rep" && Boolean(S.reportUrl));
}

function toggleChip(button) {
  button.classList.toggle("active");
}

function wire() {
  el("searchbtn").addEventListener("click", search);
  el("q").addEventListener("keydown", e => { if (e.key === "Enter") search(); });
  el("gname").addEventListener("input", drawPicked);
  el("savebtn").addEventListener("click", saveGroup);
  el("cancelbtn").addEventListener("click", resetEditor);
  el("runbtn").addEventListener("click", startRun);
  el("probebtn").addEventListener("click", probeSources);

  document.querySelectorAll("[data-mode],[data-source],#invert").forEach(b =>
    b.addEventListener("click", () => toggleChip(b)));
  document.querySelectorAll(".tab").forEach(t =>
    t.addEventListener("click", () => showTab(t.dataset.tab)));

  /* Delegated, because these lists are redrawn constantly. */
  document.body.addEventListener("click", e => {
    const btn = e.target.closest("button");
    if (!btn) return;
    if (btn.dataset.add) addPick(Number(btn.dataset.add));
    else if (btn.dataset.del) removePick(Number(btn.dataset.del));
    else if (btn.dataset.ref) { S.reference = Number(btn.dataset.ref); drawPicked(); }
    else if (btn.dataset.edit) editGroup(btn.dataset.edit);
    else if (btn.dataset.archive) archiveGroup(btn.dataset.archive);
  });
}

function init() {
  wire();
  drawHits();
  drawPicked();
  loadGroups();
  if (CFG.demo) {
    show("searchmsg",
         "Demo mode: the catalogue and the plots are synthetic, no UDL credentials in use.");
  }
  showTab("cfg");
}

window.addEventListener("load", init);
