"""M1 in a real (headless) browser: chat, inert HTML, stop, model labels.

Needs Node.js with Playwright (`npm i -g playwright && npx playwright install chromium`).
Skipped when they are not installed, so `bin/umbrous test` still runs everywhere.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama, demo_models
from tests.helpers import RunningRuntime

SCRIPT = Path(__file__).parent / "browser" / "m1_check.js"


def node_env():
    node = shutil.which("node")
    if not node:
        return None
    env = {}
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=20).stdout.strip()
        env = {"NODE_PATH": root}
    except (OSError, subprocess.SubprocessError):
        pass
    import os
    full = dict(os.environ, **env)
    ok = subprocess.run([node, "-e", "require('playwright')"], env=full, capture_output=True, timeout=20).returncode == 0
    return (node, full) if ok else None


class BrowserTests(unittest.TestCase):
    def test_chat_page_in_headless_chromium(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        fake = FakeOllama(models=demo_models()).start()
        try:
            with RunningRuntime(ollama_url=fake.url) as rt:
                url = f"{rt.origin}/#launch={rt.policy.launch_code}"
                out = subprocess.run([node, str(SCRIPT), url], env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(out.returncode, 0, out.stderr[-2000:])
                r = json.loads(out.stdout.strip().splitlines()[-1])
        finally:
            fake.stop()
        self.assertEqual(r["reply"], "You said: hello from the browser")
        self.assertTrue(r["meta"].startswith("qwen2.5:3b · "))             # exact model per reply
        self.assertIn("<script>alert(2)</script>", r["htmlText"])          # shown as text…
        self.assertEqual(r["htmlElements"], ["A href=https://example.com/docs text=https://example.com/docs"])
        self.assertEqual(r["dialogs"], [])                                  # …and nothing ran
        self.assertEqual(r["remoteRequests"], [])                           # no auto-loaded remote images
        self.assertEqual(r["cspErrors"], [])
        self.assertEqual(r["sendTitleWhileRunning"], "Stop")
        self.assertTrue(r["stoppedMeta"].startswith("Stopped by you."))
        self.assertTrue(any("qwen2.5:3b" in m and "Tool calls verified" in m for m in r["menu"]))
        self.assertTrue(any("tiny-chat:1b" in m and "Plain chat only" in m for m in r["menu"]))
        self.assertIn("Exposure | Loopback only", r["runtime"])


if __name__ == "__main__":
    unittest.main()
