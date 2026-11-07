"""M5 in a real (headless) browser: an interrupted task after a restart is
shown with its message, its recovered report and a Resume button; Resume
runs it again and the stored exchange replaces the card.

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
from tests.test_m3_fs import make_fixture
from tests.test_m5_resume import models
from umbrous.receipts.model import new_action_id, new_task_id

SCRIPT = Path(__file__).parent / "browser" / "m5_check.js"


class BrowserResumeTests(unittest.TestCase):
    def test_interrupted_task_and_resume_in_the_page(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        fake = FakeOllama(models=models()).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            ws = make_fixture(Path(tmp.name))
            home = Path(tmp.name) / "home"
            with RunningRuntime(home=home, ollama_url=fake.url) as rt:     # the run that was cut off
                rt.post_json("/api/projects/prj_general/workspace", {"root": str(ws)})
                rt.post_json("/api/models/probe", {"model": "quick:3b"})
                cid, r = rt.new_chat(), rt.runtime
                tid, aid = new_task_id(), new_action_id()
                r.tasks.create(tid, "prj_general", cid, "quick:3b", {"max_steps": 6})
                r.tasks.set_status(tid, "started")
                r.tasks.event(tid, "step.started", step=1, tools_offered=True)
                r.history.begin_pending(tid, cid, "Who owns the plan?", "quick:3b")
                r.tasks.add_action(tid, aid, 1, "read_file", "notes/plan.md")
                r.tasks.action_status(aid, "started")
            with RunningRuntime(home=home, ollama_url=fake.url) as rt:     # the restart
                args = [node, str(SCRIPT), f"{rt.origin}/#launch={rt.policy.launch_code}", cid]
                if os.environ.get("UMBROUS_SHOTS"):
                    args.append(os.environ["UMBROUS_SHOTS"])
                proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
                out = json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            fake.stop()
            tmp.cleanup()

        card = out["card"]
        self.assertEqual(card["user"], "Who owns the plan?")
        self.assertTrue(card["notice"].startswith("Task interrupted: Umbrous stopped while this task was running"))
        self.assertTrue(card["hasResume"])
        self.assertEqual(card["status"], "unknown")
        self.assertTrue(card["head"].startswith("Task outcome unknown · 1 step · 0 of 1 actions completed"))
        res = out["resumed"]
        self.assertEqual(res["users"], ["Who owns the plan?"])
        self.assertFalse(res["interruptedLeft"])
        unc = [x for l in res["lists"] for x in l]
        self.assertTrue(any("resumed an interrupted one" in x for x in unc), unc)
        self.assertEqual(out["reopened"], {"users": ["Who owns the plan?"], "interrupted": False})
        self.assertEqual(out["pageErrors"], [])
        self.assertEqual(out["cspErrors"], [])


if __name__ == "__main__":
    unittest.main()
