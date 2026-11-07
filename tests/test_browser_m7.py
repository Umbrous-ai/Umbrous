"""M7 in a real (headless) browser: a task suggests two things to remember,
you edit and accept one and reject the other under the reply, the Memory
section shows the stored item (and nothing waiting), a later reply uses it,
and a file's claim about you is not offered as a personal fact.

Skipped without Node.js + Playwright, like test_browser_m1.py.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama
from tests.helpers import RunningRuntime
from tests.test_browser_m1 import node_env
from tests.test_m7_review import FILE_CLAIM, models

SCRIPT = Path(__file__).parent / "browser" / "m7_check.js"


class BrowserReviewTests(unittest.TestCase):
    def test_review_suggestions_in_the_page(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        fake = FakeOllama(models=models()).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            ws = Path(tmp.name) / "ws"
            (ws / "notes").mkdir(parents=True)
            (ws / "notes" / "claim.md").write_text(FILE_CLAIM + ". Lesson: keep backups\n")
            with RunningRuntime(ollama_url=fake.url) as rt:
                status, _, body = rt.post_json("/api/projects/prj_general/workspace",
                                               {"root": str(ws), "profile": "workspace_read"})
                self.assertEqual(status, 200, body)
                self.assertEqual(rt.post_json("/api/models/probe", {"model": "notes:3b"})[0], 200)
                args = [node, str(SCRIPT), f"{rt.origin}/#launch={rt.policy.launch_code}"]
                if os.environ.get("UMBROUS_SHOTS"):
                    args.append(os.environ["UMBROUS_SHOTS"])
                proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                view = rt.get_json("/api/memory?project_id=prj_general")[1]
        finally:
            fake.stop()
            tmp.cleanup()

        self.assertEqual(out["cards"], 2)
        self.assertIn("→ personal preference · General", out["cardText"])
        self.assertIn("→ this project · decision", out["cardText"])
        self.assertIn("From your message: “We decided to release on Friday”", out["cardText"])
        self.assertIn("Waiting for review (2)", out["panelBefore"])
        self.assertRegex(out["accepted"], r"^Saved as mem_[0-9a-f]{16} \(this project · decision\)\.$")
        self.assertIn("Rejected.", out["rejected"])
        self.assertNotIn("Waiting for review", out["panelAfter"])
        self.assertIn("Release on Friday, after the tests", out["panelAfter"])
        self.assertEqual([m["text"] for m in view["project"]], ["Release on Friday, after the tests"])
        self.assertEqual(view["personal"], [])
        self.assertEqual([(c["kind"], c["text"]) for c in view["candidates"]], [("lesson", "keep backups")])  # the last task
        self.assertIn("Release on Friday, after the tests", out["later"]["answer"])
        self.assertEqual(out["later"]["memory"], "Memory used: " + view["project"][0]["id"])
        self.assertIn("lesson", out["claimCard"])
        self.assertIn("From the reply", out["claimCard"])
        self.assertIn("Not offered: 1 only in the reply, not your own words", out["claimCard"])
        self.assertNotIn("passwords by email", out["claimCard"].split("Not offered")[0].split("From the reply")[0])
        self.assertEqual((out["pageErrors"], out["cspErrors"]), ([], []))


if __name__ == "__main__":
    unittest.main()
