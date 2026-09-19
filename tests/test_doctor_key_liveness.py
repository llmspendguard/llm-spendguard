"""GUARD — the doctor's key line cross-references CACHED liveness. 'resolved' (the key STRING is present) is NOT
'valid' (it authenticates): a rotated/stale key resolves fine but 401s at runtime, and the old key line showed it
🟢 all-green while only the footer warned. The line must render such a provider 🟡 with the REASON (so a stale key
is distinguishable from a transient outage), and any OTHER down metered provider (gemini / zai / kimi) gets its own
liveness line, not only the footer. Pure render function → tested with plain dicts; no CLI, db, or network."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-doctorlive-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import gate   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_render = gate._render_key_status_lines
_OK = [{"prov": "openai", "name": "OPENAI_API_KEY", "resolved4": "b6a2", "state": "ok", "source": "keys.env"},
       {"prov": "anthropic", "name": "ANTHROPIC_API_KEY", "resolved4": "9f10", "state": "ok", "source": "keys.env"}]

print("-- a resolved key whose provider was last seen UNREACHABLE renders 🟡, not 🟢 --")
_down = {"openai": {"resource": "openai", "kind": "metered", "reason": "AuthenticationError: 401 invalid key",
                    "fix": "rotate the OpenAI key in keys.env", "command": ""}}
lines = _render(_OK, _down)
oai = next(ln for ln in lines if "openai" in ln)
ant = next(ln for ln in lines if "anthropic" in ln)
ck("a resolved-but-UNREACHABLE openai key is 🟡, never 🟢", "🟡" in oai and "🟢" not in oai)
ck("... it says NOT verified / UNREACHABLE (resolved ≠ valid)", "UNREACHABLE" in oai and "NOT verified" in oai)
ck("... it carries the REASON (a stale key is distinguishable from a transient outage)", "AuthenticationError" in oai)
ck("a reachable provider (not in the down cache) still reads 🟢", "🟢" in ant and "🟡" not in ant)

print("\n-- an OTHER down metered provider (gemini / zai / kimi) gets its OWN liveness line --")
_down2 = {"gemini": {"resource": "gemini", "kind": "metered", "reason": "model not supported in this location",
                     "fix": "set a supported Vertex region", "command": ""},
          "kimi": {"resource": "kimi", "kind": "metered", "reason": "at capacity", "fix": "", "command": ""}}
lines2 = _render(_OK, _down2)   # statuses cover only openai/anthropic; gemini/kimi are metered-only
ck("a down gemini (no keys.env line) is surfaced per-provider, not only in the footer",
   any("gemini" in ln and "🟡" in ln and "UNREACHABLE" in ln for ln in lines2))
ck("a down kimi (metered, lane or not) is surfaced too", any("kimi" in ln and "🟡" in ln for ln in lines2))
ck("openai/anthropic (healthy this run) stay 🟢 — down-only surfacing adds no noise",
   all("🟢" in ln for ln in lines2 if ("openai" in ln or "anthropic" in ln)))

print("\n-- healthy cache (nothing down) → all resolved keys read 🟢, no false 🟡 --")
lines3 = _render(_OK, {})
ck("no down cache → both keys 🟢, none 🟡", all("🟢" in ln for ln in lines3) and not any("🟡" in ln for ln in lines3))

print("\n-- MISSING still 🔴, SHADOWED still 🟡 (unchanged behavior) --")
lines4 = _render([{"prov": "openai", "name": "OPENAI_API_KEY", "resolved4": "", "state": "missing"}], {})
ck("missing → 🔴 MISSING", "🔴" in lines4[0] and "MISSING" in lines4[0])
lines5 = _render([{"prov": "anthropic", "name": "ANTHROPIC_API_KEY", "resolved4": "9f10", "state": "shadowed",
                   "declared4": "aaaa"}], {})
ck("shadowed → 🟡 SHADOWED", "🟡" in lines5[0] and "SHADOWED" in lines5[0])

print(f"\n{'[FAIL]' if fails else 'OK'} test_doctor_key_liveness: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
