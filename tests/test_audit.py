"""Structured logging: no forged lines, no leaked secrets."""

from __future__ import annotations

import io
import json
import logging

import pytest

from timeslides import audit
from timeslides.config import Settings


@pytest.fixture
def captured():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(audit.JsonFormatter())
    log = logging.getLogger("timeslides")
    old, old_level, old_prop = log.handlers[:], log.level, log.propagate
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    yield stream
    log.handlers[:], log.level, log.propagate = old, old_level, old_prop


def _lines(stream):
    return [json.loads(x) for x in stream.getvalue().strip().splitlines() if x]


def test_each_event_is_one_json_line(captured):
    audit.event("group.created", group="SPIDER BABIES", sats=7)
    lines = _lines(captured)
    assert len(lines) == 1
    assert lines[0]["msg"] == "group.created"
    assert lines[0]["group"] == "SPIDER BABIES"
    assert lines[0]["sats"] == 7
    assert lines[0]["level"] == "INFO"
    assert lines[0]["ts"].endswith("Z")


def test_a_newline_in_a_group_name_cannot_forge_a_log_line(captured):
    """The attack: name a group so the audit trail appears to contain an extra
    entry. The newline must not survive into the output as a line break."""
    audit.event("group.created", group='ok\n{"msg": "group.deleted", "actor": "admin"}')
    lines = _lines(captured)
    assert len(lines) == 1
    assert lines[0]["msg"] == "group.created"
    assert "\n" not in lines[0]["group"]
    assert "group.deleted" in lines[0]["group"]      # present as data, not as a line


@pytest.mark.parametrize("bad", ["\r", "\n", "\x00", "\x1b", "\x7f"])
def test_control_characters_are_stripped(bad):
    assert bad not in audit.safe(f"a{bad}b")


def test_long_fields_are_bounded():
    out = audit.safe("x" * 5000)
    assert len(out) == audit.MAX_FIELD + 3
    assert out.endswith("...")


def test_a_settings_object_in_a_log_field_does_not_leak_the_password(captured):
    """Settings can plausibly end up in a log field by accident. Its repr must
    not carry the credential."""
    audit.event("boot", config=str(Settings(udl_user="u", udl_pass="hunter2")))
    assert "hunter2" not in captured.getvalue()


def test_configure_installs_exactly_one_json_handler():
    audit.configure("DEBUG")
    log = logging.getLogger("timeslides")
    try:
        assert len(log.handlers) == 1
        assert isinstance(log.handlers[0].formatter, audit.JsonFormatter)
        assert log.level == logging.DEBUG
        assert log.propagate is False
    finally:
        log.handlers[:] = []


def test_an_unknown_level_falls_back_to_info():
    audit.configure("NONSENSE")
    log = logging.getLogger("timeslides")
    try:
        assert log.level == logging.INFO
    finally:
        log.handlers[:] = []


def test_exception_info_is_included_when_logging_an_error(captured):
    log = logging.getLogger("timeslides")
    try:
        raise ValueError("boom")
    except ValueError:
        log.error("render.failed", exc_info=True)
    line = _lines(captured)[0]
    assert "ValueError: boom" in line["exc"]
