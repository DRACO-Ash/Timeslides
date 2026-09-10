"""Configuration, entirely from environment variables.

The original script resolved credentials from environment variables, then a
local ini file, then an interactive prompt, and offered to save what you typed.
All three of the later options are gone:

  * A file on disk is the thing we are moving away from.
  * An interactive prompt in a container does not prompt anybody. It blocks on
    a stdin that never delivers, the readiness probe fails, and the platform
    restarts the pod in a loop that looks like a crash and is actually a
    question nobody can answer.

So: environment variables only, validated once at boot, failing closed. A pod
with no UDL credentials refuses to start rather than starting healthy and
failing every request.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

# The App Store sets containerPort 8080 and probes it. PORT is read with 8080 as
# the default and is never set as an ENV in the image.
DEFAULT_PORT = 8080
DEFAULT_UDL_BASE = "https://unifieddatalibrary.com"
DEFAULT_STORAGE = "/data"

# Listen address. The empty string is the address-family-agnostic form of
# "every interface", which is what socket.bind(("", port)) means and what
# uvicorn passes through; verified to produce LISTEN on 0.0.0.0:8080.
#
# The platform requires this. It sets containerPort 8080 and probes the pod's
# own address, so a container listening only on loopback builds cleanly, passes
# every test, and then fails every probe. It is exposed as HOST so the address
# is configuration rather than a hardcoded decision, and so a local run can
# narrow it to 127.0.0.1 if you want that.
DEFAULT_HOST = ""


# The storage path is the one environment variable the application then builds
# filesystem paths from, so it is validated at the boundary rather than trusted
# all the way down to the writes. It has to be an absolute, normalised path
# with no traversal segment: that is what the platform injects
# (STORAGE_MOUNT_PATH=/data), and anything else is a misconfiguration worth
# failing closed on.
#
# Checked against a character set rather than a regex. The first version used
# r"^/(?:[A-Za-z0-9._][A-Za-z0-9._-]*/?)*$", whose nested quantifier is
# ambiguous: a run of allowed characters can be divided between the inner and
# outer repetitions in exponentially many ways, so a value that fails at the
# end backtracks through all of them. Measured on this machine at roughly four
# times the work per added character, reaching 2.8 seconds at 26. A set
# membership test cannot backtrack at all and reads more plainly besides.
_PATH_CHARS = frozenset(string.ascii_letters + string.digits + "._-")


def _path_problem(candidate: str) -> str:
    """Why this is not a usable storage path, or an empty string if it is."""
    if not candidate.startswith("/"):
        return "must be an absolute path"
    if candidate.startswith("//"):
        # POSIX leaves a leading double slash implementation-defined, and
        # PurePosixPath preserves it rather than collapsing it, so the path
        # would not be the one the operator meant.
        return "must not begin with a double slash"
    for segment in candidate.split("/"):
        if segment in ("", "."):
            continue                       # a trailing slash, or a no-op segment
        if segment == "..":
            return "must not contain a traversal segment"
        if not set(segment) <= _PATH_CHARS:
            return ("must use only letters, digits, dot, dash and underscore "
                    "in each segment")
    return ""


def _storage_path(raw) -> Path:
    candidate = (raw or DEFAULT_STORAGE).strip() or DEFAULT_STORAGE
    problem = _path_problem(candidate)
    if problem:
        raise ConfigError(f"STORAGE_MOUNT_PATH {problem}, got {candidate!r}")
    # Path collapses the remaining harmless noise: a trailing slash, a "."
    # segment, a repeated separator. ".." is the only segment that could climb
    # out, and it is refused above rather than normalised away, because a
    # STORAGE_MOUNT_PATH containing one is a misconfiguration to surface, not
    # something to quietly reinterpret.
    return Path(candidate)


def _flag(env, name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(env, name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    """Everything the application reads from its environment.

    ``udl_pass`` is deliberately excluded from the dataclass repr so that a
    settings object landing in a log line or a traceback cannot leak it.
    """

    udl_base: str = DEFAULT_UDL_BASE
    udl_user: str = ""
    udl_pass: str = field(default="", repr=False)
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    storage_path: Path = Path(DEFAULT_STORAGE)
    classification: str = "UNCLASSIFIED"
    demo: bool = False
    # Requests per minute allowed against the UDL. The original script had no
    # limiting of any kind; one run fans out to sats x providers x modes calls.
    udl_rate_per_min: int = 60
    udl_timeout_s: int = 60
    max_results: int = 5000
    log_level: str = "INFO"

    @property
    def runs_path(self) -> Path:
        return self.storage_path / "runs"

    @property
    def groups_file(self) -> Path:
        return self.storage_path / "groups.json"

    def require_udl(self) -> tuple[str, str]:
        """Return the UDL credentials or fail closed."""
        if not (self.udl_user and self.udl_pass):
            raise ConfigError(
                "UDL_USER and UDL_PASS must both be set. This application reads "
                "credentials from the environment only; there is no config file "
                "and no prompt. Set TIMESLIDES_DEMO=1 to run on synthetic data "
                "with no credentials and no network."
            )
        return self.udl_user, self.udl_pass


def load_settings(env=None) -> Settings:
    """Build Settings from the environment. Called once at boot."""
    env = os.environ if env is None else env
    settings = Settings(
        udl_base=(env.get("UDL_BASE") or DEFAULT_UDL_BASE).rstrip("/"),
        udl_user=(env.get("UDL_USER") or "").strip(),
        udl_pass=env.get("UDL_PASS") or "",
        host=env.get("HOST", DEFAULT_HOST).strip(),
        port=_int(env, "PORT", DEFAULT_PORT, 1, 65535),
        storage_path=_storage_path(env.get("STORAGE_MOUNT_PATH")),
        classification=(env.get("CLASSIFICATION") or "UNCLASSIFIED").strip(),
        demo=_flag(env, "TIMESLIDES_DEMO"),
        udl_rate_per_min=_int(env, "UDL_RATE_PER_MIN", 60, 1, 600),
        udl_timeout_s=_int(env, "UDL_TIMEOUT_S", 60, 5, 600),
        max_results=_int(env, "UDL_MAX_RESULTS", 5000, 1, 20000),
        log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
    )
    if not settings.demo:
        settings.require_udl()                     # fail closed at boot
    return settings
