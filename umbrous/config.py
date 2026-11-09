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
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_SYSTEM_PROMPT = (
    "You are Umbrous, a helpful assistant running locally on the user's computer. "
    "Answer clearly and concisely."
)
_ALLOWED_KEYS = {
    "host", "port", "log_level", "open_browser",
    "codex_enabled", "claude_enabled",
    # M1: local model chat
    "ollama_url", "system_prompt", "recent_turns", "context_tokens", "reply_tokens",
    "first_token_timeout_s", "idle_timeout_s", "total_timeout_s",
    # M4: task limits
    "task_timeout_s", "task_max_steps", "task_max_tool_calls", "tool_timeout_s",
    # M6: memory retrieval limits
    "memory_max_items", "memory_tokens",
    # M7: suggestions for memory after a task
    "memory_suggestions",
    # M9: coding tasks (a profile that may change files or run commands)
    "code_task_timeout_s", "code_max_steps", "code_max_tool_calls", "command_timeout_s", "command_timeout_max_s",
}
# Integer settings: (minimum, maximum)
_INT_RANGES = {
    "recent_turns": (1, 500),
    "context_tokens": (512, 1_048_576),
    "reply_tokens": (64, 65_536),
    "first_token_timeout_s": (1, 3600),
    "idle_timeout_s": (1, 3600),
    "total_timeout_s": (1, 86_400),
    "task_timeout_s": (1, 86_400),
    "task_max_steps": (1, 50),
    "task_max_tool_calls": (1, 400),
    "tool_timeout_s": (1, 3600),
    "memory_max_items": (0, 50),
    "memory_tokens": (0, 8192),
    "code_task_timeout_s": (1, 86_400),
    "code_max_steps": (1, 200),
    "code_max_tool_calls": (1, 1000),
    "command_timeout_s": (1, 86_400),
    "command_timeout_max_s": (1, 86_400),
}
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
    codex_enabled: bool = True   # discovery only; cloud inference requires selecting a codex/ model
    claude_enabled: bool = True  # discovery only; Claude inference requires selecting a claude/ model
    # M1: local model chat. See docs/m1-chat.md.
    ollama_url: str = DEFAULT_OLLAMA_URL   # must be loopback; Umbrous never sends chats elsewhere
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    recent_turns: int = 20                 # at most this many earlier messages go to the model
    context_tokens: int = 8192             # context window asked of the model (capped by the model's own)
    reply_tokens: int = 1024               # part of the window kept free for the reply
    first_token_timeout_s: int = 180       # includes loading the model into memory
    idle_timeout_s: int = 60               # longest silence allowed mid-reply
    total_timeout_s: int = 600             # hard limit for one reply
    # M4: limits for a task (a reply that may use tools). See docs/m4-tasks.md.
    task_timeout_s: int = 600              # the whole task: every model step and tool action
    task_max_steps: int = 6                # model steps that may ask for tools
    task_max_tool_calls: int = 24          # tool calls in the whole task
    tool_timeout_s: int = 30               # one tool action, from when it starts running
    # M6: memory given to one reply. 0 turns memory off. See docs/m6-memory.md.
    memory_max_items: int = 8              # at most this many memory items per reply
    memory_tokens: int = 600               # and at most about this many tokens of them
    # M7: after a task, ask the model for things worth remembering; you review
    # each one before anything is written. See docs/m7-review.md.
    memory_suggestions: bool = True
    # M9: limits for a coding task (the project's profile may change files or
    # run commands). The same mechanism as task_*; larger, because inspecting,
    # editing, testing and fixing takes more steps. See docs/m9-coding.md.
    code_task_timeout_s: int = 3600        # the whole coding task
    code_max_steps: int = 40               # model steps that may ask for tools
    code_max_tool_calls: int = 120         # tool calls in the whole task
    command_timeout_s: int = 120           # a command's default time limit (run_command timeout_s)
    command_timeout_max_s: int = 1800      # the longest a command may be given (a full build)

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


def ensure_loopback_url(url: str) -> str:
    """Ollama must be reached on this machine's loopback address over plain HTTP.

    This is what "local model" means in M1: chat text never leaves the machine.
    Hostnames (even "localhost") are refused because they can resolve elsewhere.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "http" or parts.username or parts.password or parts.path not in ("", "/") \
            or parts.query or parts.fragment:
        raise ConfigError(f"ollama_url must look like http://127.0.0.1:11434, got {url!r}")
    try:
        port = parts.port
    except ValueError:
        raise ConfigError(f"ollama_url has an invalid port: {url!r}") from None
    host = parts.hostname or ""
    try:
        ensure_loopback(host)
    except ConfigError:
        raise ConfigError(f"ollama_url must point at 127.0.0.1 (local only), got {url!r}") from None
    return f"http://127.0.0.1:{port or 11434}"


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
    for key, (lo, hi) in _INT_RANGES.items():
        if key in out:
            v = out[key]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ConfigError(f"{key} must be an integer from {lo} to {hi}, got {v!r}")
    if "ollama_url" in out:
        out["ollama_url"] = ensure_loopback_url(str(out["ollama_url"]))
    if "system_prompt" in out and (not isinstance(out["system_prompt"], str) or len(out["system_prompt"]) > 8000):
        raise ConfigError("system_prompt must be text of at most 8000 characters")
    if "open_browser" in out and not isinstance(out["open_browser"], bool):
        raise ConfigError("open_browser must be true or false")
    if "memory_suggestions" in out and not isinstance(out["memory_suggestions"], bool):
        raise ConfigError("memory_suggestions must be true or false")
    for flag in ("codex_enabled", "claude_enabled"):
        if flag in out and not isinstance(out[flag], bool):
            raise ConfigError(f"{flag} must be true or false")
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
