"""`python -m timeslides` renders to a file with no server.

This entry point is what the pipeline simulation runs to prove the whole render
path works in the build container, so it needs to be exercised here too.
"""

from __future__ import annotations

import datetime as dt

import pytest

from timeslides.__main__ import main
from timeslides.errors import ConfigError, UpstreamError


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_MOUNT_PATH", str(tmp_path))
    monkeypatch.delenv("UDL_USER", raising=False)
    monkeypatch.delenv("UDL_PASS", raising=False)
    monkeypatch.delenv("TIMESLIDES_DEMO", raising=False)
    return tmp_path


def test_demo_mode_writes_a_report(env, monkeypatch, capsys):
    monkeypatch.setenv("TIMESLIDES_DEMO", "1")
    out = env / "report.html"
    assert main(["--out", str(out)]) == 0
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "PRC Spaceplane" in html
    assert "Wrote" in capsys.readouterr().out


def test_the_classification_is_taken_from_the_environment(env, monkeypatch):
    monkeypatch.setenv("TIMESLIDES_DEMO", "1")
    monkeypatch.setenv("CLASSIFICATION", "OFFICIAL SENSITIVE")
    out = env / "r.html"
    main(["--out", str(out)])
    assert "OFFICIAL SENSITIVE" in out.read_text(encoding="utf-8")


def test_missing_credentials_fail_closed(env):
    """Same rule as the server: no credentials, no run."""
    with pytest.raises(ConfigError, match="UDL_USER and UDL_PASS"):
        main([])


def test_an_invalid_mode_is_reported_not_a_traceback(env, monkeypatch, capsys):
    monkeypatch.setenv("TIMESLIDES_DEMO", "1")
    assert main(["--modes", "PRETEND"]) == 1
    assert "unknown data mode" in capsys.readouterr().err


def test_an_invalid_provider_is_reported(env, monkeypatch, capsys):
    monkeypatch.setenv("TIMESLIDES_DEMO", "1")
    assert main(["--sources", "acme"]) == 1
    assert "unknown state provider" in capsys.readouterr().err


def test_a_named_group_that_does_not_exist_exits_with_a_message(env, monkeypatch, capsys):
    monkeypatch.setenv("UDL_USER", "u")
    monkeypatch.setenv("UDL_PASS", "p")
    assert main(["--group", "no such group"]) == 2
    assert "no saved group matched" in capsys.readouterr().err


def test_the_live_path_seeds_the_store_and_selects_by_name(env, monkeypatch, capsys):
    """The live path is driven with a stub client so no network is touched."""
    monkeypatch.setenv("UDL_USER", "u")
    monkeypatch.setenv("UDL_PASS", "p")
    from tests.test_pipeline import FakeUDL
    fake = FakeUDL()
    monkeypatch.setattr("timeslides.udl.UDLClient", lambda settings: fake)
    out = env / "g.html"
    assert main(["--group", "PRC SpacePlane 4", "--out", str(out)]) == 0
    # The store was created and seeded from the old ini file's groups.
    assert (env / "groups.json").exists()
    # 99995 has no data in the fixture. That does not sink the group: a member
    # with no data contributes empty series and the panel still builds, which
    # is the behaviour the original had.
    assert "PRC SpacePlane 4" in out.read_text(encoding="utf-8")


def test_a_group_whose_reference_has_no_data_is_skipped_with_a_reason(env, monkeypatch, capsys):
    """A member with no data is survivable. A group where nothing can anchor
    the waterfall is not, and it is reported rather than crashing."""
    monkeypatch.setenv("UDL_USER", "u")
    monkeypatch.setenv("UDL_PASS", "p")
    from tests.test_pipeline import FakeUDL
    monkeypatch.setattr("timeslides.udl.UDLClient",
                        lambda settings: FakeUDL(empty_for=range(1, 100000)))
    assert main([]) == 1
    assert "no group produced any usable data" in capsys.readouterr().err


def test_an_upstream_failure_is_reported_as_a_message(env, monkeypatch, capsys):
    monkeypatch.setenv("UDL_USER", "u")
    monkeypatch.setenv("UDL_PASS", "p")

    class Broken:
        def objects_by_satno(self, sat_nos):
            raise UpstreamError("UDL rejected the credentials")

    monkeypatch.setattr("timeslides.udl.UDLClient", lambda settings: Broken())
    assert main([]) == 1
    assert "UpstreamError" in capsys.readouterr().err


def test_the_default_output_name_carries_the_window_end(env, monkeypatch, capsys):
    monkeypatch.setenv("TIMESLIDES_DEMO", "1")
    monkeypatch.chdir(env)
    assert main([]) == 0
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
    assert (env / f"phase_offset_{stamp}.html").exists()
