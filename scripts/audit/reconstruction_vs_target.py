"""DEV develop-and-test harness: the agentic RECONSTRUCTION (production, admin-free) vs the ADMIN TARGET (dev-only),
per axis × project. The admin path is the TARGET we develop and test against — never a production input. This is the
loop's measuring stick: reconstruct → diff vs target → see per-project where we're short → improve the reconstruction
(evidence selector / prompt) → re-run until the gap closes.

Admin keys are used HERE ONLY (a dev tool). Nothing in this file writes the ledger or runs in production.

Usage: python scripts/audit/reconstruction_vs_target.py [SINCE=2026-06-01] [--run]
  --run  re-run the caged agentic reconstruction (≈$2.30); otherwise use the cached /tmp/rt_reconstruct.json.
"""
import json
import os
import sys


def main():
    since = next((a for a in sys.argv[1:] if not a.startswith("-")), "2026-06-01")
    os.environ.setdefault("SPENDGUARD_ADMIN_ORACLE", "1")        # DEV TARGET ONLY — this file never writes the ledger

    from spendguard import realtime_oracle, resources

    # ── TARGET: the admin-usage truth, timing-matched to our conversations, per project (DEV yardstick) ──
    trows, meta = realtime_oracle.by_project_day(since)
    target = {}
    for r in trows:
        target[(r.get("project") or "").lower()] = target.get((r.get("project") or "").lower(), 0.0) + float(r.get("cost") or 0)
    target_total = float(meta.get("ours_total") or sum(target.values()))

    # ── RECONSTRUCTION: agentic, from conversation token records (production path, admin-free) ──
    if "--run" in sys.argv:
        rec = resources.reconstruct_remote_llm(run=True)
    else:
        try:
            rec = json.loads(open("/tmp/rt_reconstruct.json").read())
        except Exception:
            print("no cached reconstruction (/tmp/rt_reconstruct.json) — run with --run first"); return 1
    recon = {}
    for r in rec.get("rows", []):
        recon[(r.get("project") or "").lower()] = recon.get((r.get("project") or "").lower(), 0.0) + float(r.get("usd") or 0)
    recon_total = float(rec.get("total") or sum(recon.values()))

    print(f"REALTIME — reconstruction vs ADMIN TARGET (dev), since {since}")
    print(f"  {'project':<24}{'reconstructed':>14}{'target':>12}{'gap':>12}   verdict")
    projects = sorted(set(target) | set(recon), key=lambda p: -target.get(p, 0))
    for p in projects:
        t, c = target.get(p, 0.0), recon.get(p, 0.0)
        gap = t - c
        pct = (c / t * 100) if t > 0 else (100 if c == 0 else 0)
        verdict = "OK" if t > 0 and pct >= 80 else ("MISSING" if c < 0.01 else f"{pct:.0f}% of target")
        print(f"  {(p or '(untagged)'):<24}{('$%.2f' % c):>14}{('$%.2f' % t):>12}{('$%.2f' % gap):>12}   {verdict}")
    print(f"  {'─'*60}")
    cov = (recon_total / target_total * 100) if target_total else 0
    print(f"  {'TOTAL':<24}{('$%.2f' % recon_total):>14}{('$%.2f' % target_total):>12}{('$%.2f' % (target_total - recon_total)):>12}   {cov:.1f}% of target")
    print(f"\n  → develop the reconstruction (conv evidence selector + prompt) until coverage ≈100%. Admin is the TARGET, never the ledger.")


if __name__ == "__main__":
    sys.exit(main() or 0)
