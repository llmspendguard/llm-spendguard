"""Two verified-high GUARD-HOLE defects — the 4-LLM review found gaps in spendguard's OWN guards:

  estimate_literals.literal_sites — scanned only positional args (n.args[1:]), so a cost call passing token
     counts as KEYWORDS — realtime_cost(m, input_tokens=700, output_tokens=1500) — slipped past the "a quote
     must come from measurement, not a literal" guard entirely. Now the keyword args are scanned too.

  token_caps.cap_key / sites — the cap identity was file::symbol::kwarg=value with no occurrence discriminator,
     so two IDENTICAL caps in one function (two max_tokens=512) collapsed to one key and the second was dropped
     from the judged set — a cap nobody looked at, reading as clean. sites() now stamps an occurrence index and
     cap_key suffixes duplicates (#2, #3 …); the first keeps its bare key so prior verdicts stay valid.

Offline, isolated home; each scan runs over a tiny temp source tree.
"""
import os
import sys
import tempfile
import pathlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-guardholes-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import estimate_literals as el, token_caps as tc   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── 1. estimate_literals now flags KEYWORD literals ──────────────────────────────────────────────────────────
d1 = tempfile.mkdtemp(prefix="ghlit-")
pathlib.Path(d1, "m.py").write_text(
    "from spendguard.pricing import realtime_cost\n"
    "def quote(m):\n"
    "    return realtime_cost(m, input_tokens=700, output_tokens=1500)\n")
sites = el.literal_sites(d1)
lits = [x for s in sites for x in s["literals"]]
ck("a kwarg-literal cost call is now flagged (was invisible to the positional-only scan)",
   700 in lits and 1500 in lits, str(sites))
# a model kwarg is a string, not a token literal — must NOT be flagged
d1b = tempfile.mkdtemp(prefix="ghlit2-")
pathlib.Path(d1b, "m.py").write_text(
    "from spendguard.pricing import realtime_cost\n"
    "def quote(a, b):\n"
    "    return realtime_cost(model='gpt', in_tok=a, out_tok=b)\n")
ck("a string kwarg (model=) and non-literal args are NOT flagged", el.literal_sites(d1b) == [])


# ── 2. token_caps captures BOTH of two identical caps in one symbol, with distinct keys ───────────────────────
d2 = tempfile.mkdtemp(prefix="ghcap-")
pathlib.Path(d2, "c.py").write_text(
    "def worker():\n"
    "    a = call(max_tokens=512)\n"
    "    b = call(max_tokens=512)\n"
    "    return a, b\n")
ss = tc.sites(d2)
keys = [tc.cap_key(s) for s in ss]
ck("two identical caps in one symbol are BOTH captured (not collapsed to one)", len(ss) == 2, str(len(ss)))
ck("...with DISTINCT cap_keys (first bare, second suffixed)", len(set(keys)) == 2, str(keys))
ck("...and the first occurrence keeps its bare key (prior verdicts stay valid)",
   any("#" not in k for k in keys) and any("#2" in k for k in keys), str(keys))

print(f"\n{'[FAIL]' if fails else 'OK'} test_guard_holes_closed: {fails} failure(s)")
sys.exit(1 if fails else 0)
