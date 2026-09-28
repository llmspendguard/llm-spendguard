#!/usr/bin/env python3
"""Item 2 — the LIVE proof of the batch loop's REAL-API layer against the OpenAI Batch API.

The offline tests (tests/test_batch_tracker.py) already prove the lane_queue lifecycle (mark_batched →
collect_batched → settle) and the tracker state machine (register → poll → fail-over → resume) with REAL
lane_queue/tracker code and only the provider API faked. The one thing NO offline test can exercise is the layer
those fake: that the request envelope spendguard builds is ACCEPTED by the real OpenAI /v1/chat/completions Batch
API and that results come back keyed by the custom_id we assigned. This script proves exactly that layer, end to
end and live:

    submit_chat_tasks (guarded, estimate-first, $-capped)  →  callio.batch_status  →  callio.collect_chat_tasks

ROUND-TRIP INTEGRITY: each task asks the model to echo a distinct random nonce. A batch returns rows keyed by
custom_id and MAY return them out of order, so verifying each custom_id's result carries ITS OWN nonce proves both
the transport and the per-row mapping — the property the offline fake cannot give.

WHY NOT submit_offload / the lane_queue here: a live drain daemon competes for queue leases (observed: it ran this
proof's first rows realtime before they could be offloaded), so driving callio DIRECTLY is both cleaner and the
correct scope — the queue/tracker halves are proven offline; only this API layer needs a live run. The batch handle
is durable on OpenAI's side, so a batch that outlives the wait budget is resumed with --resume <batch_id>.

Run under the gated venv (spendguard doctor == ENFORCING HERE: YES). Nothing hardcoded: --model / --cap / --n /
--intent are inputs; nonces are generated per run.

Usage (two phases, estimate-first per the spend protocol):
    # phase 1 — the SEPARATE zero-spend estimate (no --submit):
    .venv.nosync/bin/python scripts/reliability/batch_loop_live_proof.py --model gpt-5-nano
    # phase 2 — real submit (cents, capped) + await + collect:
    .venv.nosync/bin/python scripts/reliability/batch_loop_live_proof.py --model gpt-5-nano --submit --wait-s 540
    # resume a batch that outlived the wait window (async ≤24h is normal):
    .venv.nosync/bin/python scripts/reliability/batch_loop_live_proof.py --model gpt-5-nano --resume <batch_id> --wait-s 300
"""
import argparse
import json
import os
import secrets
import sys
import time

# --- named constants (blast-radius bounds + the proof's own structural params; overridable per run) ---
DEFAULT_PROOF_CAP_USD = 1.0          # the estimate must come in under this or the gate REFUSES (a tiny bound; --cap overrides)
DEFAULT_PROBE_TASKS = 3              # a handful of tasks is enough to prove submit / out-of-order collect / mapping
DEFAULT_INTENT = "batch-loop-live-proof"
POLL_EVERY_S_DEFAULT = 20           # cadence between batch_status reads while the batch runs
_NONCE_BYTES = 5                     # per-task random token the model must echo back (hex → 10 chars)


def _build_probe_tasks(n):
    """n probe tasks, each asking the model to echo a distinct random nonce. Returns (tasks, nonce_by_content):
    tasks are {custom_id, content} dicts for submit_chat_tasks; nonce_by_content maps the exact prompt string to
    its nonce so the caller can key verification by custom_id."""
    tasks, nonce_by_content = [], {}
    for i in range(n):
        nonce = secrets.token_hex(_NONCE_BYTES)
        content = "Reply with exactly this token and nothing else: %s" % nonce
        tasks.append({"custom_id": "probe-%d" % i, "content": content})
        nonce_by_content[content] = nonce
    return tasks, nonce_by_content


def _ensure_nonce_schema(c):
    c.execute("CREATE TABLE IF NOT EXISTS batch_proof_nonces("
              "batch_id TEXT, custom_id TEXT, nonce TEXT, PRIMARY KEY(batch_id, custom_id))")
    c.commit()


def _nonce_db():
    """The proof's expected custom_id→nonce checkpoint on the SAME robust pooled ledger connection the tracker uses
    (keyed 'batch_proof_nonces' — WAL, fork-safe, transactional), so a cross-process --resume reads a durable,
    consistent map. This is 'the sqlite db we have', managed like batch_jobs — not a hand-rolled JSON file."""
    from spendguard import config
    return config.pooled_ledger_conn("batch_proof_nonces", _ensure_nonce_schema)


def _save_nonce_map(bid, nonce_by_cid):
    """Persist the expected custom_id→nonce map at submit in ONE transaction, so a --resume in a later process can
    still verify the round-trip. Best-effort: a checkpoint failure is announced but never blocks the already-paid
    submit — a same-run collect verifies in-process regardless."""
    try:
        db = _nonce_db()
        db.executemany("INSERT OR REPLACE INTO batch_proof_nonces(batch_id,custom_id,nonce) VALUES (?,?,?)",
                       [(bid, cid, nonce) for cid, nonce in nonce_by_cid.items()])
        db.commit()
    except Exception as e:
        from spendguard import config
        config.rollback_ledger_conn("batch_proof_nonces")
        print("[submit] WARN could not persist nonce checkpoint for %s (%s) — a same-run collect still verifies "
              "in-process; a cross-process --resume would then report without per-nonce verification"
              % (bid, type(e).__name__), file=sys.stderr)


def _load_nonce_map(bid):
    """Load the persisted custom_id→nonce map for a --resume; {} if none/error (then --resume reports without
    per-nonce verification, and says so)."""
    try:
        rows = _nonce_db().execute("SELECT custom_id, nonce FROM batch_proof_nonces WHERE batch_id=?",
                                   (bid,)).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        from spendguard import config
        config.rollback_ledger_conn("batch_proof_nonces")
        return {}


def estimate_only(model, cap, n):
    """Phase 1 — the SEPARATE zero-spend estimate through the same guarded chokepoint the real submit uses.
    (One caveat, surfaced not hidden: building the envelope resolves the model's accepted reasoning-effort via
    models.resolve_effort, which makes tiny live probe calls the FIRST time a model is seen in this home — cents'
    fractions, then cached. The batch cost projection itself is $0.)"""
    from spendguard import submit
    tasks, _ = _build_probe_tasks(n)
    res = submit.submit_chat_tasks(tasks, model, submit=False, cap_dollars=cap, intent=DEFAULT_INTENT)
    if res.get("error"):
        print("ESTIMATE ERROR: %s" % res["error"], file=sys.stderr)
        return 2
    from spendguard.submit import estimate_jsonl_cost
    est = estimate_jsonl_cost(res["jsonl"], model, batch=True)
    print(json.dumps({"phase": "estimate", "model": model, "cap_usd": cap, "requests": est["requests"],
                      "in_tok": est["in_tok"], "out_tok_ceiling": est["out_tok"], "out_basis": est["out_basis"],
                      "est_cost_usd_worstcase": round(est["cost"], 6), "mode": est["mode"],
                      "under_cap": est["cost"] <= cap,
                      "note": "est prices output at the model's history/ceiling (safe over-estimate); a nonce echo "
                              "emits ~10 tok/req, so ACTUAL cost is a tiny fraction of this."}, indent=2))
    try:
        os.unlink(res["jsonl"])                            # the estimate's envelope is transient (a projection artefact)
    except OSError:
        pass
    return 0


def submit_and_track(model, cap, n, intent, wait_s, poll_every_s):
    """Phase 2 — submit n nonce-echo tasks to the real OpenAI Batch API through the guarded submit_chat_tasks
    chokepoint (estimate-first + $-capped inside), then await + collect and verify every nonce by custom_id."""
    from spendguard import submit
    tasks, nonce_by_content = _build_probe_tasks(n)
    nonce_by_cid = {t["custom_id"]: nonce_by_content[t["content"]] for t in tasks}
    res = submit.submit_chat_tasks(tasks, model, submit=True, cap_dollars=cap, intent=intent)
    if res.get("error") or not res.get("batch_id"):
        print("SUBMIT FAILED: %s" % (res.get("error") or "no batch_id"), file=sys.stderr)
        return 2
    bid = res["batch_id"]
    _save_nonce_map(bid, nonce_by_cid)                      # durable checkpoint → a later --resume can still verify
    print(json.dumps({"phase": "submit", "batch_id": bid, "requests": res.get("requests"), "model": model,
                      "resume_cmd": "batch_loop_live_proof.py --model %s --resume %s --wait-s 600" % (model, bid)},
                     indent=2))
    return _await_and_collect(bid, nonce_by_cid, intent, model, wait_s, poll_every_s, submitted=n)


def resume_collect(bid, intent, model, wait_s, poll_every_s):
    """Resume a proof batch submitted by an EARLIER run (by batch_id): await + collect. Proves the async/durable
    property — the batch outlived its submitting process on OpenAI's side. Loads the durable custom_id→nonce
    checkpoint so it can STILL verify the round-trip; if the checkpoint is absent (e.g. a pre-checkpoint submit) it
    reports collected results (count + sample) without per-nonce verification and says so."""
    nonce_by_cid = _load_nonce_map(bid)
    if not nonce_by_cid:
        print("[resume] no nonce checkpoint for %s — will report collected results without per-nonce verification"
              % bid, file=sys.stderr)
    return _await_and_collect(bid, nonce_by_cid, intent, model, wait_s, poll_every_s,
                              submitted=(len(nonce_by_cid) or None))


def _await_and_collect(bid, nonce_by_cid, intent, model, wait_s, poll_every_s, submitted):
    """Poll batch_status until output is ready / the batch terminates badly / the wait budget is exhausted, then
    (if ready) collect + verify. The deadline is a LOUD stop, distinct from completion — F2: a limit engaging is
    announced, never a silent give-up."""
    from spendguard import callio, batch_tracker
    deadline = time.time() + max(0, wait_s)
    st, stopped_by_deadline = {}, False
    while True:
        st = callio.batch_status([bid]).get(bid, {})
        print("[status] batch=%s status=%s output_ready=%s completed=%s/%s failed=%s"
              % (bid, st.get("status"), st.get("output_ready"), st.get("completed"), st.get("total"),
                 st.get("failed")))
        if st.get("output_ready") or st.get("status") in batch_tracker._TERMINAL_BAD:
            break
        if time.time() >= deadline:                        # the LIMIT engaging — announce it, never a silent stop (F2)
            stopped_by_deadline = True
            print("[await] ⏱ WAIT BUDGET EXHAUSTED after %ds: batch %s is still %r (output not ready). Stopping "
                  "polling by the DEADLINE — this is the limit engaging, not batch completion; the handle is durable, "
                  "resume with --resume %s." % (wait_s, bid, st.get("status"), bid), file=sys.stderr)
            break
        time.sleep(poll_every_s)

    if not st.get("output_ready"):
        print(json.dumps({"phase": "await", "batch_id": bid, "status": st.get("status"),
                          "stopped_by_deadline": stopped_by_deadline, "LOOP_PROVEN": False,
                          "note": ("wait budget exhausted while the batch was still running — DURABLE on OpenAI's "
                                   "side; re-run --resume %s (async ≤24h is normal)" % bid) if stopped_by_deadline
                                  else "batch terminated without output (status=%s) — a real terminal-bad outcome"
                                       % st.get("status")}, indent=2))
        return 4

    col = callio.collect_chat_tasks(bid, intent, model)
    verified, mismatches = 0, []
    if nonce_by_cid:                                       # the submitting run verifies the round-trip by custom_id
        for cid, want in nonce_by_cid.items():
            got = (col.get("results") or {}).get(cid) or ""
            if want and want in got:
                verified += 1
            else:
                mismatches.append({"custom_id": cid, "want_nonce": want, "got": got[:60]})

    proven = (bool(col.get("results")) and not col.get("failed") and not col.get("anomalies")
              and not col.get("not_ready") and (submitted is None or verified == submitted))
    sample = dict(list((col.get("results") or {}).items())[:3])
    print(json.dumps({"phase": "collect", "batch_id": bid, "collected": col.get("collected"),
                      "failed": col.get("failed"), "anomalies": col.get("anomalies"),
                      "not_ready": col.get("not_ready"), "rows_submitted": submitted,
                      "nonces_verified": verified, "mismatches": mismatches, "results_sample": sample,
                      "LOOP_PROVEN": proven,
                      "note": ("live API loop proven: guarded submit → real batch → status → collect, every nonce "
                               "round-tripped by its custom_id (out-of-order safe)" if proven else
                               "collected, but verification incomplete — inspect failed/anomalies/mismatches")},
                     indent=2))
    return 0 if proven else 4


def main():
    import spendguard
    spendguard.require()                                   # fail closed: halts unless the gate is ENFORCING here

    ap = argparse.ArgumentParser(description="live proof of the batch loop's real-API layer (OpenAI Batch API)")
    ap.add_argument("--model", required=True, help="OpenAI batch model id (e.g. gpt-5-nano) — no source default")
    ap.add_argument("--cap", type=float, default=DEFAULT_PROOF_CAP_USD, help="refuse if the estimate exceeds this $")
    ap.add_argument("--n", type=int, default=DEFAULT_PROBE_TASKS, help="number of probe tasks")
    ap.add_argument("--intent", default=DEFAULT_INTENT, help="attribution label for the proof spend")
    ap.add_argument("--submit", action="store_true", help="actually submit (real, capped spend); omit for $0 estimate")
    ap.add_argument("--resume", metavar="BATCH_ID", help="resume+collect a batch submitted by an earlier run")
    ap.add_argument("--wait-s", type=int, default=0, help="max wall-clock seconds to await completion")
    ap.add_argument("--poll-every-s", type=int, default=POLL_EVERY_S_DEFAULT, help="seconds between status reads")
    a = ap.parse_args()

    if a.resume:
        return resume_collect(a.resume, a.intent, a.model, a.wait_s, a.poll_every_s)
    if not a.submit:
        return estimate_only(a.model, a.cap, a.n)
    return submit_and_track(a.model, a.cap, a.n, a.intent, a.wait_s, a.poll_every_s)


if __name__ == "__main__":
    sys.exit(main())
