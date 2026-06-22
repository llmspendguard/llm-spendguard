#!/usr/bin/env python
"""Recover manga2anime's destroyed H200/fleet GPU runtime (Ensight org) — the account remainder that the reconcile
correctly surfaced as residual but that belongs to manga2anime, NOT Healiom.

ACCOUNT-ANCHORED: the vast.ai account has exactly two tenants (lmm/Healiom + manga2anime/Ensight). With lmm fully
reconstructed per-box ($532.59) and the account balance ≈ $20 (so ≈ all $1,190 of June top-ups were consumed), the
remainder is manga2anime's:
    manga2anime = consumption − lmm = (1190 − 20 buffer) − 532.59 = 637.41
    captured (per-box, already in prod) = 119.23  →  recovered remainder = 518.18
PROVENANCE: the destroyed H200 training box (40829066, "still running after 67h" — Vietnam session 1d7d9c13) plus
sibling H200s (41008429, 41120109), a 5090, and the 3090/3060/1070Ti fleet — runtime beyond the $101 snapshot-
captured. Itemised per-box runtimes aren't cleanly recoverable (destroyed before capture), so this is ONE clearly-
labelled account-anchored line, attributed to the correct org. The durable fix is continuous snapshot().

Run from the manga2anime repo so it pushes via the Ensight key:
    cd ~/Documents/animepipe/manga2anime && <lmm-venv>/bin/python <repo>/scripts/recover_gpu_m2a.py
then `spendguard resources sync` from the same dir.
"""
import datetime
from spendguard import resources

ACCOUNT = 1190.0          # June top-ups (vast invoice)
BUFFER = 20.0             # current vast balance ≈ steady-state buffer (left as explicit residual)
LMM = 532.59             # reconstructed lmm (Healiom), per-box
# Only the LIVE fleet re-pushes as real per-box ($43.89 = 3060 + 1070Ti + 3090); the earlier H200 snapshot ($101)
# was pruned (not in this push), so the recovered line now covers the WHOLE destroyed-H200 remainder. The recovered
# line is the remainder on top of the live fleet → manga2anime total lands at the account anchor ($637.41).
CAPTURED = 43.89         # live fleet only (the pruned H200 capture is folded into the recovered remainder below)
DPH = 2.0                # representative aggregate H200 NVL rate


def ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.timezone.utc).timestamp()


def main():
    remainder = round(ACCOUNT - BUFFER - LMM - CAPTURED, 2)
    hours = remainder / DPH
    start = ts("2026-06-09 00:00")
    end = start + hours * 3600
    resources.record_recovered({
        "id": "m2a-h200-recovered", "gpu_name": "H200 NVL +fleet (recovered)", "dph_total": DPH,
        "start_date": start, "end_date": end, "label": "m2a-h200-recovered",
        "note": f"account-anchored: consumption ${ACCOUNT - BUFFER:.0f} (top-ups ${ACCOUNT:.0f} − balance ${BUFFER:.0f}) "
                f"− lmm ${LMM:.2f} − captured ${CAPTURED:.2f} = ${remainder:.2f}; destroyed H200 (67h+) + fleet beyond snapshot",
    })
    print(f"manga2anime recovered remainder = ${remainder:.2f}  ({hours:.0f}h @ ${DPH}/hr, Jun 9 → {datetime.datetime.fromtimestamp(end, datetime.timezone.utc):%b %d %H:%M})")
    print(f"  → manga2anime total ≈ captured ${CAPTURED:.2f} + recovered ${remainder:.2f} = ${CAPTURED + remainder:.2f}")
    print("  Now run `spendguard resources sync` from ~/Documents/animepipe/manga2anime to push to Ensight.")


if __name__ == "__main__":
    main()
