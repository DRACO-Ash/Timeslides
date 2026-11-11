"""Structured logging: one JSON line per privileged action, never a secret.

The line format is the App Store logging standard, not one of our own:
one JSON object per line on stdout, with a millisecond UTC timestamp,
severity_text and severity_number (TRACE=1, DEBUG=5, INFO=9, WARN=13,
ERROR=17, FATAL=21), the message as body, and a flat attributes object.
Exceptions go in attributes as exception.type, exception.message and
exception.stacktrace. Nothing is written to stderr: the handler sits on the
root logger, so uvicorn's lines and any library's arrive in the same shape, and
warnings and uncaught exceptions are routed through it too. 1.8.1 was reported
writing 55 lines to stderr and none to stdout, because a fail-closed boot
raised before logging was configured and Python printed the traceback itself.

Two properties the original script did not need and this one does:

  * Log injection. Group names and search strings are user-supplied and end up
    in log fields. A name containing a newline can forge a whole log line and
    make an audit trail say whatever the author of the name wanted. Every
    untrusted value goes through `safe()`, which strips control characters and
    bounds the length.

  * Redaction. Nothing here ever takes a credential as a field, and `safe()`
    would not save us if it did, so the guard is that the password is not in
    the Settings repr and is never passed to a log call. The test suite asserts
    the password does not appear in emitted lines.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
import threading

MAX_FIELD = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

log = logging.getLogger("timeslides")


def safe(value, limit: int = MAX_FIELD) -> str:
    """Make an untrusted value fit to appear in a log field."""
    text = _CONTROL.sub(" ", str(value))
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


# (upper bound of Python level, severity_text, severity_number), lowest first.
_SEVERITY = (
    (logging.DEBUG - 1, "TRACE", 1),
    (logging.DEBUG, "DEBUG", 5),
    (logging.INFO, "INFO", 9),
    (logging.WARNING, "WARN", 13),
    (logging.ERROR, "ERROR", 17),
)


def severity(levelno: int) -> tuple[str, int]:
    """A Python level as the standard's (severity_text, severity_number)."""
    for ceiling, text, number in _SEVERITY:
        if levelno <= ceiling:
            return text, number
    return "FATAL", 21


def _flat(value):
    """Attributes must be a flat object: scalars stay, anything else is text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return json.dumps(value, default=str)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text, number = severity(record.levelno)
        attributes = {k: _flat(v) for k, v in (getattr(record, "fields", None) or {}).items()}
        if not record.name.startswith("timeslides"):
            attributes["logger.name"] = record.name
        if record.exc_info and record.exc_info[0] is not None:
            kind, value, _tb = record.exc_info
            attributes["exception.type"] = kind.__name__
            attributes["exception.message"] = safe(value, 1000)
            attributes["exception.stacktrace"] = self.formatException(record.exc_info)
        when = dt.datetime.fromtimestamp(record.created, dt.UTC)
        return json.dumps({
            "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
            "severity_text": text,
            "severity_number": number,
            "body": record.getMessage(),
            "attributes": attributes,
        }, default=str)


def _uncaught(kind, value, tb) -> None:
    """Python's own handler prints the traceback to stderr. This one logs it."""
    log.critical("process.uncaught_exception", exc_info=(kind, value, tb))


def _uncaught_in_thread(args) -> None:
    log.critical("thread.uncaught_exception",
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                 extra={"fields": {"thread.name": getattr(args.thread, "name", "")}})


def configure(level: str = "INFO") -> None:
    """One JSON handler, on stdout, on the root logger.

    Idempotent: called first thing in app.py, so that even a configuration
    error at boot is a JSON line on stdout, and again by create_app. Handlers
    other than ours are left alone, which is what lets a test harness keep its
    own capture handler.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers
                        if not isinstance(h.formatter, JsonFormatter)] + [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    ours = logging.getLogger("timeslides")
    ours.handlers[:] = []
    ours.setLevel(logging.NOTSET)
    ours.propagate = True
    _take_over_library_loggers()
    logging.captureWarnings(True)
    sys.excepthook = _uncaught
    threading.excepthook = _uncaught_in_thread


def _take_over_library_loggers() -> None:
    """Route any logger that already writes to a console stream through ours.

    The image starts with `python -m uvicorn app:app`, and uvicorn's launcher
    installs its own stderr handlers, with propagation off, before it imports
    the app. Left alone, its startup lines went to stderr in uvicorn's format
    while ours went to stdout in the standard's: found by running the built
    image the way the DAST stage does, after a test that launched app.py
    directly had passed. File handlers and other sinks are not touched.

    Walks the loggers that already exist and calls nothing that creates one,
    so the registry cannot change size underneath the loop and needs no copy
    taken first. Placeholders, names nothing has asked for yet, have no
    handlers and are skipped. (1.8.2 copied the keys with list() and looked
    each up with getLogger; the gate flagged the copy, python:S7504.)
    """
    for logger in logging.root.manager.loggerDict.values():
        if not isinstance(logger, logging.Logger):
            continue
        console = [h for h in logger.handlers if type(h) is logging.StreamHandler
                   and h.stream in (sys.stderr, sys.stdout)]
        if console:
            for h in console:
                logger.removeHandler(h)
            logger.propagate = True


def request_fields(request) -> dict:
    """What the standard asks a request line to carry, and nothing it forbids.

    The path without its query string; the platform's request id; and the
    authenticated email the platform's sign-on puts in X-Auth-Request-Email,
    which gives the audit trail the per-user attribution AUDIT.md recorded as
    missing. Never Authorization, Cookie, a token or a query string.
    """
    fields = {"http.request.method": request.method, "url.path": safe(request.url.path)}
    request_id = request.headers.get("x-request-id")
    if request_id:
        fields["http.request.header.x-request-id"] = safe(request_id, 100)
    email = request.headers.get("x-auth-request-email")
    if email:
        fields["user.email"] = safe(email, 200)
    return fields


def event(action: str, **fields) -> None:
    """Record one action. Values are sanitised; keys are ours, not the caller's."""
    log.info(action, extra={"fields": {k: safe(v) if isinstance(v, str) else v
                                       for k, v in fields.items()}})
