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
