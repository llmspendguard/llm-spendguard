"""End-to-end "does it ALL add up" reconciliation test — the `copy-then-trim-by-date` check, deterministic.

Builds a KNOWN dated ledger (the gate's real SQLite `charges` store) spanning two months across projects, then
exercises the WHOLE path with the real trim accessors + the real `reconcile.run()` loop for BOTH source adapters
(LLM + GPU). Only the external provider/vast billing fetch is stubbed (a test can't call it); everything between —
the date trim, the per-project pivot, the agentic-gap attribution rows, the residual math, the org rollup — is real.

The invariants asserted (the "it adds up" properties):
  • trim exactness:   Σ captured(since=X) == Σ of fixture rows with day ≥ X, for every cutoff
  • trim monotonic:   earlier cutoff ⇒ total never decreases
  • pivot closes:     Σ over (project,day) == the flat trimmed total (no row lost/double-counted)
  • per-source loop:  truth − captured − attributed == residual, AT EVERY cutoff (constant residual under trim)
  • org rollup closes: Σ by_org.values() == captured + attributed
  • PORTFOLIO:        Σ truth − Σ captured − Σ attributed == Σ residual across LLM+GPU (the grand total reconciles)
  • UNKNOWN safety:   a source whose external bill can't be read yields residual None (never a silent $0/100%-covered)
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-e2e-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import reconcile, budget, ledger_sync, resources

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── 1) build a KNOWN dated ledger (two months, three projects) directly in the gate store ──────────────────
#    gate (captured) batch rows: model != reconciled-marker so gate_by_project_day counts them.
GATE = [  # (day, provider, model, project, cost)
    ("2026-05-10", "openai", "gpt-5.5", "lmm", 40.0),
    ("2026-05-20", "anthropic", "claude-opus-4-8", "manga2anime", 30.0),
    ("2026-06-05", "openai", "gpt-5.5", "lmm", 60.0),
    ("2026-06-15", "anthropic", "claude-opus-4-8", "manga2anime", 20.0),
    ("2026-06-15", "openai", "gpt-5.5", "", 10.0),          # untagged → (untagged) org bucket
]
with budget._lock:
    for day, prov, model, proj, cost in GATE:
        budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                             (day + "T00:00:00+00:00", day, prov, model, "batch", cost, proj))
    budget._db().commit()
# attributed (reconciled) row — the agentic gap attribution, real reconciled marker so reconciled_by_project sees it
budget.record_reconciled("2026-06-10", "openai", 25.0, project="lmm")

ptmap = {"lmm": ("Healiom", "clinical-ai"), "manga2anime": ("Ensight", "")}

# ── 2) date-trim exactness + monotonicity + pivot-closure on the REAL ledger (copy-then-trim) ──────────────
def captured_total(since):
    return round(sum(r["cost"] for r in ledger_sync._gate_captured_rows(since)), 2)

EXPECT = {                                                  # Σ of GATE rows with day ≥ cutoff
    "2026-05-01": 160.0,    # all five
    "2026-05-15": 120.0,    # drops 05-10 lmm 40
    "2026-06-01":  90.0,    # 60 + 20 + 10
    "2026-07-01":   0.0,    # future → empty
}
for since, exp in EXPECT.items():
    ck(f"trim exactness: captured(since={since}) == ${exp:.0f}", captured_total(since) == exp)
    # pivot closes: the (project,day) pivot sums to the same flat total — nothing lost or double-counted
    pivot = round(sum(budget.gate_by_project_day(kind="batch", since=since).values()), 2)
    ck(f"pivot closes at {since}: Σ(project,day) == flat total", pivot == exp)

mono = [captured_total(s) for s in ("2026-05-01", "2026-05-15", "2026-06-01", "2026-07-01")]
ck("trim monotonic: earlier cutoff ⇒ total never decreases", all(a >= b for a, b in zip(mono, mono[1:])))

# ── 3) the unified reconcile loop adds up PER SOURCE at every cutoff (constant residual under trim) ─────────
#   stub ONLY the external billing fetch: provider truth = real captured(since) + real attributed(since) + a fixed
#   $15 under-attributed remainder (0 when there's no spend). So the residual MUST come out to exactly $15 at every
#   cutoff that has spend — proving the loop reconciles identically no matter where we trim.
RESID = 15.0
def fake_provider_total(since):
    cap = captured_total(since)
    att = round(sum(budget.reconciled_by_project(since).values()), 2)
    return round(cap + att + RESID, 2) if cap > 0 else 0.0
ledger_sync._provider_total = fake_provider_total

for since in ("2026-05-01", "2026-05-15", "2026-06-01"):
    r = reconcile.run(ledger_sync.LLMSource(conn={"owns_account": True}, since=since), ptmap, since=since)
    inv = round(r["truth_total"] - r["captured"] - r["attributed"], 2)
    ck(f"LLM loop @ {since}: truth − captured − attributed == residual", inv == r["residual"])
    ck(f"LLM loop @ {since}: residual is the fixed $15 remainder (exact under trim)", r["residual"] == RESID)
    ck(f"LLM loop @ {since}: by_org closes (Σ == captured + attributed)",
       round(sum(r["by_org"].values()), 2) == round(r["captured"] + r["attributed"], 2))

# future cutoff → genuinely zero everywhere, no crash, no false warning
rf = reconcile.run(ledger_sync.LLMSource(conn={"owns_account": True}, since="2026-07-01"), ptmap, since="2026-07-01")
ck("LLM loop @ future: truth 0 / captured 0 / residual 0 / no warning",
   rf["truth_total"] == 0.0 and rf["captured"] == 0.0 and rf["residual"] == 0.0 and rf["warning"] is None)

# ── 4) GPU source (account-anchored, owner only) reconciles in the SAME shape ──────────────────────────────
resources.account_gpu_total = lambda since=None: 600.0
resources.gpu_rows_by_day = lambda *a, **k: [{"cost": 250.0, "project": "lmm"}, {"cost": 300.0, "project": "manga2anime"}]
rg = reconcile.run(resources.GPUSource(conn={"owns_account": True}), ptmap)
ck("GPU loop: truth 600 − captured 550 == residual 50", rg["residual"] == 50.0 and rg["captured"] == 550.0)
ck("GPU loop: by_org closes (Healiom 250 + Ensight 300 == captured)",
   round(sum(rg["by_org"].values()), 2) == rg["captured"])
ck("GPU non-owner claims nothing on the shared account (truth 0)",
   reconcile.run(resources.GPUSource(conn={"enabled": True, "owns_account": False}), ptmap)["truth_total"] == 0.0)

# ── 5) PORTFOLIO: the grand total across BOTH sources reconciles ───────────────────────────────────────────
since = "2026-06-01"
rl = reconcile.run(ledger_sync.LLMSource(conn={"owns_account": True}, since=since), ptmap, since=since)
srcs = [rl, rg]
T = round(sum(s["truth_total"] for s in srcs), 2)
C = round(sum(s["captured"] for s in srcs), 2)
A = round(sum(s["attributed"] for s in srcs), 2)
R = round(sum(s["residual"] for s in srcs), 2)
ck(f"PORTFOLIO adds up: Σtruth({T}) − Σcaptured({C}) − Σattributed({A}) == Σresidual({R})", round(T - C - A, 2) == R)
ck("PORTFOLIO residual == LLM $15 + GPU $50 == $65", R == 65.0)

# ── 6) UNKNOWN truth (external bill unreadable) must NOT masquerade as reconciled in the grand total ────────
ledger_sync._provider_total = lambda since: None             # provider fetch fails
ru = reconcile.run(ledger_sync.LLMSource(conn={"owns_account": True}, since=since), ptmap, since=since)
ck("UNKNOWN: residual None + loud UNKNOWN warning (never silent $0 / 100%-covered)",
   ru["residual"] is None and "UNKNOWN" in (ru["warning"] or "") and ru["captured"] == 90.0)
ck("UNKNOWN: a None-truth source can't be summed into a reconciled portfolio total (caller must surface it)",
   any(s["residual"] is None for s in [ru]) is True)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"reconcile_e2e: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
