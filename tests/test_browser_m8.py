"""M8 in a real (headless) browser: Options › Delete chat… previews the open
chat, Cancel sends nothing, a new preview of a chat plus all personal memory
is refused when a new item appears after it (the page shows the new
preview), confirming that deletes exactly it, a new chat opens, and the next
reply is given none of the reset memory.

Skipped without Node.js + Playwright, like test_browser_m1.py.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

from tests.fake_ollama import FakeModel, FakeOllama, notes_agent
from tests.helpers import RunningRuntime
from tests.test_browser_m1 import node_env

SCRIPT = Path(__file__).parent / "browser" / "m8_check.js"


class BrowserResetTests(unittest.TestCase):
    def test_start_fresh_in_the_page(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        fake = FakeOllama(models=[FakeModel("notes:3b", agent=notes_agent)]).start()
        try:
            with RunningRuntime(ollama_url=fake.url) as rt:
                self.assertEqual(rt.post_json("/api/models/probe", {"model": "notes:3b"})[0], 200)
                keep = rt.new_chat()
                rt.chat(keep, "notes:3b", "hello, keep this one")
                gone = rt.new_chat()
                rt.chat(gone, "notes:3b", "the codename is BLUEHERON")
                for body in ({"store": "personal", "text": "Codename BLUEHERON", "kind": "fact"},
                             {"store": "project", "project_id": "prj_general", "text": "Ship on Friday",
                              "kind": "decision"}):
                    self.assertEqual(rt.post_json("/api/memory", body)[0], 201)
                args = [node, str(SCRIPT), f"{rt.origin}/#launch={rt.policy.launch_code}", gone]
                if os.environ.get("UMBROUS_SHOTS"):
                    args.append(os.environ["UMBROUS_SHOTS"])
                proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:] + proc.stdout[-2000:])
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                view = rt.get_json("/api/memory?project_id=prj_general")[1]
                chats = [c["id"] for c in rt.get_json("/api/chats?project_id=prj_general")[1]["chats"]]
                last_prompt = json.dumps(fake.requests[-1]["body"])
        finally:
            fake.stop()

        self.assertEqual(out["chatPreviewItems"], [gone])
        self.assertIn("the codename is BLUEHERON", out["chatPreview"])
        self.assertIn("What stays", out["chatPreview"])
        self.assertIn("Time Machine", out["chatPreview"])
        self.assertEqual(out["afterCancel"], {"posts": ["/api/reset/preview"], "chat": 200})
        self.assertEqual(out["firstItems"], 2)                       # the chat and one personal item
        self.assertIn("changed since the preview", out["changed"])
        self.assertEqual(out["changedItems"], 3)                     # the new item is shown, not silently deleted
        self.assertEqual(out["chatAfterChange"], 200)                # nothing deleted on the stale preview
        self.assertIn("Deleted 1 chat(s) with 2 message(s), 2 personal and 0 project memory item(s)", out["done"])
        self.assertEqual(out["chatAfter"], 404)
        self.assertNotIn("BLUEHERON", out["sidebar"])
        self.assertIn("Ship on Friday", out["memory"])
        self.assertEqual(view["personal"], [])
        self.assertEqual([m["text"] for m in view["project"]], ["Ship on Friday"])
        self.assertIn(keep, chats)
        self.assertNotIn(gone, chats)
        self.assertNotIn("BLUEHERON", last_prompt)
        self.assertEqual(out["posts"].count("/api/reset"), 2)
        self.assertEqual((out["pageErrors"], out["cspErrors"]), ([], []))


if __name__ == "__main__":
    unittest.main()
