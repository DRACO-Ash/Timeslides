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
  selected: new Set(),   /* group ids the next render will cover */
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
  if (!message) {
    box.innerHTML = "";
    return;
  }
  const kind = isError ? "err" : "note";
  box.innerHTML = `<div class="${kind}">${esc(message)}</div>`;
}

/* --- transport ---------------------------------------------------------- */
async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options
  });
  let body = null;
  try {
    body = await res.json();
  } catch (err) {
    /* A 204, an empty body, or an error page from something sitting in front
     * of the application. The caller falls back to the status code, which is
     * the useful information in all three cases. */
    body = null;
  }
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
  const cached = group.names?.[satNo];
  return cached || `OBJECT ${satNo}`;
}

/* --- which groups a render covers ---------------------------------------- */
/* Rendering every saved group every time is the wrong default once somebody
 * has more than a couple: it is slow, it fetches data nobody asked for, and it
 * buries the group they actually care about behind tabs. The selection is kept
 * per browser so the choice survives a reload rather than having to be made
 * again on every visit. */
const SEL_KEY = "timeslides.selectedGroups";

/* Set once if the browser refuses storage outright: a private window, blocked
 * site data, or a policy that throws on the property itself. Remembering the
 * choice is a convenience and never a requirement, so the page carries on
 * without it, but the refusal is recorded rather than swallowed: it is said
 * once, and the remaining calls stop trying instead of throwing on every
 * redraw for the rest of the session. */
let storageDenied = null;

function denyStorage(err) {
  if (!storageDenied) {
    storageDenied = err;
    console.warn(
      "Timeslides: this browser will not let the group selection be " +
      "remembered, so it resets on reload. Everything else works.",
      err && err.message);
  }
  return null;
}

function readStoredSelection() {
  if (storageDenied) return null;
  let raw;
  try {
    raw = window.localStorage.getItem(SEL_KEY);
  } catch (err) {
    return denyStorage(err);
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    /* Only an array is what this code writes. The check is not belt and
     * braces: JSON "null" parses without throwing and new Set(null) is a legal
     * empty set, so without it a stored null would silently mean "nothing
     * selected" and leave the render button dead with nothing said. */
    if (!Array.isArray(parsed)) throw new TypeError("expected an array");
    return new Set(parsed);
  } catch (err) {
    /* A different failure, and conflating the two would be a bug: the store
     * works, its contents are simply not what this code wrote. Treating that
     * as a denial would stop every later write and leave the bad value in
     * place for good. Discard it and let the next write replace it. */
    console.warn("Timeslides: the remembered group selection could not be "
                 + "read and has been discarded.", err && err.message);
    return null;
  }
}

function storeSelection() {
  if (storageDenied) return;
  try {
    window.localStorage.setItem(SEL_KEY, JSON.stringify([...S.selected]));
  } catch (err) {
    denyStorage(err);
  }
}

function reconcileSelection() {
  /* Called after every load of the group list.
   *
   * Three rules, each there for a reason somebody would notice:
   *   - A group that no longer exists drops out, so an archived group cannot
   *     sit invisibly in the selection and be rendered.
   *   - A group that is new since the last visit is selected, because a group
   *     you have just built is one you want in the next render.
   *   - No stored selection at all means everything, which is what the
   *     application did before there was a choice to make.
   */
  const live = new Set(S.groups.map(g => g.id));
  const stored = S.known ? null : readStoredSelection();
  if (!S.known) {
    S.known = new Set();
    if (stored) stored.forEach(id => S.selected.add(id));
    else live.forEach(id => S.selected.add(id));
    live.forEach(id => S.known.add(id));
  }
  S.groups.forEach(g => {
    if (!S.known.has(g.id)) {           /* created since the last draw */
      S.known.add(g.id);
      S.selected.add(g.id);
    }
  });
  [...S.selected].forEach(id => { if (!live.has(id)) S.selected.delete(id); });
  [...S.known].forEach(id => { if (!live.has(id)) S.known.delete(id); });
  storeSelection();
}

/* These change the selection and nothing else. Redrawing is the caller's job,
 * so the rule about what is selected can be reasoned about, and tested,
 * without a document to draw into. */
function toggleGroup(id, on) {
  if (on) S.selected.add(id);
  else S.selected.delete(id);
  storeSelection();
}

function selectEvery(on) {
  S.selected = on ? new Set(S.groups.map(g => g.id)) : new Set();
  storeSelection();
}

/* --- group list --------------------------------------------------------- */
/* The banner at the top of the tab says this too, but the saved-groups list is
 * where somebody looks to check their work, and on a long list it scrolls well
 * clear of the banner. Repeating it here means the reminder is next to the
 * thing it applies to. */
const MEMORY_NOTE =
  "Kept in memory only: these groups will be lost when the pod restarts.";

function groupsStatus() {
  return CFG.storageWritable ? "" : MEMORY_NOTE;
}

async function loadGroups() {
  try {
    const body = await api("/api/groups");
    S.rev = body.rev;
    S.groups = body.groups || [];
    reconcileSelection();
    show("groupsmsg", groupsStatus());
  } catch (err) {
    show("groupsmsg", `Could not load groups: ${err.message}`, true);
  }
  drawGroups();
}

function groupCountLabel(chosen, total) {
  if (!total) return "";
  if (!chosen) return "No groups selected. Tick at least one to render.";
  /* "1 of 4 groups", not "1 of 4 group": with a scope the noun agrees with the
   * total, and a scope only appears when the total is at least two. */
  const all = chosen === total;
  const plural = all && chosen === 1 ? "" : "s";
  const scope = all ? "" : ` of ${total}`;
  return `${chosen}${scope} group${plural} will be rendered`;
}

function drawGroups() {
  el("groups").innerHTML = S.groups.map(g => {
    const on = S.selected.has(g.id);
    return `
    <div class="grp ${on ? "sel" : ""}">
      <div class="gh">
        <label class="pick" title="Include this group in the next render">
          <input type="checkbox" data-pick="${esc(g.id)}" ${on ? "checked" : ""}
                 aria-label="Render ${esc(g.name)}">
        </label>
        <span class="gn">${esc(g.name)}</span>
        <span class="gm">${g.sats.length} objects</span>
        <button class="btn sm ghost" data-edit="${esc(g.id)}">edit</button>
        <button class="btn sm warn" data-archive="${esc(g.id)}">archive</button>
      </div>
      <div class="chips">${g.sats.map(n =>
        `<span class="chip ${n === g.reference ? "ref" : ""}">${n}${
          n === g.reference ? " · ref" : ""}</span>`).join("")}</div>
    </div>`;
  }).join("") ||
    '<p class="note">No groups yet. Build one from the catalogue on the left.</p>';

  const chosen = S.groups.filter(g => S.selected.has(g.id)).length;
  const run = el("runbtn");
  const armed = chosen > 0;
  run.disabled = !armed;
  /* The class is set only on the transition into armed, so the animation that
   * draws the eye fires once when the operator finishes choosing rather than
   * running forever in the corner of their vision. */
  if (armed && !run.classList.contains("armed")) run.classList.add("armed");
  if (!armed) run.classList.remove("armed");
  el("groupcount").textContent = groupCountLabel(chosen, S.groups.length);
  el("selcount").textContent = S.groups.length ? `${chosen}/${S.groups.length}` : "";
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
function chipTitle(result) {
  if (result.available) return `UDL source ${result.udlSource}: answered`;
  const because = result.error ? " - " + result.error : "";
  return `UDL source ${result.udlSource}: no data${because}`;
}

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
      chip.title = chipTitle(r);
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
    /* The selection, not everything. An empty list means every live group to
     * the API, which is why the render button is disabled rather than sending
     * one: an empty selection must never quietly mean "all". */
    groupIds: S.groups.filter(g => S.selected.has(g.id)).map(g => g.id),
    days: Math.max(1, Math.min(90, Number.parseInt(el("days").value, 10) || 7)),
    modes: selectedModes(),
    sources: selectedSources(),
    invert: el("invert").classList.contains("active")
  };
  if (!body.groupIds.length) return show("runmsg", "Select at least one group.", true);
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

function progressDetail(job) {
  const p = job.progress || {};
  if (!p.total) return "";
  const where = p.current ? " \u00b7 " + p.current : "";
  return ` ${p.done}/${p.total}${where}`;
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
  el("runmsg").innerHTML =
    `<div class="note"><span class="spin"></span>${esc(j.status)}${esc(progressDetail(j))}</div>`;
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
  el("selall").addEventListener("click", () => { selectEvery(true); drawGroups(); });
  el("selnone").addEventListener("click", () => { selectEvery(false); drawGroups(); });
  /* Delegated like the other group-row controls, because the list is redrawn
   * on every change and directly bound handlers would be lost with it. */
  el("groups").addEventListener("change", e => {
    const box = e.target.closest("input[data-pick]");
    if (!box) return;
    toggleGroup(box.dataset.pick, box.checked);
    drawGroups();
  });
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
