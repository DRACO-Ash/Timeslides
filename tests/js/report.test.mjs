/* report.js against the payload the real builder renders.
 *
 * tests/js/fixtures/report-data.json is cut from a rendered report:
 * group 0 is the post-burn scenario (a rate change and passes), group 1 a
 * demo day in both data modes. tests/test_js_suite.py regenerates it and
 * fails if the committed copy drifts from what the application produces.
 */
import {fixture, load, settle} from "./dom.mjs";
import {assert, equal, test} from "./harness.mjs";

const DATA = fixture("report-data.json");

/* The controls each panel has, as report.js finds them. */
function panelControls(page, g) {
  const panel = page.byId(`q:.panel[data-panel="${g.id}"]`);
  const chip = src => page.element(`chip-${src}`, {dataset: {src}});
  panel.lists[".srcseg .srcchip"] = g.present.map(chip);
  panel.lists[".modeseg button"] = g.modeOrder.map(m => page.element(`mode-${m}`, {dataset: {mode: m}}));
  panel.lists[".horizon button"] = ["0", "3", "7", "14"].map(h => page.element(`h-${h}`, {dataset: {h}}));
  panel.lists[".datumseg button"] = ["rolling", "latest"].map(
    d => page.element(`d-${d}`, {dataset: {datum: d}}));
  return panel;
}

function boot() {
  const groups = JSON.parse(DATA).groups;
  const r = load("report.js", {"report-data": DATA}, page => {
    page.panels = groups.map(g => panelControls(page, g));
    page.setAll(".tab", groups.map(g => page.element(`tab-${g.id}`, {dataset: {tab: String(g.id)}})));
    page.setAll(".panel", page.panels);
    groups.forEach(g => {
      const form = page.byId(`manform-${g.id}`);
      form.children[".mango"] = page.element(`mango-${g.id}`);
    });
  });
  r.groups = groups;
  r.page.windowHandlers.load();
  return r;
}

const rd = (run, gid = 0) => run(`GROUPS[${gid}].modeData[ST[${gid}].mode].refData[ST[${gid}].ref]`);
/* The last call of a kind on one plot: init draws every group, so the last
 * call overall belongs to whichever group was drawn last. */
const lastCall = (page, name, div = "waterfall_0") =>
  [...page.Plotly.calls].reverse().find(c => c[0] === name && c[1] === div);

test("report: the first draw is the rolling datum, named and checked", () => {
  const {page, run} = boot();
  const d = rd(run).datum;
  equal(page.byId("datummeta-0").textContent, `Rolling · ${d.count} element sets`);
  assert(page.byId("datumcheck-0").textContent.startsWith("Check: the reference's own"));
  const restyle = page.Plotly.calls.find(c => c[0] === "restyle" && c[2].x);
  equal(restyle[2].x, rd(run).x, "the plot is drawn from the rolling reading");
  const ticks = lastCall(page, "relayout")[2].shapes.filter(s => s.name === "datum-epoch");
  equal(ticks.length, d.count);
  assert(page.byId("waterfall_0").data.some(t => t.uid === "datum-ref"), "reference trace drawn");
  assert(page.byId("refmeta-0").textContent.includes("·"));
  assert(page.byId("refsel-0").innerHTML.includes("<option"));
});

test("report: Latest only redraws offsets, cards and Look-ahead from alt", async () => {
  const {page, run} = boot();
  await page.panels[0].lists[".datumseg button"][1].click();
  equal(run("ST[0].datum"), "latest");
  const redraw = page.Plotly.calls.filter(c => c[0] === "restyle" && c[2].x).pop();
  equal(redraw[2].x, rd(run).alt.x, "the plot is redrawn from the latest-only reading");
  equal(page.byId("datummeta-0").textContent, "Latest element set only");
  assert(!page.byId("waterfall_0").data.some(t => t.uid === "datum-ref"), "no reference trace");
  equal(lastCall(page, "relayout")[2].shapes.filter(s => s.name === "datum-epoch").length, 0);
  assert(page.panels[0].lists[".datumseg button"][1].classList.contains("active"));
  await page.panels[0].lists[".datumseg button"][0].click();
  equal(run("ST[0].datum"), "rolling");
});

test("report: the Look-ahead horizon picks a view, and Off clears it", async () => {
  const {page, run} = boot();
  const list = page.byId("ahead-0");
  assert(list.innerHTML.includes("RATE CHANGE"), "the scenario's burn is listed");
  await page.panels[0].lists[".horizon button"][3].click();
  equal(run("ST[0].horizon"), "14");
  assert(page.byId("waterfall_0").data.some(t => String(t.uid).startsWith("ahead-line-")));
  await page.panels[0].lists[".horizon button"][0].click();
  assert(!page.byId("waterfall_0").data.some(t => String(t.uid).startsWith("ahead-")));
});

test("report: a source chip hides its traces, and a card isolates its object", async () => {
  const {page, run} = boot();
  const g = run("GROUPS[0]");
  const chip = page.panels[0].lists[".srcseg .srcchip"][0];
  await chip.click();
  // The objects' own restyle, not the Look-ahead's, which follows it.
  const vis = page.Plotly.calls.filter(c => c[0] === "restyle" && c[1] === "waterfall_0"
    && c[3]?.length === g.traces.length && c[2].visible).pop()[2].visible;
  g.traces.forEach((t, i) => { if (t.source === chip.dataset.src) equal(vis[i], false); });
  await chip.click();

  const card = page.element("card", {dataset: {obj: String(g.traces[0].obj)}});
  card.children[".eye"] = page.element("eye");
  page.panels[0].lists[".cards .card"] = [card];
  page.byId("cards-0").lists[".card"] = [card];
  run("buildCards(GROUPS[0])");
  await card.click();
  assert(card.classList.contains("off"));
  equal(card.children[".eye"].textContent, "HIDDEN");
  await card.dispatch("keydown", {key: "Enter"});
  assert(!card.classList.contains("off"));
  await card.dispatch("keydown", {key: "x"});
  assert(!card.classList.contains("off"), "other keys do nothing");
});

test("report: switching mode and reference redraws from that dataset", async () => {
  const {page, run, groups} = boot();
  const g1 = groups[1];
  assert(g1.modeOrder.length === 2, "the demo group carries two modes");
  await page.panels[1].lists[".modeseg button"][1].click();
  equal(run("ST[1].mode"), g1.modeOrder[1]);
  equal(page.byId("modemeta-1").textContent, g1.modeOrder[1]);
  const other = run("GROUPS[1].modeData[ST[1].mode].refs").map(r => String(r.norad))
    .find(n => n !== run("ST[1].ref"));
  page.byId("refsel-1").value = other;
  await page.byId("refsel-1").dispatch("change");
  equal(run("ST[1].ref"), other);
  run("ST[1].ref = 'missing'; applyData(GROUPS[1]);");
  equal(run("ST[1].ref"), String(run("GROUPS[1].modeData[ST[1].mode].defaultRef")),
        "a reference absent in this mode falls back to the default");
});

test("report: a manual state is measured against the datum on screen", async () => {
  const {page, run} = boot();
  const form = page.byId("manform-0");
  form.formEntries = [["kind", "state"], ["epoch", "2026-11-14T00:00:00Z"]];
  const trace = {uid: "manual-abc", x: [1], y: ["2026-11-14T00:00:00"]};
  page.reply({ok: true, body: {trace, row: '<li data-uid="manual-abc"></li>'}});
  await form.children[".mango"].click();
  await settle();
  const sent = page.requests.pop();
  equal(sent.url, "/api/manual-state");
  equal(sent.body.datum, run("GROUPS[0].modeData[ST[0].mode].chains[ST[0].ref]"));
  equal(sent.body.tle, rd(run).tle);
  assert(page.byId("waterfall_0").data.some(t => t.uid === "manual-abc"));
  assert(page.byId("manlist-0").innerHTML.includes("manual-abc"));

  // Switching datum measures it again, now against the latest element set alone.
  page.reply({ok: true, body: {trace: {...trace, uid: "manual-def"}, row: "<li></li>"}});
  await page.panels[0].lists[".datumseg button"][1].click();
  await settle();
  equal(page.requests.pop().body.datum, []);

  // A refusal is reported in words, and a shapeless one plainly.
  page.reply({ok: false, status: 400, body: {detail: "epoch is in the future"}});
  await form.children[".mango"].click();
  await settle();
  equal(page.byId("manmsg-0").textContent, "Not plotted: epoch is in the future");
  page.reply({ok: false, status: 422, body: {detail: [{msg: "x"}]}});
  await form.children[".mango"].click();
  await settle();
  equal(page.byId("manmsg-0").textContent, "Not plotted: refused");

  // Removing it from the rail takes it off the plot.
  const row = page.element("row", {dataset: {uid: "manual-def"}});
  const x = page.element("x", {closestMap: {".manx": "yes", ".manrow": row}});
  x.closestMap[".manrow"] = row;
  x.closest = sel => (sel === ".manx" ? x : row);
  await page.byId("manlist-0").dispatch("click", {target: x});
  assert(!page.byId("waterfall_0").data.some(t => t.uid === "manual-def"));
  const elsewhere = page.element("elsewhere");
  await page.byId("manlist-0").dispatch("click", {target: elsewhere});
});

test("report: tabs switch panels and resize the plot", async () => {
  const {page} = boot();
  const tabs = page.selectors[".tab"];
  await tabs[1].click();
  assert(tabs[1].classList.contains("active") && !tabs[0].classList.contains("active"));
  assert(page.Plotly.calls.some(c => c[0] === "resize" && c[1] === "waterfall_1"));
});

test("report: card and relative-motion wording covers every state", () => {
  const {run} = boot();
  equal(run(`cardKmLine({absent: true}, 7)`), "no data in this mode");
  assert(run(`cardKmLine({is_ref: true}, 7)`).startsWith("reference datum"));
  assert(run(`cardKmLine({current: -10}, 7.5)`).startsWith("trails reference by <b>~75 km"));
  assert(run(`cardKmLine({current: 10}, 7.5)`).startsWith("leads"));
  equal(run(`cardCounts({counts: {}})`), "no data");
  assert(run(`cardMarkup({absent: true, counts: {}, norad: 1, name: "<x>", colour: "#fff"}, 7)`)
    .includes("&lt;x&gt;"));
  equal(run(`pairState(0.5, 3).state`), "ALIGNED");
  equal(run(`pairState(50, 0.01).state`), "STEADY");
  equal(run(`pairState(50, -5).state`), "CLOSING");
  equal(run(`pairState(50, 5).state`), "SEPARATING");
  assert(run(`pairRow({current: 50, drift: 0, colour: "#a", name: "A"},
                      {current: 0, drift: 5, colour: "#b", name: "B"}, 7)`).includes("CLOSING"));
  run(`GROUPS[0].cards = [GROUPS[0].cards?.[0] ?? {current: 0, drift: 0}]; buildRelative(GROUPS[0]);`);
});

test("report: without Plotly the page still fills its text", () => {
  const {page, run} = boot();
  run("window.Plotly = undefined;");
  run("applyData(GROUPS[0]); drawDatum(GROUPS[0]); drawAhead(GROUPS[0]); apply(GROUPS[0]);"
      + " aheadVisibility(GROUPS[0]); showTab(0);");
  assert(page.byId("datummeta-0").textContent.length > 0);
});

test("report: a dataset without a datum block or alt still draws", () => {
  const {run} = boot();
  run(`const rd0 = GROUPS[0].modeData[ST[0].mode].refData[ST[0].ref];
       delete rd0.alt; delete rd0.datum; ST[0].datum = "latest";`);
  equal(run("dataset(GROUPS[0]) === GROUPS[0].modeData[ST[0].mode].refData[ST[0].ref]"), true);
  equal(run("datumShapes(GROUPS[0]).length"), 0);
  run("drawDatum(GROUPS[0])");
});
