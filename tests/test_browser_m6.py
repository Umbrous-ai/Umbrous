"""M6 in a real (headless) browser: the Memory section of Details adds,
edits and deletes items; a reply visibly uses them and lists their ids;
turning memory off changes the reply; deleting an item stops it being used
and leaves the old reply as it was; another project gets none of this
project's memory.

Skipped without Node.js + Playwright, like test_browser_m1.py.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama
from tests.helpers import RunningRuntime
from tests.test_browser_m1 import node_env
from tests.test_m6_memory import models

SCRIPT = Path(__file__).parent / "browser" / "m6_check.js"


class BrowserMemoryTests(unittest.TestCase):
    def test_memory_section_and_replies(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        fake = FakeOllama(models=models()).start()
        try:
            with RunningRuntime(ollama_url=fake.url) as rt:
                other = json.loads(rt.post_json("/api/projects", {"name": "Other"})[2])["project"]["id"]
                args = [node, str(SCRIPT), f"{rt.origin}/#launch={rt.policy.launch_code}", other]
                if os.environ.get("UMBROUS_SHOTS"):
                    args.append(os.environ["UMBROUS_SHOTS"])
                proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
                out = json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            fake.stop()

        personal, decision = out["ids"]
        self.assertIn("preference · all projects", out["added"][0])
        self.assertIn("The release branch is called ship", out["withMemory"]["answer"])
        self.assertEqual(set(out["withMemory"]["memory"].split(": ")[1].split(", ")), {personal, decision})
        self.assertIn(decision, out["usedNote"])
        self.assertIn("no memory", out["withoutMemory"]["answer"])
        self.assertIsNone(out["withoutMemory"]["memory"])
        self.assertEqual(out["offNote"], "Last reply: memory was off.")
        self.assertEqual(out["armed"], "Delete?")
        self.assertNotIn("ship", out["afterDelete"]["answer"])
        self.assertIn("briefly", out["afterDelete"]["answer"])
        self.assertEqual(out["afterDelete"]["memory"], "Memory used: " + personal)
        first = out["reopened"][0]
        self.assertIn("ship", first["answer"])          # the old reply is not rewritten
        self.assertIn(decision, first["memory"])        # and still lists the deleted item's id
        self.assertEqual(out["other"]["items"], [personal])
        self.assertNotIn("release branch is called ship", out["otherReply"]["answer"])
        self.assertTrue(out["partialMeta"].startswith("Incomplete"), out["partialMeta"])
        self.assertEqual(out["partial"]["memory"], "Memory used: " + personal)
        self.assertEqual(out["pageErrors"], [])
        self.assertEqual(out["cspErrors"], [])


if __name__ == "__main__":
    unittest.main()
