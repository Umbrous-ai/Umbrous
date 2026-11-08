"""Opt-in real ChatGPT coding check using only a temporary synthetic project.

Consumes subscription allowance and runs model-proposed commands as your user.
Run: python3 -m tests.live_codex_smoke --live --model codex/MODEL
"""
import argparse
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from tests.helpers import RunningRuntime
from tests.test_m9_coding import make_project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning", default="low")
    parser.add_argument("--speed", choices=("standard", "fast"), default="standard")
    args = parser.parse_args()
    if not args.model.startswith("codex/"):
        parser.error("select a codex/ model from the current catalog")
    with tempfile.TemporaryDirectory(prefix="umbrous-live-coding-") as tmp, RunningRuntime(
            codex_enabled=True, ollama_url="http://127.0.0.1:1") as rt:
        project = make_project(Path(tmp))
        original_tests = (project / "test_calc.py").read_bytes()
        rt.runtime.access.attach("prj_general", str(project), "workspace_code")
        result, _ = rt.runtime.router.probe(args.model)
        if not result.passed:
            raise RuntimeError("Tool check failed: " + result.detail)
        body = {"chat_id": rt.new_chat(), "model": args.model,
                "reasoning_effort": args.reasoning, "speed": args.speed, "use_memory": False,
                "message": "Read calc.py and test_calc.py in the attached toy project. Implement multiply(a, b), "
                           "then run python3 -B -m unittest test_calc. Fix failures until tests pass. "
                           "Do not change test_calc.py. Report the result."}
        conn = http.client.HTTPConnection("127.0.0.1", rt.port, timeout=300)
        try:
            conn.request("POST", "/api/chat", json.dumps(body), {
                "Content-Type": "application/json", "Origin": rt.origin,
                "X-Umbrous-Token": rt.policy.session_token})
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError("Chat HTTP status: " + str(response.status))
            events = [json.loads(line) for line in response.read().splitlines()]
        finally:
            conn.close()
        final = events[-1]
        if final.get("type") != "done":
            raise RuntimeError("Chat did not complete: " + final.get("code", "unknown"))
        report = final.get("report", {})
        commands = report.get("commands", [])
        if not report.get("changes") or not commands or commands[-1].get("exit") != 0:
            raise RuntimeError("The task did not record an edit and a successful command")
        if (project / "test_calc.py").read_bytes() != original_tests:
            raise RuntimeError("The model changed the tests")
        subprocess.run([sys.executable, "-B", "-m", "unittest", "test_calc"], cwd=project, check=True)
        print("PASS: real ChatGPT tool check, gated edit, and independently verified tests")


if __name__ == "__main__":
    main()
