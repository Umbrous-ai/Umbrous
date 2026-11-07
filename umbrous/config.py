"""Runtime configuration.

Configuration comes from three places, later ones winning:

1. Built-in defaults (below).
2. An optional JSON file at <home>/config.json.
3. Explicit overrides (command-line flags, or UMBROUS_PORT in the environment).

The bind host is deliberately not really configurable: it must be 127.0.0.1.
The setting exists only so that a wrong value fails loudly instead of being
silently ignored.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8417
_ALLOWED_KEYS = {"host", "port", "log_level", "open_browser"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class ConfigError(ValueError):
    """Raised when configuration is invalid. The runtime refuses to start."""


@dataclass(frozen=True)
class Config:
    home: Path
    host: str = LOOPBACK_HOST
    port: int = DEFAULT_PORT  # 0 lets the OS pick a free port (used by tests)
    log_level: str = "INFO"
    open_browser: bool = True

    @property
    def db_path(self) -> Path:
        return self.home / "umbrous.db"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def browser_profile_dir(self) -> Path:
        return self.home / "browser-profile"


def default_home() -> Path:
    env = os.environ.get("UMBROUS_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Umbrous"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "umbrous"


def ensure_loopback(host: str) -> str:
    """Accept only the IPv4 loopback address. Hostnames are refused too,
    because a hostname can resolve to something other than loopback."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(f"host must be {LOOPBACK_HOST}, got a hostname: {host!r}") from None
    if str(addr) != LOOPBACK_HOST:
        raise ConfigError(f"host must be {LOOPBACK_HOST} (loopback only), got {host!r}")
    return LOOPBACK_HOST


def _validate(values: Mapping[str, Any]) -> dict:
    unknown = set(values) - _ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)}")
    out = dict(values)
    if "host" in out:
        out["host"] = ensure_loopback(str(out["host"]))
    if "port" in out:
        port = out["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ConfigError(f"port must be an integer from 0 to 65535, got {port!r}")
    if "log_level" in out:
        level = str(out["log_level"]).upper()
        if level not in _LOG_LEVELS:
            raise ConfigError(f"log_level must be one of {sorted(_LOG_LEVELS)}")
        out["log_level"] = level
    if "open_browser" in out and not isinstance(out["open_browser"], bool):
        raise ConfigError("open_browser must be true or false")
    return out


def load_config(home: Optional[Path] = None, overrides: Optional[Mapping[str, Any]] = None) -> Config:
    home = Path(home).expanduser() if home else default_home()
    cfg = Config(home=home)

    file_path = home / "config.json"
    if file_path.exists():
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read {file_path}: {exc}") from None
        if not isinstance(data, dict):
            raise ConfigError(f"{file_path} must contain a JSON object")
        cfg = replace(cfg, **_validate(data))

    env_port = os.environ.get("UMBROUS_PORT")
    if env_port:
        try:
            cfg = replace(cfg, **_validate({"port": int(env_port)}))
        except ValueError:
            raise ConfigError(f"UMBROUS_PORT must be a number, got {env_port!r}") from None

    if overrides:
        cfg = replace(cfg, **_validate({k: v for k, v in overrides.items() if v is not None}))
    return cfg


def make_private_dir(path: Path) -> Path:
    """Create a directory only the current user can read (mode 0700)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path
