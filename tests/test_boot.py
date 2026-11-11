"""The process as the platform starts it: the DAST and Logging stages, locally.

Both stages judge a running container, not the code, and 1.8.1 failed them
together for one reason. The DAST stage starts the image with none of its
environment variables or secrets and sets APPSTORE_DAST_SCAN=true. The
application failed closed without UDL credentials, so nothing answered on
8080, and the traceback it died with went to stderr, which the Logging stage
then reported as 55 lines on stderr and none on stdout.

So these run app.py in a subprocess, exactly as the image's command does, with
a deliberately empty environment, and read the real streams. A test that
called load_settings directly would not have caught either failure: the
crash was at import, before any test-visible function ran.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from tests.conftest import repo_file

STANDARD_KEYS = {"timestamp", "severity_text", "severity_number", "body", "attributes"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bare_env(tmp_path, **extra) -> dict:
    """What the scan gives the container: nothing of ours. PATH and the
    interpreter's own variables only, so the subprocess can start at all."""
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "PYTHONPATH", "VIRTUAL_ENV", "LANG", "HOME", "TMPDIR")}
    env.update(STORAGE_MOUNT_PATH=str(tmp_path), HOST="127.0.0.1", **extra)
    return env


def _get(url: str, headers=None) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers or {})
    with opener.open(req, timeout=5) as res:
        return res.status


def _json_lines(out: str) -> list:
    lines = [line for line in out.splitlines() if line.strip()]
    parsed = []
    for line in lines:
        record = json.loads(line)           # every line must be JSON
        assert set(record) >= STANDARD_KEYS, f"missing standard keys: {line}"
        assert isinstance(record["attributes"], dict)
        parsed.append(record)
    return parsed


def _image_command(port: int) -> list:
    """The image's own CMD, read from the Dockerfile rather than restated.

    The first version of this test started app.py directly, passed, and the
    built image still wrote uvicorn's startup lines to stderr, because the
    image starts with `python -m uvicorn app:app` and that launcher configures
    logging its own way. A launch command copied into a test is a copy that
    diverges. Only the interpreter path, the listen address and the port are
    replaced, so the test runs here rather than in the container.
    """
    dockerfile = repo_file("Dockerfile", "it names the command the image runs").read_text()
    lines = dockerfile.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("CMD "))
    text = lines[start][4:]
    while text.rstrip().endswith("\\"):
        start += 1
        text = text.rstrip()[:-1] + lines[start]
    argv = json.loads(text)
    assert argv[:3] == ["/opt/venv/bin/python", "-m", "uvicorn"], argv
    swap = {"0.0.0.0": "127.0.0.1", "8080": str(port)}
    return [sys.executable, *[swap.get(a, a) for a in argv[1:]]]


@pytest.mark.parametrize("launch", ["image CMD", "python app.py"])
def test_a_dast_scan_with_no_secrets_answers_on_its_port_and_logs_only_to_stdout(tmp_path, launch):
    app = repo_file("app.py", "the entry point the image runs")
    port = _free_port()
    argv = _image_command(port) if launch == "image CMD" else [sys.executable, str(app)]
    proc = subprocess.Popen(
        argv, cwd=str(app.parent),
        env=_bare_env(tmp_path, APPSTORE_DAST_SCAN="true", PORT=str(port)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                status = _get(f"http://127.0.0.1:{port}/",
                              {"X-Request-Id": "req-42", "X-Auth-Request-Email": "dast@example.org",
                               "Authorization": "Bearer never-log-me", "Cookie": "session=never"})
                break
            except OSError:
                time.sleep(0.2)
        _get(f"http://127.0.0.1:{port}/healthz?token=never-log-this-either")
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=15)
    assert status == 200, f"nothing answered on {port}; stdout:\n{out}\nstderr:\n{err}"
    assert err == "", f"the logging standard allows nothing on stderr:\n{err}"
    records = _json_lines(out)
    bodies = [r["body"] for r in records]
    assert "boot.dast_scan" in bodies
    assert any(r["attributes"].get("logger.name", "").startswith("uvicorn") for r in records), \
        "uvicorn's own lines should arrive in the same shape"
    requests = [r["attributes"] for r in records if r["body"] == "http.request"]
    first = next(r for r in requests if r["url.path"] == "/")
    assert first["http.request.header.x-request-id"] == "req-42"
    assert first["user.email"] == "dast@example.org"
    assert first["http.response.status_code"] == 200
    for forbidden in ("never-log-me", "session=never", "never-log-this-either", "?token"):
        assert forbidden not in out


def test_a_refused_start_is_one_fatal_json_line_on_stdout_not_a_traceback(tmp_path):
    """Outside a scan, missing credentials still fail closed, as they must.
    What changes is how: a line the platform can read, not a stderr dump."""
    app = repo_file("app.py", "the entry point the image runs")
    proc = subprocess.run(
        [sys.executable, str(app)], cwd=str(app.parent), env=_bare_env(tmp_path),
        capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode != 0
    assert proc.stderr == "", f"nothing belongs on stderr:\n{proc.stderr}"
    fatal = [r for r in _json_lines(proc.stdout) if r["severity_text"] == "FATAL"]
    assert len(fatal) == 1
    assert fatal[0]["attributes"]["exception.type"] == "ConfigError"
    assert "UDL_USER and UDL_PASS" in fatal[0]["attributes"]["exception.message"]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_the_scan_flag_is_read_the_way_the_other_flags_are(value):
    from timeslides.config import load_settings
    assert load_settings({"APPSTORE_DAST_SCAN": value}).demo is True


@pytest.mark.parametrize(("env", "demo"), [
    ({"APPSTORE_DAST_SCAN": "true"}, True),
    ({"APPSTORE_DAST_SCAN": "true", "UDL_USER": "u", "UDL_PASS": "p"}, False),
])
def test_a_scan_start_is_recorded_with_what_it_will_serve(tmp_path, capsys, env, demo):
    """In process, as well as in the subprocesses above, because coverage
    measures this process only. The record says which case the scan met."""
    from fastapi.testclient import TestClient

    from timeslides.api import create_app
    from timeslides.config import load_settings
    from timeslides.jobs import JobRunner

    settings = load_settings({**env, "STORAGE_MOUNT_PATH": str(tmp_path)})
    runner = JobRunner(lambda s, p: "<html/>", settings.runs_path)
    with TestClient(create_app(settings=settings, runner=runner)) as client:
        assert client.get("/").status_code == 200
    runner.shutdown()
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()
               if line.startswith("{")]
    scan = [r["attributes"] for r in records if r["body"] == "boot.dast_scan"]
    assert scan == [{"demo": demo, "reason": scan[0]["reason"]}]
    assert ("synthetic data" in scan[0]["reason"]) is demo
