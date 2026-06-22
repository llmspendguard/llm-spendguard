#!/usr/bin/env python
"""Recover lmm GPU boxes that were DESTROYED before the snapshot recorder existed (early-mid June 2026), so they
flow through the normal reconcile (resources._reconcile) instead of being lost to the account-gap.

PROVENANCE: reconstructed from the Claude Code session transcript `ba8947b4` (Jun 2-13), which is 100% `healiom_*`
GLiNER/SapBERT/BioLORD embedding work on a multi-box A100/H100/A6000 cluster. Each box's dph is from the transcript
(e.g. "status=running $3.61/hr"); runtimes are ESTIMATES from the launch/destroy windows seen in the session — the
durable fix is continuous `snapshot()`, not after-the-fact recovery. Labels use "healiom_gpu_*" so project_of() maps
them to lmm (the label_map substring is "healiom_gpu").

Idempotent (keyed by instance id). Run from the lmm venv (gated): `lmm/.venv/bin/python scripts/recover_gpu_boxes.py`
"""
import datetime
from spendguard import resources


def ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc).timestamp()


# (id, gpu_name, dph, start, end, label, evidence) — runtimes are conservative estimates from the session windows.
BOXES = [
    (40272086, "H100 SXM",  3.61, "2026-06-09 12:00", "2026-06-11 19:00", "healiom_gpu_h100",  "status=running $3.61/hr, Jun 9-12"),
    (40172622, "H100",      2.02, "2026-06-08 12:00", "2026-06-09 18:00", "healiom_gpu_h100b", "forgotten box ~$48/day, caught+destroyed"),
    (40269092, "A6000",     0.40, "2026-06-09 06:00", "2026-06-10 12:00", "healiom_gpu_a6000", "training A6000, then destroyed"),
    (40640021, "H100",      3.61, "2026-06-11 12:00", "2026-06-11 15:00", "healiom_gpu_h100c", "broken/idle box, destroyed (~minimal)"),
    (40264082, "RTX 3060",  0.08, "2026-06-09 00:00", "2026-06-09 12:00", "healiom_gpu_3060",  "Quebec RTX 3060"),
    (40121630, "RTX 3060",  0.052, "2026-06-08 12:00", "2026-06-08 12:30", "healiom_gpu_3060b", "SapBERT/BioLORD embeddings ~30min"),
]


def main():
    total = 0.0
    print("Recovering destroyed lmm GPU boxes (early-mid June 2026) from session evidence:\n")
    for iid, gpu, dph, start, end, label, ev in BOXES:
        s, e = ts(start), ts(end)
        cost = dph * (e - s) / 3600.0
        total += cost
        resources.record_recovered({"id": iid, "gpu_name": gpu, "dph_total": dph,
                                    "start_date": s, "end_date": e, "label": label, "note": ev})
        print(f"  {iid}  {gpu:10} ${dph:.3f}/hr  {start[5:]}→{end[5:]}  ≈${cost:6.2f}  ({ev})")
    print(f"\n  recovered subtotal ≈ ${total:.2f}  (+ captured A100 $249.62 → lmm ≈ ${total + 249.62:.0f})")
    print("  Now run `spendguard resources sync` from the lmm repo to push the reconciled per-box rows.")


if __name__ == "__main__":
    main()
