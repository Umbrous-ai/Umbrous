"""Bounded, cancellable compatibility checks owned by the runtime."""
import secrets
import threading
from dataclasses import asdict
from umbrous.model.base import CancelToken, ModelError


class ProbeJobs:
    def __init__(self, router):
        self.router = router
        self.lock = threading.RLock()
        self.jobs = {}

    def start(self, model):
        with self.lock:
            for job in self.jobs.values():
                if not job['done']:
                    raise ValueError('A compatibility check is already running. Cancel it or wait for it to finish.')
            while len(self.jobs) >= 32:
                self.jobs.pop(next(iter(self.jobs)))
            ident = 'probe_' + secrets.token_hex(8)
            token = CancelToken()
            self.jobs[ident] = dict(id=ident, model=model, stage='connecting', detail='Connecting to the model provider…', done=False, passed=False, token=token)
        threading.Thread(target=self._run, args=(ident, model, token), daemon=True).start()
        return self.view(ident)

    def view(self, ident):
        with self.lock:
            return {k: v for k, v in self.jobs[ident].items() if k != 'token'}

    def _run(self, ident, model, token):
        def progress(stage, detail):
            with self.lock:
                self.jobs[ident].update(stage=stage, detail=detail)
        try:
            result, _ = self.router.probe(model, cancel=token, progress=progress)
            value = dict(asdict(result), done=True)
        except ModelError as exc:
            value = dict(done=True, passed=False, stage=exc.code, detail=exc.message)
        except Exception:
            value = dict(done=True, passed=False, stage='error', detail='The compatibility check failed. Retry the connection.')
        with self.lock:
            if token.cancelled:
                value = dict(done=True, passed=False, stage='cancelled', detail='Compatibility check cancelled. You can retry or continue chatting.')
            self.jobs[ident].update(value)

    def cancel(self, ident):
        with self.lock:
            job = self.jobs[ident]
            if not job['done']:
                job['token'].cancel()
                job.update(stage='cancelling', detail='Cancelling compatibility check…')
        return self.view(ident)

    def close(self):
        with self.lock:
            for job in self.jobs.values():
                job['token'].cancel()
