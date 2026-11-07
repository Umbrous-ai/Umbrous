"""`serve --desktop`: the pipe protocol the desktop app (desktop/) uses to run the runtime."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from umbrous.api.security import AccessPolicy

REPO = Path(__file__).resolve().parents[1]


def start(home, *extra):
    env = dict(os.environ, PYTHONPATH=str(REPO))
    env.pop("UMBROUS_PORT", None)
    return subprocess.Popen([sys.executable, "-m", "umbrous", "--home", str(home), "serve", "--desktop", *extra],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=str(REPO))


def claim(origin, code):
    req = urllib.request.Request(origin + "/api/session/claim", method="POST", data=b"",
                                 headers={"X-Umbrous-Launch": code})
    return json.load(urllib.request.urlopen(req, timeout=5))["token"]


class DesktopModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_ready_line_reissue_and_stop_when_stdin_closes(self):
        proc = start(self.home, "--port", "0")
        try:
            ready = json.loads(proc.stdout.readline())
            self.assertEqual(ready["event"], "ready")
            self.assertRegex(ready["origin"], r"^http://127\.0\.0\.1:\d+$")
            self.assertEqual(ready["pid"], proc.pid)
            first = ready["launch_code"]
            runtime = json.loads((self.home / "run" / "runtime.json").read_text())
            self.assertEqual(runtime["mode"], "desktop")
            self.assertNotIn(first, json.dumps(runtime))

            proc.stdin.write("reissue\n")
            proc.stdin.flush()
            event = json.loads(proc.stdout.readline())
            self.assertEqual(event["event"], "launch_code")
            second = event["launch_code"]
            self.assertNotEqual(first, second)
            with self.assertRaises(urllib.error.HTTPError):
                claim(ready["origin"], first)  # the old code no longer works
            token = claim(ready["origin"], second)
            with self.assertRaises(urllib.error.HTTPError):
                claim(ready["origin"], second)  # and the new one works once
        finally:
            started = time.monotonic()
            proc.stdin.close()  # what happens when the app quits or crashes
            proc.wait(timeout=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(proc.returncode, 0)
        out, err = proc.stdout.read(), proc.stderr.read()
        self.assertEqual([json.loads(l)["event"] for l in out.splitlines()], ["stopped"])
        self.assertFalse((self.home / "run" / "runtime.json").exists())
        log = (self.home / "logs" / "runtime.log").read_text()
        for secret in (first, second, token):
            self.assertNotIn(secret, log)
            self.assertNotIn(secret, err)

    def test_stdout_is_only_protocol(self):
        proc = start(self.home, "--port", "0")
        try:
            ready = json.loads(proc.stdout.readline())
            self.assertEqual(ready["event"], "ready")
            proc.stdin.write("stop\n")
            proc.stdin.flush()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
        for line in proc.stdout.read().splitlines():
            json.loads(line)  # never a human-readable line

    def test_already_running_is_reported_and_left_alone(self):
        first = start(self.home, "--port", "0")
        try:
            ready = json.loads(first.stdout.readline())
            second = start(self.home, "--port", "0")
            out, _ = second.communicate(timeout=20)
            self.assertEqual(second.returncode, 1)
            error = json.loads(out.splitlines()[0])
            self.assertEqual(error["code"], "already_running")
            self.assertEqual(error["pid"], ready["pid"])
            self.assertEqual(error["mode"], "desktop")
            self.assertIsNone(first.poll())  # the running one is untouched
        finally:
            first.stdin.close()
            first.wait(timeout=10)

    def test_port_in_use(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            proc = start(self.home, "--port", str(blocker.getsockname()[1]))
            out, _ = proc.communicate(timeout=20)
        finally:
            blocker.close()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(out.splitlines()[0])["code"], "port_in_use")

    def test_config_error(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"host": "0.0.0.0"}))
        proc = start(self.home, "--port", "0")
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(out.splitlines()[0])["code"], "config_error")


class ReissueTests(unittest.TestCase):
    def test_reissue_grants_same_session_once(self):
        now = [0.0]
        policy = AccessPolicy(1234, clock=lambda: now[0])
        token = policy.claim(policy.launch_code)
        self.assertIsNotNone(token)
        code = policy.reissue_launch_code()
        self.assertEqual(policy.claim(code), token)
        self.assertIsNone(policy.claim(code))
        code = policy.reissue_launch_code(launch_ttl=10)
        now[0] = 11
        self.assertIsNone(policy.claim(code))


if __name__ == "__main__":
    unittest.main()
