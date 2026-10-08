"""delegate author-in-worktree contract (Review 2). Offline — codex_exec.run_prompt, classify_task and
delegation_lanes_ready are stubbed; the changed-file set is exercised against a REAL throwaway git repo. Zero spend.

Pins the four DONE-MEANS points:
(a) the warm daemon is the DEFAULT agentic codex route, with an opt-out to cold exec;
(b) the sandbox/git/hook constraints are baked into the prompt codex actually receives (+ workspace-write + cwd);
(c) delegate hands back the CHANGED-FILE SET (only the agent's edits, pre-existing dirt subtracted);
(d) a not-READY codex lane REFUSES and names ready alternatives — never falls open to the capped Claude plan."""
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-delwt-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import delegate_router as dr, codex_exec, lane_registry  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


# ── (a) warm daemon is the default agentic codex route, opt-out to cold exec ───────────────────────────────────
os.environ.pop("SPENDGUARD_CODEX_DAEMON", None)
ck("warm daemon is the DEFAULT agentic codex route", codex_exec._daemon_enabled() is True)
os.environ["SPENDGUARD_CODEX_DAEMON"] = "0"
ck("explicit opt-out falls back to cold exec", codex_exec._daemon_enabled() is False)
os.environ.pop("SPENDGUARD_CODEX_DAEMON", None)

# ── (b) sandbox/git/hook constraints baked into the prompt codex actually receives ─────────────────────────────
_sent = {}
_real_run = codex_exec.run_prompt
def _capture(prompt, **kw):
    _sent["prompt"], _sent["kw"] = prompt, kw
    return {"text": "ok", "error": None, "in_tok": 10, "out_tok": 2, "cost": 0.0}
codex_exec.run_prompt = _capture
try:
    cwd = tempfile.mkdtemp(prefix="sg-wt-")
    codex_plan = next((p for p in ("codex",) if (lane_registry.lane_spec(p) or {}).get("exec") == "codex_exec"), None)
    ck("a codex plan with the codex_exec lane exists", codex_plan is not None)
    dr._route_agentic(codex_plan, "refactor the parser", "gpt-6-sol", None, cwd)
    p = _sent.get("prompt", "")
    ck("constraint present: edit files directly, no git", "Do NOT run git" in p and "EDITING FILES DIRECTLY" in p)
    ck("constraint present: no PR / no push", "do NOT open a PR" in p)
    ck("constraint present: host hooks not required, do not bail", "hooks" in p.lower() and "bail" in p.lower())
    ck("the task itself is preserved ahead of the constraints", p.startswith("refactor the parser"))
    ck("codex gets workspace-write + the explicit cwd", _sent["kw"].get("sandbox") == "workspace-write" and _sent["kw"].get("cwd") == cwd)
finally:
    codex_exec.run_prompt = _real_run

# ── (c) changed-file set = only the agent's edits (pre-existing dirt subtracted), None off-git ─────────────────
repo = tempfile.mkdtemp(prefix="sg-gitwt-")
subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "init"], check=True)
with open(os.path.join(repo, "preexist.txt"), "w") as f:
    f.write("already dirty before the delegation")
before = dr._worktree_state(repo)[1]
ck("pre-existing dirt is captured at entry", "preexist.txt" in before)
with open(os.path.join(repo, "authored.py"), "w") as f:
    f.write("print('codex wrote this')\n")
changed = dr._worktree_changed_files(repo, before)
ck("changed_files = the agent's new file", changed == ["authored.py"])
ck("pre-existing dirt is NOT mis-attributed to the agent", "preexist.txt" not in (changed or []))
nongit = tempfile.mkdtemp(prefix="sg-nongit-")
ck("a non-git cwd → changed_files None (never a wrong set)", dr._worktree_changed_files(nongit, set()) is None)

# ── (d) a not-READY codex lane REFUSES and names alternatives — never falls to the capped Claude plan ─────────
dr.classify_task = lambda task, files=None, provider="auto": {
    "kind": "agentic", "self_contained": True, "provider": "codex", "why": "edits source in a repo"}
dr.delegation_lanes_ready = lambda kind: {"ready": ["gemini", "zai-coding"]}   # codex deliberately NOT ready
res = dr.delegate_task("fix and test the module", intent="x", provider="auto", execute=True, cwd=repo)
ck("not-ready codex → REFUSED (never executed)", res["status"] == "refused")
ck("refusal names ready alternatives", bool(res.get("ready_alternatives")))
ck("refusal NEVER falls open to the Claude plan", "claude" not in str(res.get("ready_alternatives", [])).lower())

print(f"\n{'[FAIL]' if fails else 'OK'} test_delegate_author_in_worktree: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
