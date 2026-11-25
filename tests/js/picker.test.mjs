/* picker.js, the configure-and-render page, against a fake service.
 *
 * The shell payload is cut from the page the real server renders
 * (tests/js/fixtures/shell-data.json, kept in step by test_js_suite.py).
 */
import {fixture, load, settle} from "./dom.mjs";
import {assert, equal, test} from "./harness.mjs";

const SHELL = fixture("shell-data.json");
const GROUPS = {rev: 3, groups: [
  {id: "g1", name: "Cluster", sats: [1, 2, 3], reference: 1, names: {2: "TWO"}},
  {id: "g2", name: "Pair", sats: [4, 5], reference: 5},
]};

function boot({shell = SHELL, groups = GROUPS, stored, storageThrows = false} = {}) {
  const r = load("picker.js", {"shell-data": shell}, page => {
    page.storageThrows = storageThrows;
    if (stored !== undefined) page.storage["timeslides.selectedGroups"] = stored;
    page.chips = ["REAL", "SIM"].map(m => page.element(`m-${m}`, {dataset: {mode: m}}));
    page.chips[0].classList.add("active");
    page.sources = ["leolabs", "kbr"].map(s => page.element(`s-${s}`, {dataset: {source: s}}));
    page.sources[0].classList.add("active");
    page.setAll("[data-mode],[data-source],#invert", [...page.chips, ...page.sources]);
    page.tabs = ["cfg", "rep"].map(t => page.element(`t-${t}`, {dataset: {tab: t}}));
    page.setAll(".tab", page.tabs);
    page.setAll(".panel", ["cfg", "rep"].map(t => page.element(`p-${t}`, {dataset: {panel: t}})));
    page.reply({ok: true, body: groups});
  });
  r.page.windowHandlers.load();
  return r;
}

const actives = page => {
  page.setAll("[data-mode].active", page.chips.filter(c => c.classList.contains("active")));
  page.setAll("[data-source].active", page.sources.filter(c => c.classList.contains("active")));
};
const button = (page, data) => {
  const b = page.element("btn", {dataset: data});
  b.closest = sel => (sel === "button" ? b : null);
  return b;
};

test("picker: boot loads the groups, selects them all and says what will render", async () => {
  const {page, run} = boot();
  await settle();
  equal(page.requests[0].url, "/api/groups");
  assert(page.byId("groups").innerHTML.includes("Cluster"));
  equal(page.byId("groupcount").textContent, "2 groups will be rendered");
  equal(page.byId("selcount").textContent, "2/2");
  assert(page.byId("runbtn").classList.contains("armed"));
  assert(page.byId("searchmsg").innerHTML.includes("Demo mode"), "the demo flag is stated");
  equal(JSON.parse(page.storage["timeslides.selectedGroups"]), ["g1", "g2"]);
  equal(run("groupCountLabel(1, 1)"), "1 group will be rendered");
  equal(run("groupCountLabel(0, 2)"), "No groups selected. Tick at least one to render.");
  equal(run("groupCountLabel(0, 0)"), "");
});

test("picker: a remembered selection is honoured, and a bad one discarded", async () => {
  const kept = boot({stored: JSON.stringify(["g2"])});
  await settle();
  equal(kept.page.byId("groupcount").textContent, "1 of 2 groups will be rendered");
  const bad = boot({stored: "{not json"});
  await settle();
  equal(bad.page.byId("groupcount").textContent, "2 groups will be rendered");
  assert(bad.page.warnings.length === 1);
  const notArray = boot({stored: JSON.stringify({a: 1})});
  await settle();
  equal(notArray.page.byId("groupcount").textContent, "2 groups will be rendered");
});

test("picker: a browser that refuses storage still works, and says so once", async () => {
  const {page, run} = boot({storageThrows: true});
  await settle();
  equal(page.byId("groupcount").textContent, "2 groups will be rendered");
  run("toggleGroup('g1', false); storeSelection(); readStoredSelection();");
  equal(page.warnings.length, 1);
  page.storageThrows = false;
  run("toggleGroup('g1', true)");
  equal(page.storage["timeslides.selectedGroups"], undefined, "no writes once refused");
});

test("picker: ticking groups, all and none, drives the run button", async () => {
  const {page} = boot();
  await settle();
  const box = page.element("box", {dataset: {pick: "g1"}, checked: false});
  box.closest = () => box;
  await page.byId("groups").dispatch("change", {target: box});
  equal(page.byId("groupcount").textContent, "1 of 2 groups will be rendered");
  await page.byId("groups").dispatch("change", {target: page.element("nothing")});
  await page.byId("selnone").click();
  equal(page.byId("runbtn").disabled, true);
  assert(!page.byId("runbtn").classList.contains("armed"));
  await page.byId("selall").click();
  equal(page.byId("runbtn").disabled, false);
});

test("picker: search finds, reports nothing found, and reports a failure", async () => {
  const {page, run} = boot();
  await settle();
  page.byId("q").value = "";
  await page.byId("searchbtn").click();
  assert(page.byId("hits").innerHTML.includes("Search the catalogue"));
  page.byId("q").value = "cosmos";
  page.reply({ok: true, body: {results: [{satNo: 62902, name: "COSMOS <2581>"}]}});
  await page.byId("q").dispatch("keydown", {key: "Enter"});
  await settle();
  assert(page.requests.pop().url.startsWith("/api/catalogue?q=cosmos"));
  assert(page.byId("hits").innerHTML.includes("COSMOS &lt;2581&gt;"));
  page.reply({ok: true, body: {results: []}});
  await page.byId("searchbtn").click();
  await settle();
  assert(page.byId("searchmsg").innerHTML.includes("Nothing in the catalogue"));
  page.reply({ok: false, status: 502, body: {detail: "UDL down"}});
  await page.byId("searchbtn").click();
  await settle();
  assert(page.byId("searchmsg").innerHTML.includes("Catalogue search failed: UDL down"));
  page.reply({ok: false, status: 500, badJson: true});
  await page.byId("searchbtn").click();
  await settle();
  assert(page.byId("searchmsg").innerHTML.includes("HTTP 500"));
  await page.byId("q").dispatch("keydown", {key: "a"});
  let refused = null;
  try { await run("api('https://elsewhere.example/x')"); } catch (err) { refused = err.message; }
  assert(refused.includes("not this service's API"));
});

test("picker: building, saving and editing a group", async () => {
  const {page, run} = boot();
  await settle();
  run("S.hits = [{satNo: 7, name: 'SEVEN'}, {satNo: 8, name: 'EIGHT'}]");
  await page.body.dispatch("click", {target: button(page, {add: "7"})});
  await page.body.dispatch("click", {target: button(page, {add: "7"})});
  await page.body.dispatch("click", {target: button(page, {add: "8"})});
  await page.body.dispatch("click", {target: button(page, {add: "9"})});
  equal(run("S.picked.map(p => p.name)"), ["SEVEN", "EIGHT", "OBJECT 9"]);
  equal(run("S.reference"), 7);
  await page.body.dispatch("click", {target: button(page, {ref: "8"})});
  equal(run("S.reference"), 8);
  await page.body.dispatch("click", {target: button(page, {del: "8"})});
  equal(run("S.reference"), 7, "removing the reference hands it to the first");
  page.byId("gname").value = "New";
  await page.byId("gname").dispatch("input");
  equal(page.byId("savebtn").disabled, false);

  page.reply({ok: true, body: {}});
  page.reply({ok: true, body: GROUPS});
  await page.byId("savebtn").click();
  await settle();
  const post = page.requests.find(r => r.opts.method === "POST");
  equal(post.body, {name: "New", sats: [7, 9], reference: 7, rev: 3});

  await page.body.dispatch("click", {target: button(page, {edit: "g1"})});
  equal(run("S.editing"), "g1");
  equal(run("S.picked.map(p => p.name)"), ["OBJECT 1", "TWO", "OBJECT 3"]);
  assert(page.focused === page.byId("gname"));
  page.reply({ok: false, status: 409, body: {detail: "changed by someone else"}});
  await page.byId("savebtn").click();
  await settle();
  equal(page.requests.pop().opts.method, "PUT");
  assert(page.byId("savemsg").innerHTML.includes("changed by someone else"));
  await page.byId("cancelbtn").click();
  equal(run("S.editing"), null);
  run("editGroup('nope')");
  await page.body.dispatch("click", {target: page.element("plain", {closest: () => null})});
});

test("picker: archiving asks first, and reports a refusal", async () => {
  const {page, run} = boot();
  await settle();
  page.confirmAnswer = false;
  await page.body.dispatch("click", {target: button(page, {archive: "g1"})});
  equal(page.requests.length, 1, "declined: nothing sent");
  page.confirmAnswer = true;
  run("S.editing = 'g1'");
  page.reply({ok: true, body: {}});
  page.reply({ok: true, body: {rev: 4, groups: [GROUPS.groups[1]]}});
  await page.body.dispatch("click", {target: button(page, {archive: "g1"})});
  await settle();
  assert(page.requests.some(r => r.opts.method === "DELETE" && r.url.includes("rev=3")));
  equal(run("S.editing"), null);
  page.reply({ok: false, status: 404, body: {error: "NotFoundError"}});
  await page.body.dispatch("click", {target: button(page, {archive: "gone"})});
  await settle();
  assert(page.byId("groupsmsg").innerHTML.includes("NotFoundError"));
});

test("picker: a failed group load is reported, and memory-only storage noted", async () => {
  const failed = load("picker.js", {"shell-data": SHELL}, page => page.reply({ok: false, status: 500, body: {}}));
  failed.page.windowHandlers.load();
  await settle();
  assert(failed.page.byId("groupsmsg").innerHTML.includes("Could not load groups"));
  const memory = boot({shell: JSON.stringify({demo: false, classification: "U", storageWritable: false}),
                       groups: {rev: 0, groups: []}});
  await settle();
  assert(memory.page.byId("groupsmsg").innerHTML.includes("Kept in memory only"));
  assert(memory.page.byId("groups").innerHTML.includes("No groups yet"));
});

test("picker: the provider probe marks each chip and explains a gap", async () => {
  const {page} = boot();
  await settle();
  page.setAll('[data-source="leolabs"]', [page.sources[0]]);
  page.setAll('[data-source="kbr"]', [page.sources[1]]);
  const results = [
    {key: "leolabs", label: "LeoLabs", udlSource: "LeoLabs", available: true},
    {key: "kbr", label: "KBR", udlSource: "KBR", available: false, error: "HTTP 403"},
    {key: "absent", label: "X", udlSource: "X", available: true},
  ];
  page.reply({ok: true, body: {satNo: 62902, demo: false, results}});
  await page.byId("probebtn").click();
  await settle();
  assert(page.sources[1].classList.contains("gone"));
  equal(page.sources[1].title, "UDL source KBR: no data - HTTP 403");
  assert(page.byId("probemsg").innerHTML.includes("No data from KBR (probed with NORAD 62902)"));
  page.reply({ok: true, body: {satNo: 62902, demo: true, results: results.slice(0, 1)}});
  await page.byId("probebtn").click();
  await settle();
  assert(page.byId("probemsg").innerHTML.includes("Demo mode"));
  page.reply({ok: true, body: {demo: false, results: results.slice(0, 1)}});
  await page.byId("probebtn").click();
  await settle();
  assert(page.byId("probemsg").innerHTML.includes("Every provider answered"));
  page.reply({ok: false, status: 400, body: {detail: "nothing to probe with"}});
  await page.byId("probebtn").click();
  await settle();
  assert(page.byId("probemsg").innerHTML.includes("nothing to probe with"));
  equal(page.byId("probemsg").getAttribute("aria-busy"), "false");
});

test("picker: a render is refused until it is complete, then polled to the report", async () => {
  const {page, run} = boot();
  await settle();
  actives(page);
  page.byId("days").value = "400";
  page.setAll("[data-mode].active", []);
  await page.byId("runbtn").click();
  assert(page.byId("runmsg").innerHTML.includes("Select at least one data mode"));
  actives(page);
  page.setAll("[data-source].active", []);
  await page.byId("runbtn").click();
  assert(page.byId("runmsg").innerHTML.includes("Select at least one provider"));
  run("S.selected = new Set()");
  await page.byId("runbtn").click();
  assert(page.byId("runmsg").innerHTML.includes("Select at least one group"));
  run("S.selected = new Set(['g1'])");
  actives(page);
  await page.chips[1].click();      // toggles SIM on
  actives(page);

  page.reply({ok: true, body: {id: "r1", status: "queued", joined: true}});
  page.reply({ok: true, body: {id: "r1", status: "running", progress: {done: 1, total: 3, current: "TWO"}}});
  await page.byId("runbtn").click();
  await settle();
  const post = page.requests.find(r => r.url === "/api/runs");
  equal(post.body.days, 90);
  equal(post.body.modes, ["REAL", "SIM"]);
  assert(page.byId("runmsg").innerHTML.includes("running 1/3 · TWO"));
  page.reply({ok: true, body: {id: "r1", status: "done", reportUrl: "/api/runs/r1/report"}});
  await page.runTimers();
  await settle();
  equal(page.byId("repframe").src, "/api/runs/r1/report");
  assert(page.body.classList.contains("showing-report"));
  assert(page.byId("runmsg").innerHTML.includes("Report ready"));
});

test("picker: a failed render, a lost poll and a superseded run are each handled", async () => {
  const {page, run} = boot();
  await settle();
  actives(page);
  page.reply({ok: true, body: {id: "r2", status: "queued"}});
  page.reply({ok: true, body: {id: "r2", status: "failed", error: "no data"}});
  await page.byId("runbtn").click();
  await settle();
  assert(page.byId("runmsg").innerHTML.includes("no data"));
  run("S.run = {status: 'failed'}; drawRun(); S.run = null; drawRun();");
  assert(page.byId("runmsg").innerHTML.includes("The render failed."));
  equal(run("progressDetail({})"), "");

  page.reply({ok: true, body: {id: "r3", status: "queued"}});
  page.reply(new Error("network down"));
  await page.byId("runbtn").click();
  await settle();
  assert(page.byId("runmsg").innerHTML.includes("Lost track of the run: network down"));

  page.reply({ok: false, status: 400, body: {detail: "window too long"}});
  await page.byId("runbtn").click();
  await settle();
  assert(page.byId("runmsg").innerHTML.includes("window too long"));

  // A run superseded while its POST or poll was in flight changes nothing.
  page.reply({ok: true, body: {id: "r4", status: "queued"}});
  const first = run("startRun()");
  run("S.runToken += 1");
  await first;
  await settle();
  run("S.run = {id: 'r4'}; S.poll = 1; pollRun(S.runToken - 1);");
  page.reply(new Error("late"));
  run("S.runToken += 0");
  const stale = run("startRun()");
  run("S.runToken += 1");
  await stale;
  await settle();
  await page.tabs[0].click();
  assert(page.tabs[0].classList.contains("active"));
});
