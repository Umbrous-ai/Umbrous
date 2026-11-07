"""The native validation scripts must not report success they did not get.

They are run here for real, with stand-ins for the macOS tools they drive
(`open`, `ditto`, `hdiutil`, `osascript`, `pgrep`, `sleep`), so these tests
run on any platform with a POSIX shell. The `open` stand-in plays the e2e app:
per scenario it writes a complete result, a failed step, a result that stops
early, nothing at all, or fails to launch. Only a fully clean run may exit 0.
(Codex review, 2026-11-07: an injected failed assertion printed FAIL but
exited 0; then a failed launch with no results printed "ok".)
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# MODE applies to every scenario, or BAD=<scenario> gets MODE and the others pass.
OPEN_STUB = r"""#!/bin/sh
OUT=""; H=""
for a in "$@"; do case "$a" in UMBROUS_E2E_OUT=*) OUT=${a#*=};; UMBROUS_HOME=*) H=${a#*=};; esac; done
[ -z "$OUT" ] && exit 0                       # the release app, opened without a script
name=$(basename "$OUT" .jsonl)
mode=pass; if [ -z "${BAD:-}" ] || [ "$BAD" = "$name" ]; then mode=$MODE; fi
case "$mode" in
pass)     printf '{"expect":"x","ok":true}\n{"record":"end"}\n' > "$OUT";;
assert)   printf '{"expect":"x","ok":true}\n{"step":"expect y","error":"got z"}\n' > "$OUT";;
noend)    printf '{"expect":"x","ok":true}\n' > "$OUT";;
none)     ;;
panic)    printf '{"record":"end"}\n' > "$OUT"; mkdir -p "$H/logs"; echo '{"event":"app.panic"}' >> "$H/logs/desktop.log";;
openfail) exit 1;;
esac
"""
STUBS = {
    "open": OPEN_STUB,
    "ditto": '#!/bin/sh\nmkdir -p "$(dirname "$2")"; cp -R "$1" "$2"\n',
    "hdiutil": '#!/bin/sh\nif [ "$1" = attach ]; then for a; do m=$a; done; '
               'eval "mp=\\${$(($#-1))}"; mkdir -p "$mp/Umbrous.app"; fi\nexit 0\n',
    "osascript": "#!/bin/sh\nexit 0\n",
    "pgrep": "#!/bin/sh\nexit 1\n",            # no app process is ever left running
    "sleep": "#!/bin/sh\nexit 0\n",
}


@unittest.skipUnless(shutil.which("sh") and shutil.which("perl"), "needs a POSIX shell and perl")
class NativeValidationExitStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "repo"   # a copy, so stand-in apps never land in the real dist/
        shutil.copytree(ROOT / "umbrous", self.root / "umbrous", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "tests", self.root / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "desktop" / "e2e", self.root / "desktop" / "e2e")
        (self.root / "dist" / "Umbrous-e2e.app").mkdir(parents=True)
        (self.root / "dist" / "Umbrous_0.3.0_aarch64.dmg").write_text("stand-in")
        self.bin = base / "bin"
        self.bin.mkdir()
        for name, text in STUBS.items():
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.work = base / "work"

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, mode="pass", bad=None, args=None):
        env = dict(os.environ, MODE=mode, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        env.pop("BAD", None)
        if bad:
            env["BAD"] = bad
        cmd = ["sh", str(self.root / "desktop" / "e2e" / script)] + ([str(self.work)] if args is None else args)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=240)

    # ---- validate-m3.sh
    def test_m3_only_a_clean_complete_run_passes(self):
        r = self.run_script("validate-m3.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("m3-access: PASS", r.stdout)
        for mode, why in (("assert", "a step failed"), ("noend", "did not reach its end"),
                          ("none", "no results"), ("panic", "m3-access: the app panicked"),
                          ("openfail", "could not be launched")):
            r = self.run_script("validate-m3.sh", mode)
            self.assertEqual(r.returncode, 1, (mode, r.stdout))
            self.assertIn(why, r.stdout, mode)
            self.assertIn("m3-access: FAIL", r.stdout, mode)
            self.assertNotIn("m3-access: PASS", r.stdout, mode)

    def test_m3_setup_failure_stops_before_the_app(self):
        shutil.rmtree(self.root / "dist" / "Umbrous-e2e.app")
        r = self.run_script("validate-m3.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Umbrous-e2e.app is missing", r.stdout)
        self.assertIn("the app was not run", r.stdout)

    def test_usage_errors(self):
        for script in ("validate-m3.sh", "validate-native.sh"):
            self.assertEqual(self.run_script(script, args=[]).returncode, 2, script)

    # ---- validate-native.sh, the whole run
    def test_native_clean_run_passes(self):
        r = self.run_script("validate-native.sh")
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])
        self.assertIn("validate-native: PASS", r.stdout)
        self.assertNotIn("FAIL", r.stdout)

    def test_native_failed_launch_with_no_results_fails(self):
        # the reviewer's reproduction: one scenario's app never launches and writes nothing
        for scenario in ("persist-1", "crash", "no-ollama", "already-running", "bad-config"):
            r = self.run_script("validate-native.sh", "openfail", bad=scenario)
            self.assertEqual(r.returncode, 1, (scenario, r.stdout[-2000:]))
            self.assertIn(f"FAIL {scenario}: the app could not be launched", r.stdout, scenario)
            self.assertIn(f"FAIL {scenario}: no results", r.stdout, scenario)
            self.assertNotIn(f"ok   {scenario}: reached its end", r.stdout, scenario)
            self.assertIn("validate-native: FAIL", r.stdout, scenario)

    def test_native_every_kind_of_scenario_failure_fails(self):
        for mode, why in (("none", "no results"), ("noend", "did not reach its end"), ("assert", "a step failed")):
            for scenario in ("no-models", "nav-stream"):
                r = self.run_script("validate-native.sh", mode, bad=scenario)
                self.assertEqual(r.returncode, 1, (mode, scenario, r.stdout[-2000:]))
                self.assertIn(f"FAIL {scenario}: {why}", r.stdout, (mode, scenario))
                self.assertNotIn(f"ok   {scenario}: reached its end", r.stdout, (mode, scenario))
                self.assertIn("validate-native: FAIL", r.stdout, (mode, scenario))

    def test_native_panic_in_any_scenario_fails(self):
        # the reviewer's case: the panic check ran before the last two scenarios
        for scenario in ("persist-2", "nav-stream", "already-running", "bad-config"):
            r = self.run_script("validate-native.sh", "panic", bad=scenario)
            self.assertEqual(r.returncode, 1, (scenario, r.stdout[-2000:]))
            self.assertIn(f"FAIL {scenario}: the app panicked", r.stdout, scenario)
            self.assertNotIn(f"ok   {scenario}: no panic logged", r.stdout, scenario)
            self.assertIn("validate-native: FAIL", r.stdout, scenario)

    def test_native_passes_on_an_m3_failure(self):
        r = self.run_script("validate-native.sh", "noend", bad="m3-access")
        self.assertEqual(r.returncode, 1, r.stdout[-2000:])
        self.assertIn("FAIL validate-m3.sh", r.stdout)

    def test_every_scenario_script_ends_with_its_end_record(self):
        for f in (ROOT / "desktop" / "e2e" / "scripts").glob("*.txt"):
            lines = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]
            self.assertEqual(lines[-2:], ["record end", "quit"], f.name)


if __name__ == "__main__":
    unittest.main()
