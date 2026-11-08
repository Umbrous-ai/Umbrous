"""M9 in a real (headless) browser: "work on <folder>" attaches the folder
with the Code profile and shows it, the coding task's steps show their diffs
and command output, the report lists what changed and ran, and under "Code,
ask before commands" a command waits for your approval.

Skipped without Node.js + Playwright, like test_browser_m1.py.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.fake_ollama import FakeModel, FakeOllama, coder
from tests.helpers import RunningRuntime
from tests.test_browser_m1 import node_env
from tests.test_m9_coding import FIXED, make_project

SCRIPT = Path(__file__).parent / "browser" / "m9_check.js"


class BrowserCodingTests(unittest.TestCase):
    def test_coding_task_in_the_page(self):
        found = node_env()
        if not found:
            self.skipTest("Node.js with Playwright is not installed")
        node, env = found
        tmp = tempfile.TemporaryDirectory()
        proj = make_project(Path(tmp.name))
        fake = FakeOllama(models=[FakeModel("coder:3b", agent=coder())]).start()
        try:
            with RunningRuntime(ollama_url=fake.url) as rt:
                args = [node, str(SCRIPT), f"{rt.origin}/#launch={rt.policy.launch_code}", str(proj)]
                if os.environ.get("UMBROUS_SHOTS"):
                    args.append(os.environ["UMBROUS_SHOTS"])
                proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=180)
                self.assertEqual(proc.returncode, 0, proc.stderr[-3000:] + proc.stdout[-2000:])
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                content = (proj / "calc.py").read_text()
        finally:
            fake.stop()
            tmp.cleanup()

        real = os.path.realpath(proj)
        first = out["first"]
        self.assertIn("Working in " + real, first["folder"])
        self.assertIn("attached now", first["folder"])
        self.assertEqual([s.split(" ·")[0] for s in first["steps"]][:3],
                         ["Find files named “*.py”", "Read calc.py", "Edit calc.py"])
        self.assertTrue(any(s.startswith("Run python3 -B -m unittest") and "exit 1" in s for s in first["steps"]))
        self.assertEqual(first["details"], ["Changes", "Output (exit 1)", "Changes", "Output (exit 0)"])
        self.assertIn("+def multiply(a, b):", first["diffAdds"])
        self.assertIn("FAIL: test_multiply", out["firstOutputOpen"])
        self.assertIn("2 changes · 2 commands (1 not passing)", first["head"])
        self.assertIn(["Changed", "edited calc.py (+2 −1)", "edited calc.py (+1 −1)"], first["lists"])
        self.assertIn("Code in workspace", out["accessAfter"])
        self.assertIn("Run this command?", out["approval"])
        self.assertIn("not sandboxed", out["approval"])
        self.assertIn("Allow commands in this project", out["approval"])
        second = out["second"]
        self.assertTrue(any("exit 0" in s for s in second["steps"]), second["steps"])
        self.assertIn("Done.", second["answer"])
        self.assertEqual(content, FIXED)
        self.assertEqual((out["pageErrors"], out["cspErrors"]), ([], []))


if __name__ == "__main__":
    unittest.main()
