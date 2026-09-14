from __future__ import annotations


import pytest

from timeslides.config import Settings
from timeslides.ratelimit import TokenBucket


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", raise_on_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records requests and replays queued responses. No network, ever."""

    def __init__(self, responses=None, raises=None):
        self.responses = list(responses or [])
        self.raises = raises
        self.calls = []
        self.headers = {}
        self.auth = None

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(url=url, params=params or {}, timeout=timeout))
        if self.raises:
            raise self.raises
        if not self.responses:
            return FakeResponse(200, [])
        nxt = self.responses.pop(0)
        # Duck-typed rather than isinstance: pytest can load this module twice
        # (once as the conftest plugin, once as tests.conftest), which makes two
        # distinct FakeResponse classes and an isinstance check that silently
        # treats a response object as a JSON payload.
        return nxt if hasattr(nxt, "status_code") else FakeResponse(200, nxt)


@pytest.fixture
def settings(tmp_path):
    return Settings(udl_base="https://udl.test", udl_user="u", udl_pass="pw-should-not-appear",
                    storage_path=tmp_path, max_results=500)


@pytest.fixture
def instant_bucket():
    """A bucket with a fake clock, so rate-limit waits never sleep in tests."""
    now = [0.0]

    def sleep(delay):
        now[0] += delay

    return TokenBucket(600, clock=lambda: now[0], sleep=sleep)


@pytest.fixture
def client(settings, instant_bucket):
    from timeslides.udl import UDLClient

    def build(responses=None, raises=None):
        session = FakeSession(responses, raises)
        c = UDLClient(settings, session=session, bucket=instant_bucket)
        return c, session

    return build


def sv_record(epoch="2026-06-24T00:00:00.000Z", frame="J2000", n=1.0):
    return {"epoch": epoch, "xpos": n, "ypos": 2 * n, "zpos": 3 * n,
            "xvel": 4 * n, "yvel": 5 * n, "zvel": 6 * n,
            **({"referenceFrame": frame} if frame else {})}


# --------------------------------------------------------------------------- #
#  Asking git a question, in a place that may not have git
#
#  One implementation, imported by every test that needs it. There were two,
#  and the copy without the try/except raised FileNotFoundError inside a
#  skipif decorator, which is evaluated at import. A failure there is not one
#  test failing: it is a collection error, and pytest abandons the whole run.
#  684 passing tests never executed because of it.
# --------------------------------------------------------------------------- #
def in_git_worktree(root=None) -> bool:
    """True only if git is installed AND this is a work tree.

    Two separate things can be missing, and both are normal here:

    ● the .git directory, because the App Store artefact is unpacked rather
      than cloned;
    ● the git binary itself, because the pipeline's job container is
      python:3.12-slim and the checkout is done by a different container. The
      tree has .git in it and no git to read it with.

    Returns False for either, and never raises. A helper used to decide whether
    to skip must not be able to fail: it runs before any test does.
    """
    import subprocess
    from pathlib import Path

    root = root or Path(__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                              cwd=root, capture_output=True, text=True,
                              check=False)
    except OSError:
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


# --------------------------------------------------------------------------- #
#  The environment, made visible
#
#  Three pipeline failures turned on environment differences that were
#  invisible from here, so every run prints what it can see into the test
#  stage's log, which is the one part of the platform we can read.
#
#  It no longer rewrites the coverage report. An earlier version normalised the
#  report's source roots and published copies of it, on the theory that the
#  scanner could not resolve the paths. That theory was wrong: the gate read
#  79.2% from a report in the plain form pytest-cov writes, and the coverage
#  configuration was byte-identical when it later read 0.0%. Changing a working
#  artefact on an unproven theory cost four uploads. The report is now left
#  exactly as pytest-cov writes it.
# --------------------------------------------------------------------------- #
DEFAULT_REPORT_COPY = "coverage-reports/coverage-timeslides.xml"


def _coverage_report_path(config):
    """Where pytest-cov was told to write the XML, or None if it was not."""
    reports = getattr(config.option, "cov_report", None) or {}
    if "xml" not in reports:
        return None
    from pathlib import Path
    return Path(reports["xml"] or "coverage.xml")


def _report_completeness(path):
    """(number of files, line-rate) for a report, or None if unreadable."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    return len(list(root.iter("class"))), float(root.get("line-rate") or 0)


def _describe_report(path) -> list:
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    names = [c.get("filename") for c in root.iter("class")]
    roots = [(s.text or "") for s in root.iter("source")]
    absolute = [n for n in names if n and n.startswith("/")]
    return [
        f"  line-rate         {root.get('line-rate')} "
        f"({root.get('lines-covered')} of {root.get('lines-valid')} lines)",
        f"  files in report   {len(names)}",
        f"  source roots      {roots}",
        f"  first filename    {names[0] if names else '(none)'}",
        f"  absolute paths    {len(absolute)}",
    ]


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Runs after pytest-cov has written its report. Never raises.

    A hook that can fail is a hook that can take the suite down with it, which
    has already happened once in this repository, so every step here is
    guarded and reports its own failure as a line of text.
    """
    import os
    from pathlib import Path

    write = terminalreporter.write_line
    write("")
    write("=" * 72)
    write("  Timeslides environment report (printed so the pipeline is visible)")
    write("=" * 72)

    try:
        write(f"  working directory {Path.cwd()}")
        # Every file this app commits that the scan needs. sonar-project.
        # properties is committed here and was ABSENT from the pipeline's tree,
        # which is how we know the ingest does not carry everything in the
        # upload. These lines say whether the coverage reports survived it.
        for name in ("pytest.ini", ".coveragerc", "sonar-project.properties",
                     "coverage.xml", DEFAULT_REPORT_COPY,
                     "requirements.txt", "Dockerfile", ".git"):
            write(f"  {name:38} {'present' if Path(name).exists() else 'ABSENT'}")
        write(f"  {'git binary':38} "
              f"{'present' if in_git_worktree() else 'ABSENT or not a work tree'}")
    except OSError as exc:
        write(f"  could not inspect the working directory: {exc}")

    try:
        report = _coverage_report_path(config)
        if report is None:
            write("  coverage xml              NOT REQUESTED by this invocation")
        elif not report.exists():
            write(f"  coverage xml              MISSING at {report}")
        else:
            write(f"  coverage xml              {report.resolve()}")
            for line in _describe_report(report):
                write(line)
    except (OSError, ValueError) as exc:
        write(f"  could not inspect the coverage report: {exc}")

    # Names only for anything that could carry a credential. The values of the
    # few listed here are paths and identifiers, and none of them is a secret.
    safe = ("CI_PROJECT_DIR", "CI_JOB_NAME", "CI_COMMIT_SHA", "CI_PROJECT_PATH")
    for name in safe:
        if name in os.environ:
            write(f"  {name:25} {os.environ[name]}")
    # Anything that configures the scan is worth naming. Everything else is
    # noise, and anything that could carry a credential is not printed at all,
    # not even its name, because a variable name in a log is still a hint.
    configuring = sorted(k for k in os.environ
                         if ("SONAR" in k.upper() or "COVERAGE" in k.upper())
                         and not any(s in k.upper() for s in
                                     ("TOKEN", "SECRET", "PASSWORD", "KEY")))
    for name in configuring:
        write(f"  {name:25} {os.environ[name]}")
    write(f"  SONAR_TOKEN               "
          f"{'set' if os.environ.get('SONAR_TOKEN') else 'not set'}")
    write("=" * 72)


# --------------------------------------------------------------------------- #
#  A file the ingest may not have carried
#
#  THE CHAIN THAT MATTERS
#
#  A failing test stage means GitLab uploads no artefacts, which means the scan
#  stage receives no coverage report, which means the gate reads 0.0%. Two of
#  the gate failures in this project's history are exactly that: the test stage
#  failed, and the coverage number was collateral rather than the fault.
#
#  The first of those failures was a test asserting that
#  sonar-project.properties exists. It is committed here and it was not in the
#  pipeline's tree, so the ingest does not carry everything in an upload. Any
#  test that reads a repository file can therefore fail for a reason that has
#  nothing to do with the code, and take the coverage report down with it.
#
#  So a structural check whose subject is missing skips, loudly, naming the
#  file. The reason is visible in the log and the suite still passes, so the
#  artefact still ships. What must never skip is a check about the application
#  itself; this is only for files that describe the repository.
# --------------------------------------------------------------------------- #
def repo_file(relative, why=""):
    """Return the path, or skip this test saying which file is absent."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / relative
    if not path.exists():
        import pytest as _pytest
        _pytest.skip(
            f"{relative} is not in this tree, so this check has no subject. "
            "The App Store ingest does not carry every file in an upload: "
            "sonar-project.properties is committed here and was absent from "
            "the pipeline. Skipped rather than failed, because a failing test "
            "stage uploads no artefacts and the coverage report never reaches "
            f"the scan. {why}".strip())
    return path
