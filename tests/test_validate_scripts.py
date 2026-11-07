"""The native validation scripts must not report success they did not get.

desktop/e2e/validate-m3.sh is run with stand-ins for macOS's `open` and
`ditto` that write each kind of result the real app can leave behind. Only a
complete, clean run may exit 0. (Codex review, 2026-11-07: an injected failed
assertion printed FAIL but exited 0.)
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "desktop" / "e2e" / "validate-m3.sh"
NATIVE = ROOT / "desktop" / "e2e" / "validate-native.sh"

OPEN_STUB = r"""#!/bin/sh
for a in "$@"; do case "$a" in UMBROUS_E2E_OUT=*) OUT=${a#*=};; UMBROUS_HOME=*) H=${a#*=};; esac; done
case "$MODE" in
pass)     printf '{"expect":"x","ok":true}\n{"record":"end"}\n' > "$OUT";;
assert)   printf '{"expect":"x","ok":true}\n{"step":"expect y","error":"got z"}\n' > "$OUT";;
noend)    printf '{"expect":"x","ok":true}\n' > "$OUT";;
none)     ;;
panic)    printf '{"record":"end"}\n' > "$OUT"; mkdir -p "$H/logs"; echo '{"event":"app.panic"}' > "$H/logs/desktop.log";;
openfail) exit 1;;
esac
"""


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class ValidateM3ExitStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "repo"                     # a copy, so the stand-in app never lands in the real dist/
        shutil.copytree(ROOT / "umbrous", self.root / "umbrous")
        shutil.copytree(ROOT / "tests", self.root / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "desktop" / "e2e", self.root / "desktop" / "e2e")
        (self.root / "dist" / "Umbrous-e2e.app").mkdir(parents=True)
        self.bin = base / "bin"
        self.bin.mkdir()
        (self.bin / "open").write_text(OPEN_STUB)
        (self.bin / "ditto").write_text('#!/bin/sh\ncp -R "$1" "$2"\n')
        for f in self.bin.iterdir():
            f.chmod(0o755)
        self.work = base / "work"

    def tearDown(self):
        self.tmp.cleanup()

    def run_mode(self, mode):
        env = dict(os.environ, MODE=mode, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run(["sh", str(self.root / "desktop" / "e2e" / "validate-m3.sh"), str(self.work)],
                              env=env, capture_output=True, text=True, timeout=120)

    def test_only_a_clean_complete_run_passes(self):
        r = self.run_mode("pass")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m3-access: PASS", r.stdout)
        for mode, why in (("assert", "assertions failed"), ("noend", "did not reach its end"),
                          ("none", "wrote no results"), ("panic", "the app panicked"),
                          ("openfail", "could not be opened")):
            r = self.run_mode(mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m3-access: FAIL", r.stdout, mode)

    def test_setup_failure_stops_before_the_app(self):
        shutil.rmtree(self.root / "dist")
        r = self.run_mode("pass")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Umbrous-e2e.app is missing", r.stdout)
        self.assertIn("the app was not run", r.stdout)

    def test_usage_error(self):
        r = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)


class ValidateNativePropagates(unittest.TestCase):
    def test_m3_failure_and_every_fail_line_reach_the_exit_status(self):
        text = NATIVE.read_text()
        self.assertIn('validate-m3.sh" "$W" || fail ', text)
        self.assertTrue(text.rstrip().endswith("exit $STATUS"))
        body = text.split("\n", 12)[-1]                                 # after the fail() definition
        self.assertNotIn('echo "FAIL', body)                            # every FAIL goes through fail()


if __name__ == "__main__":
    unittest.main()
