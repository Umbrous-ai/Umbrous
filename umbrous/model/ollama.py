"""The Ollama adapter: the only code that talks to Ollama's HTTP API.

Ollama runs on this machine and listens on 127.0.0.1:11434 by default. The
endpoints used:

    GET  /api/version   is it running, which version
    GET  /api/tags      installed models
    POST /api/show      one model's details: context length, capabilities
    POST /api/chat      a chat reply, streamed as one JSON object per line

Everything goes through `_request()`, which applies the connect timeout and
turns network failures into the errors in model/base.py. Streaming replies
read one line at a time with the socket timeout set to the first-token
deadline, then the idle deadline, and check the total deadline and the cancel
token between lines.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from dataclasses import replace
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import urlsplit

from umbrous.model.base import (
    CancelToken, Cancelled, ChatDelta, ChatDone, ChatToolCalls, Deadlines, ModelError, ModelInfo, ModelNotInstalled,
    ModelTimeout, ProviderError, ProviderUnavailable, StreamInterrupted, ToolsUnsupported,
)

MAX_LINE = 1_000_000  # one streamed line should be tiny; refuse absurd ones


def _abort(conn: http.client.HTTPConnection) -> None:
    """Interrupt a read blocked in another thread.

    Only shut the socket down here. shutdown() wakes the blocked read at once
    (it sees end-of-stream or an error); the reading thread then closes the
    connection itself. Closing it from this thread would race with the reader
    inside http.client.
    """
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _tool_call(raw: dict) -> tuple:
    """Ollama's {"function": {"name", "arguments"}} → (name, arguments). Arguments may
    arrive as an object or as JSON text; anything else is passed on for the gate to refuse."""
    fn = raw.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    return (fn.get("name"), args if args is not None else {})


class OllamaAdapter:
    provider = "ollama"

    def __init__(self, base_url: str = "http://127.0.0.1:11434", deadlines: Deadlines = Deadlines()) -> None:
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 11434
        self.base_url = f"http://{self.host}:{self.port}"
        self.deadlines = deadlines
        # digest -> (context_length, capabilities). Only the details are cached,
        # never a whole ModelInfo: two names can share a digest (ollama cp, or
        # "x" and "x:latest") and each must keep its own name.
        self._show_cache: Dict[str, Tuple[Optional[int], Tuple[str, ...]]] = {}

    # --- low level ---------------------------------------------------------
    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _unreachable(self, exc: BaseException) -> ProviderUnavailable:
        return ProviderUnavailable(
            f"Ollama is not answering at {self.base_url} ({exc.__class__.__name__}). "
            "Start the Ollama app or run `ollama serve`, then try again."
        )

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 timeout: Optional[float] = None, cancel=None) -> Tuple[int, Any]:
        """One non-streaming call. Returns (status, parsed JSON or text)."""
        conn = self._connect(self.deadlines.connect)
        if cancel is not None:
            if cancel.cancelled:
                raise Cancelled("Compatibility check cancelled.")
            cancel.on_cancel(lambda: _abort(conn))
        try:
            try:
                conn.connect()
            except OSError as exc:
                raise self._unreachable(exc) from None
            if cancel is not None and cancel.cancelled:
                raise Cancelled("Compatibility check cancelled.")
            conn.sock.settimeout(timeout or self.deadlines.first_token)
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            try:
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            except socket.timeout:
                raise ModelTimeout(f"Ollama did not answer {path} within {timeout or self.deadlines.first_token:.0f}s.") from None
            except OSError as exc:
                raise self._unreachable(exc) from None
            try:
                return resp.status, json.loads(raw) if raw else None
            except ValueError:
                return resp.status, raw.decode("utf-8", "replace")
        finally:
            conn.close()

    @staticmethod
    def _error_text(data: Any) -> str:
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            return data["error"]
        return str(data)[:300]

    def _raise_for(self, status: int, data: Any, model: str) -> None:
        text = self._error_text(data)
        if status == 404 or "not found" in text.lower():
            raise ModelNotInstalled(f"The model {model!r} is not installed in Ollama. Pull it with `ollama pull {model}`.")
        if "does not support tools" in text.lower():
            raise ToolsUnsupported(f"{model} does not support tool calls (Ollama: {text}).")
        raise ProviderError(f"Ollama returned HTTP {status}: {text}")

    # --- discovery -----------------------------------------------------------
    def version(self) -> str:
        status, data = self._request("GET", "/api/version", timeout=self.deadlines.connect)
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(f"Unexpected answer from Ollama /api/version (HTTP {status}).")
        return str(data.get("version", "unknown"))

    def list_models(self) -> List[ModelInfo]:
        status, data = self._request("GET", "/api/tags", timeout=10)
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(f"Unexpected answer from Ollama /api/tags (HTTP {status}).")
        models = []
        for m in data.get("models", []):
            name = m.get("model") or m.get("name")
            if not isinstance(name, str):
                continue
            details = m.get("details") or {}
            models.append(ModelInfo(
                name=name, digest=str(m.get("digest", "")), size_bytes=int(m.get("size") or 0),
                family=str(details.get("family", "")), parameter_size=str(details.get("parameter_size", "")),
                quantization=str(details.get("quantization_level", "")),
            ))
        return sorted(models, key=lambda x: x.name)

    def show(self, model: ModelInfo) -> ModelInfo:
        """Add context length and advertised capabilities. Details cached by digest."""
        cached = self._show_cache.get(model.digest)
        if cached is not None:
            ctx, caps = cached
            return replace(model, context_length=ctx, advertised_capabilities=caps)
        status, data = self._request("POST", "/api/show", {"model": model.name}, timeout=15)
        if status != 200 or not isinstance(data, dict):
            self._raise_for(status, data, model.name)
        ctx = None
        for key, value in (data.get("model_info") or {}).items():
            if key.endswith(".context_length") and isinstance(value, int):
                ctx = value
                break
        caps = tuple(c for c in data.get("capabilities") or [] if isinstance(c, str))
        if model.digest:
            self._show_cache[model.digest] = (ctx, caps)
        return replace(model, context_length=ctx, advertised_capabilities=caps)

    # --- chat ------------------------------------------------------------------
    def chat_once(self, model: str, messages: List[dict], tools: Optional[List[dict]] = None,
                  options: Optional[dict] = None, timeout: Optional[float] = None, cancel=None) -> dict:
        """A complete, non-streamed reply. Used by the tool-call probe."""
        body: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        if options:
            body["options"] = options
        status, data = self._request("POST", "/api/chat", body, timeout=timeout or self.deadlines.first_token, cancel=cancel)
        if status != 200 or not isinstance(data, dict):
            self._raise_for(status, data, model)
        if "error" in data:
            self._raise_for(status, data, model)
        return data

    def chat_stream(self, model: str, messages: List[dict], options: Optional[dict] = None,
                    cancel: Optional[CancelToken] = None,
                    deadlines: Optional[Deadlines] = None,
                    tools: Optional[List[dict]] = None) -> Iterator[Union[ChatDelta, ChatToolCalls, ChatDone]]:
        """Yield ChatDelta pieces as they arrive, then one ChatDone.

        Raises Cancelled, ModelTimeout, StreamInterrupted, ProviderUnavailable,
        ModelNotInstalled or ProviderError. Never retries.
        """
        d = deadlines or self.deadlines
        cancel = cancel or CancelToken()
        started = time.monotonic()
        conn = self._connect(min(d.connect, d.total))
        cancel.on_cancel(lambda: _abort(conn))
        # The total deadline is enforced by a timer, not only between lines: a
        # read blocked inside the socket is woken the moment time runs out.
        total_hit = threading.Event()

        def _total_expired() -> None:
            total_hit.set()
            _abort(conn)

        watchdog = threading.Timer(d.total, _total_expired)
        watchdog.daemon = True
        watchdog.start()

        def remaining() -> float:
            return d.total - (time.monotonic() - started)

        def bounded(phase: float) -> float:
            return max(0.001, min(phase, remaining()))

        def stopped(exc_msg: str) -> ModelError:
            """Decide what an interrupted operation really means."""
            if cancel.cancelled:
                return Cancelled("Stopped.")
            if total_hit.is_set() or remaining() <= 0:
                return ModelTimeout(f"The reply took longer than the {d.total:g}s limit and was stopped.")
            return StreamInterrupted(exc_msg)

        try:
            try:
                conn.connect()
            except OSError as exc:
                if cancel.cancelled or total_hit.is_set():
                    raise stopped("") from None
                raise self._unreachable(exc) from None
            if cancel.cancelled or total_hit.is_set():
                raise stopped("")
            conn.sock.settimeout(bounded(d.first_token))
            # M1 displays answers only; do not spend its reply budget on hidden thinking.
            body = {"model": model, "messages": messages, "stream": True,
                    "think": False, "options": options or {}}
            if tools:
                body["tools"] = tools
            try:
                conn.request("POST", "/api/chat", body=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
            except socket.timeout:
                if cancel.cancelled or remaining() <= 0:
                    raise stopped("") from None
                raise ModelTimeout(f"{model} did not start answering within {d.first_token:g}s.") from None
            except (OSError, http.client.HTTPException) as exc:
                if cancel.cancelled or total_hit.is_set():
                    raise stopped("") from None
                raise self._unreachable(exc) from None

            if resp.status != 200:
                raw = resp.read()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = raw.decode("utf-8", "replace")
                self._raise_for(resp.status, data, model)

            got_first = False
            while True:
                if cancel.cancelled or remaining() <= 0:
                    raise stopped("")
                conn.sock.settimeout(bounded(d.idle if got_first else d.first_token))
                try:
                    line = resp.readline(MAX_LINE + 1)
                except socket.timeout:
                    if cancel.cancelled or remaining() <= 0:
                        raise stopped("") from None
                    if got_first:
                        raise ModelTimeout(f"{model} went silent for more than {d.idle:g}s mid-reply.") from None
                    raise ModelTimeout(f"{model} did not start answering within {d.first_token:g}s.") from None
                except (OSError, http.client.HTTPException, ValueError) as exc:
                    raise stopped(f"The connection to Ollama broke mid-reply ({exc.__class__.__name__}).") from None
                if cancel.cancelled or total_hit.is_set():
                    raise stopped("")
                if not line:
                    raise StreamInterrupted("Ollama ended the reply early, without saying it was finished.")
                if len(line) > MAX_LINE:
                    raise StreamInterrupted("Ollama sent an unexpectedly large piece of data; stopped.")
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    raise StreamInterrupted("Ollama sent something that is not JSON mid-reply.") from None
                if not isinstance(chunk, dict):
                    raise StreamInterrupted("Ollama sent an unexpected reply shape.")
                if "error" in chunk:
                    raise ProviderError(f"Ollama reported an error mid-reply: {self._error_text(chunk)}")
                got_first = True
                message = chunk.get("message") or {}
                text = message.get("content") or ""
                if text:
                    yield ChatDelta(text)
                calls = message.get("tool_calls")
                if calls:
                    yield ChatToolCalls(tuple(_tool_call(c) for c in calls if isinstance(c, dict)))
                if chunk.get("done"):
                    yield ChatDone(
                        model=str(chunk.get("model") or model),
                        prompt_tokens=chunk.get("prompt_eval_count"),
                        reply_tokens=chunk.get("eval_count"),
                        done_reason=str(chunk.get("done_reason") or ""),
                    )
                    return
        finally:
            watchdog.cancel()
            conn.close()
