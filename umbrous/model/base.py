"""Shared model types and errors.

Every failure a model call can end in has its own error class with a short
`code` the page can show and a sentence a person can act on. None of them
trigger a retry on another model or provider: Umbrous reports, it never
silently switches.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional, Tuple


class ModelError(Exception):
    code = "model_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProviderUnavailable(ModelError):
    code = "ollama_unreachable"


class ModelNotInstalled(ModelError):
    code = "model_not_installed"


class ModelTimeout(ModelError):
    code = "timeout"


class StreamInterrupted(ModelError):
    code = "stream_interrupted"


class Cancelled(ModelError):
    code = "cancelled"


class ToolsUnsupported(ModelError):
    code = "tools_unsupported"


class ProviderError(ModelError):
    code = "provider_error"


@dataclass(frozen=True)
class ModelInfo:
    """What the adapter knows about one installed model."""

    name: str                       # exact name, e.g. "qwen2.5:7b"
    digest: str                     # content hash; changes when the model is re-pulled
    size_bytes: int = 0
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    context_length: Optional[int] = None      # from the model itself, if known
    advertised_capabilities: Tuple[str, ...] = ()  # what Ollama says (e.g. "tools"); not proof


@dataclass(frozen=True)
class Deadlines:
    """How long a model call may take. Seconds."""

    connect: float = 5.0
    first_token: float = 180.0   # includes loading the model into memory
    idle: float = 60.0           # longest gap between two pieces of the reply
    total: float = 600.0


@dataclass
class CancelToken:
    """Set from another thread to stop a running call. The adapter registers a
    closer so a blocked network read is interrupted immediately."""

    _event: threading.Event = field(default_factory=threading.Event)
    _closers: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            closers = list(self._closers)
        for close in closers:
            try:
                close()
            except Exception:
                pass

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def on_cancel(self, close) -> None:
        with self._lock:
            if self._event.is_set():
                run_now = True
            else:
                self._closers.append(close)
                run_now = False
        if run_now:
            close()


@dataclass(frozen=True)
class ChatDelta:
    text: str


@dataclass(frozen=True)
class ChatToolCalls:
    """The model asked for tools (M3). Each call is (name, arguments). Only a request:
    nothing runs until the gate says so."""
    calls: tuple


@dataclass(frozen=True)
class ChatDone:
    model: str                      # the model Ollama says actually answered
    prompt_tokens: Optional[int] = None
    reply_tokens: Optional[int] = None
    done_reason: str = ""
