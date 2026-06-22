"""Shared classifier — iso_period buckets (incl the `ytd` regression), project_team_map, classify_items parse.
NO network: adapters.call + calls.context are stubbed. Isolated SPENDGUARD_HOME."""
import os, sys, tempfile, contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import attribution, adapters, calls

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# iso_period — the ytd branch was missing (fell through to month)
ck("iso_period ytd", attribution.iso_period("2026-06-21", "ytd") == "2026-YTD")
ck("iso_period quarter", attribution.iso_period("2026-06-21", "quarter") == "2026-Q2")
ck("iso_period month", attribution.iso_period("2026-06-21", "month") == "2026-06")
ck("iso_period week", attribution.iso_period("2026-06-21", "week").startswith("2026-W"))
ck("iso_period day", attribution.iso_period("2026-06-21", "day") == "2026-06-21")
ck("iso_period bad-day safe", attribution.iso_period("", "month") == "?")

# project_team_map
taxo = {"orgs": ["O"], "default_org": "Personal", "teams": [{"name": "t", "org": "O"}],
        "projects": [{"name": "P1", "org": "O", "team": "t"}]}
ck("project_team_map", attribution.project_team_map(taxo).get("p1") == ("O", "t"))

# classify_items — stub the caged call + context (no spend, no corpus writes)
calls.context = lambda **k: contextlib.nullcontext()
adapters.call = lambda *a, **k: {"text": '{"items":[{"i":0,"org":"O","team":"t","project":"P1","confidence":90}]}',
                                 "cost": 0.0, "error": None}
res = attribution.classify_items([{"id": "x", "text": "build the P1 thing"}], taxo, run=True)
ck("classify_items maps id→assignment", res.get("x", {}).get("project") == "P1")
ck("classify_items confidence parsed (was dead 0)", res.get("x", {}).get("confidence") == 90)
ck("classify_items estimate-only returns {}", attribution.classify_items([{"id": "y", "text": "z"}], taxo, run=False) == {})
ck("classify_items skips empty text", attribution.classify_items([{"id": "z", "text": ""}], taxo, run=True) == {})

# tolerant parse: truncated/garbled JSON still recovers per-item
adapters.call = lambda *a, **k: {"text": 'noise {"items":[{"i":0,"org":"O","team":"t","project":"P2"}]} trailing',
                                 "cost": 0.0, "error": None}
ck("classify_items tolerant parse", attribution.classify_items([{"id": "a", "text": "x"}], taxo, run=True).get("a", {}).get("project") == "P2")

print(("\n[FAIL] " if fails else "\n[OK] ") + f"attribution: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
