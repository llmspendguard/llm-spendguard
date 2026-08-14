"""tag.py — the project-attribution cascade (the FREE deterministic pass + move/estimate). Money-relevant: wrong
tags = wrong P&L. Pure, offline, isolated SPENDGUARD_HOME. Script-style (ck + sys.exit) like the rest of the suite."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-tag-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import tag, budget
from spendguard import ledger as L

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

def _insert(day, kind, cost, project, model="gpt-5.5"):
    # seed the money-of-record (spend_events) via the production charge→event mapping (tag reads/writes it now)
    ev = budget.charge_to_event("openai", model, kind, float(cost))
    ev["project_primary"] = project; ev["projects"] = [project] if project else []
    ev["occurred_at"] = ev["ts_utc"] = day + "T00:00:00+00:00"
    ev["source"] = ev["recorded_by"] = "test"
    ev["dedup_key"] = "test:%s:%s:%s:%s" % (day, kind, cost, project)
    budget._ledger().record(ev)

# ── retag_deterministic: meta → 'llm-spendguard'; empty workload → the repo project; existing tags untouched ──
_insert("2026-06-01", "meta", 1.0, "")          # → llm-spendguard
_insert("2026-06-01", "batch", 2.0, "")         # → repo project (budget._project())
_insert("2026-06-02", "batch", 3.0, "already")  # must NOT be overridden
proj = budget._project()
changed = tag.retag_deterministic()
ck("retag_deterministic changed exactly the 2 empty rows", changed == 2)

def _proj_of(kind, cost):
    # costs are distinct in this fixture, so the amount identifies the row (in whichever money column it lands)
    want = L.to_dec(cost)
    for r in budget._ledger().query():
        if any(L.to_dec(r.get(c)) == want for c in L.USD_COLS):
            return r["project_primary"]
    return None
ck("meta row → 'llm-spendguard'", _proj_of("meta", 1.0) == "llm-spendguard")
ck("empty workload row → the repo project", _proj_of("batch", 2.0) == proj and proj not in ("", None))
ck("already-tagged row is NOT overridden", _proj_of("batch", 3.0) == "already")
ck("re-running is a no-op (nothing empty left)", tag.retag_deterministic() == 0)

# ── ambiguous_count: untagged rows remaining (0 after the free pass when everything had context) ──
ck("ambiguous_count is 0 after the deterministic pass", tag.ambiguous_count() == 0)
_insert("2026-06-03", "batch", 4.0, "")         # a fresh untagged row
ck("ambiguous_count counts a fresh untagged row", tag.ambiguous_count() == 1)

# ── move_project: case-insensitive re-tag across the ledger ──
_insert("2026-06-04", "batch", 5.0, "Documents")
_insert("2026-06-04", "batch", 6.0, "documents")
moved = tag.move_project("DOCUMENTS", "vision-pipeline")   # case-insensitive match
ck("move_project re-tags both case variants (case-insensitive)", moved == 2)
n_vp = budget._ledger().count(where={"project_primary": "vision-pipeline"})
n_doc = budget._ledger().count(filt="lower(COALESCE(project_primary,''))='documents'")
ck("after move: both rows are 'vision-pipeline', none left as documents", n_vp == 2 and n_doc == 0)

# ── estimate_llm_retag: ZERO-SPEND estimate (per the API spend protocol), exact formula ──
est = tag.estimate_llm_retag()
exp = round(tag.ambiguous_count() / 25 * 0.0008, 4)
ck("estimate_llm_retag returns {rows, est_usd, model, note}, est = rows/25*0.0008",
   est["rows"] == tag.ambiguous_count() and est["est_usd"] == exp and est["model"] == "gpt-5-nano" and "llm-spendguard" in est["note"])

# ── cmd: dispatch + return codes ──
ck("cmd move → 0", tag.cmd(["move", "a", "b"]) == 0)
ck("cmd estimate → 0", tag.cmd(["estimate"]) == 0)
ck("cmd (no args) → usage, returns 1", tag.cmd([]) == 1)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"tag: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
