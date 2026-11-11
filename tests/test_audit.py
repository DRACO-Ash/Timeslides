"""Structured logging: no forged lines, no leaked secrets."""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import threading
import warnings

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
    assert lines[0]["body"] == "group.created"
    assert lines[0]["attributes"] == {"group": "SPIDER BABIES", "sats": 7}
    assert lines[0]["severity_text"] == "INFO"
    assert lines[0]["severity_number"] == 9
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", lines[0]["timestamp"])
    assert set(lines[0]) == {"timestamp", "severity_text", "severity_number", "body", "attributes"}


def test_a_newline_in_a_group_name_cannot_forge_a_log_line(captured):
    """The attack: name a group so the audit trail appears to contain an extra
    entry. The newline must not survive into the output as a line break."""
    audit.event("group.created", group='ok\n{"body": "group.deleted", "actor": "admin"}')
    lines = _lines(captured)
    assert len(lines) == 1
    assert lines[0]["body"] == "group.created"
    assert "\n" not in lines[0]["attributes"]["group"]
    assert "group.deleted" in lines[0]["attributes"]["group"]   # data, not a line


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


@pytest.fixture
def configured():
    """configure() changes process-wide state; put all of it back."""
    root = logging.getLogger()
    ours = logging.getLogger("timeslides")
    saved = (root.handlers[:], root.level, ours.handlers[:], ours.level, ours.propagate,
             sys.excepthook, threading.excepthook)
    yield root
    (root.handlers[:], root.level, ours.handlers[:], ours.level, ours.propagate,
     sys.excepthook, threading.excepthook) = saved
    logging.captureWarnings(False)


def test_configure_puts_exactly_one_json_handler_on_the_root_logger(configured):
    """On the root, so uvicorn's lines and any library's arrive in the same
    shape; exactly one, however often it is called; and other handlers, such
    as a test harness's, left in place."""
    keep = logging.NullHandler()
    configured.addHandler(keep)
    audit.configure("DEBUG")
    audit.configure("DEBUG")
    ours = [h for h in configured.handlers if isinstance(h.formatter, audit.JsonFormatter)]
    assert len(ours) == 1
    assert ours[0].stream is sys.stdout
    assert keep in configured.handlers
    assert configured.level == logging.DEBUG
    app_logger = logging.getLogger("timeslides")
    assert app_logger.handlers == [] and app_logger.propagate is True


def test_an_unknown_level_falls_back_to_info(configured):
    audit.configure("NONSENSE")
    assert configured.level == logging.INFO


def test_uncaught_exceptions_and_warnings_are_routed_through_the_log(configured):
    """Python prints both to stderr by default, which is exactly what the
    platform's logging check counts against an app."""
    audit.configure("INFO")
    assert sys.excepthook is audit._uncaught
    assert threading.excepthook is audit._uncaught_in_thread
    assert warnings.showwarning is not warnings._showwarning_orig


@pytest.mark.parametrize(("level", "text", "number"), [
    (5, "TRACE", 1), (logging.DEBUG, "DEBUG", 5), (logging.INFO, "INFO", 9),
    (logging.WARNING, "WARN", 13), (logging.ERROR, "ERROR", 17),
    (logging.CRITICAL, "FATAL", 21),
])
def test_severity_follows_the_standard_s_pairs(level, text, number):
    assert audit.severity(level) == (text, number)


def test_attributes_stay_flat(captured):
    audit.event("x", nested={"a": 1}, many=[1, 2], none=None, ok=True)
    attrs = _lines(captured)[0]["attributes"]
    assert attrs == {"nested": '{"a": 1}', "many": "[1, 2]", "none": None, "ok": True}


def test_a_library_s_line_names_its_logger(captured):
    other = logging.getLogger("uvicorn.error")
    other.addHandler(captured_handler := logging.StreamHandler(captured))
    captured_handler.setFormatter(audit.JsonFormatter())
    other.propagate, saved = False, other.propagate
    try:
        other.warning("something")
    finally:
        other.removeHandler(captured_handler)
        other.propagate = saved
    line = _lines(captured)[0]
    assert line["attributes"]["logger.name"] == "uvicorn.error"
    assert line["severity_text"] == "WARN"


def test_an_uncaught_exception_is_logged_as_fatal(captured):
    try:
        raise RuntimeError("at boot")
    except RuntimeError:
        audit._uncaught(*sys.exc_info())
    line = _lines(captured)[0]
    assert line["severity_text"] == "FATAL" and line["severity_number"] == 21
    assert line["attributes"]["exception.type"] == "RuntimeError"


def test_an_uncaught_exception_in_a_thread_names_the_thread(captured):
    class Args:
        exc_type, exc_value, exc_traceback = ValueError, ValueError("x"), None
        thread = threading.Thread(name="render-1")
    audit._uncaught_in_thread(Args)
    line = _lines(captured)[0]
    assert line["body"] == "thread.uncaught_exception"
    assert line["attributes"]["thread.name"] == "render-1"


class _Request:
    def __init__(self, headers, path="/api/runs", method="GET"):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.method = method
        self.url = type("U", (), {"path": path})()


def test_a_request_carries_its_id_and_the_signed_in_email():
    fields = audit.request_fields(_Request({"X-Request-Id": "abc-123",
                                            "X-Auth-Request-Email": "ash@example.org"}))
    assert fields == {"http.request.method": "GET", "url.path": "/api/runs",
                      "http.request.header.x-request-id": "abc-123",
                      "user.email": "ash@example.org"}


def test_a_request_without_those_headers_carries_neither():
    assert audit.request_fields(_Request({})) == {
        "http.request.method": "GET", "url.path": "/api/runs"}


def test_exception_info_is_included_when_logging_an_error(captured):
    log = logging.getLogger("timeslides")
    try:
        raise ValueError("boom")
    except ValueError:
        log.error("render.failed", exc_info=True)
    line = _lines(captured)[0]
    assert line["attributes"]["exception.type"] == "ValueError"
    assert line["attributes"]["exception.message"] == "boom"
    assert "ValueError: boom" in line["attributes"]["exception.stacktrace"]
    assert line["severity_text"] == "ERROR"


def test_a_library_logger_already_writing_to_stderr_is_taken_over(configured):
    """What uvicorn's launcher does before it imports the app."""
    lib = logging.getLogger("uvicorn.error.test-double")
    console = logging.StreamHandler(sys.stderr)
    elsewhere = logging.NullHandler()
    lib.addHandler(console)
    lib.addHandler(elsewhere)
    lib.propagate = False
    try:
        audit.configure("INFO")
        assert console not in lib.handlers
        assert elsewhere in lib.handlers, "only console handlers are ours to replace"
        assert lib.propagate is True
    finally:
        lib.handlers[:] = []
