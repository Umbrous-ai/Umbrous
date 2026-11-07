"""End to end through the real command: start, connect as the page would, stop."""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "umbrous"


def run(*args, home):
    env = dict(os.environ, UMBROUS_PYTHON=sys.executable)
    env.pop("UMBROUS_PORT", None)
    return subprocess.run([str(BIN), "--home", str(home), *args], capture_output=True, text=True, env=env, timeout=30)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def test_version(self):
        out = run("version", home=self.home)
        self.assertEqual(out.returncode, 0)
        self.assertRegex(out.stdout, r"umbrous \S+ \(M4\)")

    def test_config_error_stops_startup(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"host": "0.0.0.0"}))
        out = run("serve", "--no-open", home=self.home)
        self.assertEqual(out.returncode, 2)
        self.assertIn("loopback only", out.stderr)

    def test_purge_logs_needs_yes(self):
        (self.home / "logs").mkdir(parents=True)
        (self.home / "logs" / "runtime.log").write_text("{}\n")
        self.assertEqual(run("purge", "logs", home=self.home).returncode, 1)
        self.assertTrue((self.home / "logs" / "runtime.log").exists())
        self.assertEqual(run("purge", "logs", "--yes", home=self.home).returncode, 0)
        self.assertFalse((self.home / "logs" / "runtime.log").exists())

    def test_serve_claim_health_and_clean_stop(self):
        env = dict(os.environ, UMBROUS_PYTHON=sys.executable)
        proc = subprocess.Popen([str(BIN), "--home", str(self.home), "serve", "--no-open", "--port", "0"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            url = None
            for _ in range(10):
                line = proc.stdout.readline()
                m = re.search(r"(http://127\.0\.0\.1:\d+)/#launch=(\S+)", line)
                if m:
                    origin, code = m.groups()
                    url = origin
                    break
            self.assertIsNotNone(url, "runtime did not print its launch URL")

            # A second copy refuses to start while this one runs.
            second = run("serve", "--no-open", "--port", "0", home=self.home)
            self.assertEqual(second.returncode, 1)
            self.assertIn("already running", second.stderr)

            req = urllib.request.Request(url + "/api/session/claim", method="POST", data=b"",
                                         headers={"X-Umbrous-Launch": code, "Origin": url})
            token = json.load(urllib.request.urlopen(req, timeout=5))["token"]
            health = json.load(urllib.request.urlopen(
                urllib.request.Request(url + "/api/health", headers={"X-Umbrous-Token": token}), timeout=5))
            self.assertEqual(health["status"], "ok")

            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(url + "/api/health", timeout=5)
            self.assertEqual(err.exception.code, 401)
            self.assertTrue((self.home / "run" / "runtime.json").exists())
            self.assertNotIn(token, (self.home / "run" / "runtime.json").read_text())
        finally:
            proc.send_signal(signal.SIGTERM)
            out, errout = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, errout)
        self.assertIn("Umbrous stopped.", out)
        self.assertFalse((self.home / "run" / "runtime.json").exists())
        self.assertNotIn(token, errout)
        self.assertNotIn(token, (self.home / "logs" / "runtime.log").read_text())


if __name__ == "__main__":
    unittest.main()
