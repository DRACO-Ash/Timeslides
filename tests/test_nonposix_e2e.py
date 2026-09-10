"""The whole application, over HTTP, on a filesystem that is not POSIX.

Why this file exists, in the words of the failure it is here to prevent.

    unexpected OSError: [Errno 38] Function not implemented:
    '/data/runs/<id>.html.tmp' -> '/data/runs/<id>.html'

The App Store's File Storage add-on is S3-backed, and S3-backed FUSE mounts
commonly implement neither fsync nor rename. The group store was fixed for
that. The job runner was not, so saving a group worked and then every render
died. Two write paths, one fixed.

The unit-level simulation did not catch it either, and that is the more
important lesson. It replaced the `os` attribute of one module, so it only
ever affected code that reached the filesystem through that module's `os`.
The job runner used pathlib, whose rename goes straight to the real os, so the
simulation reported success on the exact code that was broken. A test that
patches a module tests that module; it does not test the mount.

So this runs the real server in a subprocess with os.replace and os.fsync
disabled process-wide before the application is imported, and drives it over
HTTP. Nothing inside the application is stubbed. Any write path anywhere in
the app, present or future, in any module, through pathlib or os or shutil,
meets a filesystem that refuses those calls.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

# Each entry disables a capability process-wide, the way a mount lacking it
# would. "readonly" is the no-volume case: nothing can be written at all.
MOUNTS = {
    "s3": """
import errno, os
def _nosys(*a, **k):
    raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
os.replace = _nosys
os.rename = _nosys
os.fsync = _nosys
""",
    "objectstore": """
import builtins, errno, io, os
from pathlib import Path
def _nosys(*a, **k):
    raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
os.replace = _nosys
os.rename = _nosys
os.fsync = _nosys
_real_open = builtins.open
def _guarded(file, mode="r", *a, **k):
    # No overwrite of an existing object, only fresh sequential writes.
    if any(m in str(mode) for m in ("w", "a", "+")) and os.path.exists(file):
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    return _real_open(file, mode, *a, **k)
builtins.open = _guarded
io.open = _guarded
""",
    # mountpoint-for-s3: no fsync, no rename, and mkdir refused outright,
    # because there are no real directories on an object store. A write to a
    # key beneath a path that "does not exist" still succeeds.
    "implicitdirs": """
import errno, os
from pathlib import Path
def _nosys(*a, **k):
    raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
os.replace = _nosys
os.rename = _nosys
os.fsync = _nosys
os.mkdir = _nosys
os.makedirs = _nosys
Path.mkdir = _nosys
""",
    "readonly": """
import builtins, errno, io, os
def _rofs(*a, **k):
    raise OSError(errno.EROFS, os.strerror(errno.EROFS))
_real_open = builtins.open
def _guarded(file, mode="r", *a, **k):
    if any(m in str(mode) for m in ("w", "a", "x", "+")):
        _rofs()
    return _real_open(file, mode, *a, **k)
builtins.open = _guarded
io.open = _guarded
os.replace = _rofs
os.rename = _rofs
os.fsync = _rofs
os.mkdir = _rofs
os.makedirs = _rofs
""",
}

BOOT = """
{disable}
import uvicorn
from timeslides.api import create_app
from timeslides.config import Settings
settings = Settings(storage_path={storage!r}, demo=True, classification="OFFICIAL")
uvicorn.run(create_app(settings=settings), host="127.0.0.1", port={port},
            log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


def _post(url, payload, timeout=30):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture(params=sorted(MOUNTS), ids=sorted(MOUNTS))
def server(request, tmp_path_factory):
    """The real server, in its own process, on a crippled filesystem."""
    storage = tmp_path_factory.mktemp(request.param)
    if request.param == "implicitdirs":
        # On an object store the separator only looks like a directory, so a
        # write to a key beneath one that was never created still lands. This
        # is a real filesystem, so the directory has to exist for that to be a
        # faithful model; what is being simulated is mkdir being refused, not
        # the path being absent.
        (storage / "runs").mkdir()
    port = _free_port()
    script = BOOT.format(disable=MOUNTS[request.param],
                         storage=str(storage), port=port)
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"the server exited during boot:\\n{proc.stdout.read()}")
        try:
            status, _ = _get(f"{base}/healthz", timeout=2)
        except OSError:
            time.sleep(0.2)
            continue
        if status == 200:
            break
    else:
        proc.kill()
        pytest.fail(f"the server never became ready:\\n{proc.stdout.read()}")
    yield request.param, base, storage
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _await_run(base, run_id, timeout=180.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, body = _get(f"{base}/api/runs/{run_id}")
        run = json.loads(body)
        if run["status"] in ("done", "failed"):
            return run
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


def test_the_application_boots_and_serves_its_probe_target(server):
    _kind, base, _storage = server
    status, page = _get(base)
    assert status == 200
    assert "Timeslides" in page or "TIMESLIDES" in page.upper()
    status, health = _get(f"{base}/healthz")
    assert status == 200
    assert json.loads(health)["status"] == "ok"


def test_the_storage_mode_is_reported_honestly(server):
    """A mount that works says volume; one that does not says memory. Neither
    is allowed to fail the probe target, because the pod can still serve."""
    kind, base, _ = server
    storage = json.loads(_get(f"{base}/healthz")[1])["storage"]
    if kind == "readonly":
        assert storage["mode"] == "memory"
        assert storage["writable"] is False
    else:
        assert storage["mode"] == "volume", storage
        assert storage["writable"] is True
        assert storage["strategy"] in ("direct", "recreate"), storage


def test_a_group_can_be_saved_and_listed(server):
    kind, base, storage = server
    status, made = _post(f"{base}/api/groups",
                         {"name": "E2E Group", "sats": [62902, 62903, 62904],
                          "reference": 62902})
    assert status == 201, made
    _, listed = _get(f"{base}/api/groups")
    names = [g["name"] for g in json.loads(listed)["groups"]]
    assert "E2E Group" in names
    if kind != "readonly":
        on_disk = json.loads((storage / "groups.json").read_text(encoding="utf-8"))
        assert "E2E Group" in [g["name"] for g in on_disk["groups"]]
    else:
        assert not (storage / "groups.json").exists()


def test_a_report_renders_and_is_served(server):
    """The failure this file was written for. The render writes a file, and on
    these mounts the POSIX way of writing one does not work."""
    kind, base, storage = server
    status, started = _post(f"{base}/api/runs", {"days": 7})
    assert status == 202, started
    run = _await_run(base, started["id"])
    assert run["status"] == "done", run.get("error")
    status, report = _get(f"{base}{run['reportUrl']}", timeout=60)
    assert status == 200
    assert "plotly" in report.lower(), "the waterfall did not reach the report"
    if kind != "readonly":
        written = list((storage / "runs").glob("*.html"))
        assert len(written) == 1, written
        assert not list((storage / "runs").glob("*.tmp")), "temp file left behind"


def test_saving_then_rendering_that_group_works_end_to_end(server):
    _kind, base, _storage = server
    status, made = _post(f"{base}/api/groups",
                         {"name": "Render Me", "sats": [62902, 62903],
                          "reference": 62902})
    assert status == 201, made
    status, started = _post(f"{base}/api/runs", {"groupIds": [made["id"]]})
    assert status == 202, started
    run = _await_run(base, started["id"])
    assert run["status"] == "done", run.get("error")
    assert _get(f"{base}{run['reportUrl']}", timeout=60)[0] == 200


def test_nothing_is_left_lying_beside_the_files_that_matter(server):
    """A write that falls back must not leave probe or spare files behind."""
    _kind, base, storage = server
    _post(f"{base}/api/groups", {"name": "Tidy", "sats": [1, 2], "reference": 1})
    run = _await_run(base, _post(f"{base}/api/runs", {"days": 7})[1]["id"])
    assert run["status"] == "done", run.get("error")
    leftovers = [f.name for f in storage.rglob("*")
                 if f.is_file() and (".tmp" in f.name or ".spare" in f.name
                                     or f.name.startswith(".writetest"))]
    assert leftovers == [], leftovers


def test_only_the_storage_module_writes_to_the_filesystem():
    """The structural guarantee behind this file.

    The bug this suite exists to prevent was not a subtle one: the group store
    wrote through a capability-probing writer and the job runner wrote with a
    hand-rolled temp-file-and-rename, so the fix for an S3-backed mount landed
    in one and not the other. One write path is what makes the mount tests
    above cover the whole application rather than one module of it.

    So this fails if a second write path appears anywhere in the package, and
    the fix is to route it through timeslides.storage rather than to add the
    new call here.
    """
    import re
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "timeslides"
    writes = re.compile(
        r"os\.(replace|rename|fsync|remove|truncate|makedirs|mkdir)\b"
        r"|\.write_text\(|\.write_bytes\(|\.touch\(|shutil\."
        r"|open\([^)]*[\"'][rbt]*[wax]")
    offenders = {}
    for source in sorted(package.rglob("*.py")):
        if source.name == "storage.py":
            continue                      # the one place that is allowed to
        hits = [f"{source.relative_to(package)}:{n}: {line.strip()}"
                for n, line in enumerate(
                    source.read_text(encoding="utf-8").splitlines(), 1)
                if writes.search(line)]
        if hits:
            offenders[str(source.relative_to(package))] = hits
    assert offenders == {}, (
        "these write to the filesystem outside timeslides/storage.py, so the "
        "non-POSIX mount tests do not cover them:\n"
        + "\n".join(line for hits in offenders.values() for line in hits))


def test_unlinking_is_allowed_outside_the_storage_module():
    """Deleting is not the problem and never was.

    Recorded so the guard above is not mistaken for a ban on all filesystem
    access. Removal works on every mount the app meets, and the runner deletes
    evicted reports, so unlink is deliberately not in that pattern.
    """
    from pathlib import Path

    jobs = (Path(__file__).resolve().parent.parent
            / "timeslides" / "jobs.py").read_text(encoding="utf-8")
    assert "unlink(missing_ok=True)" in jobs
