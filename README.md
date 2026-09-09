# Timeslides

Step through a sequence of LEO satellite states to see how the time axis
changes the relative geometry, as a phase offset.

Phase offset is not a stored field. It is a derived along-track timing error:
how many seconds, measured along the orbit, an object sits ahead of (+) or
behind (-) a fixed reference orbit. The Unified Data Library (UDL) holds the
ingredients, not the answer. This application pulls state vectors and element
sets, takes one common reference orbit as the waterfall anchor, and projects
every object onto that reference's along-track direction.

Packaged as a Bluestaq App Store application. Deployment, gate and threat-model
detail is in [AUDIT.md](AUDIT.md).

## Running it

Credentials come from the environment. There is no configuration file and no
prompt, and the application fails to start without them.

```bash
export UDL_USER=... UDL_PASS=...
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                     # http://localhost:8080
```

No credentials to hand? Demo mode is synthetic and needs no network:

```bash
TIMESLIDES_DEMO=1 .venv/bin/python app.py
```

To render straight to a file with no server:

```bash
TIMESLIDES_DEMO=1 .venv/bin/python -m timeslides --out report.html
```

## Using it

**Configure tab.** Search the UDL on-orbit catalogue by name or NORAD number,
tick objects into a group, nominate one of them as the reference, and save.
The reference is the waterfall anchor: every other offset is measured from it
and it sits near zero against itself. Groups persist on the storage volume and
are shared by everyone using the deployment.

**Render.** Choose a window, the data modes and the state-vector providers,
then render. Five providers are configured: LeoLabs, NorthStar, KBR, PPEC and
Space-Track, all as state vectors from `/udl/statevector`. Their UDL source
names are the names these providers are known by rather than values read back
from a tenant, so **check availability** asks the UDL for one record from each
and says which answered. A provider that does not answer simply never appears
in a report, which is why the button exists.

Alongside the providers there is always an **Element sets** series: every
two-line element set in the window propagated to its own epoch. It is not a
provider and cannot be switched off, because it is also where the reference
orbit comes from. A render is a background job, because a wide window across several
providers is tens of UDL requests and a lot of propagation. Identical requests
share one job.

**Report tab.** The waterfall, exactly as it is produced for download. Toggle
providers, re-anchor on a different reference, switch data mode, tap an object
to isolate it.

## Reading the plot

**Offset (s)** is along-track timing: how far apart along the orbit, expressed
as travel time. Minus 120 s means the object passes a given point about 120 s
after the reference, so it trails by roughly 120 s.

**Drift (s/day)** is how fast that gap is changing.

**Closing and separating** combine the two. This is along-track timing only,
not a conjunction: radial and cross-track separation are not shown.

## Layout

```
app.py                     entry point, binds 0.0.0.0:8080
timeslides/
  api.py                   HTTP surface, create_app(deps)
  shell.py                 the page at /: Configure and Report tabs
  config.py                environment settings, fails closed
  udl.py                   the only external dependency
  groups.py                revisioned group store on the volume
  jobs.py                  background renders, single-flight
  pipeline.py              fetch and build
  physics.py               the phase-offset maths
  report/builder.py        the report renderer
  report/assets/           report.css, report.js, shell.css, picker.js
  demo.py                  synthetic data, and the test fixtures
tests/                     320 tests, including 13 browser tests
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                        # everything, with coverage
.venv/bin/python -m pytest -m browser             # just the browser suite
```

Three requirements files, because the platform reads only one of them:
`requirements.txt` is what the App Store's test stage installs, so it carries
the test runner as well as the runtime; `requirements-runtime.txt` is what the
container image installs, runtime only; `requirements-dev.txt` adds Playwright
for the browser suite, which needs a browser and so stays local. See AUDIT.md.
