"""Thread-safe chat ownership and cancellation, independent of model providers."""

import secrets
import threading
from typing import Dict
from umbrous.model.base import CancelToken


class ChatLocks:
    """One lock per chat id: a chat waits for at most one reply at a time."""

    def __init__(self) -> None:
        self._locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, chat_id: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(chat_id, threading.Lock())

    def forget(self, chat_ids) -> None:
        """M8: a reset chat no longer exists; drop its lock."""
        with self._lock:
            for cid in chat_ids:
                self._locks.pop(cid, None)


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

    def cancel_all(self) -> None:
        with self._lock:
            tokens = list(self._runs.values())
        for token in tokens:
            token.cancel()
