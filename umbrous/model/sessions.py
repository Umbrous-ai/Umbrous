"""Keep one provider conversation per task between its model steps.

A cloud provider charges far less for the unchanged start of a request when it
can serve it from its prompt cache, and a conversation that stays open keeps
that start unchanged. The run loop sends every step with the whole transcript;
an adapter uses a SessionPool to continue the task's open conversation instead,
sending only what came after the model's previous answer.

The rules are the ones reviewed for the ChatGPT provider (codex._Session):
  - a session belongs to the task that started it: its CancelToken (one per run
    of the run loop) is its owner, and nothing else may continue it, whatever the
    content; a request without a token never reuses one;
  - it is continued only when the request starts with exactly the transcript the
    conversation holds, followed by the model's own last answer;
  - it is kept only after a step that proposed tools, never after a failure,
    Stop or time limit; idle sessions close after `idle_s`, at most `size` wait;
  - `generation` changes on every park, claim, eviction and close(), so a stale
    idle timer cannot close a session someone else holds; parking happens under
    the lock close() takes, and a session made before a close() is never parked.
"""
from __future__ import annotations

import threading
from typing import Callable, List, Optional


def split(messages):
    """(leading system text, the rest, the rest without trailing system notes).

    Leading system text is the conversation's instructions. System notes at the
    end of a request (the progress record, "no more tools") are replaced every step
    by the run loop, so they are sent but never part of what must match."""
    lead = 0
    while lead < len(messages) and messages[lead].get("role") == "system":
        lead += 1
    rest = list(messages[lead:])
    end = len(rest)
    while end and rest[end - 1].get("role") == "system":
        end -= 1
    return "\n\n".join(m.get("content", "") for m in messages[:lead]), rest, rest[:end]


def answer_matches(message, expect) -> bool:
    """Is `message` the run loop's record of the answer (reply, calls) the model gave?"""
    reply, calls = expect
    got = [((c.get("function") or {}).get("name"), (c.get("function") or {}).get("arguments"))
           for c in message.get("tool_calls") or []]
    return message.get("role") == "assistant" and message.get("content") == reply and got == list(calls)


class Session:
    """One task's open conversation. `handle` is the adapter's own object (a process)."""

    def __init__(self, handle, close: Callable[[], None], key, owner, epoch: int) -> None:
        self.handle, self._close, self.key, self.owner, self.epoch = handle, close, key, owner, epoch
        self.stable: list = []          # the transcript the conversation already holds
        self.expect = None              # (reply, calls) the model gave last
        self.started = False            # the first turn (instructions and full transcript) was sent
        self.keep = False
        self.generation = 0
        self.timer: Optional[threading.Timer] = None
        self.closed = False

    def close(self) -> None:
        self.closed = True
        try:
            self._close()
        except Exception:
            pass

    def continuation(self, rest: list) -> list:
        """The messages after the model's previous answer (the part to send)."""
        return rest[len(self.stable) + 1:]

    def done_step(self, stable: list, reply: str, calls) -> None:
        """The step proposed tools: keep the conversation for the task's next step."""
        self.stable, self.expect, self.keep = stable, (reply, tuple(calls)), True


class SessionPool:
    def __init__(self, idle_s: float = 120.0, size: int = 4) -> None:
        self.idle_s, self.size = idle_s, size
        self._lock = threading.Lock()
        self._idle: List[Session] = []
        self._busy: set = set()
        self._epoch = 0

    def claim(self, key, stable: list, owner) -> Optional[Session]:
        if owner is None:
            return None
        with self._lock:
            for sess in list(self._idle):
                n = len(sess.stable)
                if (sess.owner is owner and sess.key == key and not sess.closed
                        and stable[:n] == sess.stable and len(stable) > n
                        and answer_matches(stable[n], sess.expect)):
                    self._idle.remove(sess)
                    sess.generation += 1
                    if sess.timer is not None:
                        sess.timer.cancel()
                    self._busy.add(sess)
                    sess.keep = False
                    return sess
        return None

    def open(self, make: Callable[[], tuple], key, owner) -> Session:
        """A new session: `make()` returns (handle, close)."""
        with self._lock:
            epoch = self._epoch
        handle, close = make()
        sess = Session(handle, close, key, owner, epoch)
        with self._lock:
            stale = epoch != self._epoch
            if not stale:
                self._busy.add(sess)
        if stale:                                  # close() ran meanwhile
            sess.close()
        return sess

    def _expire(self, sess: Session, generation: int) -> None:
        with self._lock:
            if sess.generation != generation or sess not in self._idle:
                return
            self._idle.remove(sess)
        sess.close()

    def release(self, sess: Session, keep: bool) -> None:
        extra, timer = [], None
        with self._lock:
            self._busy.discard(sess)
            if keep and not sess.closed and sess.epoch == self._epoch:
                sess.generation += 1
                timer = threading.Timer(self.idle_s, self._expire, (sess, sess.generation))
                timer.daemon = True
                sess.timer = timer
                self._idle.append(sess)
                if len(self._idle) > self.size:
                    extra = self._idle[:-self.size]
                    for old in extra:
                        self._idle.remove(old)
                        old.generation += 1
        if timer is not None:
            timer.start()
        else:
            sess.close()
        for old in extra:
            if old.timer is not None:
                old.timer.cancel()
            old.close()

    def close(self) -> None:
        """Close every session: idle ones now, busy ones too (their step then fails)."""
        with self._lock:
            self._epoch += 1
            victims = self._idle + list(self._busy)
            for sess in self._idle:
                sess.generation += 1
                if sess.timer is not None:
                    sess.timer.cancel()
            self._idle = []
        for sess in victims:
            sess.close()
