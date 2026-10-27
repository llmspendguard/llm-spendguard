"""Guard: spendguard's COMPACTION LIFECYCLE — the PreCompact + SessionStart(compact) hooks and their installer.
  1. guidance_text() returns the preservation directive (decisions+rationale, paths/ids) — config-pathed, else fallback.
  2. record_precompact() records the event (trigger + pre-context) and persists the WHOLE preservation digest.
  3. SessionStart(source=compact) fills post-context, measures k×, and re-injects that persisted digest; a
     non-compact SessionStart is ignored.
  4. the installer wires BOTH hooks into settings.json and removes ONLY ours (a user's other hooks in the same group
     survive).
Hermetic: isolated SPENDGUARD_HOME + a monkeypatched ~/.claude for the installer. Zero spend, never touches real config."""
import os, sys, tempfile, json, pathlib, io, contextlib

os.environ["OPENAI_API_KEY"] = "sk-test-compaction-offline"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-compaction-offline"

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-compact-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402

from spendguard import compaction, receipt

fails = []
HOME = os.environ["SPENDGUARD_HOME"]
def tx(path, i, cr, cw, o):
    with open(path, "w") as f:
        f.write(json.dumps({"message": {"usage": {"input_tokens": i, "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw, "output_tokens": o}, "model": "claude-opus-4-8"}}) + "\n")
pre = os.path.join(HOME, "pre.jsonl"); tx(pre, 10000, 490000, 0, 100)      # pre-compaction context = 500,000
post = os.path.join(HOME, "post.jsonl"); tx(post, 5000, 40000, 0, 50)      # post-compaction context = 45,000

gt = compaction.guidance_text()
record_script_check(fails, "guidance_text names decisions+rationale and paths/ids", "rationale" in gt.lower() and "path" in gt.lower())

g = compaction.record_precompact({"session_id": "s1", "transcript_path": pre, "trigger": "auto"})
record_script_check(fails, "record_precompact returns the persisted guidance", gt == g)
record_script_check(fails, "event recorded with its trigger (auto)", compaction.event_summary()["by_trigger"].get("auto") == 1)
record_script_check(fails, "k× not measured until a post-compaction context arrives", compaction.measured_explicit_compaction_ratio() == (None, 0))

compaction.record_sessionstart({"session_id": "s1", "source": "compact", "transcript_path": post})
k, n = compaction.measured_explicit_compaction_ratio()
record_script_check(fails, "real per-event k× = pre/post = 500K/45K ≈ 11.1 (measured, not a heuristic)", round(k, 1) == 11.1 and n == 1)
compaction.record_sessionstart({"session_id": "sX", "source": "startup", "transcript_path": post})
record_script_check(fails, "a NON-compact SessionStart is ignored (still 1 event)", compaction.event_summary()["events"] == 1)

# ── authoritative Claude Code hook schemas + cross-compaction re-injection ──
def hook(args, payload):
    old = receipt.sys.stdin
    receipt.sys.stdin = io.StringIO(json.dumps(payload))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = receipt.cli(args)
    finally:
        receipt.sys.stdin = old
    record_script_check(fails, "hook exits cleanly: " + args[0], rc == 0)
    return json.loads(buf.getvalue()) if buf.getvalue().strip() else {}

whole_digest = "goal [NEXT] edit receipt.py:1383; decision=SessionStart because PreCompact cannot inject; port 4317"
old_guidance = compaction.guidance_text
compaction.guidance_text = lambda: whole_digest
pre_out = hook(["--precompact-hook"], {"session_id": "hook-s", "transcript_path": pre, "trigger": "manual"})
compaction.guidance_text = old_guidance
allowed_precompact = {"continue", "systemMessage", "suppressOutput", "terminalSequence", "decision", "reason"}
record_script_check(fails, "PreCompact emits only allowed top-level fields", set(pre_out) <= allowed_precompact)
record_script_check(fails, "PreCompact never emits hookSpecificOutput", "hookSpecificOutput" not in pre_out)

compact_out = hook(["--sessionstart-hook"], {"session_id": "hook-s", "source": "compact",
                                             "transcript_path": post})
specific = compact_out.get("hookSpecificOutput") or {}
record_script_check(fails, "SessionStart(compact) uses its supported hookEventName", specific.get("hookEventName") == "SessionStart")
record_script_check(fails, "SessionStart(compact) re-injects the persisted digest WHOLE",
   whole_digest in specific.get("additionalContext", ""))
startup_out = hook(["--sessionstart-hook"], {"session_id": "hook-s", "source": "startup",
                                             "transcript_path": post})
record_script_check(fails, "SessionStart(startup) emits no preservation directive", "hookSpecificOutput" not in startup_out)

# Stop keeps the user-facing tally and additionally nudges the MODEL only when over threshold.
with open(os.path.join(HOME, "compaction_hint.json"), "w") as f:
    json.dump({"threshold_tokens": 100000, "k": 11.2}, f)
stop_big = hook(["--stop-hook"], {"transcript_path": pre})
record_script_check(fails, "over-threshold Stop emits user systemMessage", "systemMessage" in stop_big and "/compact" in stop_big["systemMessage"])
record_script_check(fails, "over-threshold Stop also emits model-visible additionalContext",
   (stop_big.get("hookSpecificOutput") or {}).get("hookEventName") == "Stop" and
   "/compact" in (stop_big.get("hookSpecificOutput") or {}).get("additionalContext", ""))
stop_small = hook(["--stop-hook"], {"transcript_path": post})
record_script_check(fails, "under-threshold Stop emits tally only", "systemMessage" in stop_small and "hookSpecificOutput" not in stop_small)

# ── installer round-trip on an isolated ~/.claude ──
tmphome = tempfile.mkdtemp()
pathlib.Path.home = classmethod(lambda cls: pathlib.Path(tmphome))
sp = pathlib.Path(tmphome) / ".claude" / "settings.json"

def _cmds(evt):
    st = json.loads(sp.read_text()) if sp.exists() else {}
    return [h.get("command", "") for grp in (st.get("hooks", {}).get(evt) or []) for h in (grp.get("hooks") or [])]

receipt._install_claude_code()
record_script_check(fails, "install wires a PreCompact hook", any("precompact-hook" in c for c in _cmds("PreCompact")))
record_script_check(fails, "install wires a SessionStart(compact) hook", any("sessionstart-hook" in c for c in _cmds("SessionStart")))

# a user's OWN PreCompact hook in the same group must survive removal
_st = json.loads(sp.read_text())
_st["hooks"]["PreCompact"][0]["hooks"].append({"type": "command", "command": "my/own/hook.py"})
sp.write_text(json.dumps(_st))

receipt._install_claude_code(remove=True)
record_script_check(fails, "remove strips OUR PreCompact + SessionStart hooks", not any("precompact-hook" in c or "sessionstart-hook" in c
   for c in _cmds("PreCompact") + _cmds("SessionStart")))
record_script_check(fails, "remove KEEPS the user's own hook in the same group", any("my/own/hook.py" in c for c in _cmds("PreCompact")))

print(("[OK]" if not fails else "[FAIL]") + " compaction-lifecycle: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
