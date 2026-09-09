"""Structured logging: one JSON line per privileged action, never a secret.

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

import json
import logging
import re
import sys
import time

MAX_FIELD = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

log = logging.getLogger("timeslides")


def safe(value, limit: int = MAX_FIELD) -> str:
    """Make an untrusted value fit to appear in a log field."""
    text = _CONTROL.sub(" ", str(value))
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("timeslides")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False


def event(action: str, **fields) -> None:
    """Record one action. Values are sanitised; keys are ours, not the caller's."""
    log.info(action, extra={"fields": {k: safe(v) if isinstance(v, str) else v
                                       for k, v in fields.items()}})
