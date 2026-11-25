# Timeslides: what to know before changing anything

Along-track phase offset for LEO objects, from UDL state vectors and element
sets. FastAPI service, Plotly report, deployed as a Bluestaq App Store app.

`AUDIT.md` is the long record: architecture, verification evidence, open risks.
`CODE-QUALITY.md` is the register of gate findings and the rules that prevent
them. This file is the short version of what a new session must not get wrong.

## Settled. Do not re-litigate without new evidence

Each of these cost at least one upload to establish. Changing one on reasoning
alone has already cost several more.

● **The coverage report is rooted at the project, never the package.**
  `.coveragerc` uses `relative_files = True` and `source = .`, and `pytest.ini`
  passes a bare `--cov`. A `--cov=timeslides` overrides the config and roots
  the report at the package, which writes an absolute path from the test runner
  and bare filenames; the scanner then resolves nothing and the gate reports
  0.0%. Verified on a real SonarQube: project-rooted gives 100.0%,
  package-rooted gives 0.0%. `docker/sonar-probe.sh` reruns that experiment.
● **Offsets are measured against a ROLLING datum** (1.10): the reference
  object's element sets, each point offset against the two either side of its
  own time, blended (`physics.Datum`). This replaced "the LATEST element set",
  which had itself replaced "the earliest". Both single-element-set datums put
  SGP4's growing error on every series at once, where it reads as relative
  motion; latest only moved it to the left-hand edge. The evidence, including
  the one case where latest-only measured better, is in AUDIT.md under "The
  rolling datum". Latest only is still computed (`alt` in every dataset) and
  reachable with the report's Datum toggle, and a chain of one element set is
  bit-identical to the old datum: keep it that way, tests/test_datum.py holds
  it. The reference's own element sets sit at exactly zero under the rolling
  datum by construction, so they never count as evidence for it; the report's
  check uses the reference's state vectors only. `demo.py` must still advance
  each element set's angles with SGP4's own secular rates (`mdot`, `argpdot`,
  `nodedot`) or the synthetic element sets disagree with each other.
● **Credentials come from the environment.** `UDL_USER` and `UDL_PASS`, never a
  file in the repo. The application fails closed at boot without them, with one
  exception: the platform's DAST stage starts the image with no secrets and
  `APPSTORE_DAST_SCAN=true`, and only that combination starts in demo mode.
  With credentials present the flag changes nothing.
● **Logs follow the App Store standard, on stdout only.** One JSON object per
  line: `timestamp` (UTC, ms), `severity_text`, `severity_number`, `body`, flat
  `attributes`. The handler is on the root logger and takes over uvicorn's,
  because the image starts with `python -m uvicorn`, whose launcher installs
  stderr handlers before importing the app. Nothing may reach stderr.
● **The client-side code stays in separate `.js` and `.css` files** so the gate
  analyses it. Inlining it into Python strings would hide it from analysis as
  well as from coverage. That trade has been considered and refused.
● **The container is a minimal rootfs, not the Debian userland.** See
  `docker/build-rootfs.sh`. The packages it does ship are declared to the
  scanner in `status.d`, deliberately: minimal is not the same as invisible.
● **Storage is probed, never assumed.** `/data` is S3-backed and implements
  neither `fsync` nor `rename`. All writes go through `timeslides/storage.py`.

## The App Store pipeline, as observed

● Nine stages. `test` and `code-quality-scan` are **separate jobs with separate
  fresh checkouts**. Anything written by one is not automatically in the other.
● The scan's configuration arrives as `-D` flags via `SONAR_SCANNER_OPTS`, which
  override `sonar-project.properties` and the plugin defaults alike.
● `sonar-project.properties` is committed here and has been observed **absent**
  from the pipeline's tree. Do not assume its settings apply.
● The job container has **no git binary**, though `.git` may exist. A helper
  that shells out to git must never raise: see `in_git_worktree()` in
  `tests/conftest.py`.
● Playwright is not installed there, so the browser suite module-skips. Correct,
  and it should stay visible.
● Later stages judge the running image: **DAST** (no secrets, scans port 8080),
  **Logging** (stdout, the standard's JSON shape) and **Container Scan**.
  `tests/test_boot.py` runs the image's own CMD that way, locally.
● **JavaScript with no runnable test project counts as wholly uncovered.**
  1.10.0 failed the gate at 72.2% with every Python line covered, for that
  reason alone. `package.json` (no dependencies) makes the platform run
  `npm test -- --coverage`, which is `tests/js/run.mjs`: the assets loaded
  into a fake page, V8's own coverage through the built-in inspector, LCOV to
  `coverage/lcov.info`. Its fixtures are cut from pages the real builder
  renders and `tests/test_js_suite.py` fails if they drift. A local SonarQube
  with that LCOV reads 99.4%. Adding a `package.json` risks the platform
  re-detecting the template as node-react; the platform itself asked for one,
  and that risk is recorded in AUDIT.md under "Quality gate".
● **A failing test stage uploads no artefacts**, so the scan gets no coverage
  report and the gate reads 0.0%. A test that fails for an environmental reason
  therefore breaks the coverage gate. Structural checks use `repo_file()`, which
  skips with a named reason rather than failing.

## Before any upload

```bash
.venv/bin/python -m pytest          # 100% of timeslides, no exceptions
npm test                            # the JavaScript suite and coverage/lcov.info
.venv/bin/ruff check .
npx eslint .
sh docker/appstore-sim.sh           # both passes: normal, and hostile ingest
sh docker/sast-probe.sh             # the platform's own SAST rules, locally
docker build -f Dockerfile .        # from a git ls-files tree; runs the rootfs self-check
```

The image build is not optional. 1.8.0 removed a function that the rootfs
self-check in `docker/build-rootfs.sh` still imported. That check is Python
inside a shell heredoc, so a search of `.py` files and the whole test suite
missed it, and the Container Build stage found it. Search for a removed name
with `git grep` across every file, and build the image before every upload.

`appstore-sim.sh` reproduces the platform's environment rather than remembering
it: unpacked tree, no `.git`, no git binary, no `sonar-project.properties`, and
a second pass with every strippable file removed. Three pipeline failures were
environment differences that this would have caught.

Build the upload with `git ls-files`, so an untracked file shows up as missing.
`dist/` is ignored: it was tracked once and every package then contained the
package before it.

## The stop rule

**Two consecutive fixes failing on the same symptom means stop changing code.**
Get the missing evidence, or build a local replica of whatever is judging the
work. Six uploads went on theories about a stage whose log was never available;
running a real SonarQube in Docker settled it in ten minutes.

Corollaries, each learned here:

● An unresolvable report reads as 0.0%, not as "no data". A number that is
  exactly zero usually means nothing matched, not that something scored badly.
● Reading a build script is not running it. Building the image found three
  defects that review did not.
● A check that greps a file will match the comment explaining the check. Parse,
  or strip comments first. This has happened four times.
● A helper copied is a helper that will diverge, and the copy nobody is looking
  at is the one that breaks.

## Working preferences

UK English. Lead with the point. Mark fact, inference and speculation in any
risk or incident work. Prefer Opus for diagnosis and Sonnet for mechanical
execution. Keep replies to the answer, the evidence and the next step.
