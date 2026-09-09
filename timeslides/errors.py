"""Typed exceptions.

The original script called ``sys.exit`` / raised ``SystemExit`` from deep inside
its data path. That is correct for a command-line tool and fatal for a service:
a request carrying a bad reference satellite would take the whole worker down
rather than returning a status code. Every one of those exits is now one of the
exceptions below, and the HTTP layer is the only place that decides what a
failure looks like to a caller.
"""

from __future__ import annotations


class TimeslidesError(Exception):
    """Base for every error this application raises deliberately."""

    status = 500


class ConfigError(TimeslidesError):
    """Missing or unusable configuration. Raised at boot so the pod fails
    closed rather than starting up and failing every request."""

    status = 500


class ValidationError(TimeslidesError):
    """Untrusted input rejected at the trust boundary. Never coerced."""

    status = 400


class NotFoundError(TimeslidesError):
    """A named group, run or object does not exist."""

    status = 404


class ConflictError(TimeslidesError):
    """A write carried a stale revision, so it was refused rather than
    silently overwriting a concurrent edit."""

    status = 409


class ComputeError(TimeslidesError):
    """The requested render cannot be produced from the available data, for
    example a reference object with no TLEs to anchor the waterfall."""

    status = 422


class UpstreamError(TimeslidesError):
    """The UDL was unreachable, refused the credentials, or answered with
    something this application cannot parse."""

    status = 502
