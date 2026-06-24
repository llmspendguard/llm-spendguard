"""receipt.py — the inline per-flow + running-tally emitter. Script-style, offline (no network, no LLM).

Guards the invariants the feature exists to hold:
  • the TWO AXES (actual-$ billed vs est-value plan-usage) render as SEPARATE lines and are never summed;
  • verbosity gating (off → silent; flow/verbose → emit; footer → tally only);
  • per-flow aggregation from the calls log (tokens + count) with graceful fallback to the budget-$ delta;
  • est-value is stamped per-source so claude-code and claude.ai SUM rather than clobber, and carries an as-of date;
  • emit_flow NEVER raises into the caller and stays silent when a flow neither called nor spent.
"""
import os, sys, io, tempfile, contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-receipt-")
    os.environ["SPENDGUARD_CALLS"] = "1"            # exercise the rich per-call flow aggregation
    os.environ.pop("SPENDGUARD_RECEIPTS", None)     # default level = flow
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
ck("render_tally: actual-$ and est-value are SEPARATE lines", "actual-$ (billed)" in out and "est-value (plan, not billed)" in out)
ck("render_tally: no combined total (axes never summed)", "11.20" not in out and "$11" not in out)  # 4.20+7.00 must NOT appear
ck("render_tally: actual today shown", "today $4.20" in out)

# ── render_flow: est → actual variance + the tally underneath ────────────────
flow = {"intent": "loinc-typing", "n": 42, "in_tok": 1_200_000, "out_tok": 300_000, "est": 2.10, "actual": 1.87}
rf = receipt.render_flow(flow, "flow", t)
ck("render_flow: what + calls + tokens", "loinc-typing" in rf and "42 calls" in rf and "in 1.2M / out 300.0K" in rf)
ck("render_flow: est → actual + variance", "est $2.10 → actual $1.87" in rf and "(−11%)" in rf)
ck("render_flow: running tally is included underneath", "actual-$ (billed)" in rf and "month $212.40" in rf)
flow_noest = {"intent": "x", "n": 1, "actual": 0.5}
ck("render_flow: no estimate → actual only (no arrow)", "→" not in receipt.render_flow(flow_noest, "flow", t))

# ── render_line: one compact line for a status bar, both axes still separate ──
ln = receipt.render_line(t)
ck("render_line: single line", "\n" not in ln)
ck("render_line: billed + plan both present, labelled", "billed" in ln and "plan" in ln)
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
ck("cli: returns 0 and prints a (scoped) tally to stdout", rc == 0 and "actual-$ (billed)" in buf.getvalue())
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    receipt.cli(["--all"])
ck("cli --all: includes the global 'all repos' total", "all repos" in buf.getvalue())
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
ck("--stop-hook: valid JSON, systemMessage carries the tally", "systemMessage" in hk and "billed" in hk["systemMessage"])

_real_stdin = receipt.sys.stdin
receipt.sys.stdin = io.StringIO('{"workspace":{"current_dir":"/a/b/myrepo"},'
                                '"model":{"display_name":"Opus 4.8"},"context_window":{"used_percentage":12.4}}')
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    receipt.cli(["--statusline"])
receipt.sys.stdin = _real_stdin
sl = buf.getvalue()
ck("--statusline: prefixes cwd · model · ctx% then the tally",
   "myrepo" in sl and "Opus 4.8" in sl and "12% ctx" in sl and "billed" in sl)

# ── configurable sinks: a file sink routes the auto-emitted receipt to a log (any-host / Codex surfacing) ──
import pathlib as _pl
logf = _pl.Path(tempfile.mkdtemp()) / "r.log"
os.environ["SPENDGUARD_RECEIPTS_SINK"] = f"file:{logf}"
ck("sinks: env override parsed", receipt._sinks() == [f"file:{logf}"])
receipt._out("hello-sink-line")
del os.environ["SPENDGUARD_RECEIPTS_SINK"]
ck("sink file: writes the receipt to the configured log", logf.exists() and "hello-sink-line" in logf.read_text())
ck("sinks: default is stderr", receipt._sinks() == ["stderr"])

# ── per-project SCOPING: est-value buckets by project; tally(project=…) scopes + labels both axes ──
receipt.stamp_est_value([{"day": TODAY, "spend_micros": 3_000_000, "billed": False, "project": "lmm"},
                         {"day": TODAY, "spend_micros": 1_000_000, "billed": False, "project": "manga2anime"}],
                        source="claude-code")
ck("est scope: lmm bucket isolated", abs(receipt._est_tally(project="lmm")["today"] - 3.0) < 1e-9)
ck("est scope: manga2anime bucket isolated", abs(receipt._est_tally(project="manga2anime")["today"] - 1.0) < 1e-9)
ck("est scope: unknown project → 0", receipt._est_tally(project="nope")["today"] == 0)
ck("est scope: global still sums all projects", receipt._est_tally()["today"] >= 4.0)
ts = receipt.tally(project="lmm")
ck("tally(project): carries the scope label", ts.get("scope") == "lmm")
ck("render: scope label shown in block + line", "[lmm]" in receipt.render_tally(ts) and "[lmm]" in receipt.render_line(ts))
ck("_project_for_cwd: basename fallback", receipt._project_for_cwd("/a/b/MyRepo") == "myrepo")

# ── proportional plan share: repo est as % of total, + $ slice when a plan price is set ──
os.environ["SPENDGUARD_PLAN_USD"] = "200"
tl = receipt.tally(project="lmm")
ck("proportional: est_pct present (scoped + est exists)", tl.get("est_pct") is not None)
ck("proportional: plan_slice set when plan price configured", tl.get("plan_slice") is not None)
ck("render_line: shows '% of plan'", "% of plan" in receipt.render_line(tl))
del os.environ["SPENDGUARD_PLAN_USD"]
ck("proportional: no plan_slice without a plan price (still shows %)",
   receipt.tally(project="lmm").get("plan_slice") is None and receipt.tally(project="lmm").get("est_pct") is not None)

# ── contextual collapse/expand: conversation repo(s) vs all repos ──
ck("_all_projects: includes est-only repos", {"lmm", "manga2anime"}.issubset(set(receipt._all_projects())))
out_all = receipt._render_scope(scope_all=True, line=True)
ck("--all (expanded): lists each repo + an 'all repos' total", "[lmm]" in out_all and "[manga2anime]" in out_all and "all repos" in out_all)
out_collapsed = receipt._render_scope(scope_all=False, cwd="/a/b/lmm", line=True)
ck("default (collapsed): scoped to the cwd repo + offers --all", "[lmm]" in out_collapsed and ("more repo" in out_collapsed or "--all" in out_collapsed))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
