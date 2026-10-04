"""Guard for the claude-code LANE token-capture fix (subscription_exec.run_prompt), offline + $0.

THE BUG IT LOCKS DOWN: the Claude CLI splits the input the model processed across THREE usage fields —
input_tokens (fresh), cache_creation_input_tokens (cache write), cache_read_input_tokens (cache read). The
original code read ONLY input_tokens, so a call that actually processed ~23K tokens recorded in_tok=2 — a
~25,000x undercount that silently corrupted lane input-usage accounting and any per-item cost projection built
from lane rows. The fix SUMS all three; falls back to a provider-aware content count only when the CLI reports
an all-zero usage block (measured: it sometimes does), marking the row tok_estimated.

Nothing spends: the claude binary is stubbed and subprocess.run is monkeypatched to return a crafted CLI JSON."""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-subexec-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None                      # bypass the fail-closed gate FOR THIS OFFLINE TEST only
from spendguard import subscription_exec as se         # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


class _R:                                              # a stand-in for subprocess.CompletedProcess
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _run_with_cli_json(payload, prompt="classify this", system=None):
    """Drive run_prompt with the claude binary stubbed and subprocess.run returning one crafted CLI JSON line."""
    se._bin = lambda: "/fake/claude"                   # pretend the CLI is installed
    se.subprocess.run = lambda *a, **k: _R(json.dumps(payload))
    return se.run_prompt(prompt, system=system, model="claude-haiku-4-5")


print("-- SUM of the three input fields (fresh + cache_creation + cache_read) is the real read-side input --")
r = _run_with_cli_json({"result": "Neutral.", "usage": {"input_tokens": 9, "cache_creation_input_tokens": 23086,
                                                         "cache_read_input_tokens": 0, "output_tokens": 312}})
ck("in_tok = 9 + 23086 + 0 = 23095 (NOT the fresh 9, and never the old in=2)", r.get("in_tok") == 23095,
   extra="got %s" % r.get("in_tok"))
ck("out_tok = 312 (CLI's own count, trusted when > 0)", r.get("out_tok") == 312, extra="got %s" % r.get("out_tok"))
ck("not flagged tok_estimated (CLI reported real usage)", not r.get("tok_estimated"))

print("-- warm repeat: the bulk arrives as cache_read, still summed --")
r = _run_with_cli_json({"result": "ok", "usage": {"input_tokens": 5, "cache_creation_input_tokens": 0,
                                                   "cache_read_input_tokens": 40000, "output_tokens": 100}})
ck("in_tok = 5 + 0 + 40000 = 40005", r.get("in_tok") == 40005, extra="got %s" % r.get("in_tok"))

print("-- REGRESSION: a fresh-only in=2 with cache present must NEVER be recorded as in=2 --")
r = _run_with_cli_json({"result": "x" * 20, "usage": {"input_tokens": 2, "cache_creation_input_tokens": 15000,
                                                      "cache_read_input_tokens": 0, "output_tokens": 50}})
ck("in_tok = 15002, not the buggy 2", r.get("in_tok") == 15002, extra="got %s" % r.get("in_tok"))

print("-- all-zero usage block (the CLI sometimes returns it): fall back to a provider-aware content count --")
big_prompt = "Summarize: " + " ".join("w%d" % i for i in range(400))
r = _run_with_cli_json({"result": "a summary of several words here", "usage": {"input_tokens": 0,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}},
                       prompt=big_prompt, system="You are terse.")
ck("in_tok fell back to a realistic content count (>> 0, not 0)", (r.get("in_tok") or 0) > 50,
   extra="got %s" % r.get("in_tok"))
ck("out_tok fell back to a content count of the returned text (> 0)", (r.get("out_tok") or 0) > 0,
   extra="got %s" % r.get("out_tok"))
ck("flagged tok_estimated=True (a fallback is never passed off as exact)", r.get("tok_estimated") is True)

print("-- is_error from the CLI still returns an error (no token fields invented) --")
r = _run_with_cli_json({"is_error": True, "result": "boom"})
ck("error surfaced, no in_tok", r.get("error") and r.get("in_tok") is None)

print(f"\n{'OK' if not fails else 'FAIL'} test_subscription_lane_token_capture: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
