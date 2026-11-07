"""M4 in a real (headless) browser: the task report under a reply, the Trace
in Details, and the report again when a stored chat is reopened.

Skipped without Node.js + Playwright, like test_browser_m1.py.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama, demo_models
from tests.helpers import RunningRuntime
from tests.test_browser_m1 import node_env
from tests.test_m3_fs import make_fixture

SCRIPT = Path(__file__).parent / "browser" / "m4_check.js"


class BrowserTaskTests(unittest.TestCase):
    def test_report_and_trace_in_the_page(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        fake = FakeOllama(models=demo_models()).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            ws = make_fixture(Path(tmp.name))
            with RunningRuntime(home=Path(tmp.name) / "home", ollama_url=fake.url) as rt:
                args = [node, str(SCRIPT), f"{rt.origin}/#launch={rt.policy.launch_code}", str(ws)]
                if os.environ.get("UMBROUS_SHOTS"):
                    args.append(os.environ["UMBROUS_SHOTS"])
                proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
                r = json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            fake.stop()
            tmp.cleanup()

        self.assertTrue(r["traceHiddenAtStart"])
        self.assertTrue(r["placeholderGone"])
        live = r["live"]
        self.assertEqual(live["steps"], ["List the workspace folder", "Search for “Ship”", "Read notes/plan.md"])
        self.assertEqual(live["status"], "completed")
        self.assertTrue(live["head"].startswith("Task completed · 4 steps · 3 of 3 actions completed"))
        self.assertEqual(live["lists"], [["Not fully checked",
                                          # the fixture has a binary, a too-large file, links and a pipe
                                          "The search in the workspace folder skipped 5 file(s) it could not read "
                                          "(binary, too large, links or excluded).",
                                          "The answer cites notes/team.md, which was not read in this task."]])
        self.assertIn("notes/plan.md:1-3", live["sources"])
        self.assertEqual(r["trace"][:3], ["task.requested", "task.started", "step.started"])
        self.assertEqual(r["trace"][-1], "task.completed")
        self.assertEqual(r["trace"].count("action.completed"), 3)
        self.assertIn("Limits: 6 steps, 24 actions, 30s per action, 600s in all", r["traceText"])
        self.assertNotIn("Ship M3 on Friday", r["traceText"])     # no file content in the trace
        self.assertEqual(r["stored"]["head"], live["head"])
        self.assertEqual(r["stored"]["lists"], live["lists"])
        self.assertEqual(r["pageErrors"], [])
        self.assertEqual(r["cspErrors"], [])


if __name__ == "__main__":
    unittest.main()
