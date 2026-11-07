"""The M1 run loop: one chat message in, one streamed reply out.

    page ──message──▶ run_chat()
                        1. resolve the chosen model (installed? no fallback)
                        2. assemble context (system + recent turns + message, within budget)
                        3. stream the reply from the adapter, passing pieces on as events
                        4. record the turn in this chat's memory

Events (one JSON object per line on the wire):
    start  {run_id, model_requested, context}
    delta  {text}
    done   {model, prompt_tokens, reply_tokens, estimated_prompt_tokens, ms, done_reason}
    error  {code, message, partial}

Chats live in memory only until M2 adds history: restarting the runtime
forgets them. Turn contents are never logged; logs get ids, counts and timings.

What is remembered after a failure: if any reply text arrived (stopped,
timed out, interrupted), the message and the partial reply are kept and the
partial reply is marked as incomplete for the page. If nothing arrived, the
message is not kept, so sending it again does not duplicate it.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Iterator, List

from umbrous.config import Config
from umbrous.context.assemble import MessageTooLong, Turn, assemble
from umbrous.logging_setup import log_event
from umbrous.model.base import CancelToken, Cancelled, ChatDelta, ChatDone, Deadlines, ModelError
from umbrous.model.router import ModelRouter

log = logging.getLogger("umbrous.runloop")

MAX_MESSAGE_CHARS = 32_000
MAX_CHATS = 100
MAX_TURNS_PER_CHAT = 500
CHAT_ID_RE = re.compile(r"^chat_[A-Za-z0-9_-]{8,64}$")


class ChatBusy(Exception):
    code = "chat_busy"
    message = "This chat is already waiting for a reply. Stop it or wait for it to finish."


@dataclass
class Chat:
    chat_id: str
    turns: List[Turn] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class ChatStore:
    """In-memory chats (M2 replaces this with persistent history)."""

    def __init__(self) -> None:
        self._chats: "OrderedDict[str, Chat]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, chat_id: str) -> Chat:
        if not isinstance(chat_id, str) or not CHAT_ID_RE.match(chat_id):
            raise ValueError("chat_id must look like chat_<8 to 64 letters, digits, - or _>")
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None:
                chat = Chat(chat_id)
                self._chats[chat_id] = chat
                while len(self._chats) > MAX_CHATS:
                    self._chats.popitem(last=False)
            self._chats.move_to_end(chat_id)
            return chat


class RunRegistry:
    """Running replies by run id, so a separate request can stop one."""

    def __init__(self) -> None:
        self._runs: Dict[str, CancelToken] = {}
        self._lock = threading.Lock()

    def start(self) -> "tuple[str, CancelToken]":
        run_id = "run_" + secrets.token_hex(8)
        token = CancelToken()
        with self._lock:
            self._runs[run_id] = token
        return run_id, token

    def finish(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            token = self._runs.get(run_id)
        if token is None:
            return False
        token.cancel()
        return True


def deadlines_from(config: Config) -> Deadlines:
    return Deadlines(first_token=config.first_token_timeout_s, idle=config.idle_timeout_s,
                     total=config.total_timeout_s)


def run_chat(config: Config, router: ModelRouter, store: ChatStore, runs: RunRegistry,
             chat_id: str, model_name: str, message: str) -> Iterator[dict]:
    if not isinstance(message, str) or not message.strip():
        yield {"type": "error", "code": "empty_message", "message": "Type a message first.", "partial": False}
        return
    if len(message) > MAX_MESSAGE_CHARS:
        yield {"type": "error", "code": "message_too_long", "partial": False,
               "message": f"Messages are limited to {MAX_MESSAGE_CHARS:,} characters."}
        return
    try:
        chat = store.get(chat_id)
    except ValueError as exc:
        yield {"type": "error", "code": "bad_chat_id", "message": str(exc), "partial": False}
        return
    if not chat.lock.acquire(blocking=False):
        yield {"type": "error", "code": ChatBusy.code, "message": ChatBusy.message, "partial": False}
        return

    run_id, cancel = runs.start()
    started = time.monotonic()
    outcome, text_parts = "error", []
    try:
        try:
            model = router.resolve(model_name)
            plan = assemble(config.system_prompt, chat.turns, message, recent_turns=config.recent_turns,
                            context_tokens=config.context_tokens, reply_tokens=config.reply_tokens,
                            model_context=model.context_length)
        except (ModelError, MessageTooLong) as exc:
            outcome = exc.code
            yield {"type": "error", "code": exc.code, "message": exc.message, "partial": False}
            return

        log_event(log, "run.start", run_id=run_id, chat_id=chat_id, model=model.name,
                  turns_sent=plan.turns_included, est_tokens=plan.estimated_tokens, window=plan.window)
        yield {"type": "start", "run_id": run_id, "model_requested": model.name, "context": plan.summary()}

        options = {"num_ctx": plan.window, "num_predict": plan.reply_tokens}
        try:
            for piece in router.adapter.chat_stream(model.name, plan.messages, options=options,
                                                    cancel=cancel, deadlines=deadlines_from(config)):
                if isinstance(piece, ChatDelta):
                    text_parts.append(piece.text)
                    yield {"type": "delta", "text": piece.text}
                elif isinstance(piece, ChatDone):
                    if not "".join(text_parts).strip():
                        outcome = "empty_response"
                        detail = ("The model reached its output limit before producing an answer. "
                                  "Try a shorter request or increase reply_tokens."
                                  if piece.done_reason == "length" else
                                  "The model returned no answer text. Try again or choose another model.")
                        yield {"type": "error", "code": outcome, "message": detail, "partial": False}
                        return
                    outcome = "ok"
                    chat.turns.append(Turn("user", message))
                    chat.turns.append(Turn("assistant", "".join(text_parts)))
                    yield {"type": "done", "model": piece.model, "prompt_tokens": piece.prompt_tokens,
                           "reply_tokens": piece.reply_tokens, "estimated_prompt_tokens": plan.estimated_tokens,
                           "ms": int((time.monotonic() - started) * 1000), "done_reason": piece.done_reason}
        except ModelError as exc:
            outcome = exc.code
            partial = bool(text_parts)
            if partial:
                chat.turns.append(Turn("user", message))
                chat.turns.append(Turn("assistant", "".join(text_parts)))
            yield {"type": "error", "code": exc.code, "message": exc.message, "partial": partial}
        except GeneratorExit:
            # The page went away mid-reply: stop the model call too.
            cancel.cancel()
            outcome = Cancelled.code
            raise
    finally:
        del chat.turns[:-MAX_TURNS_PER_CHAT]
        runs.finish(run_id)
        chat.lock.release()
        log_event(log, "run.end", run_id=run_id, chat_id=chat_id, outcome=outcome,
                  reply_chars=sum(len(t) for t in text_parts), ms=int((time.monotonic() - started) * 1000))
