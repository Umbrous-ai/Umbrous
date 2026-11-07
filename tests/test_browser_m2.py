"""M2 in a real (headless) browser: projects, saved chats, archive and restore, and a restart.

Skipped without Node.js + Playwright, like test_browser_m1.py.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama, demo_models
from tests.helpers import RunningRuntime
from tests.test_browser_m1 import node_env

SCRIPT = Path(__file__).parent / "browser" / "m2_check.js"


class BrowserHistoryTests(unittest.TestCase):
    def run_page(self, home, fake, mode):
        node, env = self.found
        with RunningRuntime(home=home, ollama_url=fake.url) as rt:
            url = f"{rt.origin}/#launch={rt.policy.launch_code}"
            out = subprocess.run([node, str(SCRIPT), url, mode], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            return json.loads(out.stdout.strip().splitlines()[-1])

    def test_history_in_the_page_and_after_a_restart(self):
        self.found = node_env()
        if not self.found:
            self.skipTest("Node.js with Playwright is not installed")
        fake = FakeOllama(models=demo_models()).start()
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name) / "home"
        try:
            first = self.run_page(home, fake, "first")
            again = self.run_page(home, fake, "after-restart")   # a new runtime, same data
        finally:
            fake.stop()
            tmp.cleanup()

        self.assertEqual(first["projectsAtStart"], ["General"])
        self.assertEqual(first["projectsAfterAdd"], ["General", "Garden"])
        self.assertIn("already exists", first["duplicateError"])
        self.assertEqual(first["sessionsBeforeSend"], [])               # nothing saved before the first message
        self.assertEqual(first["gardenSessions"], ["Best soil for roses", "When should I plant tulips?"])
        self.assertEqual(first["title"], "Best soil for roses")
        self.assertEqual(first["generalSessions"], [])                   # projects are separate
        self.assertIn("No chats in General yet", first["generalEmptyText"])
        self.assertEqual(first["storedHtmlElements"], ["A https://example.com/docs"])  # only the safe link
        self.assertEqual(first["transcript"], ["When should I plant tulips?", "You said: When should I plant tulips?",
                                               "And daffodils?", "You said: And daffodils?"])
        self.assertEqual(first["optionItems"], ["Archive chat", "[off] Delete chat"])   # no working delete
        self.assertTrue(first["inputDisabledWhenArchived"])
        self.assertTrue(first["sendDisabledWhenArchived"])
        self.assertEqual(first["transcriptWhileArchived"], 2)            # still all there
        self.assertTrue(any("archived" in s for s in first["sessionsWithArchived"]))
        self.assertEqual(first["transcriptAfterRestore"], ["When should I plant tulips?", "And daffodils?",
                                                           "And tulips in pots?"])

        self.assertEqual(first["archivedMidReplyUser"], ["Best soil for roses", "Tell me slowly about compost"])
        self.assertIn("Stopped by you", first["archivedMidReplyMeta"][1])   # the partial reply was saved first

        self.assertEqual(again["projects"], ["General", "Garden"])
        self.assertEqual(again["sessions"], ["When should I plant tulips?"])     # the archived one is hidden
        self.assertEqual(again["toggle"], "Show archived (1)")
        self.assertEqual(again["archivedSessions"], ["Best soil for roses"])
        self.assertEqual(again["transcript"], ["When should I plant tulips?", "And daffodils?", "And tulips in pots?"])
        self.assertEqual(again["sessProject"], "Garden")
        self.assertEqual(again["allArchivedText"], "No active chats in Garden. 2 archived.")
        for r in (first, again):
            self.assertTrue(r["initialNotice"].startswith("New chat in General."), r["initialNotice"])
            self.assertEqual(r["dialogs"], [])
            self.assertEqual(r["cspErrors"], [])
            self.assertEqual(r["pageErrors"], [])


if __name__ == "__main__":
    unittest.main()
