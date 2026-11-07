"""M3 in a real (headless) browser: the Access view, tool steps, approval cards
(once, grant, deny), hard blocks that never ask, and Sources from receipts.

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
from tests.test_m3_fs import SECRET, make_fixture

SCRIPT = Path(__file__).parent / "browser" / "m3_check.js"


class BrowserToolTests(unittest.TestCase):
    def test_tools_approvals_and_access_in_the_page(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        fake = FakeOllama(models=demo_models()).start()
        tmp = tempfile.TemporaryDirectory()
        try:
            ws = make_fixture(Path(tmp.name))
            with RunningRuntime(home=Path(tmp.name) / "home", ollama_url=fake.url) as rt:
                url = f"{rt.origin}/#launch={rt.policy.launch_code}"
                args = [node, str(SCRIPT), url, str(ws)]
                if os.environ.get("UMBROUS_SHOTS"):
                    args.append(os.environ["UMBROUS_SHOTS"])
                proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
                r = json.loads(proc.stdout.strip().splitlines()[-1])
        finally:
            fake.stop()
            tmp.cleanup()

        self.assertFalse(r["accessHidden"])
        self.assertIn("No workspace", r["accessEmpty"])
        self.assertEqual(r["accessRoot"], os.path.realpath(ws))
        self.assertEqual(r["profile"], "workspace_read")

        read = r["read"]
        self.assertEqual([s[1] for s in read["steps"]], ["Read notes/plan.md"])
        self.assertEqual(read["steps"][0][0], "step")                     # allowed: no warning class
        self.assertIn("Ship M3 on Friday.", read["answer"])
        self.assertEqual(read["sources"], "Sources: notes/plan.md:1-3")

        for key, code in (("outside", "outside"), ("excluded", "excluded"), ("symlink", "symlink")):
            res = r[key]
            self.assertIn("denied", res["steps"][0][0], key)
            self.assertIn("Blocked", res["steps"][0][2], key)
            self.assertIsNone(res["sources"], key)
            self.assertEqual(res["approvals"], [], key)                   # hard blocks never ask
        self.assertNotIn(SECRET, json.dumps(r))
        self.assertIn("readme.txt:2", r["search"]["sources"])

        self.assertIn("Allow this read?", r["card"]["text"])
        self.assertIn("readme.txt", r["card"]["text"])
        self.assertEqual(r["card"]["buttons"], ["Approve once", "Allow reads in this project", "Deny"])
        self.assertIn("Approved once", r["once"]["approvals"][0])
        self.assertIn("garden needs water", r["once"]["answer"])
        self.assertIn("Allowed for this project", r["granted"]["approvals"][0])
        self.assertEqual(r["grants"], ["fs.read"])
        self.assertEqual(r["withGrant"]["approvals"], [])
        self.assertIn("Ship M3", r["withGrant"]["answer"])
        self.assertIn("denied", r["denied"]["steps"][0][0])
        self.assertIn("Denied", r["denied"]["approvals"][0])
        self.assertNotIn("garden needs water", r["denied"]["answer"])
        self.assertEqual(r["outsideUnderAsk"]["approvals"], [])
        self.assertIn("denied", r["outsideUnderAsk"]["steps"][0][0])

        self.assertEqual(r["stored"][0], {"steps": [["step", "Read notes/plan.md"]],
                                          "sources": "Sources: notes/plan.md:1-3"})
        self.assertIn("denied", r["stored"][1]["steps"][0][0])
        self.assertTrue(r["detached"])
        self.assertEqual(r["dialogs"], [])
        self.assertEqual(r["cspErrors"], [])
        self.assertEqual(r["pageErrors"], [])


if __name__ == "__main__":
    unittest.main()
