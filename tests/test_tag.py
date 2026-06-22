"""tag.py — the project-attribution cascade (the FREE deterministic pass + move/estimate). Money-relevant: wrong
tags = wrong P&L. Pure, offline, isolated SPENDGUARD_HOME. Script-style (ck + sys.exit) like the rest of the suite."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-tag-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import tag, budget

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

def _insert(day, kind, cost, project, model="gpt-5.5"):
    with budget._lock:
        budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                             (day + "T00:00:00+00:00", day, "openai", model, kind, cost, project))
        budget._db().commit()

# ── retag_deterministic: meta → 'llmseg'; empty workload → the repo project; existing tags untouched ──
_insert("2026-06-01", "meta", 1.0, "")          # → llmseg
_insert("2026-06-01", "batch", 2.0, "")         # → repo project (budget._project())
_insert("2026-06-02", "batch", 3.0, "already")  # must NOT be overridden
proj = budget._project()
changed = tag.retag_deterministic()
ck("retag_deterministic changed exactly the 2 empty rows", changed == 2)

def _proj_of(kind, cost):
    with budget._lock:
        r = budget._db().execute("SELECT project FROM charges WHERE kind=? AND cost=?", (kind, cost)).fetchone()
    return r[0]
ck("meta row → 'llmseg'", _proj_of("meta", 1.0) == "llmseg")
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
with budget._lock:
    n_vp = budget._db().execute("SELECT COUNT(*) FROM charges WHERE project='vision-pipeline'").fetchone()[0]
    n_doc = budget._db().execute("SELECT COUNT(*) FROM charges WHERE lower(project)='documents'").fetchone()[0]
ck("after move: both rows are 'vision-pipeline', none left as documents", n_vp == 2 and n_doc == 0)

# ── estimate_llm_retag: ZERO-SPEND estimate (per the API spend protocol), exact formula ──
est = tag.estimate_llm_retag()
exp = round(tag.ambiguous_count() / 25 * 0.0008, 4)
ck("estimate_llm_retag returns {rows, est_usd, model, note}, est = rows/25*0.0008",
   est["rows"] == tag.ambiguous_count() and est["est_usd"] == exp and est["model"] == "gpt-5-nano" and "llmseg" in est["note"])

# ── cmd: dispatch + return codes ──
ck("cmd move → 0", tag.cmd(["move", "a", "b"]) == 0)
ck("cmd estimate → 0", tag.cmd(["estimate"]) == 0)
ck("cmd (no args) → usage, returns 1", tag.cmd([]) == 1)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"tag: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
