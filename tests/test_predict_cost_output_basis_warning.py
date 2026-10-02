"""calibrate.predict_cost must make a NO-empirical-output quote LOUD, not silent.

When there is no learned fill AND no out-per-in for a (label, model), the output side — the DOMINANT cost term — falls
back to the caller's raw est_out_max (basis='cap', n_obs=0). That silent assumed-output is how a quote was handed to a
human 4.5x low (measured: 56 real out-tok/item vs an assumed 10). predict_cost now returns a `warning` (and warns once
to stderr) in that case, while STILL returning the number (a usable floor). Offline + hermetic: an empty ledger under an
isolated SPENDGUARD_HOME, so every cell is no-history.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-predict-warn-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import calibrate   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# no history anywhere (empty ledger) + a caller est_out_max → basis 'cap', output ASSUMED → must be LOUD.
r = calibrate.predict_cost("zzz-no-history-intent", n=10, model="gpt-5.5", transport="realtime",
                           est_in_tokens=100, est_out_max=10)
ck("basis is 'cap' (no learned fill / out-per-in for this cell)", r["basis"] == "cap")
ck("n_obs is 0", r["n_obs"] == 0)
ck("a warning is returned, not silent", isinstance(r.get("warning"), str) and "ASSUMED" in r["warning"])
ck("the warning names the assumed est_out_max", "est_out_max=10" in (r.get("warning") or ""))
ck("the $ is STILL returned (a usable floor, not a raise)", isinstance(r["p50_usd"], (int, float)) and r["p50_usd"] > 0)

# the OTHER no-history shape: no est_out_max AND no history → still a LOUD failure (raises), never a silent zero-output quote.
raised = False
try:
    calibrate.predict_cost("zzz-no-history-intent", n=10, model="gpt-5.5", transport="realtime", est_in_tokens=100)
except ValueError:
    raised = True
ck("no est_out_max + no history → raises (loud), never a silent zero-output quote", raised)

print(f"\n{'[FAIL]' if fails else 'OK'} test_predict_cost_output_basis_warning: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
