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
ck("residual_warning fires when residual is large", reconcile.residual_warning(1000.0, 300.0) is not None)
ck("residual_warning silent when residual ≈ buffer", reconcile.residual_warning(1000.0, 20.0) is None)

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

print(("\n[FAIL] " if fails else "\n[OK] ") + f"reconcile_core: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
