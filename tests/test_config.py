"""Configuration comes from the environment and fails closed."""

from __future__ import annotations

import pytest

from timeslides.config import DEFAULT_PORT, Settings, load_settings
from timeslides.errors import ConfigError


def test_missing_credentials_refuse_to_boot():
    """A pod with no credentials must not start healthy and then fail every
    request. It must not start."""
    with pytest.raises(ConfigError, match="UDL_USER and UDL_PASS"):
        load_settings({})


def test_a_username_without_a_password_is_still_a_refusal():
    with pytest.raises(ConfigError):
        load_settings({"UDL_USER": "someone"})


def test_demo_mode_needs_no_credentials():
    s = load_settings({"TIMESLIDES_DEMO": "1"})
    assert s.demo is True


def test_credentials_are_accepted_from_the_environment():
    s = load_settings({"UDL_USER": "u", "UDL_PASS": "p"})
    assert s.require_udl() == ("u", "p")


def test_defaults_match_the_app_store_runtime_contract():
    s = load_settings({"TIMESLIDES_DEMO": "1"})
    assert s.port == DEFAULT_PORT == 8080
    assert str(s.storage_path) == "/data"
    assert s.classification == "UNCLASSIFIED"


def test_storage_paths_hang_off_the_mount_point():
    s = load_settings({"TIMESLIDES_DEMO": "1", "STORAGE_MOUNT_PATH": "/mnt/x"})
    assert str(s.groups_file) == "/mnt/x/groups.json"
    assert str(s.runs_path) == "/mnt/x/runs"


def test_the_password_is_absent_from_the_settings_repr():
    """A Settings object in a log line or a traceback must not carry the secret."""
    s = Settings(udl_user="u", udl_pass="hunter2")
    assert "hunter2" not in repr(s)
    assert "u" in repr(s)


@pytest.mark.parametrize("value", ["abc", "", "  ", "1.5"])
def test_a_non_integer_port_is_rejected_not_silently_defaulted(value):
    env = {"TIMESLIDES_DEMO": "1", "PORT": value}
    if value.strip():
        with pytest.raises(ConfigError, match="PORT must be an integer"):
            load_settings(env)
    else:
        assert load_settings(env).port == DEFAULT_PORT


@pytest.mark.parametrize("value", ["0", "70000", "-1"])
def test_an_out_of_range_port_is_rejected(value):
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        load_settings({"TIMESLIDES_DEMO": "1", "PORT": value})


@pytest.mark.parametrize("raw,want", [("1", True), ("true", True), ("YES", True),
                                      ("on", True), ("0", False), ("no", False),
                                      ("", False), ("anything", False)])
def test_flags_accept_the_usual_spellings(raw, want):
    # Credentials are supplied so that a false flag does not fail the boot check.
    s = load_settings({"UDL_USER": "u", "UDL_PASS": "p", "TIMESLIDES_DEMO": raw})
    assert s.demo is want


def test_an_unset_flag_defaults_to_off():
    assert load_settings({"UDL_USER": "u", "UDL_PASS": "p"}).demo is False


def test_trailing_slash_on_the_base_url_is_normalised():
    s = load_settings({"TIMESLIDES_DEMO": "1", "UDL_BASE": "https://x.test/"})
    assert s.udl_base == "https://x.test"


def test_load_settings_does_not_mutate_the_process_environment():
    """load_settings takes an env mapping for testing. It must not reach into
    os.environ to do it, because the server is threaded."""
    import os
    before = dict(os.environ)
    load_settings({"TIMESLIDES_DEMO": "1", "PORT": "9999"})
    assert dict(os.environ) == before


# --------------------------------------------------------------------------- #
#  Packaging: what the upload artefact must carry for the gate to pass
# --------------------------------------------------------------------------- #
def _root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


def _in_git_worktree() -> bool:
    """The artefact is unzipped, not cloned, so git is not always present.

    The first version of these tests assumed it was and failed inside the
    extracted artefact, which is precisely the environment the platform runs
    them in. The substantive check does not need git; only the ignore-rule
    checks do, and those skip.
    """
    import subprocess
    try:
        done = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                              cwd=_root(), capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def test_the_coverage_configuration_ships_with_the_package():
    """The gate reads the coverage report, not the suite.

    A broad `*.ini` rule in .gitignore once swallowed pytest.ini, so the
    uploaded artefact carried no coverage configuration, the platform's pytest
    run emitted no coverage.xml, and a codebase at 99 per cent line coverage
    scored zero at the SonarQube gate. Found only by unzipping the artefact and
    running the tests inside it, which is why the pipeline simulation exists.

    This assertion runs wherever the tests run, including inside the extracted
    artefact, so a missing pytest.ini fails the test stage rather than passing
    it and failing the scan stage with an unexplained zero.
    """
    ini = _root() / "pytest.ini"
    assert ini.exists(), "pytest.ini is missing from the package"
    text = ini.read_text(encoding="utf-8")
    assert "--cov=timeslides" in text
    assert "--cov-report=xml" in text, "the gate needs coverage.xml"
    assert "browser" in text, "the browser marker must be registered"


@pytest.mark.skipif(not _in_git_worktree(), reason="not a git work tree")
def test_the_coverage_configuration_is_not_git_ignored():
    import subprocess
    ignored = subprocess.run(["git", "check-ignore", "pytest.ini"],
                             cwd=_root(), capture_output=True, text=True)
    assert ignored.returncode != 0, "pytest.ini is git-ignored and will not ship"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "pytest.ini"],
                             cwd=_root(), capture_output=True, text=True)
    assert tracked.returncode == 0, "pytest.ini is not tracked and will not ship"


@pytest.mark.skipif(not _in_git_worktree(), reason="not a git work tree")
def test_credential_files_are_still_ignored():
    """Narrowing the ignore rule must not have reopened the original hole."""
    import subprocess
    for name in ("credentials.ini", "secrets.ini", "udl-credentials.ini"):
        done = subprocess.run(["git", "check-ignore", name],
                              cwd=_root(), capture_output=True, text=True)
        assert done.returncode == 0, f"{name} is not ignored"


def test_the_platform_install_file_declares_the_test_runner():
    """The platform's test stage runs exactly two commands:

        pip install -r requirements.txt
        pytest --cov --cov-report=xml:coverage.xml

    It never reads requirements-dev.txt. Keeping pytest out of
    requirements.txt gave "pytest: command not found" and exit 127, and no
    amount of local green could have shown it, because locally the tooling was
    already installed. So the file the platform installs has to declare the
    runner the platform invokes.
    """
    text = (_root() / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest==" in text, "requirements.txt must pin pytest"
    assert "pytest-cov==" in text, "requirements.txt must pin pytest-cov"
    assert "httpx==" in text, "the FastAPI TestClient needs httpx"
    assert "-r requirements-runtime.txt" in text, "runtime deps must be included"


def test_the_runtime_install_file_carries_no_test_tooling():
    """The image installs requirements-runtime.txt, and the container scan
    judges what is in the image. Test tooling in there is surface for code that
    never runs in production."""
    text = (_root() / "requirements-runtime.txt").read_text(encoding="utf-8")
    for tool in ("pytest", "httpx", "playwright", "coverage"):
        assert tool not in text.lower(), f"{tool} does not belong in the image"
    for runtime in ("astropy", "fastapi", "numpy", "plotly", "sgp4", "uvicorn"):
        assert runtime in text, f"{runtime} is missing from the runtime set"


def test_the_dockerfile_installs_the_runtime_set_not_the_platform_set():
    text = (_root() / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements-runtime.txt" in text
    assert "pip install --no-cache-dir -r requirements.txt" not in text


def test_the_coverage_source_is_pinned_for_the_platforms_bare_cov():
    """`pytest --cov` with no value takes its source from configuration. Without
    this the platform's invocation measures whatever was imported and dilutes
    the figure the gate reads with test files and site-packages."""
    text = (_root() / ".coveragerc").read_text(encoding="utf-8")
    assert "source = timeslides" in text


def test_the_dockerfile_does_not_set_the_port():
    """The platform sets containerPort 8080 and probes it; the app reads PORT
    with 8080 as its default. Setting it in the image is how you end up serving
    on a port nothing probes."""
    # Comment lines are stripped first: the Dockerfile documents the absence
    # of ENV PORT in a comment, and matching that comment is not the same as
    # matching a directive.
    lines = [ln for ln in (_root() / "Dockerfile").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    directives = "\n".join(lines)
    assert "ENV PORT=" not in directives
    assert "PORT" not in directives.replace("--port", ""), \
        "PORT must be read with a default, never set in the image"
    assert "USER 1000:1000" in directives
