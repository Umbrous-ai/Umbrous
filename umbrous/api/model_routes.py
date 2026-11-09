"""ModelRoutes; authentication and body limits remain in the common dispatcher."""

from __future__ import annotations

import json
import logging
from typing import Tuple

from umbrous.logging_setup import log_event
from umbrous.runloop.chat import run_chat
from umbrous.api.contracts import BASE_HEADERS

log = logging.getLogger("umbrous.api")


class ModelRoutes:
    def _probe_job(self, path, body):
        try:
            if path.endswith("/start"):
                result = self.runtime.probes.start(str(body.get("model", "")))
            elif path.endswith("/cancel"):
                result = self.runtime.probes.cancel(str(body.get("id", "")))
            else:
                result = self.runtime.probes.view(str(body.get("id", "")))
        except KeyError:
            self._send_json(404, {"error": "no_such_probe", "message": "This check is no longer available. Retry compatibility."})
            return 404, "no_such_probe"
        except ValueError as exc:
            self._send_json(409, {"error": "probe_busy", "message": str(exc)})
            return 409, "probe_busy"
        self._send_json(200, result)
        return 200, "probe_status"

    def _probe(self, body: dict) -> Tuple[int, str]:
        from umbrous.model.base import ModelError
        try:
            result, model = self.runtime.router.probe(str(body.get("model", "")))
        except ModelError as exc:
            self._send_json(409, {"error": exc.code, "message": exc.message})
            return 409, exc.code
        self._send_json(200, {"model": model.name, "passed": result.passed, "stage": result.stage,
                              "detail": result.detail, "ms": result.ms})
        log_event(log, "model.probe", model=model.name, passed=result.passed, stage=result.stage, ms=result.ms)
        return 200, "probe_passed" if result.passed else "probe_failed"

    def _provider(self, path: str) -> Tuple[int, str]:
        from umbrous.model.base import ModelError
        if path in ("/api/providers/claude", "/api/providers/grok"):
            # Status only: sign-in happens in the vendor's own CLI (`claude auth login`, `grok login`).
            if self.command != "GET":
                self._send_json(405, {"error": "method_not_allowed"})
                return 405, "method_not_allowed"
            name = path.rsplit("/", 1)[1]
            adapter = getattr(self.runtime.router, name)
            out = adapter.status() if adapter is not None else {
                "available": False, "authenticated": False, "auth_type": None,
                "error": f"{name.title()} is disabled in this runtime ({name}_enabled)."}
            self._send_json(200, out)
            return 200, name + "_status"
        codex = self.runtime.router.codex
        login = path.endswith("/login")
        if self.command != ("POST" if login else "GET"):
            self._send_json(405, {"error": "method_not_allowed"})
            return 405, "method_not_allowed"
        if login and self._read_json() is None:
            self._send_json(400, {"error": "expected a JSON object with Content-Type: application/json"})
            return 400, "bad_json"
        if codex is None:
            self._send_json(409 if login else 200, {"available": False, "authenticated": False,
                            "auth_type": None, "error": "ChatGPT is disabled in this runtime (codex_enabled)."})
            return (409 if login else 200), "codex_disabled"
        try:
            out = codex.start_login() if login else codex.status()
        except ModelError as exc:
            self._send_json(409, {"error": exc.code, "message": exc.message})
            return 409, exc.code
        self._send_json(200, out)
        return 200, "codex_login" if login else "codex_status"

    def _chat(self, body: dict) -> Tuple[int, str]:
        """Stream the reply as JSON lines. No Content-Length: the response ends
        when the connection closes (HTTP/1.0), which the page treats as the end."""
        rt = self.runtime
        events = run_chat(rt.config, rt.router, rt.history, rt.chat_locks, rt.runs, str(body.get("chat_id", "")),
                          str(body.get("model", "")), body.get("message"), gate=rt.gate,
                          approvals=body.get("approvals") is True, tasks=rt.tasks,
                          resume_task=body.get("resume_task") if "resume_task" in body else None,
                          memory=rt.memory, use_memory=body.get("use_memory") is not False, review=rt.review,
                          commands=rt.commands, reasoning_effort=body.get("reasoning_effort"), speed=body.get("speed"))
        self.send_response(200)
        for k, v in BASE_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        last = "ok"
        attached = True
        try:
            for event in events:
                if event["type"] == "error":
                    last = event["code"]
                if attached:
                    try:
                        self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                        self.wfile.flush()
                    except OSError:
                        # The runtime owns accepted work. Keep consuming without
                        # buffering: run_chat persists the result and releases its
                        # lock normally. Explicit Stop/Delete still cancels it.
                        attached = False
                        log_event(log, "chat.client_detached", chat_id=str(body.get("chat_id", "")))
        finally:
            events.close()
        return 200, "chat_" + last
