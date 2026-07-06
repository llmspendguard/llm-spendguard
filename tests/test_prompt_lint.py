"""Prompt-efficiency lint (prompts.py) + the pluggable judge seam (equivalence mode='custom:…').
Seeds a real calls table in the isolated HOME and checks each finding class fires on the shape it
hunts — and stays silent below thresholds. Offline, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-prompts-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import sqlite3
from spendguard import prompts, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

con = sqlite3.connect(config.db_path())
con.execute("""CREATE TABLE IF NOT EXISTS calls(
    id TEXT PRIMARY KEY, ts TEXT, chain TEXT, intent TEXT, caller TEXT, provider TEXT, model TEXT, kind TEXT,
    in_tok INTEGER, out_tok INTEGER, cost REAL, latency REAL,
    prompt_hash TEXT, prompt_snip TEXT, output_snip TEXT, finish TEXT,
    quality TEXT, quality_src TEXT, quality_conf REAL)""")
def row(i, intent, model="gpt-5.5", in_tok=100, out_tok=50, cost=0.01, finish="stop", snip=""):
    con.execute("INSERT INTO calls (id, ts, intent, model, in_tok, out_tok, cost, finish, prompt_snip) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{intent}-{i}", "2026-07-01T00:00:00", intent, model, in_tok, out_tok, cost, finish, snip))

BOILER = "You are an expert clinical coder. Follow the 12 rules below exactly. Rules: " + "R" * 40
for i in range(8):
    row(i, "typing", snip=BOILER + f" item {i}")                      # long shared prefix ≥50% of prompt
for i in range(10):
    row(i, "rag-answer", in_tok=(400 if i < 8 else 4000))             # p95 ≫ 3× p50, spread > 500 tok
for i in range(6):
    row(i, "extract", out_tok=200, finish=("length" if i < 2 else "stop"))   # 2 truncations
for i in range(12):
    row(i, "classify", model=("gpt-5-nano" if i < 6 else "claude-opus-4-8"),
        cost=(0.001 if i < 6 else 0.05))                              # 50x cheaper alternative in the mix
for i in range(3):
    row(i, "tiny", snip=BOILER)                                       # below min_calls → silent
con.commit(); con.close()

fs = prompts.lint()
kinds = {(f["intent"], f["kind"]) for f in fs}
ck("boilerplate prefix flagged", ("typing", "boilerplate") in kinds)
ck("context spread flagged", ("rag-answer", "context_spread") in kinds)
ck("truncation flagged with a p99-based recommendation",
   any(f["kind"] == "truncation" and "max_tokens ≈" in f["next"] for f in fs))
ck("model mix flagged as a measured cascade candidate",
   any(f["kind"] == "model_mix" and "experiment" in f["next"] for f in fs))
ck("below min_calls stays silent", not any(f["intent"] == "tiny" for f in fs))
ck("every finding carries a next step", all(f.get("next") for f in fs))
ck("ranked by $ at stake", [f.get("est_usd") or 0 for f in fs] == sorted((f.get("est_usd") or 0 for f in fs), reverse=True))
ck("intent filter narrows", {f["intent"] for f in prompts.lint(intent="typing")} == {"typing"})
ck("boilerplate savings priced via pricing.py (not None for a priced model)",
   any(f["kind"] == "boilerplate" and f["est_usd"] for f in fs))

# ── the pluggable judge seam: mode='custom:<module.fn>' drives grade() ──
modtmp = tempfile.mkdtemp()
with open(os.path.join(modtmp, "myjudge.py"), "w") as f:
    f.write("def score(ref, out):\n    return 0.75 if ref != out else 1.0\n")
sys.path.insert(0, modtmp)
from spendguard import equivalence
ck("custom judge called via mode=custom:", equivalence.grade("a", "b", mode="custom:myjudge.score") == (0.75, "custom"))
ck("custom judge score clamped to 0..1",
   equivalence.grade("a", "a", mode="custom:myjudge.score") == (1.0, "custom"))

import inspect
from spendguard import cli
ck("CLI wired: `spendguard prompts`", '"prompts"' in inspect.getsource(cli.main))

print(("[OK]" if not fails else "[FAIL]") + " prompt-lint: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
