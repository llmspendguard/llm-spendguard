"""The shared reconcile core (reconcile.py) — the ONE loop LLM + GPU + subscription + storage all plug into via a
Source adapter. Pure, deterministic, no network. Isolated SPENDGUARD_HOME."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import reconcile

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── account-anchor guard (same one LLM ledger_sync + GPU resources now share) ──
ck("owner_ok: standalone (no conn) reconciles fully", reconcile.owner_ok({})[0] is True)
ck("owner_ok: connected account OWNER reconciles", reconcile.owner_ok({"enabled": True, "owns_account": True})[0] is True)
ck("owner_ok: connected NON-owner must not (shared account)", reconcile.owner_ok({"enabled": True, "owns_account": False})[0] is False)

# ── residual + by-org rollup + warning ──
ptmap = {"lmm": ("Healiom", "clinical-ai"), "manga2anime": ("Ensight", "")}
rows = [{"cost": 250.0, "project": "lmm"}, {"cost": 300.0, "project": "manga2anime"}, {"cost": 50.0, "project": ""}]
ck("residual = truth − Σcaptured", reconcile.residual(900.0, 600.0) == 300.0)
ck("rollup_by_org maps project→org (unlabeled → (untagged))",
   reconcile.rollup_by_org(rows, ptmap) == {"Healiom": 250.0, "Ensight": 300.0, "(untagged)": 50.0})
ck("residual_warning: large POSITIVE → under-attributed", "UNDER" in (reconcile.residual_warning(1000.0, 300.0) or ""))
ck("residual_warning: large NEGATIVE → over-attributed/stale", "OVER" in (reconcile.residual_warning(1000.0, -300.0) or ""))
ck("residual_warning silent when |residual| ≈ buffer (both directions)",
   reconcile.residual_warning(1000.0, 20.0) is None and reconcile.residual_warning(1000.0, -50.0) is None)

# ── the loop via a Source adapter — identical shape for ANY spend source ──
class FakeSource(reconcile.Source):
    name = "fake"
    def conn(self):
        return {"enabled": True, "owns_account": True}
    def truth_total(self, since=None):
        return 900.0
    def captured(self, since=None):
        return [{"cost": 250.0, "project": "lmm"}, {"cost": 300.0, "project": "manga2anime"}]
    def attribute_gap(self, gap, since=None):          # AGENTIC step fills the gap (here: a stub)
        return [{"cost": 300.0, "project": "manga2anime"}]

r = reconcile.run(FakeSource(), ptmap)
ck("run: truth=900, captured=550, attributed=300", r["truth_total"] == 900.0 and r["captured"] == 550.0 and r["attributed"] == 300.0)
ck("run: residual = truth − captured − attributed (= 50)", r["residual"] == 50.0)
ck("run: by_org rolls captured+attributed", r["by_org"].get("Ensight") == 600.0 and r["by_org"].get("Healiom") == 250.0)

class NonOwner(FakeSource):
    def conn(self):
        return {"enabled": True, "owns_account": False}
ck("run: non-owner skips the agentic gap attribution (no cross-tenant claim)", reconcile.run(NonOwner(), ptmap)["attributed"] == 0.0)

# ── the REAL source adapters conform to the interface + run() drives them identically (stubbed I/O, no network) ──
from spendguard import resources, ledger_sync
resources.account_gpu_total = lambda since=None: 1000.0
resources.gpu_rows_by_day = lambda since_ts=None, now=None, label_map=None: [
    {"cost": 250.0, "project": "lmm"}, {"cost": 300.0, "project": "manga2anime"}]
rg = reconcile.run(resources.GPUSource(conn={"owns_account": True}), ptmap)
ck("GPUSource via run(): truth 1000, captured 550, residual 450",
   rg["truth_total"] == 1000.0 and rg["captured"] == 550.0 and rg["residual"] == 450.0)
ck("GPUSource by_org: lmm→Healiom, m2a→Ensight", rg["by_org"].get("Healiom") == 250.0 and rg["by_org"].get("Ensight") == 300.0)
ck("GPUSource non-owner → truth 0 (doesn't claim shared account)",
   reconcile.run(resources.GPUSource(conn={"enabled": True, "owns_account": False}), ptmap)["truth_total"] == 0.0)

ledger_sync._provider_total = lambda since: 800.0
ledger_sync._gate_captured_rows = lambda since: [{"cost": 600.0, "project": "lmm"}]
rl = reconcile.run(ledger_sync.LLMSource(conn={"owns_account": True}, since="2026-06-01"), ptmap)
ck("LLMSource via run(): truth 800, captured 600, residual 200",
   rl["truth_total"] == 800.0 and rl["captured"] == 600.0 and rl["residual"] == 200.0)

# ── truth UNKNOWN (fetch failed) must NOT read as $0/reconciled — the silent-undercount guard ──
ck("residual: None truth → None (not a number)", reconcile.residual(None, 500.0) is None)
ck("residual_warning: None truth → UNKNOWN (loud, not silent)", "UNKNOWN" in (reconcile.residual_warning(None, None) or ""))

class FetchFail(reconcile.Source):
    name = "ff"
    def conn(self):
        return {"owns_account": True}
    def truth_total(self, since=None):
        return None                                        # the external bill couldn't be read
    def captured(self, since=None):
        return [{"cost": 100.0, "project": "lmm"}]
rf = reconcile.run(FetchFail(), ptmap)
ck("run: None truth → residual None + UNKNOWN warning (never $0/100%-covered)",
   rf["residual"] is None and "UNKNOWN" in (rf["warning"] or "") and rf["captured"] == 100.0)

# ── cross-source COMPLETENESS verdict: the SYSTEM surfaces an under-reconstructed source (e.g. realtime remote) ──
comp_ok = reconcile.completeness({
    "batch": {"truth_total": 800.0, "residual": 10.0},        # reconciled (within buffer)
    "realtime": {"truth_total": 500.0, "residual": 400.0},    # UNDER — remote box calls not reconstructed
    "gpu": {"truth_total": None, "residual": None},           # truth unreadable → unknown
})
ck("completeness: not complete when a source is under/unknown", comp_ok["complete"] is False)
ck("completeness: batch reconciled", comp_ok["sources"]["batch"]["status"] == "reconciled")
ck("completeness: realtime flagged UNDER with the unreconstructed gap", comp_ok["sources"]["realtime"]["status"] == "under" and comp_ok["sources"]["realtime"]["gap"] == 400.0)
ck("completeness: unknown-truth source never reads as complete", comp_ok["sources"]["gpu"]["status"] == "unknown")
ck("completeness: msg names the UNDER source (system surfaces the gap, not the human)", "realtime: UNDER" in comp_ok["msg"])
allgood = reconcile.completeness({"batch": {"truth_total": 800.0, "residual": 5.0}, "gpu": {"truth_total": 100.0, "residual": -3.0}})
ck("completeness: all within buffer → complete", allgood["complete"] is True and allgood["msg"] == "all sources reconciled")

print(("\n[FAIL] " if fails else "\n[OK] ") + f"reconcile_core: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
