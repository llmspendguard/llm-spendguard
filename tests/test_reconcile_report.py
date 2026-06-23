"""reconcile.py — the unified multi-source view: the base Source defaults, all_sources() (run the one loop for
every spend source), and report() (the printed source-of-truth). Complements test_reconcile_core (run/residual)
and test_reconcile_e2e (does-it-add-up). Offline, isolated home, stubbed provider/account I/O. Script-style."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-recrep-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import reconcile, ledger_sync, resources, saas

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── base Source: safe, inert defaults (a subclass overrides what it needs) ──
s = reconcile.Source()
ck("Source.conn() default {}", s.conn() == {})
ck("Source.truth_total() default 0.0", s.truth_total() == 0.0)
ck("Source.captured() default []", s.captured() == [])
ck("Source.attribute_gap() default []", s.attribute_gap(100.0) == [])

# ── stub both sources' EXTERNAL I/O so all_sources/report run fully offline + account-owned ──
saas.conn = lambda: {"enabled": True, "owns_account": True, "visibility": "org"}
ledger_sync._provider_total = lambda since: 800.0
ledger_sync._gate_captured_rows = lambda since: [{"cost": 600.0, "project": "lmm"}]
resources.account_gpu_total = lambda since=None: 1000.0
resources.gpu_rows_by_day = lambda *a, **k: [{"cost": 250.0, "project": "lmm"}, {"cost": 300.0, "project": "manga2anime"}]
ptmap = {"lmm": ("Healiom", "clinical-ai"), "manga2anime": ("Ensight", "")}

res = reconcile.all_sources(ptmap, since="2026-06-01")
ck("all_sources returns all three sources keyed by name", set(res.keys()) == {"llm", "realtime", "gpu"})
ck("all_sources llm: truth 800 − captured 600 → residual 200", res["llm"]["truth_total"] == 800.0 and res["llm"]["residual"] == 200.0)
ck("all_sources gpu: truth 1000 − captured 550 → residual 450", res["gpu"]["truth_total"] == 1000.0 and res["gpu"]["residual"] == 450.0)
ck("all_sources gpu by_org splits across orgs", res["gpu"]["by_org"].get("Healiom") == 250.0 and res["gpu"]["by_org"].get("Ensight") == 300.0)
# realtime: admin oracle OFF by default → truth None (offline, client-safe); captured = gate realtime (empty here)
ck("all_sources realtime: truth None (admin oracle opt-in off → no network), present in the cross-check",
   res["realtime"]["truth_total"] is None)

# report() prints AND returns the same dict — call it (covers the print/format + None-formatting branches)
rep = reconcile.report(ptmap, since="2026-06-01")
ck("report() returns the same {llm,realtime,gpu} reconciliation it prints", set(rep.keys()) == {"llm", "realtime", "gpu"})

# all_sources with ptmap=None exercises the taxonomy-fallback branch (empty taxonomy in an isolated home → {})
res2 = reconcile.all_sources(None, since="2026-06-01")
ck("all_sources(None) still runs (derives ptmap from taxonomy, falls back to {})", set(res2.keys()) == {"llm", "realtime", "gpu"})

# a source whose adapter raises is captured as {error: ...}, never crashes the unified view
ledger_sync._provider_total = lambda since: (_ for _ in ()).throw(RuntimeError("provider down"))
res3 = reconcile.all_sources(ptmap, since="2026-06-01")
ck("all_sources isolates a failing source as {error}, others still reconcile",
   "error" in res3["llm"] and res3["gpu"]["residual"] == 450.0)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"reconcile_report: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
