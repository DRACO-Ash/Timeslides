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

**Coverage exclusions** are recorded with their reasons in
`sonar-project.properties`. There are two: `app.py`, whose `main()` cannot run
under the test runner and is verified out of process instead, and
`timeslides/report/assets/**`, which runs in a browser where the Python
coverage reporter cannot see it and is covered in substance by the Playwright
suite. Neither is excluded from analysis.

## Verification loop

Run before every upload. A green repository loop is not a green upload, so the
last step reproduces what the platform actually installs.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                    # 320 tests, coverage.xml
PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  .venv/bin/python -m pytest -m browser       # the 13 browser tests
TIMESLIDES_DEMO=1 .venv/bin/python -m timeslides --out /tmp/check.html
```

**A skipped browser suite is not a passed one.** The Playwright fixture skips
when no usable Chromium is present, so a build agent without one reports
honestly rather than failing on infrastructure. Mapping "could not verify" to
"passed" is the fail-open defect, so the browser suite must be run and seen
green before an upload, not merely not-failed.

The pipeline simulation was run for this build: a clean directory containing
only what `git archive` produces, a fresh virtual environment from
`requirements-dev.txt` alone, `GITLAB_CI=true`, 320 tests passed, coverage.xml
written, 98 per cent.

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

● **FACT.** The NorthStar and KBR provider source strings in
  `models.STATE_SOURCES` are unverified against any tenant. The original script
  carried the same caveat in its own comments. LeoLabs is the only one to
  rely on until checked.
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
