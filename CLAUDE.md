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
● **Credentials come from the environment.** `UDL_USER` and `UDL_PASS`, never a
  file in the repo. The application fails closed at import without them.
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
● **A failing test stage uploads no artefacts**, so the scan gets no coverage
  report and the gate reads 0.0%. A test that fails for an environmental reason
  therefore breaks the coverage gate. Structural checks use `repo_file()`, which
  skips with a named reason rather than failing.

## Before any upload

```bash
.venv/bin/python -m pytest          # 100% of timeslides, no exceptions
.venv/bin/ruff check .
npx eslint .
sh docker/appstore-sim.sh           # both passes: normal, and hostile ingest
```

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
