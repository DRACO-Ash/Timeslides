/* The JavaScript test suite, and the coverage report the quality gate reads.
 *
 * Why this exists: the App Store counts JavaScript with no runnable test
 * project as wholly uncovered, and 1.10.0 failed its coverage gate at 72.2%
 * against 80% for that reason, with the Python at 100%. The platform runs
 * `npm test -- --coverage` and reads coverage/lcov.info, the conventional
 * Node location.
 *
 * Why no dependencies: a test framework or coverage tool from npm would be
 * a new supply-chain surface for the Dependency Scanning stage to judge, for
 * something Node already does. Coverage comes from V8 itself through the
 * built-in inspector (precise, block-level, with call counts) and is turned
 * into LCOV line hits here. The harness is a dozen lines.
 *
 * Extra arguments are ignored on purpose: the platform appends --coverage,
 * and coverage is always on.
 *
 *     npm test            or    node tests/js/run.mjs
 */
import fs from "node:fs";
import inspector from "node:inspector/promises";
import path from "node:path";
import {pathToFileURL} from "node:url";

import {ASSETS, ROOT} from "./dom.mjs";
import {TESTS} from "./harness.mjs";

/* The ingest has been seen to drop files from an upload (sonar-project
 * .properties), and docker/appstore-sim.sh removes eslint.config.mjs in its
 * second pass to prove nothing depends on it. So the config's tests and its
 * coverage record are there only when the config is: a suite that fails over
 * a missing lint config would fail the test stage, and a failed test stage
 * uploads no coverage at all. */
const ESLINT_CONFIG = path.join(ROOT, "eslint.config.mjs");
const HAVE_ESLINT_CONFIG = fs.existsSync(ESLINT_CONFIG);
const COVERED = [
  path.join(ASSETS, "report.js"),
  path.join(ASSETS, "picker.js"),
  ...(HAVE_ESLINT_CONFIG ? [ESLINT_CONFIG] : []),
];

/* Coverage starts before any covered file is loaded, so top-level code
 * counts. */
const session = new inspector.Session();
session.connect();
await session.post("Profiler.enable");
await session.post("Profiler.startPreciseCoverage", {callCount: true, detailed: true});

const MODULES = ["report.test.mjs", "picker.test.mjs"];
if (HAVE_ESLINT_CONFIG) MODULES.push("eslint.test.mjs");
else console.log("skip - eslint.config.mjs is not in this tree, so its rules were not tested");
for (const mod of MODULES) {
  await import(pathToFileURL(path.join(ROOT, "tests", "js", mod)).href);
}

let failed = 0;
for (const t of TESTS) {
  try {
    await t.fn();
    console.log(`ok - ${t.name}`);
  } catch (err) {
    failed += 1;
    console.log(`not ok - ${t.name}\n  ${String(err?.stack ?? err).split("\n").join("\n  ")}`);
  }
}

const {result} = await session.post("Profiler.takePreciseCoverage");
session.disconnect();

/* V8 reports byte ranges with counts, nested: the script, then each
 * function, then each block. The innermost range containing an offset gives
 * its count, so ranges are painted outermost first. */
function lineHits(source, functions) {
  const counts = new Int32Array(source.length).fill(-1);
  const ranges = functions.flatMap(f => f.ranges)
    .sort((a, b) => (b.endOffset - b.startOffset) - (a.endOffset - a.startOffset));
  for (const r of ranges) counts.fill(r.count, r.startOffset, Math.min(r.endOffset, source.length));
  const hits = [];
  let offset = 0;
  let inComment = false;
  source.split("\n").forEach((line, i) => {
    const trimmed = line.trim();
    const lead = offset + line.indexOf(trimmed);
    const wasComment = inComment;
    if (trimmed.startsWith("/*")) inComment = !trimmed.includes("*/");
    else if (inComment && trimmed.includes("*/")) inComment = false;
    const comment = wasComment || trimmed.startsWith("/*") || trimmed.startsWith("//");
    // Code lines only: a blank line or a comment is not something to cover.
    if (trimmed && !comment && counts[lead] >= 0) hits.push([i + 1, counts[lead]]);
    offset += line.length + 1;
  });
  return hits;
}

const byFile = new Map();
for (const script of result) {
  const file = script.url.startsWith("file:") ? new URL(script.url).pathname : script.url;
  if (!COVERED.includes(file)) continue;
  const source = fs.readFileSync(file, "utf8");
  const merged = byFile.get(file) || new Map();
  for (const [line, n] of lineHits(source, script.functions)) {
    merged.set(line, (merged.get(line) || 0) + n);
  }
  byFile.set(file, merged);
}

const lcov = [];
const summary = [];
for (const file of COVERED) {
  const hits = byFile.get(file) || new Map();
  const lines = [...hits.entries()].sort((a, b) => a[0] - b[0]);
  const hit = lines.filter(([, n]) => n > 0).length;
  lcov.push(`SF:${path.relative(ROOT, file)}`);
  lines.forEach(([line, n]) => lcov.push(`DA:${line},${n}`));
  lcov.push(`LF:${lines.length}`, `LH:${hit}`, "end_of_record");
  summary.push(`${path.relative(ROOT, file)}: ${hit}/${lines.length} lines`
               + ` (${lines.length ? (100 * hit / lines.length).toFixed(1) : "0.0"}%)`);
}
fs.mkdirSync(path.join(ROOT, "coverage"), {recursive: true});
fs.writeFileSync(path.join(ROOT, "coverage", "lcov.info"), lcov.join("\n") + "\n");

console.log(`\n${TESTS.length - failed} passed, ${failed} failed`);
summary.forEach(s => console.log(`coverage ${s}`));
process.exitCode = failed ? 1 : 0;
