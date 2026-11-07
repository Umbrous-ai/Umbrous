"""Model Router: picks the adapter and model for a request, and nothing else.

M1 has one adapter (Ollama). The router's job is mostly to say no:
  - only models that are installed right now can be used;
  - the model you picked is the model that is called. If it is missing or
    broken, the request fails with a clear error. There is no fallback to
    another model or provider, ever ("no silent switching").

It also keeps the tool-call probe results, so the UI can label each model and
M3 can refuse tool work for models that have not passed.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from umbrous.model.base import ModelInfo, ModelNotInstalled, ProviderUnavailable
from umbrous.model.ollama import OllamaAdapter
from umbrous.model.probe import ProbeResult, run_probe
from umbrous.persistence import db


class ModelRouter:
    def __init__(self, adapter: OllamaAdapter, db_path: Path) -> None:
        self.adapter = adapter
        self.db_path = db_path
        self._lock = threading.Lock()
        self._probe_lock = threading.Lock()

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), isolation_level=None)

    def status(self) -> dict:
        """Is Ollama up, and which models are installed? Never raises."""
        try:
            version = self.adapter.version()
        except Exception as exc:
            message = getattr(exc, "message", str(exc))
            return {"reachable": False, "error": message, "url": self.adapter.base_url, "models": []}
        try:
            models = [self._describe(m) for m in self.adapter.list_models()]
        except Exception as exc:
            return {"reachable": True, "version": version, "error": getattr(exc, "message", str(exc)),
                    "url": self.adapter.base_url, "models": []}
        return {"reachable": True, "version": version, "url": self.adapter.base_url, "models": models}

    def _describe(self, m: ModelInfo) -> dict:
        try:
            m = self.adapter.show(m)
        except Exception:
            pass  # details are nice to have; the model is still listed
        probe = self.probe_result(m)
        return {
            "name": m.name, "digest": m.digest[:12], "size_bytes": m.size_bytes, "family": m.family,
            "parameter_size": m.parameter_size, "quantization": m.quantization,
            "context_length": m.context_length,
            "advertises_tools": "tools" in m.advertised_capabilities,
            "probe": probe,
            "tool_capable": bool(probe and probe["passed"]),
        }

    def probe_result(self, m: ModelInfo) -> Optional[dict]:
        conn = self._db()
        try:
            return db.load_probes(conn).get((m.name, m.digest))
        finally:
            conn.close()

    def resolve(self, name: str) -> ModelInfo:
        """The installed model with exactly this name, with details. No fallback."""
        if not isinstance(name, str) or not name:
            raise ModelNotInstalled("Pick a model first.")
        for m in self.adapter.list_models():  # raises ProviderUnavailable if Ollama is down
            if m.name == name:
                try:
                    return self.adapter.show(m)
                except ProviderUnavailable:
                    raise
                except Exception:
                    return m
        raise ModelNotInstalled(
            f"The model {name!r} is not installed in Ollama. Pick an installed model, "
            f"or install it with `ollama pull {name}`. Umbrous will not switch to another model for you.")

    def tool_capable(self, name: str) -> bool:
        """Used by M3: only models that passed the probe may start tool work."""
        m = self.resolve(name)
        probe = self.probe_result(m)
        return bool(probe and probe["passed"])

    def probe(self, name: str, timeout: float = 180.0) -> Tuple[ProbeResult, ModelInfo]:
        m = self.resolve(name)
        with self._probe_lock:  # one probe at a time; each loads a model into memory
            result = run_probe(self.adapter, m.name, timeout=timeout)
        conn = self._db()
        try:
            db.save_probe(conn, m.name, m.digest, result.passed, result.stage, result.detail, result.ms)
        finally:
            conn.close()
        return result, m

    def list_names(self) -> List[str]:
        return [m.name for m in self.adapter.list_models()]

    def models_by_name(self) -> Dict[str, ModelInfo]:
        return {m.name: m for m in self.adapter.list_models()}
