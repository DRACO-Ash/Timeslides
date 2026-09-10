# Timeslides: audit and deployment record

Phase-offset waterfall for LEO objects, built on the Unified Data Library (UDL).
Packaged for the Bluestaq App Store.

## What this application is

It answers one question: how far ahead of or behind a reference orbit does each
object in a group sit, measured along-track in seconds, and how is that gap
changing. The UDL holds the ingredients but not the answer, so the application
pulls state vectors and element sets, projects each observed position onto one
common reference orbit's along-track direction, and plots the result.

## Parameters

Every value the application reads from its environment. All are optional except
the two credentials, and demo mode replaces those.

| Variable | Default | Purpose |
|---|---|---|
| `UDL_USER` | none | UDL username. Required. Boot fails without it. |
| `UDL_PASS` | none | UDL password. Required. Excluded from every log and repr. |
| `UDL_BASE` | `https://unifieddatalibrary.com` | UDL base URL. |
| `PORT` | `8080` | Read, never set as an image `ENV`. |
| `STORAGE_MOUNT_PATH` | `/data` | Persistent volume. Holds `groups.json` and `runs/`. |
| `CLASSIFICATION` | `UNCLASSIFIED` | Banner text on the shell and every report. |
| `TIMESLIDES_DEMO` | off | Synthetic data. No credentials, no network. |
| `UDL_RATE_PER_MIN` | `60` | Token-bucket budget for UDL requests. |
| `UDL_TIMEOUT_S` | `60` | Per-request timeout. |
| `UDL_MAX_RESULTS` | `5000` | `maxResults` on each UDL query. |
| | | Five state providers times the data modes times the objects is the per-run request count, so `UDL_RATE_PER_MIN` matters more than it did with three. |
| `LOG_LEVEL` | `INFO` | Log threshold. |

## Platform requirements

Three things the App Store must provide. The first two are hard.

● **Egress to `unifieddatalibrary.com`.** The only outbound dependency. Without
  it the application runs but every render fails at the first request. Nothing
  else is contacted: Plotly is inlined into each report, the favicon is a data
  URI, and there are no CDN or font requests.
● **The storage add-on, with `fsGroup` set** in the pod `securityContext`. The
  container runs as uid 1000 and cannot write a root-owned mount. Without
  `fsGroup` the application looks healthy and every group save returns EACCES.
  The error message names `fsGroup` for exactly this reason.
● **A single replica.** Render jobs are held in process and the group store is
  one file on one volume, so a second replica splits both. Concurrency comes
  from the render thread pool, not from extra pods or workers.

## Runtime contract

| Requirement | How it is met | Verified |
|---|---|---|
| Port 8080, probed at `/` | uvicorn on 8080; `/` renders from local assets | Test asserts the index page never builds a UDL client |
| Bound to all interfaces | `--host 0.0.0.0` | `LISTEN on 0.0.0.0:8080` in `/proc/net/tcp`; reached on three addresses |
| Non-root, numeric | `USER 1000:1000` | Serving process `Uid: 1000 1000 1000 1000`, `CapEff: 0000000000000000` |
| Readiness independent of upstreams | `/` and `/healthz` make no UDL call | Test injects an exploding client factory and asserts 200 |
| Writes only to the volume | Code tree read-only | Ran with the tree owned by root and mode `a-w`; group and report writes landed on the volume owned 1000:1000 |
| No `ENV PORT=` | Absent from the Dockerfile by design | Read with a default instead |

## The storage volume: one write path, probed not assumed

The File Storage add-on is **S3-backed**, mounted at `STORAGE_MOUNT_PATH`
(`/data`). S3-backed FUSE mounts commonly implement neither `fsync` nor
`rename`, and `mountpoint-for-s3` also refuses `mkdir` because an object store
has no real directories. The POSIX habit of writing a temp file and renaming it
over the target does not work there: it fails with `ENOSYS`, Function not
implemented.

This cost three releases, in this order:

1. The group store assumed POSIX. Every save failed. Fixed with a probing
   writer that walks a ladder of mechanisms.
2. The boot probe wrote an empty file with `write_text`, taking none of the
   steps a real write takes, so it reported the volume writable while every
   save on it failed. A probe that does not run the real path manufactures
   confidence. Fixed by probing with the real mechanism.
3. The job runner still had its own hand-rolled temp-and-rename, so saving
   worked and every render died. **Two write paths meant two chances to get it
   wrong**, and the unit simulation missed it because it patched one module's
   `os` while the runner wrote through `pathlib`.

### The contract

● Every filesystem write in the package goes through `timeslides/storage.py`.
  Nothing else may write. `tests/test_nonposix_e2e.py` fails if a second write
  path appears anywhere in `timeslides/`. Deleting is exempt: `unlink` works on
  every mount the app meets.
● The mechanism is discovered by probing, not assumed. The ladder, best first:

  | Strategy | Mechanism | Crash-safe |
  |---|---|---|
  | `atomic` | temp file, flush, `fsync` where implemented, `rename` | yes |
  | `direct` | written in place | no |
  | `recreate` | removed then rewritten | no |

● `fsync` is best-effort. It is a durability guarantee, not part of the
  atomicity guarantee; the rename is what makes a replacement atomic.
● `mkdir` never fails a write. On an object store the separator only looks like
  a directory, and `mountpoint-for-s3` refuses `mkdir` on a path that is
  perfectly writable. The write is the arbiter and reports the real errno.
● `recreate` removes nothing until the mount has proved it will take a write.
  Two earlier versions lost data here: the first unlinked with no way back, the
  second restored afterwards using a different call than the write it was
  compensating for, so it only ever worked in the test.
● If no mechanism works, the application degrades rather than breaking. Groups
  are held in memory, and so are up to `MAX_HELD_IN_MEMORY` rendered reports.
  A report is several megabytes, so that bound is deliberately small: enough to
  render, look, and render again, not enough to risk the pod's memory limit.
● The degradation is never silent: an audit event at boot, a `storage` block on
  `/healthz` naming the mode and strategy, and a standing warning on the page.
  A working but non-crash-safe mount gets one quiet line instead of an alarm.

### How it is tested

`tests/test_nonposix_e2e.py` runs the **real server in a subprocess** with the
relevant syscalls disabled process-wide before the application is imported, and
drives it over HTTP. Nothing inside the application is stubbed, so any write
path in any module, through `pathlib` or `os` or `shutil`, meets a filesystem
that refuses those calls. Four mounts:

| Mount | Disabled | Expected |
|---|---|---|
| `s3` | `fsync`, `rename` | `direct`, everything on the volume |
| `implicitdirs` | `fsync`, `rename`, `mkdir` | `direct`, everything on the volume |
| `objectstore` | `fsync`, `rename`, overwrite of an existing file | `recreate`, everything on the volume |
| `readonly` | every write | memory mode, app fully usable, nothing on the volume |

A test that patches a module tests that module. It does not test the mount.
That distinction is why the third release above shipped broken.

## Provenance: which source is a point actually from

The state-vector series are queried per provider
(`/udl/statevector?source=KBR`), so a triangle is a KBR report by
construction. The element-set series is not: the query is
`/udl/elset?epoch=...&satNo=...` with **no source filter**, so whatever the
tenant holds comes back, from any originator.

That made the one series which is always plotted and cannot be deselected also
the only one with no attribution. It was asked, correctly, whether "Element
sets" meant 18 SDS, Space-Track or KBR, and the application could not say.

● Each record now carries its own `source`, `origin` and `created`, read
  through `PROVENANCE_FIELDS` in `timeslides/udl.py`. Those names are aliased
  the same way as the on-orbit fields and for the same reason: **they come from
  the public UDL data model, not from a call against your tenant.** A name that
  does not match degrades to "source not stated" rather than raising.
● The originator travels with the point, not alongside it. `compute_series`
  returns `(epoch, offset, source)` triples, so there is no second list that
  could fall out of order with the first.
● The element-set tooltip shows it: `Element sets · 18 SDS`. State-vector
  tooltips do not repeat their provider, because the query already fixed it and
  the label above the line says it.
● `udl.elset.sources` is logged on every elset query, listing the distinct
  originators the tenant actually returned. That is the answer to "which
  source", from the tenant rather than from the data model.

On 18 SDS versus Space-Track: 18th Space Defense Squadron produces the general
perturbations catalogue and Space-Track is its public distribution front end,
so both labels commonly describe the same lineage. Which label your tenant
attaches is a property of your tenant, and the log above is what tells you.

**A literal middot, not `&middot;`.** Plotly draws its tooltip as SVG text and
does not decode HTML entities there, so the entity appeared on screen verbatim.
Only hovering a real point in a real browser caught that, which is why the
browser suite hovers rather than inspecting the document.

## Duplicate and conflicting reports

A waterfall of provenance-labelled points is only as good as the claim that
each point is one independent report, and duplication breaks that claim
invisibly: two records at one epoch overplot, so the chart looks identical
whether a source sent one report or five. Checked at ingestion, in
`timeslides/quality.py`, before anything reaches a figure.

Two things are separated because they matter differently:

| | Definition | Effect on the chart | Handling |
|---|---|---|---|
| **Duplicate** | Same source, same epoch, same values | None; they overplot | Collapsed, counted, shown as a note |
| **Conflict** | Same source, same epoch, **different** values | One is plotted and one is not | One kept, shown as an alert |

● **Same source only.** Two originators reporting one object at one epoch is
  two independent element sets, which is the plot working. `dedupe_mixed`
  groups by the record's own source first, so nothing is ever deduplicated
  against another producer.
● **Conflicts resolve the same way on every run**, in favour of the newest
  `created` stamp, so one feed does not draw two different charts.
● **The report says whether that choice meant anything.** Two records with no
  stamp, or with identical stamps, are undecidable: first seen wins and the
  band says so rather than implying the pick was evidence-based. An earlier
  version reported identical stamps as "resolved by the feed's creation stamp",
  which was a false claim about the data.
● Reported in three places: a `quality.duplicates` audit event, the finding on
  the object, and a data-quality band in the report panel. A clean feed renders
  no band at all, so the band stays a finding rather than becoming furniture.

## Container build: not verified in this session

**The image was not built.** Docker is available here but this session's egress
policy denies Docker Hub's blob content delivery network
(`production.cloudfront.docker.com` answers 403 to the manifest and layer
fetches, while `auth.docker.io` and `registry-1.docker.io` respond normally).
The `python:3.11-slim-bookworm` base image therefore cannot be pulled, and per
the proxy guidance a policy denial is reported rather than routed around.

What that leaves unverified: the `containerize` and `container-scan` stages,
which is to say the flatten actually collapsing to one layer, the setuid strip
assertion firing, and the image passing the policy scan. The Dockerfile parses
(BuildKit loaded the build definition before failing on the pull) and every
runtime property the image is meant to deliver is verified separately by the
runtime contract check below, run as uid 1000 against the same entry point.

**Do this before the first upload:** build the image on a host with registry
access and confirm the prep stage's setuid assertion passes, the final image
reports `USER 1000:1000`, and `docker history` shows a single layer above
scratch.

The base image is `registry.bluestaq.com/container/library/python:3.12-slim`,
the internal mirror at the tag the platform's own runners pull. A `docker.io`
reference will not resolve on builders in this environment. Note also that with
`requirements.txt` present the App Store auto-detects the python template and
may build the image from that template rather than from this Dockerfile, in
which case this file is the record of what the image must satisfy rather than
the thing that produces it.

## Container image policy

The image is flattened. A `chmod -s` in a later layer does not remove a bit an
earlier layer set, because the policy reads layer blobs and history, so the
build strips the rootfs in a prep stage and then collapses it with a single
`COPY --from=prep / /` onto `FROM scratch`. The prep stage also fails the build
itself if any setuid or setgid file survives the strip, so a policy problem
surfaces with a readable message at build time instead of at container-scan.

pip, setuptools and wheel are removed after the dependencies are installed, and
the runtime carries the interpreter and the dependencies only: no build
toolchain, no package manager, no shell utilities that were setuid.

## Quality gate

| Gate | Status |
|---|---|
| Coverage at or above 80 per cent | **98 per cent**, `coverage.xml` emitted for the scan stage |
| Cognitive complexity at or below 15 | `main()` (156 lines, 47 branch tokens) and `build_panel` (25) decomposed into named helpers |
| Zero open violations | Dead code removed: `build_demo_groups`, the unused `MU` constant, and the `tle_source` parameter that stopped meaning anything when Space-Track was dropped |
| Client-side code analysed | CSS and JavaScript are asset files, not Python string literals, so the gate can see them. Not excluded from analysis |
| Security hotspots | Every reflection site escaped server-side and client-side; JSON embedded with `<`, `>`, `&` escaped; no credential in any log, error or response |

### The listen address, and the one finding that may need a human

The gate raised `app.py`: "Avoid binding the application to all network
interfaces". The application must do exactly that, because the platform sets
`containerPort: 8080` and probes the pod's own address, so a container bound to
loopback builds cleanly, passes every test and fails every probe.

The address is now configuration rather than a hardcoded decision: `HOST`,
defaulting to the empty string, which is the address-family-agnostic form of
"every interface" that `socket.bind(("", port))` means. Verified to produce
`LISTEN on 0.0.0.0:8080`. That removes the literal the rule matches on.

**If the gate still raises it, it needs marking as a reviewed hotspot by a
person.** The behaviour is required and cannot be changed, and "security
hotspots reviewed" is part of what the gate asks for.

**Coverage exclusions** are recorded with their reasons in
`sonar-project.properties`. There are two: `app.py`, whose `main()` cannot run
under the test runner and is verified out of process instead, and
`timeslides/report/assets/**`, which runs in a browser where the Python
coverage reporter cannot see it and is covered in substance by the Playwright
suite. Neither is excluded from analysis.

## Dependency files, and why there are three

The platform's generated test stage runs exactly two commands and reads exactly
one requirements file:

```
pip install -r requirements.txt
pytest --cov --cov-report=xml:coverage.xml
```

That drives the split.

| File | Installed by | Contents |
|---|---|---|
| `requirements.txt` | the platform's test stage | runtime, plus pytest, pytest-cov, httpx and quickjs |
| `requirements-runtime.txt` | the container image | runtime only, eight packages |
| `requirements-dev.txt` | a developer, locally | the platform set plus Playwright |

The test tooling has to be in `requirements.txt` because that is the only file
the platform installs; leaving it in `requirements-dev.txt` gave
`pytest: command not found` and exit 127 at the test stage. It must not be in
the image, because the container scan judges what the image contains, so the
Dockerfile installs `requirements-runtime.txt` instead. Playwright stays local:
the runner is a slim Python image with no browser, so installing it there buys
nothing and the browser module skips itself either way.

`.coveragerc` pins `source = timeslides`, because the platform passes a bare
`--cov` and that means "take the source from configuration". Without it the
figure the gate reads is diluted by test files and site-packages.

Five tests guard all of this: that `requirements.txt` declares the runner, that
`requirements-runtime.txt` carries no test tooling, that the Dockerfile
installs the runtime set, that the coverage source is pinned, and that the
Dockerfile never sets `PORT`.

## Testing the browser assets

The gate put line coverage at 79.2% against a threshold of 80% while the Python
was at 100%. The arithmetic explains it: roughly 320 lines of `report.js` and
`picker.js` were in the denominator with no coverage data, and no amount of
Python testing can lift a ratio whose ceiling is about 80.3%.

The honest question that followed was why the JavaScript was not tested. It
was, but only one way and only sometimes:

● `tests/test_smoke.py` drives it in a real browser, 20 tests over the whole
  flow. That is the only way to prove the wiring, and it needs a browser, so it
  skips wherever one is not provisioned, including the platform's test stage.

So it now has unit tests as well. `tests/test_assets_js.py` loads the shipped
files into a JavaScript engine in process, under pytest, and exercises their
pure functions: escaping, number formatting, the card fragments, the four
relative-motion states and their thresholds, and the picker's label helpers.
56 tests, and they run everywhere the Python tests run, pipeline included. The
engine is `quickjs`, which publishes a cp312 manylinux wheel, so the slim
runner needs no compiler.

There is no second copy of the logic: the tests read
`timeslides/report/assets/*.js` as published. Their teeth were checked by
mutation, not assumed. Stopping `esc()` escaping `<` fails two tests; inverting
the closing-versus-separating sign test fails two; swapping the precedence of
"no data" over "reference datum" fails one.

**Coverage measurement is still not possible.** Sonar reads LCOV for
JavaScript and this engine does not emit it. The assets are therefore excluded
from the coverage metric with that rationale recorded in
`sonar-project.properties`, and they are **not** excluded from analysis: the
gate raised six findings in them, all fixed, which is the whole reason they are
real files rather than strings inside Python. If measured JavaScript coverage
is wanted later, the route is a Node toolchain producing LCOV, which the
platform's python template does not provide today.

## Local pre-flight for the quality gate

The gate is SonarQube and it runs in the pipeline, so on its own it can only
tell you about a problem after an upload. `ruff.toml` configures ruff with the
rule families that gate actually raised against this project, including the
same cognitive-complexity cap of 15:

```bash
.venv/bin/ruff check timeslides tests app.py
```

It is not a substitute for the gate. It found 37 issues the gate would also
have raised, and it cannot see the CSS, the JavaScript, or Sonar's own
cognitive-complexity measure. Run it anyway; it takes a second.

For the two asset files, `node --check` catches syntax and a short grep in the
verification loop catches the patterns the gate objected to (nested ternaries,
`Object.assign`, bare `parseInt`).

## Verification loop

Run before every upload. A green repository loop is not a green upload, so the
last two steps reproduce what the platform actually does.

```bash
# Everything, including the browser suite
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers .venv/bin/python -m pytest
TIMESLIDES_DEMO=1 .venv/bin/python -m timeslides --out /tmp/check.html

# The quality gate's rule families, locally
.venv/bin/ruff check timeslides tests app.py
node --check timeslides/report/assets/report.js
node --check timeslides/report/assets/picker.js

# The platform's test stage, verbatim, in a clean directory with only
# requirements.txt installed. This is the step that catches what local green
# cannot: tooling that is present on your machine and absent on the runner.
pip install -r requirements.txt
pytest --cov --cov-report=xml:coverage.xml
```

**A skipped browser suite is not a passed one.** The Playwright fixture skips
when no usable Chromium is present, so a build agent without one reports
honestly rather than failing on infrastructure. Mapping "could not verify" to
"passed" is the fail-open defect, so the browser suite must be run and seen
green before an upload, not merely not-failed.

**A green unit suite is not a green deployment.** Three releases shipped a
volume defect that every unit test passed, because the tests patched a module
while the platform gave the app a filesystem. `tests/test_nonposix_e2e.py`
exists for that gap and must be seen green: it runs the real server in a
subprocess against four crippled filesystems and drives it over HTTP.

Two further checks that no test in the repository can make for you, both worth
running by hand when the write path or the dependency split changes:

```bash
# Boot on runtime dependencies ONLY. The image installs requirements-runtime.txt,
# so a runtime module importing something that lives only in requirements.txt
# crashes the container while the whole suite still passes.
python -m venv /tmp/rt && /tmp/rt/bin/pip install -r requirements-runtime.txt
STORAGE_MOUNT_PATH=/tmp/data TIMESLIDES_DEMO=1 \
  /tmp/rt/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8099

# As uid 1000, the user the container actually runs as, against a volume owned
# by root. Expect memory mode and the fsGroup advice, not a crash.
setpriv --reuid=1000 --regid=1000 --clear-groups /tmp/rt/bin/python -c "..."
```

Both were run for this build. Runtime-only boot: healthy, three seeded groups,
catalogue search returned COSMOS 2581/82/83, a group saved, a report rendered
to 4,508,579 bytes on the volume. As uid 1000: a volume owned 1000:1000 gave
`mode=volume strategy=atomic` and saved; a volume owned by root gave
`mode=memory` with `PermissionError EACCES (errno 13)` and the fsGroup advice.

The pipeline simulation was run for this build: a clean directory containing
only what `git archive` produces, a fresh virtual environment, the platform's
own two commands, 532 passed, 1 skipped (no browser), coverage.xml written,
100 per cent line coverage.

## Threat model

**Trust boundary.** The HTTP edge. Everything arriving over it is untrusted:
group names, NORAD numbers, catalogue queries, run parameters, revisions.
Everything arriving from the UDL is also untrusted, because object names come
from a tenant this application does not control.

**Defended.**
● Injection into HTML. Group names and object names reach eight sinks: three
  server-rendered interpolations, the Plotly hovertemplate, and four
  `innerHTML` templates in the client. All escaped, both sides, with an
  end-to-end browser test that creates a group named after an injection
  attempt, renders it, and asserts no element was created and no script ran.
● Breaking out of the embedded JSON. A group named `</script>` would otherwise
  close the data block. `<`, `>` and `&` are escaped to `\uXXXX`.
● Log forgery. Group names and search strings are stripped of control
  characters and length-bounded before they reach a log field.
● Credential disclosure. `UDL_PASS` is absent from the `Settings` repr, from
  every error message, and from every response body. Tested by asserting the
  fixture password appears nowhere.
● Stale writes. A monotonic revision means one person's save cannot silently
  discard another's.
● Resource exhaustion. Windows capped at 90 days, runs at 12 groups, groups at
  40 objects, the job registry and the volume both bounded, UDL requests
  rate-limited, and identical renders share one job.
● Data loss. Atomic writes, archive rather than erase, and a corrupt store is
  reported rather than overwritten.

**Deliberately out of scope.**
● **Application-level authentication and authorisation.** Decided: the App
  Store's single sign-on fronts the deployment and the application trusts the
  request. The residual risk is that anything with network reach to the pod can
  create, edit and archive groups and spend the shared UDL quota, with no
  per-user attribution in the audit log. Recorded for OPS-002. The seam for a
  shared bearer token with a constant-time comparison, and later for per-user
  single sign-on, is the one HTTP layer in `api.py`.
● Multi-tenancy. One deployment, one group list, shared by everyone who can
  reach it.
● Report confidentiality after download. A generated report is a plain HTML
  file carrying the configured classification banner and nothing that enforces
  it.

## Known limitations, marked

● **FACT.** All five provider source strings in `models.STATE_SOURCES`
  (`LeoLabs`, `NorthStar`, `KBR`, `PPEC`, `Space-Track`) are the names these
  providers are commonly known by, not values read back from a live tenant.
  The original script carried the same caveat for NorthStar and KBR; it applies
  to all five. A tenant that spells one differently returns an empty series
  rather than an error, so the provider silently never appears in a report.
  **`GET /api/sources/probe`, and the "check availability" button next to the
  provider chips, answer this directly**: one record requested per provider
  against a real object, reporting which answered and why the others did not.
  Run it once against the tenant and correct `STATE_SOURCES` from the result.
● **INFERENCE.** The `/udl/onorbit` endpoint and its field names, used by the
  catalogue picker, are taken from the public UDL data model and not from a
  call against a live tenant. Aliases are gathered in `udl.ONORBIT_FIELDS` so a
  correction is one edit, a record with no usable `satNo` is dropped rather
  than guessed, and a tenant that ignores the `name` filter still returns a
  usable page rather than nothing.
● **FACT.** A state vector with no `referenceFrame` is assumed to be in its
  provider's declared frame. If that assumption is wrong the offsets are wrong,
  so every occurrence is recorded as a `udl.statevector.frame_assumed` audit
  event with the counts.
● **FACT.** `astropy` is required because UDL state vectors are J2000 and SGP4
  works in TEME. It is a heavy dependency and it enlarges the surface the
  container scan reads. Avoidable only if the UDL will serve TEME directly.
● **FACT.** A report is roughly 4.4 MB because Plotly is inlined once per
  document. That is what makes it self-contained and offline, and it is why the
  report is streamed with a conditional GET rather than re-sent on every view.
● **FACT.** Element sets are labelled "Space-Track" in the legend. They are
  fetched from the UDL, but the records the UDL serves are 18th Space Defense
  Squadron two-line element sets, which is what the old Space-Track path
  fetched, so the provenance label is accurate.

## Rollback

There is no separate rollback flow. Repackage the previous build and resubmit.

## Series and providers

Two kinds of series are plotted, and an earlier version of this build confused
them.

● **State-vector series**, one per provider, from `/udl/statevector` filtered by
  `source`. These are measured positions. Five are configured: LeoLabs,
  NorthStar, KBR, PPEC and Space-Track. Filled marker shapes.
● **The element-set series**, key `elset`, label "Element sets", derived from
  `/udl/elset` by propagating each two-line element set to its own epoch. Not a
  provider, and not optional: it is also where the reference orbit that anchors
  the whole waterfall comes from. The one open marker shape.

The element-set key used to be `spacetrack`, on the reasoning that the records
the UDL serves on `/udl/elset` are 18 SDS two-line element sets. That was
accurate but it took the name a real state-vector provider needs, and it made
the Configure tab's provider list disagree with the report's legend: the tab
offered state providers only while the legend also carried the element-set
series. The provider chips are now rendered server-side from the same table the
report legend uses, and a test asserts the two lists cannot drift apart again.

Shape encodes the source and colour encodes the object, so six series need six
distinct shapes: circle, diamond, triangle, cross, saltire, and the open square
for the element sets. A browser test asserts all six are distinct and that each
marker actually has area, because a clip-path typo yields an invisible chip.

## Migration from the original script

`LEO_Waterfall_Phase_Offset.py` and `credentials.ini` are both deleted.

● The phase-offset method is unchanged. The per-point loops are batched for
  speed; frame conversion and propagation are bit-identical batched, the
  along-track projection differs by up to 1.1e-13 seconds, and the test suite
  asserts both against the original implementation kept as an oracle.
● The report renderer is unchanged. Its output was compared against the
  original script's own `--demo` output: zero differences across the entire
  client data payload and identical counts of every structural element.
● The three groups that were in `credentials.ini` are seeded into an empty
  store on first boot, references included. No live credential was ever
  committed to this repository; every historical revision of that file had
  blank username, password and token values, so there is nothing to rotate.
