"""Guard: spendguard's COMPACTION LIFECYCLE — the PreCompact + SessionStart(compact) hooks and their installer.
  1. guidance_text() returns the preservation directive (decisions+rationale, paths/ids) — config-pathed, else fallback.
  2. record_precompact() records the event (trigger + pre-context) and RETURNS the guidance to inject.
  3. record_sessionstart(source=compact) fills the post-context → the REAL per-event k× = pre/post is measured; a
     non-compact SessionStart is ignored.
  4. the installer wires BOTH hooks into settings.json and removes ONLY ours (a user's other hooks in the same group
     survive).
Hermetic: isolated SPENDGUARD_HOME + a monkeypatched ~/.claude for the installer. Zero spend, never touches real config."""
import os, sys, tempfile, json, pathlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-compact-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import compaction, receipt

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

HOME = os.environ["SPENDGUARD_HOME"]
def tx(path, i, cr, cw, o):
    with open(path, "w") as f:
        f.write(json.dumps({"message": {"usage": {"input_tokens": i, "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw, "output_tokens": o}, "model": "claude-opus-4-8"}}) + "\n")
pre = os.path.join(HOME, "pre.jsonl"); tx(pre, 10000, 490000, 0, 100)      # pre-compaction context = 500,000
post = os.path.join(HOME, "post.jsonl"); tx(post, 5000, 40000, 0, 50)      # post-compaction context = 45,000

gt = compaction.guidance_text()
ck("guidance_text names decisions+rationale and paths/ids", "rationale" in gt.lower() and "path" in gt.lower())

g = compaction.record_precompact({"session_id": "s1", "transcript_path": pre, "trigger": "auto"})
ck("record_precompact returns the injected guidance", gt[:24] in g)
ck("event recorded with its trigger (auto)", compaction.event_summary()["by_trigger"].get("auto") == 1)
ck("k× not measured until a post-compaction context arrives", compaction.measured_k() == (None, 0))

compaction.record_sessionstart({"session_id": "s1", "source": "compact", "transcript_path": post})
k, n = compaction.measured_k()
ck("real per-event k× = pre/post = 500K/45K ≈ 11.1 (measured, not a heuristic)", round(k, 1) == 11.1 and n == 1)
compaction.record_sessionstart({"session_id": "sX", "source": "startup", "transcript_path": post})
ck("a NON-compact SessionStart is ignored (still 1 event)", compaction.event_summary()["events"] == 1)

# ── installer round-trip on an isolated ~/.claude ──
tmphome = tempfile.mkdtemp()
pathlib.Path.home = classmethod(lambda cls: pathlib.Path(tmphome))
sp = pathlib.Path(tmphome) / ".claude" / "settings.json"

def _cmds(evt):
    st = json.loads(sp.read_text()) if sp.exists() else {}
    return [h.get("command", "") for grp in (st.get("hooks", {}).get(evt) or []) for h in (grp.get("hooks") or [])]

receipt._install_claude_code()
ck("install wires a PreCompact hook", any("precompact-hook" in c for c in _cmds("PreCompact")))
ck("install wires a SessionStart(compact) hook", any("sessionstart-hook" in c for c in _cmds("SessionStart")))

# a user's OWN PreCompact hook in the same group must survive removal
_st = json.loads(sp.read_text())
_st["hooks"]["PreCompact"][0]["hooks"].append({"type": "command", "command": "my/own/hook.py"})
sp.write_text(json.dumps(_st))

receipt._install_claude_code(remove=True)
ck("remove strips OUR PreCompact + SessionStart hooks", not any("precompact-hook" in c or "sessionstart-hook" in c
   for c in _cmds("PreCompact") + _cmds("SessionStart")))
ck("remove KEEPS the user's own hook in the same group", any("my/own/hook.py" in c for c in _cmds("PreCompact")))

print(("[OK]" if not fails else "[FAIL]") + " compaction-lifecycle: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
