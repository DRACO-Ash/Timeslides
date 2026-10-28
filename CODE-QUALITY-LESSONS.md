# Code quality lessons: the Timeslides register

Bluestaq Limited · portable edition

Every entry below is a defect that was actually found during the development of
one application, Timeslides, together with the mechanism behind it and the rule
that now prevents it. Nothing here is invented, aspirational or copied from a
style guide. Each cost a pipeline run, an upload, a release or a round trip to
discover.

This file is written to be read by an agent working on a different codebase. It
is deliberately not the same document as `CODE-QUALITY.md`, which is the
project's own narrative register. This one strips the narrative and keeps the
transferable part.

## What this came from

● One FastAPI service with a Plotly front end, deployed on an internal
  enterprise app store with a 9-stage pipeline and a SonarQube quality gate.
● 41 commits, 755 tests across 18 modules, 100 per cent Python line coverage.
● Roughly 20 uploads, of which 6 were spent re-deriving things already known
  and 4 on inference about a pipeline stage whose log was never available.
● Three independent gates: a code quality gate, a container image policy scan,
  and the platform's own test stage.

## How to use this file

For an agent starting work on a new project:

1. Read **Part 1** first. The meta-rules generated most of the specific
   findings, so adopting those 12 prevents more than adopting any 12 entries
   from Part 2.
2. Treat **Part 2** as a checklist to run against the codebase in front of you,
   not as a description of it. An entry that does not apply costs one line to
   dismiss.
3. **Part 4** is the enforcement scaffolding. If you copy one thing, copy that
   pattern: a detector is a named function, shown both the offender it was
   written for and the lookalike it must leave alone.
4. Entries marked **STILL LIVE** are unresolved in the source project. They are
   included because an unfixed finding is still a transferable lesson.

## Field key

Each entry carries the same 6 fields.

● **Symptom** what was observed, in the terms the observer saw it.
● **Cause** the mechanism. Not a restatement of the symptom.
● **Fix** what was changed.
● **Rule** the generalisable instruction, stated imperatively.
● **Enforced** the exact test, config key or script that prevents recurrence.
  `none` where nothing does, which is itself information.
● **Source** commit or file, and the recorded cost where one exists.

Identifier prefixes: `META` principles, `GATE` quality gate and coverage, `PIPE`
pipeline and packaging, `CTR` container image, `JS`, `CSS`, `PY`, `STOR`
storage, `SEC` security, `TEST` testing and verification, `DATA` correctness,
`UI` interface and accessibility, `DOC` process and documentation.

# Part 1: the meta-rules

These 12 generated most of Part 2. They are ordered by how much they would have
saved.

### META-01 An unresolvable artefact reads as zero, not as "no data"

A number that is exactly zero usually means nothing matched, not that something
scored badly. The quality gate reported `Line coverage: 0.0%` against a suite
measured at 100 per cent, and the advice attached to it was to write more tests.
Nothing was wrong with the tests. The arithmetic was available before the
experiment: an imported report would have scored about 73 per cent, so exactly
0.0 could only mean no report was read at all.

**Rule.** When a measurement comes back as exactly zero, first establish whether
the measuring instrument could see anything at all. Do not act on the number.

### META-02 Two consecutive fixes failing on the same symptom means stop

Three rounds of plausible explanations for the 0.0 per cent gate produced three
wrong fixes and cost 4 uploads. The fourth round ran a real SonarQube in Docker
against the repository in 3 configurations and read the answer off the API in
10 minutes.

**Rule.** After the second failed fix on one symptom, stop changing code. Get
the missing evidence, or build a local replica of whatever is judging the work.
Six uploads went on theories about a stage whose log was never available.

### META-03 Mapping "could not verify" onto "passed" is the defect that hides every other one

This recurs in 4 unrelated places: a browser suite that skipped silently where
no Chromium was present; a container scanner that found no package database and
reported no operating-system packages; a probe that wrote an empty file and
declared a volume writable; a detector that passed because its own input had
been blanked.

**Rule.** Every check must distinguish "verified good" from "could not look".
A skip is only honest if it is loud and names its reason.

### META-04 A check that reads a file whole will match the comment explaining the check

This happened 4 times in one project: the `!important` rule flagged its own
justification, the hovertemplate rule flagged the comment warning against
entities, a Dockerfile rule flagged the comment describing the line it forbids,
and a structural test matched its own source.

**Rule.** Parse, or strip comments and strings first. Then prove the stripper
works by planting a known offender and asserting the detector fires.

### META-05 A test that passes on a clean repository is exactly what a broken detector also does

**Rule.** Every detector gets two companion tests: one showing it fires on the
offender it was written for, one showing it leaves the lookalike alone. A
further test asserts no detector exists without that pair, because a detector
nobody calls is a rule nobody enforces.

### META-06 Reproduce the environment, do not remember it

Three pipeline failures in a row were environment differences that passed
locally. A local simulation that installed the developer requirements file
reproduced the author's intent rather than the platform's behaviour.

**Rule.** Build a script that reproduces the target environment's constraints
exactly, including what it does not have. Verify the script itself by reverting
a known defect and confirming it goes red.

### META-07 A helper copied is a helper that will diverge, and the copy nobody is looking at is the one that breaks

A git-probe helper existed in `conftest.py` with a docstring describing the
exact hazard and an `except OSError`. A second copy was written beside it
without the guard. It raised inside a `skipif` at import, pytest reported a
collection error, and 684 passing tests never executed.

**Rule.** One implementation. Enforce it structurally: parse the tree and fail
if a second appears.

### META-08 Two places configuring the same thing will disagree

`--cov=timeslides` on the command line silently overrode `source` in
`.coveragerc`, so the local run and the pipeline run produced differently
shaped reports from the same repository.

**Rule.** One source of truth per setting, and a test pinning which one it is.

### META-09 An artefact handed to another tool must be unambiguous, not merely correct

The coverage report was correct: right numbers, relative paths, every file
present. It also carried an empty `<source>` element ahead of the real one, and
a consumer that takes the first root resolves every file to a path that does not
exist.

**Rule.** When a format allows two readings, emit the one reading. Check the
artefact in the shape the receiving stage will read it, not the shape that looks
right where it was made.

### META-10 Reading a script is not running it

Reviewing a build script on the page found nothing. Running it found 2 defects.
Building the image found 3 more, none of them visible on the page.

**Rule.** A build step is not verified until it has been run end to end.

### META-11 A rule that cries wolf gets switched off, and then it protects nothing

Two rules had to be rewritten because the first version flagged its own
explanatory comment. One ESLint selector had to become a local rule because
esquery cannot compare two fields of a node, so it flagged `g && window.Plotly`
where the identifiers are unrelated.

**Rule.** A rule earns a place only if it is mechanically checkable with close
to no false positives. A linter that reports on files that are not yours is a
linter people stop running.

### META-12 A hedge must be labelled as a hedge

Writing an artefact to the path a scanner searches by default is a guess about
configuration nobody can see.

**Rule.** Put the word hypothesis in the code, with the cost of being wrong
stated, so nobody later reads a guess as something that was verified. Mark fact,
inference and speculation separately in any record.

# Part 2: the register

## A. Quality gate and coverage

### GATE-01 Coverage report rooted at the package, not the project
**Symptom** Gate reports `Line coverage is 0.0%` against a suite measured at 100 per cent. The gate's own advice is to write more tests.
**Cause** `source = timeslides` makes coverage.py treat the package directory as the report root. It writes the package's absolute path on the test runner as `<source>` and names every file relative to it, as `api.py`. A scanner resolves a coverage entry by joining a source root to a filename and has to land on a file it analysed. That absolute path belongs to another machine, so nothing matches and every analysed line is counted uncovered.
**Fix** `relative_files = True` stops absolute paths being written. `source = .` roots the report at the project, so files are named `timeslides/api.py`, which is how the scanner has indexed them.
**Rule** Root a coverage report at the same directory the analyser indexes files against, and never at the package.
**Enforced** `tests/test_coverage_report.py`, which generates a report and asserts the paths. Both assertions were confirmed to fail against the old configuration.
**Source** `2b147c9`, `5437e6c`. Verified on a real SonarQube 26.9: project-rooted 100.0 per cent, package-rooted 0.0 per cent. Cost roughly 6 uploads.

### GATE-02 An empty source root emitted ahead of the real one
**Symptom** A correct coverage report still resolves to nothing.
**Cause** coverage.py writes `<sources><source></source><source>.</source></sources>`. Joining an empty root to `timeslides/api.py` gives `/timeslides/api.py`, an absolute path that exists nowhere, so a parser taking the first root rather than trying each resolves no file at all.
**Fix** The empty element is stripped after the report is written, leaving exactly one root. No coverage data is altered.
**Rule** Emit one reading where a format allows two. Correct-but-ambiguous reads downstream as wrong.
**Enforced** `_strip_empty_source_roots()` in `tests/conftest.py`, called from the terminal summary hook.
**Source** `74c38b0`.

### GATE-03 A command-line flag silently overriding the configuration file
**Symptom** The local run and the pipeline run produce differently shaped reports from the same repository.
**Cause** `--cov=timeslides` in `pytest.ini` overrides `source` in `.coveragerc`. The config says the project root and names files `timeslides/api.py`; the command line says the package and names them `api.py`.
**Fix** Pass a bare `--cov` so the configuration file decides, and record the reason in the configuration file rather than in the command line.
**Rule** One source of truth per setting. Where a tool accepts the same setting in two places, pin which one wins with a test.
**Enforced** `tests/test_coverage_report.py` asserts the bare form.
**Source** `5437e6c`.

### GATE-04 The coverage report was git-ignored and the scan ran in a separate checkout
**Symptom** Every fix to the coverage report changed a file the scanner never opened.
**Cause** The code-quality stage is a separate job with a fresh checkout. `coverage.xml` is written by the test run and was git-ignored, so it did not exist in the scan's tree.
**Fix** Commit the report, fenced: deterministic with the generation timestamp zeroed, so a diff means coverage really moved.
**Rule** Establish which filesystem each pipeline stage actually sees before fixing anything it reads. Separate jobs do not share a working directory unless the platform says they do.
**Enforced** `tests/test_coverage_report.py` fails if the committed copy is stale or has lost a module.
**Source** `5fd348a`. Measured: no report present at all gives exactly 0.0 per cent with "No report was found for sonar.python.coverage.reportPaths".

### GATE-05 A generated report rewritten by a partial test run
**Symptom** A committed coverage report silently becomes a false statement about the project.
**Cause** pytest-cov rewrites `coverage.xml` on every run including a run of one test file. That report is a true statement about that run and a false one about the project.
**Fix** A run that measured fewer files or covered less restores the committed content instead of overwriting it, and says so in the environment block.
**Rule** A generated artefact under version control needs a no-downgrade rule, or it will be replaced by a narrower measurement.
**Enforced** `tests/test_coverage_report.py`.
**Source** `6234cb0`.

### GATE-06 Scanner configuration arriving as command-line flags that override the properties file
**Symptom** Settings committed in `sonar-project.properties` have no effect.
**Cause** The pipeline passes its Sonar configuration via `SONAR_SCANNER_OPTS`. Command-line properties override `sonar-project.properties` and the plugin's defaults alike, so `-Dsonar.python.coverage.reportPaths=coverage.xml` means the report must be at the repository root, not at the plugin's default path.
**Rule** Find out how the scanner is configured before configuring it. A committed properties file may be read, ignored, or absent from the tree entirely.
**Enforced** none. This is platform behaviour, recorded in `CLAUDE.md`.
**Source** `6234cb0`.

### GATE-07 A properties file committed to the repository but absent from the pipeline's tree
**Symptom** A test asserting the file exists fails the pipeline with `FileNotFoundError`, while every assertion it made was true of the repository.
**Cause** The platform's test stage runs against the unpacked upload, and the ingest does not carry everything in an upload.
**Fix** Where a file is legitimately absent in one environment, the check skips with a reason that says so, and a companion check in a git work tree fails if the file is actually missing from the repository, so the skip cannot swallow a deletion.
**Rule** A test may only assert what is true of the code, not of the tree it happens to run in.
**Enforced** `repo_file()` in `tests/conftest.py`.
**Source** `67e9881`, `bff16a9`.

### GATE-08 The analysis published under one project key and the gate querying another
**Symptom** Every gate log reads `Quality Gate FAILED -- timeslides` while the properties file declares `sonar.projectKey=bluestaq-timeslides`.
**Cause** Unproven. A gate querying a project that has never received an analysis reports exactly 0.0 per cent coverage.
**Fix** Removed the declaration to eliminate it as a variable. If the platform passes its own key the line is ignored and nothing changes.
**Rule** When a gate names a project, check that the name matches what your configuration publishes under. Removing a variable you cannot observe costs nothing.
**Enforced** none.
**Source** `f325984`. Marked unproven in the commit message, which is the correct handling.

### GATE-09 Reverting a correct fix on archaeological reasoning
**Symptom** A working configuration was replaced, the gate stayed red, and 2 further uploads were spent.
**Cause** Git archaeology showed the coverage configuration was byte-identical when the gate read 79.2 per cent and when it later read 0.0 per cent, which was taken to prove it could not be the cause. It was: the 79.2 per cent run resolved the absolute path because it happened to match, and later runs did not.
**Fix** Reverted the revert, and wrote `CLAUDE.md` recording the decision as settled so it is not re-litigated on reasoning alone.
**Rule** "This was identical when it worked" does not prove a setting is not the cause, if the setting's correctness depends on the environment. Record settled decisions where the next session will read them.
**Source** `35541cb` then `5437e6c`. Cost 2 uploads.

### GATE-10 Untested client-side code dragging the coverage ratio below the threshold
**Symptom** Gate reports 79.2 per cent against a threshold of 80 while the Python sits at 98.7.
**Cause** Roughly 320 lines of JavaScript were in the denominator with no coverage data at all. The ceiling with perfect Python coverage was about 80.3 per cent, so adding Python tests alone could not have fixed it.
**Fix** Unit tests that load the shipped `.js` files into an in-process JavaScript engine under pytest and exercise their pure functions. 56 tests, running wherever the Python tests run.
**Rule** Work out the arithmetic ceiling of a ratio before trying to raise it. If untestable lines are in the denominator, more tests elsewhere cannot help.
**Enforced** `tests/test_assets_js.py`.
**Source** `3c80466`.

### GATE-11 JavaScript coverage that cannot be measured at all
**Symptom** The assets are tested but contribute nothing to the coverage metric.
**Cause** SonarQube reads LCOV for JavaScript and the in-process engine does not emit it.
**Fix** The assets are excluded from the coverage metric with that rationale recorded in `sonar-project.properties`, and deliberately not excluded from analysis: the gate raised 6 findings in them, all fixed.
**Rule** Exclude from the metric what you cannot measure, never from the analysis. Record the reason at the exclusion, not in a ticket.
**Enforced** `sonar.coverage.exclusions`, and a test that the two exclusion lists agree.
**Source** `3c80466`.

### GATE-12 A coverage margin that depends on a file the scanner may not read (STILL LIVE)
**Symptom** Measured on a real SonarQube with the pipeline's own flags: 100.0 per cent with `sonar-project.properties` present, 80.6 per cent without it, against a threshold of 80.
**Cause** Without the properties file the coverage exclusions do not apply and the 388 lines of client-side JavaScript count against the metric.
**Fix** None available in the repository. Nothing committed can reach a scan configured on the command line.
**Rule** Where a gate margin depends on a file you cannot guarantee is read, the durable fix is configuration on the gate's own server, not in your repository. Say so rather than leaving a 0.6-point margin undocumented.
**Enforced** none. Requires the platform team to set `sonar.coverage.exclusions` on the SonarQube project.
**Source** `5437e6c`.

### GATE-13 Rooting coverage at the project means the whole root is walked
**Symptom** The measured figure is diluted by test files and third-party packages.
**Cause** `source = .` makes coverage walk everything under the project root, including a virtualenv a runner happens to leave there.
**Fix** Explicit omits for `tests/*`, the process bootstrap, the browser assets, `.venv/*`, `*/site-packages/*` and `*/dist-packages/*`, each carrying its reason inline.
**Rule** Every exclusion carries a comment saying why. An exclusion without a reason is indistinguishable from one added to make a number look better.
**Enforced** `.coveragerc`, and a test that its exclusions agree with the scanner's.
**Source** `.coveragerc`, `f11c206`.

## B. Pipeline and packaging

### PIPE-01 A broad ignore rule swallowing a file the build needs
**Symptom** None locally. In the artefact, `pytest` runs with no options, writes no coverage report, and a codebase at 99 per cent line coverage scores zero at the gate.
**Cause** `.gitignore` carried a blanket `*.ini` rule intended to keep credential files out. It also silently swallowed `pytest.ini`, which was therefore never committed and never in the upload.
**Fix** The ignore rule is now specific (`credentials.ini`, `secrets.ini`, `*credentials*.ini`) with an explicit negation for `pytest.ini`.
**Rule** A broad ignore pattern written for one hazard will catch something you need. Narrow it, then test both directions: that the needed file is tracked and not ignored, and that the original hazard is still ignored.
**Enforced** Two tests: one asserts `pytest.ini` exists, declares the coverage report and the browser marker, is tracked and not ignored; the other asserts the credential patterns are still ignored.
**Source** `0ba9d4e`. Found by unzipping the artefact and running the tests inside it.

### PIPE-02 Test tooling in a requirements file the platform never reads
**Symptom** Test stage fails with exit 127, `pytest: command not found`.
**Cause** The platform's generated job runs exactly 2 commands and reads exactly one requirements file. The test tooling was in a developer requirements file.
**Fix** Three dependency files split by who installs them: the platform's file carries runtime plus the test runner; the container's file carries runtime only, because the image scan judges what the image contains; the developer's file adds the browser driver.
**Rule** Find out which dependency file the platform installs, and put the runner it invokes in that file. Keep test tooling out of the image for the same reason you keep it in the test file.
**Enforced** Five tests: that the platform file declares the runner, that the runtime file carries no test tooling, that the Dockerfile installs the runtime set, that the coverage source is pinned, and that the Dockerfile never sets the port.
**Source** `f11c206`.

### PIPE-03 A local simulation that reproduced the author's intent rather than the platform's behaviour
**Symptom** Everything green locally, exit 127 in the pipeline.
**Cause** The local pipeline simulation installed the developer requirements file, which is not what the platform installs.
**Rule** A simulation must reproduce what the target does, including what it omits. Write it from the platform's own job definition, not from what you believe the job does.
**Enforced** `docker/appstore-sim.sh`, which runs the suite the way the platform does: unpacked tree, no `.git`, no git binary, no properties file.
**Source** `f11c206`, `0fd9f11`.

### PIPE-04 Tests assuming a git work tree inside an unzipped artefact
**Symptom** Two tests fail inside the extracted package while passing in the repository.
**Cause** The artefact is unzipped, not cloned.
**Fix** Split by what each check actually needs. The substantive assertion needs no git and runs everywhere; the ignore-rule checks need git and skip when there is none.
**Rule** A test that breaks in the environment the platform runs it in is worse than no test. Separate the assertion from its means of checking.
**Enforced** `in_git_worktree()` in `tests/conftest.py`.
**Source** `431b643`.

### PIPE-05 A skip probe that could raise, evaluated at import
**Symptom** `FileNotFoundError: [Errno 2] No such file or directory: 'git'`, `Interrupted: 1 error during collection`, and 684 passing tests never executed.
**Cause** A `skipif` decorator is evaluated at import, so an exception in it is not one test failing: pytest reports a collection error and abandons the whole run. The job container has `.git` but no git binary.
**Fix** One exception-safe probe in `conftest.py`, with `except OSError`.
**Rule** A probe that decides whether to skip must not be able to fail. Assume every external binary is absent, including the ones that are obviously present.
**Enforced** `in_git_worktree()`, and `test_there_is_only_one_git_probe`, which parses the test tree with the ast module and fails if a second implementation appears.
**Source** `67e9881`.

### PIPE-06 A failing test stage breaking the coverage gate as collateral
**Symptom** The gate reports 0.0 per cent coverage for a reason that has nothing to do with coverage.
**Cause** A failing test stage uploads no artefacts, so the scan receives no coverage report. Two of this project's gate failures were exactly that.
**Fix** Structural checks skip with a named reason rather than failing.
**Rule** Map the dependency chain between stages before writing a test that can fail for an environmental reason. In a pipeline, a test failure can be a coverage failure two stages later.
**Enforced** `repo_file()` in `tests/conftest.py`.
**Source** `35541cb`.

### PIPE-07 A build output tracked in the repository, feeding itself
**Symptom** The upload package contains the previous upload package. Version 1.6.3 carried 203 KB of version 1.4.0, and the ingested repository held an old copy of the whole application.
**Cause** `dist/` was tracked, and the upload is built from `git ls-files`.
**Fix** Untracked and git-ignored.
**Rule** Never track a build output that the build then packages. Build the upload from `git ls-files` so an untracked file shows up as missing rather than being silently included.
**Enforced** none. Found by the working tree being dirty after a release.
**Source** `bf409a0`.

### PIPE-08 Inference about a stage whose log was never read
**Symptom** Four uploads spent on plausible theories.
**Cause** The scan job's log was not available, so every diagnosis was inference.
**Fix** An environment block printed into the test stage's log naming every file the scan needs and saying whether it is present. Then a local replica of the judging tool.
**Rule** When you cannot read the log of the stage that is failing, make the stage you can read say everything the failing one would need. Then build a local replica rather than iterating blind.
**Enforced** `pytest_terminal_summary` hook in `tests/conftest.py`; `docker/sonar-probe.sh`.
**Source** `bff16a9`, `5fd348a`.

### PIPE-09 A hostile-ingest pass that proves the suite survives a stripped tree
**Symptom** Three pipeline failures in a row were environment differences.
**Fix** A second simulation pass that runs the whole suite with every strippable file removed, so a structural check cannot start the chain in PIPE-06 again.
**Rule** Run your suite twice: once in the environment you expect, once with everything optional removed. Both must pass.
**Enforced** `docker/appstore-sim.sh`, both passes.
**Source** `35541cb`, `0fd9f11`.

### PIPE-10 A linter walking the whole tree including third-party code
**Symptom** `npx eslint .` reports 6 errors from the JavaScript bundled inside the virtualenv's browser driver, all of them inline disable comments naming rules this config does not load.
**Cause** A flat config with only a `files` pattern and no `ignores` still walks everything.
**Fix** The config ignores the virtualenv, `node_modules`, the build output and the reference directory.
**Rule** A linter that reports on files that are not yours is a linter people stop running. Configure the ignores before the rules.
**Enforced** `test_the_linter_only_looks_at_this_project_s_own_javascript`.
**Source** `0ed8d15`.

### PIPE-11 A green repository loop is not a green upload
**Symptom** Two defects found only by unzipping the artefact and running the tests inside it.
**Rule** Before every upload, extract the artefact you are about to send and run the suite inside it. The repository is not the thing being judged.
**Enforced** `docker/appstore-sim.sh`.
**Source** `0ba9d4e`.

### PIPE-12 A dependency pinned without checking it ships a wheel for the target
**Symptom** Potential: a slim runner with no compiler cannot build a source distribution.
**Fix** The JavaScript engine chosen for the asset tests publishes a manylinux wheel, which was checked before depending on it.
**Rule** Before adding a dependency to the file a constrained runner installs, confirm it ships a binary wheel for that runner's platform and interpreter version.
**Enforced** none, beyond the simulation running on a clean environment.
**Source** `3c80466`.

## C. Container image

### CTR-01 A build toolchain scanned as though it shipped
**Symptom** Container image policy check fails on 7 critical, 62 high, 60 medium and 13 low vulnerabilities. None of it came from the application.
**Cause** The build stage installed a toolchain and the final stage copied the whole filesystem into the runtime image, so every package the build needed and never used was scanned as if it shipped.
**Fix** The runtime image is assembled from what the application actually loads: the interpreter, the virtualenv, and the shared-library closure walked with `ldd` to a fixed point. Result: 15 libraries, 6,088 files, no package database, no shell, no package installer.
**Rule** The image carries only what the application needs, computed rather than assumed. A multi-stage build that copies the whole filesystem is a single-stage build with extra steps.
**Enforced** `tests/test_rootfs_script.py`, and the build's own chroot verification.
**Source** `0ed8d15`.

### CTR-02 A hardening script matching files but not directories
**Symptom** `SUID or SGID found set on file /var/mail` survives a strip that was supposed to clear it.
**Cause** `harden.sh` looked only at `-type f`. `/var/mail` is a directory.
**Fix** Strips and checks files and directories alike.
**Rule** When stripping a filesystem attribute, enumerate every node type the attribute can sit on.
**Enforced** `tests/test_harden.py`.
**Source** `0ed8d15`.

### CTR-03 The assertion meant to catch a bug having the same shape as the bug
**Symptom** The check that should have caught CTR-02 passed.
**Cause** It used the same `-type f` predicate as the strip it was verifying.
**Fix** The regression test defeats `chmod` on PATH rather than matching the script's own text, so a strip that quietly misses something fails the build.
**Rule** A verification must not share its implementation with the thing it verifies. Test the effect, not the text.
**Enforced** `tests/test_harden.py`.
**Source** `0ed8d15`.

### CTR-04 A minimal image that the scanner cannot see into
**Symptom** A clean scan reporting no operating-system packages at all.
**Cause** Dropping the Debian userland drops `/var/lib/dpkg` with it, and a scanner that cannot find a package database reports nothing. 14 distribution libraries genuinely remained.
**Fix** The 9 packages that own those libraries are declared in the `status.d` layout that Syft, Grype and Trivy read, with `os-release` alongside so an advisory has a release to match. Metadata only: no package manager is in the image.
**Rule** Minimal is not the same as invisible. A clean result that means "could not see" rather than "nothing to see" is the same fail-open defect as a test that passes when it could not verify, and it is not something to hand a security manager.
**Enforced** `test_the_image_declares_its_packages_to_the_scanner`. The build fails if the C library is undeclared.
**Source** `6cd7984`.

### CTR-05 An interpreter symlink chain broken by a partial copy
**Symptom** Chroot verification dies with exit 127. Every pod start would have failed the same way.
**Cause** The virtualenv's entry point points at `/usr/local/bin/python`, a symlink to `python3`, a symlink to `python3.13`. The script copied only `python3.13`, so the entry point dangled.
**Fix** All three names copied as links, and the verification asserts the entry point resolves.
**Rule** When copying an interpreter into a minimal rootfs, resolve and copy the whole symlink chain, then assert the entry point the image's command names actually resolves.
**Enforced** The build's chroot self-verification.
**Source** `6cd7984`.

### CTR-06 A failure tolerance applied to a whole command chain
**Symptom** The build goes green and the virtualenv is empty.
**Cause** `|| true` sat on the end of the build stage's whole `&&` chain rather than on the single command it was meant to tolerate. With no route to the package index the dependency install failed and the stage reported success.
**Fix** Removed. The command it was protecting already exits 0 for the case it was protecting against.
**Rule** `|| true` goes on one command, never on a chain. Before adding one, check whether the command already succeeds in the case you are tolerating.
**Enforced** none.
**Source** `6cd7984`.

### CTR-07 A package-ownership query parsed without handling its preamble
**Symptom** A package named `diversionbylibc6from` is declared to the scanner.
**Cause** `dpkg -S` prints `diversion by libc6 from: ...` ahead of the real ownership line, and cutting at the first colon turned that into a package name.
**Fix** Filter diversion lines before parsing.
**Rule** Read the full output of a command you are parsing, including its informational lines, before writing the parser.
**Enforced** `tests/test_rootfs_script.py`.
**Source** `6cd7984`.

### CTR-08 A base image reference that bypassed the builder's registry mirror
**Symptom** Build dies with exit 125 and `no such host`.
**Cause** The job's first step writes a registry mirror rule redirecting the public registry to an internal one. Naming the internal registry directly bypasses that rule, and the build container has no DNS for it.
**Fix** Both stages use the fully qualified public name so it matches the mirror prefix, rather than a bare short name that depends on the builder's unqualified-search list.
**Rule** Read the job log's own setup steps before choosing a base image reference. An inference about a builder's egress is an inference.
**Enforced** none.
**Source** `8de7d5b`. The same mistake had been made in an earlier commit, on an inference marked as inference that was wrong.

### CTR-09 A layered image where a later strip does not remove an earlier bit
**Symptom** A policy scan finds a setuid bit that the Dockerfile removes.
**Cause** The image policy reads layer blobs and history, so a `chmod -s` in a later layer does not remove a bit an earlier layer set.
**Fix** A prep stage builds and strips the whole rootfs, then a single copy onto an empty base collapses it into one layer with no history to scan. The prep stage fails the build itself if any setuid or setgid file survives.
**Rule** For a policy scanner that reads history, removing something in a later layer does not remove it. Flatten, and fail the build at the point of the problem rather than at the scan.
**Enforced** The prep stage's own assertion.
**Source** `95cb524`.

### CTR-10 A user declared by name rather than by number
**Cause** The policy reads the uid, and a name it cannot resolve reads as root.
**Rule** Declare the runtime user numerically.
**Enforced** `tests/test_rootfs_script.py`.
**Source** `95cb524`.

### CTR-11 The listening port set in the image
**Cause** The platform sets the container port and probes it, and the application reads the port from the environment with a default.
**Rule** Read the port, never set it as an image environment variable. Setting it in the image is how you end up serving on a port nothing probes.
**Enforced** A test that the Dockerfile never sets the port.
**Source** `95cb524`.

### CTR-12 A build directive that pulls a frontend image
**Cause** A `# syntax=` directive makes the build pull a frontend image, which fails in any network-restricted builder.
**Rule** Omit it deliberately and record why, or it will be added back by someone who thinks its absence is an oversight.
**Source** `95cb524`.

### CTR-13 A comment inside a shell continuation, under a non-default builder
**Cause** A `#` comment inside a multi-line command continuation is parser-dependent, and this builder was not the default one.
**Fix** The hardening step moved from an inline command into a script file, which also made it testable.
**Rule** Keep build logic in a script file rather than inline. A script can be tested; an inline chain cannot.
**Enforced** `tests/test_harden.py`.
**Source** `8de7d5b`.

### CTR-14 Vulnerabilities with an upstream fix that the base image predates
**Cause** A finding with an upstream fix is cleared by upgrading. A finding without one can only be cleared by the package not being in the image.
**Fix** The build applies the distribution's security updates before anything is copied, and the base tag is pinned to a patch version at or above the fixed-in versions.
**Rule** Separate the two classes before deciding on a remedy. The first shapes your upgrade step, the second shapes your whole image strategy.
**Enforced** `test_the_build_applies_the_distribution_security_updates`, `test_the_base_image_is_pinned_to_a_patch_version`.
**Source** `0ed8d15`.

### CTR-15 A build recorded as unverified when the environment refused to build it
**Symptom** The egress policy denies the registry's blob CDN, so the base image cannot be pulled.
**Fix** The record says so plainly and names what that leaves unverified, with what to check on the first build on a host with registry access.
**Rule** A policy denial gets reported, not routed around. State what remains unverified rather than letting silence imply it was checked.
**Source** `c70aeb5`, `6cd7984`.

### CTR-16 A directory created in the image that nothing uses
**Cause** Output is written under the storage mount path on the volume, so creating a directory under the application root did nothing.
**Rule** Remove build steps that do nothing. Each one is a thing a reader must work out the purpose of.
**Source** `c70aeb5`.

## D. JavaScript

### JS-01 A redundant guard where an optional chain belongs
**Symptom** Gate finding "Prefer using an optional chain expression", raised twice.
**Cause** `x && x.y` where the guard and the access are the same thing.
**Rule** `x && x.y` is `x?.y`. The rule applies to the redundant form only.
**Enforced** A local ESLint rule, plus `find_redundant_guards` as a standard-library backstop.
**Source** `ce605f0`.

### JS-02 The optional-chain rule changing behaviour when applied too widely
**Symptom** A correct guard rewritten as a chain introduces a null dereference.
**Cause** `sel && sel.value !== st.ref` rewritten as `sel?.value !== st.ref` yields `undefined` for a null `sel`, the comparison is then true, and the body runs on the null.
**Rule** Exempt the comparison form. A rule that is right most of the time and wrong occasionally is worse than no rule, because its output stops being read.
**Enforced** `test_the_optional_chain_detector_leaves_a_genuine_guard_alone`.
**Source** `ce605f0`.

### JS-03 A selector-based rule that cannot compare two fields of a node
**Symptom** The first version of the optional-chain rule flagged `g && window.Plotly`, where the identifiers are unrelated.
**Cause** esquery selectors match structure, not equality between two parts of the matched structure.
**Fix** A local ESLint rule with a `sameSource()` function comparing the two sides, handling identifiers, `this` and non-computed member expressions recursively.
**Rule** When a rule's correctness depends on two parts of an expression being the same, a selector cannot express it. Write the rule.
**Source** `ce605f0`.

### JS-04 An exception caught and discarded
**Symptom** Gate finding "Handle this exception or don't catch it at all".
**Rule** No empty catch block, and no catch binding that is never read.
**Enforced** `no-empty`, `no-unused-vars`, `find_empty_catches`, `find_unread_catch_bindings`.
**Source** `ce605f0`.

### JS-05 One catch covering two failures that need different answers
**Symptom** Fixing a swallowed exception by logging it would have been wrong.
**Cause** The catch covered both a browser refusing storage outright, where the right answer is to record it once and stop trying, and a stored value this code did not write, where the store works and only its contents are wrong. Treating the second as a refusal would have stopped every later write and left the bad value in place for good.
**Fix** Two catches, two behaviours, and the distinction is a test.
**Rule** Before handling an exception, enumerate the distinct causes that reach that catch. If they need different answers, they need different catches.
**Source** `7042f7e`.

### JS-06 A stored null parsing successfully into a legal empty value
**Symptom** A dead control and nothing said.
**Cause** A stored JSON `null` parses without throwing, and `new Set(null)` is a legal empty set, so a null meant "nothing selected".
**Fix** The parsed value is required to be an array, which is the only thing this code ever writes.
**Rule** Validate the shape of anything read back from storage, not just that it parsed. A permissive constructor turns bad data into plausible data.
**Source** `7042f7e`.

### JS-07 A test harness with no console, so a warning would throw
**Symptom** Code that works in a browser throws in the stub.
**Cause** The harness stubbed enough of the document to run the file but provided no console.
**Fix** A console that records what was said, which is what lets a test assert the operator was told why.
**Rule** A stub that omits a global the code calls does not fail fast, it fails in the one path you are least likely to test. Record stubbed calls rather than discarding them: a warning the code emits is part of what it does.
**Source** `7042f7e`.

### JS-08 Two poll chains running at once after a restart
**Symptom** The finished report could be opened twice.
**Cause** Starting a render while one was still polling left both chains running. Each reset the shared timer and one was orphaned.
**Fix** Every render carries a token, and a reply whose token has moved on is dropped.
**Rule** Any polling loop that can be restarted needs a generation token. This was found by running a linter for the first time, not by a user.
**Source** `ce605f0`.

### JS-09 A state-changing function that also redraws
**Symptom** The rule about what is selected is untestable without a document.
**Fix** The functions change state and leave drawing to the caller.
**Rule** Separate state change from rendering, or the pure logic cannot be unit tested and ends up covered only by a browser suite that skips.
**Source** `20a07aa`.

### JS-10 Cognitive complexity over the gate's cap
**Symptom** One function measured 20 against a cap of 15.
**Fix** Split into 3 named functions, with the chained ternary extracted into a lookup table.
**Rule** Set the same complexity cap locally that the gate applies, and measure before uploading.
**Enforced** ruff `C90` for Python at `max-complexity = 15`; for JavaScript, splitting and review.
**Source** `8de7d5b`.

### JS-11 Assorted gate findings, each now a rule
**Rule** No empty object literal spread or merged. No bare `parseInt`; use the `Number` namespace. No `Object.assign` where a spread belongs. No unary-plus coercion; `Number(x)` says what it does. No `var`. No nested ternaries. No nested template literals.
**Enforced** `prefer-object-spread`, `no-restricted-globals`, `no-implicit-coercion`, `no-var`, and the standard-library detectors `find_object_assign`, `find_bare_number_globals`, `find_unary_plus_coercions`, `find_var_declarations`.
**Source** `ce605f0`, `3c80466`, `8de7d5b`.

### JS-12 Client-side code held inside a server-side string literal
**Symptom** The gate, the linter and the coverage tool all see nothing.
**Cause** The JavaScript and CSS lived inside Python f-strings.
**Fix** Extracted to real `.js` and `.css` files, inlined at render time. The two values Python used to interpolate are read from a JSON block instead.
**Rule** Code held inside another language's string literal is invisible to every tool. Extract it, even if that means a build step to inline it.
**Enforced** The file layout itself, and `tests/test_assets_js.py` reading the published files.
**Source** `4ce6fd1`.

### JS-13 An escaper paired with a project rather than with a sink
**Symptom** An apostrophe typed by an operator would render as `&#x27;` on a chart.
**Cause** The project's one escaper emits numeric character references, which is correct for an `innerHTML` sink. The charting library decodes only 4 named entities in its text fields and leaves the rest literal.
**Fix** A second named escaper for that sink, escaping the 4 it decodes and leaving the apostrophe literal, which is safe in SVG text content.
**Rule** An escaper is paired with a sink, not with a project. Enumerate your sinks and confirm each one's decoding rules before reusing an escaper across them.
**Enforced** none yet. Design finding, not yet implemented.
**Source** This session's GUI design review, generalising `192cc2e`.

## E. CSS

### CSS-01 A duplicate top-level selector
**Symptom** Gate finding. One rule pair worked only because it happened not to overlap.
**Cause** The same selector was written twice, once for a resting shadow and once for an animation. A duplicated selector means the later rule silently governs any property the two share.
**Fix** Merged.
**Rule** One top-level rule per selector. A rule inside a media query is a different context and is how an override is meant to be written, so the check is deliberately top-level only.
**Enforced** `find_duplicate_selectors`, `test_no_duplicate_top_level_selector`, with `test_the_duplicate_selector_detector_allows_a_media_query_override` as the lookalike.
**Source** `7042f7e`.

### CSS-02 An override added for everyone and taken away again
**Symptom** A browser that does not understand the preference query gets an unstoppable animation.
**Fix** The animation is added inside a `prefers-reduced-motion: no-preference` query rather than added for everyone and removed in a `reduce` query. Same result, one fewer rule, and it fails safe.
**Rule** Add an effect under the positive preference query rather than removing it under the negative one, so an unsupported query degrades to no effect.
**Source** `7042f7e`.

### CSS-03 `!important` with no stated reason
**Rule** `!important` is not banned, it is required to carry its reason in a comment. Beating an inline style that JavaScript sets, and honouring a reduced-motion preference whatever else the sheet says, are both right answers; the author has to be able to say which.
**Enforced** `find_unexplained_important`, with `test_the_important_detector_wants_a_reason_not_abstinence`.
**Source** `ce605f0`.

### CSS-04 A semi-transparent background defeating a contrast check
**Symptom** A contrast ratio of 2.0 to 1.
**Cause** The error style declared a semi-transparent background, which a contrast checker cannot composite, so it fell back to white.
**Fix** An opaque pair at 11.5 to 1.
**Rule** Declare an opaque colour pair wherever contrast must be verifiable. A checker cannot composite what the browser composites.
**Source** `8de7d5b`.

### CSS-05 A class used in a page whose stylesheet that page does not load
**Symptom** A band would have rendered unstyled inside the frame, while every class assertion still passed.
**Cause** A new component was given classes that live in the shell's stylesheet. The report document does not load that stylesheet.
**Rule** Every class a page uses exists in a stylesheet that page loads. Asserting on a class name proves nothing when the stylesheet is inlined.
**Enforced** `test_every_class_the_report_uses_exists_in_the_stylesheet_it_loads`, `test_the_shell_only_uses_classes_one_of_its_stylesheets_defines`.
**Source** `192cc2e`.

### CSS-06 Three layout defects invisible to class-list assertions
**Symptom** Both tabs painted on top of each other; a placeholder pushed the frame a full viewport below the fold; the report frame collapsed to zero height. Every class assertion passed throughout.
**Cause** An unscoped rule outranked another at equal specificity from a later stylesheet; a display rule outranked the hidden attribute's user-agent style; hiding three children left a row off the `1fr` track of a 5-row grid template.
**Fix** Each now has a test that asserts the rendered geometry, not the class list.
**Rule** Assert computed geometry, not class names. A class list is a statement about intent; geometry is a statement about what the user sees.
**Enforced** `tests/test_smoke.py`.
**Source** `c7ec481`.

## F. Python

### PY-01 Binding to all network interfaces
**Symptom** Gate vulnerability "Avoid binding the application to all network interfaces".
**Cause** The application must bind every interface, because the platform probes the pod's own address.
**Fix** The address is configuration, in one place, defaulting to the empty string, which is the address-family-agnostic form of "every interface". The record states that if the gate still raises it, it needs marking as a reviewed hotspot, because the behaviour cannot change.
**Rule** Where a finding describes behaviour you need, make the behaviour configurable and record why it is what it is. Do not silence the rule.
**Enforced** `test_the_listen_address_is_configuration_rather_than_a_literal`.
**Source** `8de7d5b`.

### PY-02 A filesystem path built from user-controlled data
**Symptom** Gate vulnerability.
**Cause** The storage mount path is the one environment variable the application builds filesystem paths from, and it was trusted all the way down to the writes.
**Fix** Validated at the boundary: absolute, normalised, no traversal segment, or the process fails closed at boot. A trailing slash, a `.` segment and a repeated separator are tidied; `..` is reported rather than normalised away, because a path containing one is a misconfiguration to surface, not something to quietly reinterpret.
**Rule** Validate at the boundary and fail closed. Normalise what is cosmetic, report what is a misconfiguration.
**Enforced** `tests/test_config.py`.
**Source** `d3a525f`.

### PY-03 A regular expression vulnerable to exponential backtracking
**Symptom** Gate finding. Measured at roughly 4 times the work per added character: 0.7 seconds at 24 characters, 2.8 at 26, and on that curve about a fortnight at 60.
**Cause** `^/(?:[A-Za-z0-9._][A-Za-z0-9._-]*/?)*$`. A run of allowed characters can be divided between the inner and outer repetitions in exponentially many ways, so a value that fails only at its final character backtracks through all of them.
**Fix** A set membership test per path segment. It cannot backtrack and reads more plainly. The refusal also now says which condition failed rather than listing all 4.
**Rule** No repeat inside a repeat. Where a character test will do, do not write a regular expression. Audit the rest of the codebase in the same change.
**Enforced** `find_nested_quantifiers`, `test_no_regex_with_a_nested_quantifier`, plus the 60-character input as a test with a 1-second ceiling on work that takes microseconds.
**Source** `ee1eb0f`.

### PY-04 An assertion comparing an expression with itself
**Symptom** Gate bug. The assertion could not fail.
**Fix** Builds two specifications and asserts that equal inputs give an equal key.
**Rule** An assertion whose two sides are the same expression is not a test. Look for this specifically; it passes forever.
**Source** `8de7d5b`.

### PY-05 A raises block containing more than the call under test
**Symptom** 16 blocks where a failure in the setup helper would have read as a pass.
**Fix** Setup hoisted out; every block contains exactly one call.
**Rule** One call per expected-exception block.
**Enforced** ruff `PT012` family.
**Source** `8de7d5b`.

### PY-06 An automated code transform that corrupted every file, twice
**Symptom** Two failed attempts before a working one.
**Cause** The first computed all edit offsets from one parse and produced nested calls. The second treated offsets as character indices when Python reports them as UTF-8 byte offsets, which broke every file containing a non-ASCII character.
**Fix** The third transforms the original tree the same way and refuses to write a file whose resulting abstract syntax tree does not match.
**Rule** An automated transform must verify its own output structurally before writing. Offsets from a parse are byte offsets, and they are invalidated by the first edit.
**Source** `8de7d5b`.

### PY-07 An exception that only passes
**Rule** No `except` block that only passes. `contextlib.suppress` says the same thing and says it on purpose.
**Enforced** `find_except_that_only_passes`, `test_the_silent_except_detector`.
**Source** `ce605f0`.

### PY-08 A type annotation that was never exercised
**Symptom** Constructing the settings object with a string gives a working object that raises on first use.
**Cause** The field was annotated as a path and the loader always passed one, so nothing exercised the string case.
**Fix** The constructor coerces.
**Rule** An annotation is not a coercion. Where a type is only ever supplied correctly by your own code, a caller outside it will eventually supply the other thing.
**Source** `8c7fb63`. Found by a new suite passing the path through a subprocess argument.

### PY-09 Library scalars that the serialiser rejects
**Symptom** Would have failed only at render time.
**Cause** The numerical library's float type is not what the JSON serialiser accepts.
**Fix** Converted to plain floats at the boundary of the computation.
**Rule** Convert library scalar types to builtins at the boundary, not at the serialiser.
**Source** `4ce6fd1`.

### PY-10 Process exit used as error handling in library code
**Symptom** A bad reference object took the process down. Fine for a command-line tool, fatal for a worker.
**Fix** Typed exceptions, mapped to status codes in one place at the HTTP layer.
**Rule** When lifting a script into a service, every exit becomes an exception. One handler decides what a failure looks like.
**Source** `4ce6fd1`, `c7ec481`.

### PY-11 An environment mapping swapped into the process environment
**Symptom** A race under a threaded server.
**Fix** The mapping is threaded through the parsers rather than swapped into the process environment.
**Rule** Never mutate process-global state to pass a value to a function in a threaded program. Pass it.
**Source** `3d7b515`.

### PY-12 Dead code excused rather than removed
**Fix** A superseded builder, an unreferenced constant, and a parameter that stopped meaning anything when a data source was dropped and was never read, all removed rather than excluded from analysis.
**Rule** Remove dead code rather than excluding it. An exclusion is permanent and a deletion is reversible.
**Source** `95cb524`.

### PY-13 A per-point loop that cannot serve a request
**Symptom** A frame conversion measured at 33 ms per vector. A representative run carries order 1e5 vectors, which is roughly 58 minutes.
**Fix** Batched: 0.2 ms per vector, about 21 seconds. Frame conversion and propagation are bit-identical batched, asserted exactly. The projection differs by up to 1.1e-13 seconds because the batched summation orders terms differently, which is stated in the module docstring and held to a tolerance rather than claimed as exact.
**Rule** Measure the per-item cost before lifting a script into a service. Where a batched form is not bit-identical, state the difference and bound it; do not claim exactness you have not got.
**Source** `4ce6fd1`.

## G. Storage and persistence

### STOR-01 Assuming POSIX semantics on an object-store mount
**Symptom** Deployed application saves groups and then dies on every render with `[Errno 38] Function not implemented` on a rename.
**Cause** The storage add-on is S3-backed, and S3-backed FUSE mounts commonly implement neither `fsync` nor `rename`. The POSIX habit of writing a temp file and renaming it over the target does not work there.
**Fix** A writer that probes a ladder of mechanisms, best first: atomic replace, then write in place for mounts with no rename, then remove and rewrite for mounts that also refuse to overwrite.
**Rule** Probe the filesystem, never assume it. Degrade through a ladder rather than failing at the first rung.
**Enforced** `tests/test_nonposix_e2e.py` across 4 simulated mounts.
**Source** `13bd382`. Cost 3 releases.

### STOR-02 Treating a durability guarantee as part of the atomicity guarantee
**Cause** `fsync` was treated as fatal. It is a durability guarantee; the rename is what makes a replacement atomic.
**Fix** Flushed best-effort and the write proceeds.
**Rule** Separate durability from atomicity. A mount that cannot do the first can still do the second.
**Source** `13bd382`.

### STOR-03 A boot probe that did not run the real write path
**Symptom** The probe reports the volume writable while every save on it fails.
**Cause** It wrote an empty file with a convenience method, taking none of the steps a real write takes.
**Fix** The probe runs the real mechanism and settles on the best one the mount supports.
**Rule** A probe that does not exercise the real path is worse than no probe: it manufactures confidence.
**Enforced** `tests/test_nonposix_e2e.py`.
**Source** `13bd382`.

### STOR-04 Two write paths, one fixed
**Symptom** Saving works and every render dies.
**Cause** The group store had been fixed for the mount. The job runner had its own hand-rolled temp-file-and-rename and had not.
**Fix** One storage module is the only place in the package that writes a file.
**Rule** Every filesystem write goes through one module. A structural test fails if a second write path appears, and the fix for that is to route it through the writer, not to add the new call to the test.
**Enforced** `test_only_the_storage_module_writes_to_the_filesystem`.
**Source** `8c7fb63`.

### STOR-05 A simulation that patched a module rather than the filesystem
**Symptom** The unit simulation reports success on the exact code that was broken.
**Cause** It replaced one module's `os` attribute, so it only affected code reaching the filesystem through that module. The runner used a different standard-library path whose rename goes straight to the real `os`.
**Fix** The real server runs in a subprocess with the relevant syscalls disabled process-wide before the application is imported, and is driven over HTTP with nothing stubbed.
**Rule** A test that patches a module tests that module. It does not test the mount. Disable the capability at the process boundary, not at the import site.
**Enforced** `tests/test_nonposix_e2e.py`.
**Source** `8c7fb63`.

### STOR-06 A fallback that destroyed the data it was compensating for
**Symptom** On a volume where the write then also failed, the existing document was gone.
**Cause** The remove-and-rewrite strategy deleted the target before writing, with no way back.
**Fix** Nothing is removed until the mount has proved it will take a write. The strategy writes a sibling file first and leaves the target alone if that fails.
**Rule** Prove the destination will accept a write before destroying what is there.
**Enforced** A test driven directly against the strategy rather than through the ladder, because the earlier rungs mask it.
**Source** `13bd382`, `d3a525f`.

### STOR-07 A restore that used a different call than the write it compensated for
**Symptom** The test covering it passed, and it would never have worked.
**Cause** The restore path used a second write primitive. Wherever the write genuinely could not land, neither could the restore.
**Fix** The module is back to one write primitive.
**Rule** A compensating action must use the same mechanism as the action it compensates for, or it only ever works in the test.
**Source** `d3a525f`. The quality gate flagged this line as a path built from user-controlled data, which was a different and lesser point; the real defect was underneath it.

### STOR-08 Directory creation failing on a path that is perfectly writable
**Cause** On an object store the separator only looks like a directory, and the FUSE driver refuses directory creation.
**Fix** Directory creation never fails a write. The write is the arbiter and reports the real errno; what is checked is that the parent is there afterwards.
**Rule** Do not let a preparatory call decide whether the real call would have worked.
**Source** `13bd382`, `8c7fb63`.

### STOR-09 A degradation notice written but never wired into the page
**Symptom** It rendered nowhere.
**Fix** Caught by the browser test, and now also a unit test, because the inlined stylesheet means asserting on a class name proves nothing.
**Rule** Assert that a warning reaches the user, not that the code emitted it.
**Source** `13bd382`.

### STOR-10 An error message naming a cause the exception type excludes
**Symptom** The message sent the operator after the wrong cause: it advised a permissions fix for a bare `OSError`, which cannot be the permission error, because Python raises a dedicated subclass for that.
**Fix** The advice keys on the errno: one value says no volume is mounted, another says fix the group ownership, another says the volume is full.
**Rule** Key operational advice on the errno, and check that the exception type you are handling can actually carry the cause you are naming.
**Source** `2d4912a`.

### STOR-11 Degrading silently
**Fix** Degradation is reported 3 ways: an audit event at boot, a block on the health endpoint naming the mode and strategy, and a standing warning on the page. A working but non-crash-safe mount gets one quiet line instead of an alarm.
**Rule** Degrade rather than break, and never degrade silently. Match the volume of the warning to the severity of the degradation.
**Source** `13bd382`, `2d4912a`.

### STOR-12 Readiness blocked on something that is not readiness
**Fix** Boot is never blocked and the health endpoint stays 200 in memory mode. Readiness is about whether this pod can serve, and it can.
**Rule** A readiness probe answers "can this instance serve", not "is everything ideal".
**Source** `2d4912a`.

## H. Security

### SEC-01 A credentials file in the repository
**Fix** Deleted. Every historical revision was checked and found to have blank values, so nothing needed rotating. Credentials come from the environment only, validated once at boot, failing closed.
**Rule** Credentials come from the environment. Check the history before concluding nothing leaked, and say which you did.
**Enforced** `tests/test_config.py`.
**Source** `0e2d673`, `3d7b515`.

### SEC-02 An interactive credential prompt inside a container
**Symptom** Would block on a standard input that never delivers, the readiness probe fails, and the platform restarts the pod in a loop that looks like a crash.
**Fix** The prompt path is deleted, not guarded.
**Rule** A prompt in a container prompts nobody. Delete the interactive path rather than conditioning it.
**Source** `3d7b515`.

### SEC-03 A secret reachable through an object's representation
**Fix** The password is excluded from the settings representation, from every error message and from every response body.
**Rule** Exclude secrets from the representation explicitly, and assert by test that the fixture value appears nowhere in any output.
**Enforced** `tests/test_config.py`, `tests/test_udl.py`.
**Source** `3d7b515`.

### SEC-04 Untrusted values reaching HTML without escaping
**Symptom** Names arriving over HTTP and from a third-party tenant reached 8 sinks: 3 server-rendered interpolations, a charting library's tooltip template, and 4 client-side `innerHTML` templates.
**Fix** Escaped on both sides, with an end-to-end browser test that creates a record named after an injection attempt, renders it, and asserts no element was created and no script ran.
**Rule** Enumerate your sinks. Escaping is per sink, and the count is the thing to write down.
**Enforced** A static scan in `tests/test_report.py` forbidding a raw interpolation of the name fields, plus the browser test.
**Source** `4ce6fd1`.

### SEC-05 Breaking out of an embedded JSON block
**Cause** A record named with a closing script tag would otherwise close the data block.
**Fix** The three characters that can start a tag are escaped to unicode escapes in the embedded JSON.
**Rule** Embedded JSON inside a script element is a distinct sink with its own escaping, separate from HTML escaping.
**Source** `4ce6fd1`.

### SEC-06 Log forgery through an untrusted value
**Cause** A name containing a newline could forge an audit entry.
**Fix** Untrusted values are stripped of control characters and length-bounded before they reach a log field.
**Rule** Sanitise anything untrusted that reaches a structured log, for the same reason you sanitise anything that reaches HTML.
**Source** `3d7b515`.

### SEC-07 Mass assignment through unknown payload keys
**Fix** Unknown keys in a payload are dropped, so a caller cannot choose its own identifier or set the archived flag.
**Rule** Reject or drop unknown keys at the boundary. Validate rather than coerce: bound names, range-check numbers, and require referential constraints such as a reference being a member of its own group.
**Source** `f0ac364`.

### SEC-08 Lost updates in a shared deployment
**Cause** Several people share one deployment, so a blind write would let the last save discard someone else's edit.
**Fix** A monotonic revision. A write may present the revision it read, and a stale one is refused with a conflict. An in-process lock serialises concurrent writes, tested with 8 threads.
**Rule** Any shared mutable document needs a revision check, and the test needs real concurrency.
**Source** `f0ac364`.

### SEC-09 Deletes that erase
**Fix** Deletes archive rather than erase. Archived names free up for reuse, and a restore that would collide with a reused name is refused.
**Rule** Archive rather than erase anything a user spent effort creating, and think through the name-reuse collision before shipping the restore.
**Source** `f0ac364`.

### SEC-10 A corrupt store overwritten rather than reported
**Fix** A corrupt document is reported and left alone. Starting empty would read as "all your records vanished" and then overwrite the evidence.
**Rule** Never overwrite data you failed to parse. Report it.
**Source** `f0ac364`.

### SEC-11 Unbounded fan-out against a shared upstream quota
**Cause** One run fans out to objects multiplied by providers multiplied by modes, issued as fast as the network allows. Fine for a script run by hand, not fine for a service several people can trigger at once.
**Fix** A blocking token bucket shared across workers, plus caps on the window, the number of groups, the number of objects and the result count. Blocking rather than raising, because the right answer for a half-finished job is to go slower, not to fail.
**Rule** A script becoming a service needs a rate limiter and a cap on every dimension of its fan-out. Choose blocking or raising deliberately.
**Source** `3d7b515`, `c7ec481`.

### SEC-12 Authentication deliberately out of scope, and recorded as such
**Fix** The record names the residual risk plainly: anything with network reach to the pod can create, edit and archive records and spend the shared upstream quota, with no per-user attribution in the audit log. It names the seam where a token check would go.
**Rule** Where you accept a risk, write down what the risk is, who accepted it, and where the fix would land. "Out of scope" without those 3 is not a decision.
**Source** `95cb524`.

## I. Testing and verification

### TEST-01 A suite that skips silently where its dependency is absent
**Symptom** The browser suite was the only thing testing the client code, and it skipped in the platform's test stage. The assets were behaviourally tested on a developer machine and untested everywhere else.
**Fix** The fixture skips with a loud reason naming what was not verified, and pure-function unit tests were added that run everywhere.
**Rule** A skipped suite is not a passed one. Say so in the record, make the skip loud, and make sure something else covers the same ground where the dependency is absent.
**Enforced** `tests/test_smoke.py` skip reason, `tests/test_assets_js.py`.
**Source** `95cb524`, `3c80466`.

### TEST-02 Unit tests against a copy of the logic
**Fix** The tests load the shipped files as published, with no second copy of the logic to drift out of step.
**Rule** Test the published artefact, not a transcription of it.
**Enforced** `tests/test_assets_js.py` reads `timeslides/report/assets/*.js` directly.
**Source** `3c80466`.

### TEST-03 Test teeth assumed rather than checked
**Fix** Checked by mutation. Stopping the escaper escaping one character fails 2 tests; inverting a sign test fails 2; swapping a precedence fails 1.
**Rule** Prove a new test suite can fail. Mutate the code it covers and confirm the specific count of failures.
**Source** `3c80466`.

### TEST-04 A polling helper with no wait
**Symptom** A new test failed under coverage and would have failed the platform's test stage.
**Cause** The helper busy-looped a fixed 200 times with no wait, taking well under a second in total. Long enough for a stub, not for a real implementation.
**Fix** Waits on a deadline.
**Rule** A test that polls waits on a deadline, never on an iteration count.
**Source** `3c80466`.

### TEST-05 A test that depended on the clock
**Symptom** A single-flight join test became a race.
**Cause** The deduplication window is truncated to the second, so awaiting the first operation before starting the second made joining timing-dependent.
**Fix** The tests pin the window and separate the two behaviours deliberately: two requests in one moment join, a window that has moved on does not.
**Rule** Pin the time source. Where a behaviour depends on a window, test both inside and outside it explicitly.
**Source** `0ed8d15`.

### TEST-06 No settle condition, so a test asserted before the answer arrived
**Symptom** Passed in isolation and failed in file order, which is the signature of a race rather than a broken feature.
**Cause** The in-flight spinner and the finished result both rendered with the same class, so nothing in the document distinguished "asking" from "answered".
**Fix** The message region is a live region carrying a busy attribute, set true while the request is in flight and false when it settles. That is both the correct accessibility signal and something real to wait on.
**Rule** Give asynchronous work an explicit settle signal in the document. A class the loading state also uses is not one.
**Source** `0ba9d4e`.

### TEST-07 Verification sharing its implementation with the thing verified
See CTR-03. The general form: a check written from the same mental model as the code will share its blind spot.
**Rule** Write the check from the requirement, not from the implementation.

### TEST-08 Every detector shown its offender and its lookalike
**Rule** A detector is a named function, called by a test that shows it the offender it was written for and a second test that shows it the lookalike it must leave alone. A further test asserts no detector exists without that pair.
**Enforced** `test_every_detector_in_this_file_is_exercised_by_a_test`.
**Source** `ce605f0`.

### TEST-09 A check that matched its own documentation
**Symptom** Four occurrences across one project.
**Fix** Comments and string bodies are blanked before a detector runs, preserving line lengths so a computed line number still points at the right line.
**Rule** Strip comments and strings before pattern-matching source. Then test the stripper.
**Enforced** `test_no_check_in_this_repository_searches_a_file_including_its_comments`.
**Source** `f11c206`, `ce605f0`, `192cc2e`.

### TEST-10 The comment stripper itself blind to regular-expression literals (STILL LIVE)
**Symptom** Every JavaScript standards check in the project currently passes because it is looking at almost nothing.
**Cause** The stripper has no handling for a regular-expression literal. A character class containing a quote character opens a phantom string that never closes correctly, and the tokeniser never recovers.
**Measured** Detectors see a planted offender through line 28 of the file and are blind from line 32. 125 of 233 code lines are blanked; 1,004 of 6,473 non-space characters survive after the offending line. A second asset loses 375 of 601 lines. A deliberately planted empty catch block appended to either file is not reported, while the same detector finds it standalone.
**Fix** Not yet applied. Recognise a regular-expression literal by the previous significant character, scan to the closing delimiter honouring backslash escapes and character classes, consume the flags, and blank the span preserving length.
**Rule** A tokeniser written for comments and strings will mis-handle any other literal form the language has. Prove it by appending a known offender to every real source file and asserting each detector fires. This is the single highest-value test in a repository full of static checks, because without it every other one is decorative.
**Enforced** none.
**Source** Found in this session, in `tests/test_code_standards.py`.

### TEST-11 Three defects found by driving the real interface
**Symptom** All 3 were invisible to unit tests: two stylesheets fighting, a placeholder outranking a user-agent style, and a grid row collapsing.
**Rule** Drive the real interface in a real browser for anything layout-dependent. A class assertion is not a geometry assertion.
**Source** `c7ec481`.

### TEST-12 A defect found by hovering rather than by inspecting
**Symptom** A named HTML entity appeared on screen verbatim in a tooltip.
**Cause** The charting library draws its tooltip as SVG text and does not decode named entities there.
**Rule** Where a component renders text through a path your document inspection does not exercise, the test has to exercise that path. The browser suite hovers.
**Enforced** `test_no_named_html_entity_in_a_plotly_hovertemplate`.
**Source** `192cc2e`.

### TEST-13 A test asserting the old, wrong behaviour with a wrong explanation
**Symptom** A test asserted the defective anchoring and attributed the resulting drift to a text round trip.
**Fix** Corrected rather than deleted, with a companion test that a drifting object is still far from zero, since a reference that sat near zero against everything would satisfy the first test and mean the plot showed nothing.
**Rule** When a test encoded the defect, correct it and add the companion that stops the corrected test being satisfiable by a different failure.
**Source** `3195167`.

### TEST-14 A fixture whose own behaviour differed from production
**Symptom** `create_app`'s real wiring was never exercised, because the fixtures inject the collaborators.
**Fix** The default factory, both real render paths, and the seed-failure path are now tested directly.
**Rule** Dependency injection makes the wiring itself untested. Test the default construction explicitly.
**Source** `3c80466`.

## J. Correctness and data integrity

### DATA-01 A reference datum taken from the oldest available observation
**Symptom** Apparent relative motion that is not real, growing with distance from the start of the window.
**Cause** The reference orbit was taken from the earliest element set after an ascending sort. That propagates the oldest orbit across the whole window, and propagation error grows with time since epoch, so error belonging to the reference appears in every series at once and reads as real motion.
**Fix** The latest element set, which is the best current knowledge.
**Rule** A datum everything else is measured against must be the best current knowledge, not the first record you happen to have sorted to the front. `[0]` after a sort is a decision; write down which end you meant.
**Enforced** `tests/test_physics.py`.
**Source** `3195167`. Present since the first refactor and never noticed.

### DATA-02 A synthetic generator that was not self-consistent
**Symptom** Exposed only by fixing DATA-01: the demo's own reference object sat 73 seconds from zero against itself.
**Cause** The generator advanced one orbital angle naively and held two others fixed, which is a slightly different orbit. It was invisible while the reference came from the first record, because that one is the base case and matches by construction.
**Fix** Advance the angles with the secular rates the propagator itself derives. Measured: 72.930 seconds to 0.001.
**Rule** A synthetic data generator must satisfy the same invariants as the real data, and the test for that is to measure the thing against itself. A fixture that is wrong hides defects and invents others.
**Enforced** `tests/test_physics.py`.
**Source** `3195167`.

### DATA-03 Duplication invisible on the output
**Symptom** None. Two records at one timestamp overplot, so the picture is identical whether a source sent one report or five.
**Fix** Checked at ingestion, before anything reaches a figure, and separated into two cases that differ in what they cost you: a duplicate is collapsed and counted, a conflict means one record is plotted and another is not and is shown as an alert.
**Rule** Where duplication is invisible in the output, it must be detected at ingestion. Separate "same values" from "different values" because they have different consequences.
**Enforced** `tests/test_quality.py`.
**Source** `192cc2e`.

### DATA-04 Deduplicating across independent producers
**Cause** Two originators reporting one object at one timestamp is two independent observations, which is the system working.
**Fix** Grouped by the record's own source first, so nothing is ever deduplicated against another producer.
**Rule** Establish the identity key for a duplicate before writing the deduplication. Grouping too coarsely destroys signal.
**Source** `192cc2e`.

### DATA-05 A tie-break reported as though it were evidence
**Symptom** Two records with identical creation stamps were reported as "resolved by the feed's creation stamp", which was a false claim about the data.
**Fix** The flag says whether the stamps decided it, not whether one was present. Two records with no stamp, or with identical stamps, are undecidable: first seen wins and the record says so.
**Rule** Report the basis of an automated choice honestly, including when the basis was arbitrary. An arbitrary pick described as evidence-based is worse than an arbitrary pick.
**Source** `192cc2e`.

### DATA-06 Non-deterministic conflict resolution
**Fix** Conflicts resolve the same way on every run, towards the newest stamp, so one feed cannot draw two different outputs.
**Rule** A tie-break must be deterministic, or the same input produces different reports.
**Source** `192cc2e`.

### DATA-07 Attribution kept in a parallel list
**Fix** The originator travels with the point: the computation returns triples, so there is no second list that could fall out of order with the first.
**Rule** Attach provenance to the record, never to a parallel array.
**Source** `192cc2e`.

### DATA-08 Two lists of the same thing, maintained separately
**Symptom** The configuration page offered 3 providers while the output legend showed 4, and the two disagreed about what one of the names meant.
**Fix** Both are rendered from the same table, so they cannot drift apart again, and a test asserts it.
**Rule** Two renderings of one list must share a source. Where they cannot, a test asserts they agree.
**Source** `4b082b8`.

### DATA-09 A key name taken by the wrong concept
**Cause** A derived series was keyed with the name of a real provider, so the name was unavailable to the provider that needed it.
**Rule** Reserve a name for the concept that genuinely owns it, and rename the squatter early. This gets more expensive every release.
**Source** `4b082b8`.

### DATA-10 External identifiers taken from a published model rather than from the live system
**Symptom** A system that spells one differently returns an empty result rather than an error, so the provider silently never appears and the failure is invisible.
**Fix** The aliases are gathered in one constant so a correction is one edit, records with no usable identifier are dropped rather than guessed, and a probe endpoint asks the live system for one record per provider and reports which answered and why the others did not.
**Rule** Mark values taken from a published data model as inference, not fact. Provide a probe that converts the inference into a measurement against the real system, and make a silent empty result loud.
**Source** `4b082b8`, `3d7b515`.

### DATA-11 An assumption that changes the answer, recorded as an event
**Cause** A record with no reference frame is assumed to be in its provider's declared frame. If that assumption is wrong the results are wrong.
**Fix** Every occurrence is recorded as an audit event with counts.
**Rule** Where a defaulting assumption can change the output, count and record every time you apply it.
**Source** `3d7b515`.

### DATA-12 A demo path ignoring a control the real path honours
**Symptom** Deselecting a provider appeared to do nothing in demo mode, which reads as a broken control rather than an unused one.
**Rule** A demo or sample path must honour the same controls as the real one, or it teaches the user the control is broken.
**Source** `4b082b8`.

## K. Interface and accessibility

### UI-01 No machine-readable signal for an in-flight state
**Fix** A live region with a politeness setting and a busy attribute, set true while the request is in flight and false when it settles.
**Rule** The correct accessibility signal is usually also the correct test signal. Build it once.
**Source** `0ba9d4e`.

### UI-02 A control at the browser's default size reading as decoration
**Symptom** A checkbox that states what an operation will cover read as decoration next to the label.
**Fix** Sized deliberately at 20 pixels.
**Rule** Size a control to its consequence, not to the browser's default.
**Source** `20a07aa`.

### UI-03 A selected state marked only by a highlight
**Symptom** It took a second look to tell which rows were in and which were out.
**Fix** Selected rows carry a coloured left edge and a tint, and unselected rows dim back.
**Rule** Mark both ends of a binary state, not just the positive one.
**Source** `20a07aa`.

### UI-04 An empty selection meaning the opposite of what the user intended
**Cause** The interface read an empty selection as "nothing", while the API read an empty list as "everything".
**Fix** An empty selection disables the control rather than sending a request, asserted on both sides.
**Rule** Where an empty value is overloaded, assert the guard on both sides of the boundary. The two sides will disagree by default.
**Source** `20a07aa`.

### UI-05 A count line whose noun agreed with the wrong number
**Symptom** "1 of 4 group".
**Rule** With a scope, the noun agrees with the total, not with the number chosen.
**Source** `20a07aa`.

### UI-06 A permanent animation used as a signal
**Fix** One shot rather than a permanent animation, which stops being a signal and is fatiguing to sit beside, and skipped entirely under a reduced-motion preference.
**Rule** A signal that never stops is not a signal.
**Source** `20a07aa`.

### UI-07 An unprompted request on every page load
**Symptom** The browser asked for an icon unprompted and got a 404 on every page load.
**Fix** An inline SVG icon, so the page makes no external request.
**Rule** Check the network panel on a cold load. A self-contained page makes zero external requests.
**Source** `c7ec481`.

### UI-08 A readiness probe that depends on an upstream
**Cause** Building an upstream client to serve the index page would turn their outage into a restart loop of your own.
**Fix** The index renders from local assets and never builds a client, asserted by a test that fails the build if it ever does.
**Rule** A readiness target must not touch a dependency you do not control.
**Enforced** `tests/test_api.py`.
**Source** `c7ec481`.

### UI-09 A large response re-sent on every view
**Symptom** Every reload pushed another 4 MB.
**Cause** The framework does not implement conditional requests itself.
**Fix** Streamed from the volume with an explicit entity-tag check.
**Rule** Do not assume your framework implements conditional requests. Check, then implement it.
**Source** `c7ec481`.

### UI-10 A failure that costs the whole run
**Fix** A group that yields no usable data is skipped with a recorded reason instead of failing the whole run. One dead input should not cost you the others.
**Rule** Decide per unit of work whether a failure is fatal to that unit or to the batch, and record the skip.
**Source** `c7ec481`.

### UI-11 Long work inside a request
**Cause** A representative run is tens of upstream calls and order 1e5 computations: about 20 seconds locally and minutes on a slow upstream.
**Fix** The request returns an identifier, the page polls, the finished document is written to the volume and streamed from there. Identical specifications share one job; failures are deliberately not joinable, or a transient upstream outage would poison that specification until the process restarted.
**Rule** Move long work out of the request, and think about whether failures should be shared by the deduplication.
**Source** `c7ec481`.

## L. Process and documentation

### DOC-01 Re-deriving settled decisions
**Symptom** Six uploads in one week went on re-establishing things the project had already proved, and one of them reverted a correct fix because nothing recorded that it was settled.
**Fix** A short file naming the decisions that cost an upload each and must not be re-litigated on reasoning alone, the pipeline as actually observed rather than as assumed, the pre-upload verification loop and the stop rule.
**Rule** Keep a short, deliberately incomplete file of settled decisions, separate from the long record. The long record is where things go to be complete; the short one is where they go to be read.
**Source** `37fc669`.

### DOC-02 A record that contradicts the code it describes (STILL LIVE)
**Symptom** `AUDIT.md:725` states that the coverage configuration pins the package as its source and explains why that is correct. That is the exact configuration that produced the 0.0 per cent gate, and the configuration file itself now carries a 25-line comment explaining why it must be the project root. `AUDIT.md:414` describes the same thing correctly in the past tense.
**Cause** One mention was updated and the other was not.
**Rule** A document is a copy, and a copy diverges. When a decision reverses, grep for every mention of the old answer rather than editing the one you were reading. Where it matters, assert the agreement with a test.
**Enforced** none.
**Source** Found in this session.

### DOC-03 Mark fact, inference and speculation separately
**Fix** The limitations section labels each entry FACT or INFERENCE. An inference about a builder's egress was marked as inference and turned out to be wrong, which cost one pipeline run and no confusion.
**Rule** Label the epistemic status of every claim in a technical record. The inference that is wrong is survivable; the inference recorded as fact is not.
**Source** `c70aeb5`, `f11c206`, `95cb524`.

### DOC-04 A second copy of the logic kept "just in case"
**Fix** The original script was deleted rather than kept, because keeping it would have left a second copy of the mathematics to drift out of step plus a live copy of the credential-prompt code the move was meant to remove.
**Rule** Delete the thing you replaced. Version control is the "just in case".
**Source** `c7ec481`.

### DOC-05 A verification loop that exists only in someone's head
**Fix** The loop is written down as runnable commands in the record, and includes the platform simulation, the runtime-dependencies-only boot, and a run as the unprivileged user the container actually uses against a volume owned by root.
**Rule** Write the pre-flight loop as commands, in a file, with each step saying what class of failure it catches that the others do not.
**Source** `AUDIT.md`, `CLAUDE.md`.

# Part 3: what is still open

Four entries in this register are unresolved in the source project. They are
the honest state, not an oversight.

● **TEST-10.** The comment stripper is blind to regular-expression literals, so
  every JavaScript standards check in the project is currently examining
  roughly 15 per cent of the file it thinks it is checking. Highest priority of
  the four, because it silently disables every other static check.
● **GATE-12.** The coverage margin is 0.6 of a point when the scanner does not
  read the committed properties file. The durable fix is configuration on the
  gate's own server.
● **DOC-02.** The long record contradicts the configuration file on the
  coverage question.
● **JS-13.** The second escaper for the charting sink is a design finding, not
  yet implemented.

# Part 4: the enforcement scaffolding

The pattern below is the most transferable single thing in this file. Three
layers, and knowing which is which matters.

| Layer | Covers | Runs where | Status |
|---|---|---|---|
| Language linter configuration | one language | locally | pre-flight |
| A JavaScript linter with a local rule | client code, deeply | locally | pre-flight |
| A standard-library test module | everything else, plus a backstop for the above | everywhere, including the platform's test stage | the guarantee |

The third layer is pure standard library on purpose. A pipeline test stage
typically installs one requirements file and runs one command, so a check that
needs a tool installed is a check that will not run there. Anything that must
not regress belongs in that file.

Its internal shape:

● A comment-and-string stripper that preserves line lengths, so a line number
  computed from an offset still points at the right line. **Test the stripper
  itself against a planted offender in every real source file.** See TEST-10.
● Each rule is a named `find_*` function over text, not a loop inside a test.
● Each `find_*` has at least two tests: one showing it fires on the offender it
  was written for, one showing it leaves the lookalike alone.
● One test asserts that no `find_*` exists without that pair.
● One test asserts that no check in the repository searches a file including
  its comments.
● Structural checks that read a file which may be absent from a deployment tree
  use a helper that skips with a named reason, with a companion check in a
  version-control work tree that fails if the file is genuinely missing.
● Any probe that shells out to an external binary is exception-safe and exists
  exactly once, enforced by parsing the test tree.

Alongside it:

● A terminal-summary hook printing an environment block into the test log:
  every file a later stage will need, and whether it is present. This is what
  you read when the failing stage's own log is unavailable.
● A script that reproduces the deployment environment rather than remembering
  it, run twice: once as expected, once with every optional file stripped.
● A script that stands up a real instance of whatever is judging the work, so a
  disputed finding can be measured in 10 minutes instead of 6 uploads.

# Part 5: provenance and limits

● Every entry traces to a commit in one repository, a line in its test suite,
  or a measurement recorded in this session. Entries marked STILL LIVE were
  measured during the compilation of this file.
● Counts quoted are from the repository at the time of writing: 41 commits, 755
  tests, 18 test modules, 11 named detectors, 32 detector-verification tests.
● This register is biased by its source. It is heavy on pipeline, packaging,
  container and coverage findings because that is what this project fought, and
  light on database, concurrency at scale, and front-end framework concerns
  because this project had none of those.
● No TODO, FIXME, HACK or XXX marker exists anywhere in the source tree, and
  every skip carries a written reason. That is a practice worth copying, not
  just an absence worth noting.
● The register is not a style guide. Every entry is a defect that happened. An
  entry you cannot imagine happening in your codebase is one to dismiss in a
  line, not one to adopt on principle.
