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
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

# The App Store sets containerPort 8080 and probes it. PORT is read with 8080 as
# the default and is never set as an ENV in the image.
DEFAULT_PORT = 8080
DEFAULT_UDL_BASE = "https://unifieddatalibrary.com"
DEFAULT_STORAGE = "/data"


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
        port=_int(env, "PORT", DEFAULT_PORT, 1, 65535),
        storage_path=Path(env.get("STORAGE_MOUNT_PATH") or DEFAULT_STORAGE),
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
