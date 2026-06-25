"""Codex adapter + OpenAI Responses-API gating — "spendguard works great for OpenAI + Codex". Script-style, offline.

Guards:
  • codex._digest reads the FINAL cumulative token_count + the model from turn_context, prices it, project=cwd;
  • day_totals emits channel=codex, billed=false, provider=openai (est-value, never actual-$) — server contract
    identical to claudecode;
  • show() stamps est-value under source=codex so the receipt SUMS claude-code + claude-ai + codex;
  • the gate's Responses-API estimator/actual read input/output_tokens + input_tokens_details.cached_tokens
    (the modern OpenAI surface Codex and newer SDK code use — previously an un-gated realtime gap).
"""
import os, sys, io, json, tempfile, contextlib, pathlib

# CODEX_DIR must exist whether THIS script self-isolates OR test_runner.py provides isolation (it sets SPENDGUARD_HOME
# + SPENDGUARD_TEST_ISOLATED but NOT CODEX_DIR). setdefault so both paths get a valid session dir. Before any import.
os.environ.setdefault("SPENDGUARD_CODEX_DIR", tempfile.mkdtemp(prefix="spendguard-codex-sess-"))

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-codex-home-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import codex, pricing, gate, receipt

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── build a synthetic Codex session in the real on-disk layout (sessions/YYYY/MM/DD/rollout-*.jsonl) ──
sess_dir = pathlib.Path(os.environ["SPENDGUARD_CODEX_DIR"]) / "2026" / "06" / "23"
sess_dir.mkdir(parents=True, exist_ok=True)
session = sess_dir / "rollout-2026-06-23T10-00-00-abc.jsonl"
rows = [
    {"type": "session_meta", "payload": {"session_id": "abc", "timestamp": "2026-06-23T10:00:00Z",
                                          "cwd": "/Users/x/Documents/myproj", "model_provider": "openai"}},
    {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "high"}},
    {"type": "event_msg", "payload": {"type": "user_message", "message": "build the LOINC mapper"}},
    {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"plan_type": "team"},
        "info": {"total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                       "output_tokens": 200, "reasoning_output_tokens": 50, "total_tokens": 1200}}}},
    {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"plan_type": "team"},
        "info": {"total_token_usage": {"input_tokens": 3000, "cached_input_tokens": 1500,
                                       "output_tokens": 600, "reasoning_output_tokens": 100, "total_tokens": 3600}}}},
]
session.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

# ── _digest: final cumulative usage + model + project + prompt ────────────────
d = codex._digest(str(session))
exp_cost = pricing.realtime_cost("gpt-5.5", 3000, 600, 1500)
ck("_digest: model from turn_context", d and d["model"] == "gpt-5.5")
ck("_digest: uses the FINAL cumulative token_count (3000/600/1500)", d and d["in_tok"] == 3000 and d["out_tok"] == 600 and d["cached_tok"] == 1500)
ck("_digest: project = cwd basename", d and d["project"] == "myproj")
ck("_digest: prompt seed = first user_message", d and d["prompt"] == "build the LOINC mapper")
ck("_digest: priced via realtime_cost (cached discount)", d and abs(d["cost"] - exp_cost) < 1e-9 and d["cost"] > 0)
ck("_digest: plan_type captured", d and d["plan"] == "team")

# ── incremental watermark: FIRST (on fresh state, before show/day_totals mine) ─
st, n1 = codex.update()
st, n2 = codex.update()
ck("update(): re-mines on first pass, skips unchanged on second", n1 >= 1 and n2 == 0)

# ── day_totals: channel=codex, billed=false, provider=openai (est-value axis) ─
dt = codex.day_totals("me@x.com", org_label=None)   # no cls → cwd-fallback project, local view
ck("day_totals: one row", len(dt) == 1)
r0 = dt[0] if dt else {}
ck("day_totals: channel=codex", r0.get("channel") == "codex")
ck("day_totals: billed=False (est-value, NOT actual-$)", r0.get("billed") is False)
ck("day_totals: provider=openai", r0.get("provider") == "openai")
ck("day_totals: project from cwd", r0.get("project") == "myproj")
ck("day_totals: spend_micros = cost×1e6", r0.get("spend_micros") == round(exp_cost * 1_000_000))
ck("day_totals: token breakdown carried", r0.get("in_tokens") == 3000 and r0.get("out_tokens") == 600 and r0.get("cached_in_tokens") == 1500)

# ── show() stamps est-value under source=codex → receipt SUMS it ─────────────
with contextlib.redirect_stdout(io.StringIO()):
    codex.show()
ev = json.loads((pathlib.Path(os.environ["SPENDGUARD_HOME"]) / "receipt_cache.json").read_text())
ck("show(): stamps est_value_by_source['codex']", "codex" in ev.get("est_value_by_source", {}))
ck("show(): codex est-value month = the session cost", abs(ev["est_value_by_source"]["codex"]["month"] - exp_cost) < 1e-6)
# prove the receipt sums sources (add a fake claude-code stamp, then the tally adds both)
receipt.stamp_est_value([{"day": codex.datetime.date.today().isoformat(), "spend_micros": 5_000_000, "billed": False}], source="claude-code")
t = receipt._est_tally()
ck("receipt._est_tally SUMS codex + claude-code", t and abs(t["month"] - (exp_cost + 5.0)) < 1e-6)

# ── the gate's OpenAI Responses-API estimator/actual (the modern surface) ─────
class _Obj:
    def __init__(self, **k): self.__dict__.update(k)

# actual: Responses usage = input_tokens / output_tokens (not prompt/completion)
res = _Obj(usage=_Obj(input_tokens=1000, output_tokens=200))
ck("_act_oai_resp: reads input/output_tokens", gate._act_oai_resp(res) == (1000, 200))
ck("_act_oai_resp: no usage → None", gate._act_oai_resp(_Obj()) is None)
# cached tokens come from input_tokens_details on the Responses API
res.usage.input_tokens_details = _Obj(cached_tokens=400)
ck("_cached_in: reads Responses input_tokens_details.cached_tokens", gate._cached_in(res) == 400)
# estimate: string input + instructions + output ceiling
m, i, o = gate._est_oai_resp({"model": "gpt-5.5", "input": "hello there world", "instructions": "be terse", "max_output_tokens": 500})
ck("_est_oai_resp: counts input + instructions, out = max_output_tokens", m == "gpt-5.5" and i > 0 and o == 500)
# estimate: list-of-messages input
_, i2, _ = gate._est_oai_resp({"model": "gpt-5.5", "input": [{"role": "user", "content": "a longer user message here"}]})
ck("_est_oai_resp: handles list input items", i2 > 0)

# ── git-root bucketing: a repo SUBDIR collapses to the repo (matches actual-$), not the cwd basename ──
from spendguard import config as _cfg
_repo_sub = os.path.dirname(os.path.abspath(__file__))     # .../llm-spendguard/tests  → git root = llm-spendguard
ck("git_root_project: a repo subdir → the repo name, not the subdir", _cfg.git_root_project(_repo_sub) == "llm-spendguard")
ck("git_root_project: non-repo path → None (caller falls back to basename)", _cfg.git_root_project("/nope/x/y") is None)
ck("codex._project_of: buckets a real repo subdir at the repo", codex._project_of(_repo_sub) == "llm-spendguard")

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
