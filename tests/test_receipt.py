"""receipt.py — the inline per-flow + running-tally emitter. Script-style, offline (no network, no LLM).

Guards the invariants the feature exists to hold:
  • the TWO AXES (actual-$ billed vs est-value plan-usage) render as SEPARATE lines and are never summed;
  • verbosity gating (off → silent; flow/verbose → emit; footer → tally only);
  • per-flow aggregation from the calls log (tokens + count) with graceful fallback to the budget-$ delta;
  • est-value is stamped per-source so claude-code and claude.ai SUM rather than clobber, and carries an as-of date;
  • emit_flow NEVER raises into the caller and stays silent when a flow neither called nor spent.
"""
import os, sys, io, tempfile, contextlib

# Set unconditionally (before any spendguard import) so they hold whether THIS script self-isolates OR an external
# runner (test_runner.py) already provided the isolated SPENDGUARD_HOME + SPENDGUARD_TEST_ISOLATED. Previously these
# lived inside the self-isolation block, so under test_runner the flow-aggregation tests ran with calls logging off.
os.environ["SPENDGUARD_CALLS"] = "1"            # exercise the rich per-call flow aggregation
os.environ.pop("SPENDGUARD_RECEIPTS", None)     # default level = flow

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-receipt-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import receipt, calls, budget

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

TODAY, WEEK, MONTH = receipt._windows()

# ── formatting ────────────────────────────────────────────────────────────---
ck("_money: None → em-dash", receipt._money(None) == "—")
ck("_money: thousands", receipt._money(1234.5) == "$1,234.50")
ck("_tok: compact M/K", receipt._tok(1_200_000) == "1.2M" and receipt._tok(12_300) == "12.3K" and receipt._tok(0) == "0")
ck("_pct: actual under estimate → negative", receipt._pct(2.10, 1.87).strip() == "(−11%)")
ck("_pct: missing estimate → blank", receipt._pct(None, 1.0) == "" and receipt._pct(0, 1.0) == "")

# ── level resolution ──────────────────────────────────────────────────────---
os.environ["SPENDGUARD_RECEIPTS"] = "off"
ck("level: env off", receipt.level() == "off")
os.environ["SPENDGUARD_RECEIPTS"] = "bogus"
ck("level: unknown env → flow", receipt.level() == "flow")
os.environ["SPENDGUARD_RECEIPTS"] = "verbose"
ck("level: env verbose", receipt.level() == "verbose")
del os.environ["SPENDGUARD_RECEIPTS"]
ck("level: default → flow", receipt.level() == "flow")

# ── est-value stamping: per-source SUM (no clobber) + as-of ──────────────────
receipt.stamp_est_value([{"day": TODAY, "spend_micros": 5_000_000, "billed": False},   # $5.00
                         {"day": TODAY, "spend_micros": 1_000_000, "billed": True}],    # billed → MUST be ignored
                        source="claude-code")
receipt.stamp_est_value([{"day": TODAY, "spend_micros": 2_000_000, "billed": False}],   # $2.00
                        source="claude-ai")
ev = receipt._est_tally()
ck("est-value: billed rows excluded; two sources SUM to $7.00", ev and abs(ev["today"] - 7.0) < 1e-9)
ck("est-value: carries an as-of date", ev and ev.get("asof") == TODAY)

# ── the tally: two axes, separate, never summed ──────────────────────────────
budget.spent_since = lambda day, project=None, conv=None: {TODAY: 4.20, WEEK: 31.50, MONTH: 212.40}.get(day, 99.0)   # stub the gate ledger
t = receipt.tally()
ck("tally: actual-$ windows from the gate ledger", t["actual"]["today"] == 4.20 and t["actual"]["month"] == 212.40)
ck("tally: est-value present + distinct dict", t["est_value"] and abs(t["est_value"]["today"] - 7.0) < 1e-9)
out = receipt.render_tally(t)
ck("render_tally: real-$ (API+subs+remote) and est-value are SEPARATE", "real $ this month" in out and "est sub value (plan usage, NOT billed)" in out)
# HARD RULE: the two axes are never summed. real month = API 212.40 + subs 400 = 612.40; est month = 7.00. Neither
# real+est (619.40) nor API+est (219.40) may ever appear as one number.
ck("render_tally: no combined total (axes never summed)", "619.40" not in out and "219.40" not in out)
ck("render_tally: API today shown", "today $4.20" in out)

# ── render_flow: est → actual variance + the tally underneath ────────────────
flow = {"intent": "loinc-typing", "n": 42, "in_tok": 1_200_000, "out_tok": 300_000, "est": 2.10, "actual": 1.87}
rf = receipt.render_flow(flow, "flow", t)
ck("render_flow: what + calls + tokens", "loinc-typing" in rf and "42 calls" in rf and "in 1.2M / out 300.0K" in rf)
ck("render_flow: est → actual + variance", "est $2.10 → actual $1.87" in rf and "(−11%)" in rf)
ck("render_flow: running tally is included underneath", "real $ this month" in rf and "$212.40" in rf)
flow_noest = {"intent": "x", "n": 1, "actual": 0.5}
# no est→actual arrow in the flow HEAD (the tally line below may carry a '→ N× subscription' note — check head only)
ck("render_flow: no estimate → actual only (no arrow)", "→" not in receipt.render_flow(flow_noest, "flow", t).split("\n")[0])

# ── render_line: one compact line for a status bar, both axes still separate ──
ln = receipt.render_line(t)
ck("render_line: single line", "\n" not in ln)
ck("render_line: real-$ + est value both present, labelled, separate", "real" in ln and "est value" in ln and "::" in ln)
ck("render_line: _k compacts ≥$1000 to k, keeps small values plain",
   receipt._k(2015.43) == "$2.0k" and receipt._k(212.40) == "$212" and receipt._k(None) == "—")

# ── verbose adds the caller (and a tip only if the advisor has one) ──────────
rv = receipt.render_flow({"intent": "x", "n": 1, "actual": 0.5, "caller": "run.py:main:10"}, "verbose", t)
ck("render_flow verbose: shows caller", "run.py:main:10" in rv)

# ── flow aggregation from the calls log ──────────────────────────────────────
ck("calls logging enabled in this test", calls.enabled())
start = calls._max_rowid()
calls.record("anthropic", "claude-opus-4-8", "completion", 0.10, in_tok=1000, out_tok=200, intent="t", chain="run-1")
calls.record("anthropic", "claude-opus-4-8", "completion", 0.05, in_tok=500, out_tok=100, intent="t", chain="run-1")
calls.record("openai", "gpt-5.5", "completion", 0.20, in_tok=9, out_tok=9, intent="t", chain="OTHER")
agg = calls.flow_agg(start, chain="run-1")
ck("flow_agg: counts only this chain's calls since the marker", agg and agg["n"] == 2)
ck("flow_agg: sums tokens", agg and agg["in_tok"] == 1500 and agg["out_tok"] == 300)
ck("flow_agg: sums cost", agg and abs(agg["cost"] - 0.15) < 1e-9)

# ── emit_flow: gating + silence + never-raises ───────────────────────────────
def _emit_capture(intent, chain, start, level_val):
    os.environ["SPENDGUARD_RECEIPTS"] = level_val
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        receipt.emit_flow(intent, chain, start)
    del os.environ["SPENDGUARD_RECEIPTS"]
    return buf.getvalue()

s0 = calls._max_rowid()
calls.record("anthropic", "claude-opus-4-8", "completion", 0.07, in_tok=300, out_tok=50, intent="emit-test", chain="run-2")
ck("emit_flow: level off → silent", _emit_capture("emit-test", "run-2", (s0, 0.0), "off") == "")
emitted = _emit_capture("emit-test", "run-2", (s0, 0.0), "flow")
ck("emit_flow: level flow → emits a receipt with the intent", "spendguard ▸ emit-test" in emitted)
mark = calls._max_rowid()
start_usd = budget.spent_since("1970-01-01")    # snapshot consistently, as calls.context does → idle delta is 0
ck("emit_flow: nothing happened since marker → silent", _emit_capture("idle", "none", (mark, start_usd), "flow") == "")

# never-raises: a broken budget must not blow up the caller's flow
_orig = budget.spent_since
budget.spent_since = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    receipt.emit_flow("x", "y", (0, 0.0))   # must swallow
    ck("emit_flow: swallows internal errors (never raises into caller)", True)
except Exception:
    ck("emit_flow: swallows internal errors (never raises into caller)", False)
budget.spent_since = _orig

# ── integration: the real `with calls.context(...)` boundary emits on exit ───
os.environ["SPENDGUARD_RECEIPTS"] = "flow"
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    with calls.context(intent="ctx-flow", chain="ctx-1"):
        calls.record("anthropic", "claude-opus-4-8", "completion", 0.03, in_tok=100, out_tok=20, chain="ctx-1")
del os.environ["SPENDGUARD_RECEIPTS"]
ck("context(): emits a per-flow receipt on exit (real boundary)", "spendguard ▸ ctx-flow" in buf.getvalue())
os.environ["SPENDGUARD_RECEIPTS"] = "off"
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    with calls.context(intent="ctx-flow", chain="ctx-2"):
        calls.record("anthropic", "claude-opus-4-8", "completion", 0.03, chain="ctx-2")
del os.environ["SPENDGUARD_RECEIPTS"]
ck("context(): respects level off (no emit)", buf.getvalue() == "")

# ── cli: prints the tally to STDOUT (what the in-chat hook captures) ──────────
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = receipt.cli([])
ck("cli: returns 0 and prints a (scoped) tally to stdout", rc == 0 and "Actual $" in buf.getvalue() and "Est value $" in buf.getvalue())
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    receipt.cli(["--all"])
ck("cli --all: renders the org → team → project tree", "org → team → project" in buf.getvalue())
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    receipt.cli(["--json"])
ck("cli --json: machine-readable, keeps axes separate", '"actual"' in buf.getvalue() and '"est_value"' in buf.getvalue())

# ── hook protocols: --stop-hook (systemMessage JSON) + --statusline (stdin JSON → line) ──
import json as _json
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    receipt.cli(["--stop-hook"])
hk = _json.loads(buf.getvalue())   # MUST be valid JSON — Claude Code parses it
ck("--stop-hook: valid JSON, systemMessage carries the tally", "systemMessage" in hk and "real" in hk["systemMessage"] and "est value" in hk["systemMessage"])

_real_stdin = receipt.sys.stdin
receipt.sys.stdin = io.StringIO('{"workspace":{"current_dir":"/a/b/myrepo"},'
                                '"model":{"display_name":"Opus 4.8"},"context_window":{"used_percentage":12.4}}')
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    receipt.cli(["--statusline"])
receipt.sys.stdin = _real_stdin
sl = buf.getvalue()
ck("--statusline: prefixes cwd · model · ctx% then the tally",
   "myrepo" in sl and "Opus 4.8" in sl and "12% ctx" in sl and "real" in sl)

# ── configurable sinks: a file sink routes the auto-emitted receipt to a log (any-host / Codex surfacing) ──
import pathlib as _pl
logf = _pl.Path(tempfile.mkdtemp()) / "r.log"
os.environ["SPENDGUARD_RECEIPTS_SINK"] = f"file:{logf}"
ck("sinks: env override parsed", receipt._sinks() == [f"file:{logf}"])
receipt._out("hello-sink-line")
del os.environ["SPENDGUARD_RECEIPTS_SINK"]
ck("sink file: writes the receipt to the configured log", logf.exists() and "hello-sink-line" in logf.read_text())
ck("sinks: default is stderr", receipt._sinks() == ["stderr"])

# ── ORG → TEAM → PROJECT attribution: est-value cells + tree (the model — matches the server) ──
receipt.stamp_est_value([
    {"day": TODAY, "spend_micros": 3_000_000, "billed": False, "org": "Healiom", "team": "clinical-ai", "project": "medical-taxonomy"},
    {"day": TODAY, "spend_micros": 2_000_000, "billed": False, "org": "Healiom", "team": "fundraising-exec", "project": "investor-deck"},
    {"day": TODAY, "spend_micros": 1_000_000, "billed": False, "org": "manga2anime", "team": "", "project": "caption"},
], source="claude-code")
ck("est scope: ORG rollup (Healiom = 3+2 = $5)", abs(receipt._est_tally(org="Healiom")["today"] - 5.0) < 1e-9)
ck("est scope: TEAM rollup (Healiom/clinical-ai = $3)", abs(receipt._est_tally(org="Healiom", team="clinical-ai")["today"] - 3.0) < 1e-9)
ck("est scope: PROJECT (medical-taxonomy = $3)", abs(receipt._est_tally(project="medical-taxonomy")["today"] - 3.0) < 1e-9)
ck("est scope: other org isolated (manga2anime = $1)", abs(receipt._est_tally(org="manga2anime")["today"] - 1.0) < 1e-9)
ck("est scope: global sums all sources (≥ this source's $6)", receipt._est_tally()["today"] >= 6.0)

tree = receipt._est_tree()
ck("_est_tree: org → team → project nesting",
   "healiom" in tree and "clinical-ai" in tree["healiom"]["teams"] and "medical-taxonomy" in tree["healiom"]["teams"]["clinical-ai"]["projects"])
ck("_est_tree: org rollup month = Σ its teams ($5)", abs(tree["healiom"]["month"] - 5.0) < 1e-9)
ck("_est_tree(scope_org): limits to one org", set(receipt._est_tree("manga2anime").keys()) == {"manga2anime"})

rt = receipt.render_tree()
ck("render_tree: shows org → team → project", "healiom" in rt.lower() and "clinical-ai" in rt and "medical-taxonomy" in rt)
ck("render_tree: header is the two-axis Actual$ | Est-value$ TABLE", "Actual $" in rt and "Est value $" in rt and "never added" in rt)

# ── two-axis TABLE: Actual $ and Est value $ are SEPARATE COLUMNS, totalled independently, never one number ──
tt = receipt.tally()
tbl = receipt._two_axis_table(tt)
joined = "\n".join(tbl)
ck("_two_axis_table: header names both columns", "Actual $" in tbl[0] and "Est value $" in tbl[0])
ck("_two_axis_table: billed components in Actual col (API/Remote/Subscription rows present)",
   any("API" in r for r in tbl) and any("Remote" in r for r in tbl) and any("Subscription" in r for r in tbl))
ck("_two_axis_table: plan-usage row is est-value with — in the Actual column (a $0 actual, never mixed)",
   any("Plan usage" in r and "—" in r for r in tbl))
ck("_two_axis_table: TOTAL row carries BOTH column totals, separately (not summed)",
   any(r.startswith("TOTAL") for r in tbl))

# ── global running tally (header / statusline line) + proportional plan multiple ──
t = receipt.tally()
ck("tally(): global, both axes present", t["actual"]["month"] is not None and t["est_value"] is not None)
os.environ["SPENDGUARD_PLAN_USD"] = "200"
ck("tally: plan multiple set when price given, not assumed", receipt.tally().get("plan_mult") is not None and receipt.tally().get("plan_assumed") is False)
del os.environ["SPENDGUARD_PLAN_USD"]
ck("tally: defaults to assumed plan (Anthropic Max + OpenAI Pro)", receipt.tally().get("plan_assumed") is True)
ck("_plan_usd: default total = Anthropic Max + OpenAI Pro = $400", receipt._plan_usd() == (400.0, True))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
