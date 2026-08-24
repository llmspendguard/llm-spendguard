#!/usr/bin/env python3
"""Confirm the batched-embedding gate fix on the REAL OpenAI path, via the actual bakeoff embedder.

Reproduces the bug's exact condition — ONE embeddings.create call whose `input` is a LIST summing PAST the
context window while each item fits it — through mmg's real `encode_openai`, under the enforcing gate. The
bug quarantined exactly this (recorded $0, lost the vectors). Verifies, after a tiny real call:
  • real vectors come back (shape = items × dim);
  • the call is NOT quarantined (no new void row for the model);
  • the spend is RECORDED (a posted realtime row appears; the ledger does not read it as free).

Estimate-first (a separate, zero-spend estimate is printed) per the API spend protocol. Must run under the
gated venv (`import spendguard; spendguard.require()` fails closed otherwise). The bakeoff script path is a
REQUIRED argument — nothing is hard-coded to one checkout.

  python scripts/repro/confirm_embed_gate_fix.py --bakeoff /abs/path/to/embed_recall_bakeoff.py
"""
import argparse
import importlib.util
import sqlite3

import spendguard
spendguard.require()                       # fail closed: refuse to run if the gate is not actually enforcing here

from spendguard import config, pricing, budget
from spendguard.gate import _ct
from spendguard.ledger import to_dec


def load_encoder(path):
    """Import the REAL encode_openai + its openai model id from the bakeoff script at `path` (no re-implementation)."""
    spec = importlib.util.spec_from_file_location("bakeoff_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.encode_openai, mod.MODELS["openai"][1]


def _void_count(model):
    con = sqlite3.connect(config.db_path())
    n = con.execute("SELECT COUNT(*) FROM spend_events WHERE status='void' AND model=?", (model,)).fetchone()[0]
    con.close()
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bakeoff", required=True, help="absolute path to embed_recall_bakeoff.py")
    args = ap.parse_args()

    encode_openai, model_id = load_encoder(args.bakeoff)
    window = pricing.max_input_tokens(model_id) or 8191

    # a corpus whose SUM exceeds the window while each item is far under it — the exact shape the bug quarantined.
    unit = "BRCA1 — BRCA1 DNA repair associated breast cancer type 1 susceptibility protein"
    u = max(_ct(unit), 1)
    n = min(900, (window * 12) // (u * 10) + 1)          # sum ≈ 1.2× window, item count well under OpenAI's cap
    corpus = [f"{unit} variant {i}" for i in range(n)]
    total_tok = sum(_ct(s) for s in corpus)
    max_item = max(_ct(s) for s in corpus)
    est = pricing.realtime_cost(model_id, total_tok, 0)

    print(f"model={model_id}  window={window:,}")
    print(f"corpus={len(corpus)} items · batch SUM={total_tok:,} tok (> window) · largest item={max_item} tok (< window)")
    print(f"ESTIMATE (zero spend): ${est:.5f}   <- the whole batch, at the realtime table rate")
    if total_tok <= window:
        raise SystemExit("precondition failed: the batch SUM does not exceed the window — not a faithful repro")

    day = budget._db().execute("SELECT date('now','-2 days')").fetchone()[0]   # anchor back 2d (UTC boundary safe)
    void_before = _void_count(model_id)
    spent_before = budget.spent_since(day)

    print("\nRUNNING the real batched embeddings.create through mmg.encode_openai ...")
    vecs = encode_openai(model_id, corpus, False, None)   # THE production call: cli.embeddings.create(input=[list])

    void_after = _void_count(model_id)
    spent_after = budget.spent_since(day)
    dim = vecs.shape[1] if getattr(vecs, "ndim", 0) == 2 else None

    print(f"\nvectors: shape={getattr(vecs, 'shape', None)} (items × dim)")
    print(f"new VOID rows for {model_id}: {void_after - void_before}   (0 = NOT quarantined — the bug is fixed)")
    print(f"spent_since({day}): ${spent_before:.5f} -> ${spent_after:.5f}  (+${spent_after - spent_before:.5f} recorded, not $0)")

    ok = (getattr(vecs, "shape", (0,))[0] == len(corpus) and dim and dim > 0
          and void_after == void_before and (spent_after - spent_before) > 0)
    print("\n" + ("PASS — real batched embedding: vectors returned, not quarantined, spend recorded."
                  if ok else "FAIL — see the three checks above."))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
