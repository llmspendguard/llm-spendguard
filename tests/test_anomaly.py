"""Daily anomaly detection (anomaly.py) — the automated version of the gut that caught both 2× P0s.
Guards: a doubled TOTAL trips even when each source looks tame (the exact P0 shape); robust to a prior
legit spike (median/MAD, not mean/std); never flags penny days, short histories, or normal variation;
report.py is actually wired through it. Offline, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-anomaly-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import anomaly

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

def days(vals, today_val=None):
    m = {f"2026-06-{i+1:02d}": v for i, v in enumerate(vals)}
    if today_val is not None:
        m["2026-07-01"] = today_val
    return m

T = "2026-07-01"

# ── stable history + normal today → clean ──
ck("stable series not flagged", anomaly.flag_today(days([20, 22, 19, 21, 20, 23, 21, 20], 22), T) is None)
# ── the P0 shape: today ≈ 2×+ history → flagged ──
f = anomaly.flag_today(days([20, 22, 19, 21, 20, 23, 21, 20], 95), T)
ck("~5x spike flagged", f is not None and f["z"] >= anomaly.Z_THRESHOLD and f["usd"] == 95)
# ── robustness: ONE legit prior spike must not poison the baseline (mean/std would) ──
f = anomaly.flag_today(days([20, 22, 19, 400, 20, 23, 21, 20], 95), T)
ck("prior spike doesn't mask today's anomaly (median/MAD)", f is not None)
# ── guards against noise ──
ck("short history (<7d) not judged", anomaly.flag_today(days([20, 21, 22], 500), T) is None)
ck("penny day never flagged", anomaly.flag_today(days([0.1] * 10, 0.9), T) is None)
ck("flat-zero history + real spend today flagged", anomaly.flag_today(days([0.0] * 10, 25), T) is not None)
ck("constant nonzero history, same today → clean", anomaly.flag_today(days([50.0] * 10, 50.0), T) is None)
ck("modest excess (<1.5x median) → not flagged", anomaly.flag_today(days([100, 101, 99, 100, 102, 98, 100, 101], 130), T) is None)
ck("~1.8x systematic inflation (the P0 shape) IS flagged", anomaly.flag_today(days([100, 101, 99, 100, 102, 98, 100, 101], 180), T) is not None)

# ── lines(): TOTAL catches a spike hiding in a source too NEW to judge on its own (short history) ──
oai = days([20, 22, 19, 21, 20, 23, 21, 20], 21)                       # established + calm
rt = {"2026-06-29": 5, "2026-06-30": 6, T: 60}                          # 3 days old → not judged alone
out = anomaly.lines({"OpenAI batch": oai, "Real-time": rt}, T)
ck("TOTAL flags a spike hiding in a short-history source", any("TOTAL" in ln for ln in out))
ck("the short-history source itself was not judged", not any("Real-time" in ln for ln in out))
ck("clean day → no lines", anomaly.lines({"OpenAI batch": days([10] * 8, 10)}, T) == [])
ck("line names the source + says verify-vs-provider",
   all("ANOMALY" in ln and "provider totals" in ln for ln in out))

# ── wiring guard: the daily report actually runs the check ──
import inspect
from spendguard import report
src = inspect.getsource(report._run)
ck("report._run wired through anomaly.lines", "anomaly" in src and "anomaly check could not run" in src)

print(("[OK]" if not fails else "[FAIL]") + " anomaly: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
