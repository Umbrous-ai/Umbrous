"""measure.py APP HOME IDLE_S [E2E_SCRIPT]: launch Umbrous, let it idle, report phys_footprint, RSS and CPU per process, and Ollama separately (docs/desktop.md)."""
import re, subprocess, sys, time, os
app, home, idle = sys.argv[1], sys.argv[2], int(sys.argv[3])
script = sys.argv[4] if len(sys.argv) > 4 else None
sh = lambda *a: subprocess.run(a, capture_output=True, text=True).stdout
def pgrep(pat): return [int(x) for x in sh("pgrep", "-f", pat).split()]
def fp(pid):
    m = re.search(r"phys_footprint:\s+([\d.]+ \w+)", sh("footprint", "-p", str(pid), "--noCategories"))
    return m.group(1) if m else "?"
def cpu(pid):
    t = sh("ps", "-o", "cputime=", "-p", str(pid)).strip(); s = 0.0
    for p in t.split(":"): s = s * 60 + float(p)
    return s
def rss(pid): return int(sh("ps", "-o", "rss=", "-p", str(pid)).strip() or 0) / 1024
args = ["open", "--env", f"UMBROUS_HOME={home}"]
if script:
    args += ["--env", f"UMBROUS_E2E_SCRIPT={script}", "--env", f"UMBROUS_E2E_OUT={home}/mem-e2e.jsonl"]
subprocess.run(args + [app]); time.sleep(8)
pids = pgrep(app + "/Contents/MacOS/")[:1] + pgrep(app + "/Contents/Resources/backend/python")[:1]
c0 = {p: cpu(p) for p in pids}; t0 = time.time()
time.sleep(idle)
el = time.time() - t0
print(f"process | phys_footprint | RSS MB | CPU % over {idle}s idle")
for p, n in zip(pids, ["Umbrous (Rust/GPUI)", "python3 (runtime)"]):
    print(f"{n} | {fp(p)} | {rss(p):.0f} | {100 * (cpu(p) - c0[p]) / el:.2f}%")
print("--- Ollama (external)")
for p in pgrep("Ollama.app/Contents"):
    print(f"{sh('ps','-o','args=','-p',str(p)).strip().split('/')[-1][:50]} | {fp(p)} | {rss(p):.0f}")
