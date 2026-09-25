"""`spendguard batch-submit` / `batch-fetch` — the GATED human-run halves of an OpenAI Batch-API job.

A caller supplies TASKS ({custom_id, content} per line) + a shared --system + --model; spendguard BUILDS the
per-model request envelope (via models.apply_call_params — the ONE authority for max_tokens-vs-max_completion_tokens
and reasoning), so a caller never hand-rolls a body and can't send a model-wrong param (the failure that returned
250/250 HTTP 400 "use max_completion_tokens"). The SPEND and the result download run HERE, under the enforcing
gate, one deliberate command each:

  spendguard batch-submit --tasks tasks.jsonl --system-file sys.txt --model gpt-5.6-luna --cap 5 --dry-run  # $0 estimate
  spendguard batch-submit --tasks tasks.jsonl --system-file sys.txt --model gpt-5.6-luna --cap 5            # SUBMIT (metered, capped)
  spendguard batch-fetch  --batch-id batch_abc --out out.jsonl                                              # poll; download when done

  (legacy: --jsonl reqs.jsonl accepts a PRE-BUILT request .jsonl — you then own each body's per-model params.)

custom_id is the caller's mapping key, preserved verbatim and returned on each result line. submit reuses
submit.build_chat_batch_jsonl (task → per-model envelope) + submit.guarded_submit (estimate → enforce cap →
submit); fetch polls batches.retrieve and, on completion, downloads the output (and any error file) so failures
stay visible. Nothing hardcoded: tasks/jsonl, system, model, cap, avg-out, max-out, batch id, out path are args.
"""
import argparse
import os
import sys
import time


def submit_batch_jsonl(rest):
    ap = argparse.ArgumentParser(prog="spendguard batch-submit")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--tasks", help='tasks .jsonl — each line {"custom_id": <id>, "content": <user text>}; '
                     "spendguard builds the per-model request envelope (RECOMMENDED — the caller never hand-rolls a body, "
                     "so a model-wrong param like max_tokens-vs-max_completion_tokens can't reach the API)")
    src.add_argument("--jsonl", help="a PRE-BUILT request .jsonl (legacy — you own each body's per-model params)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--system", help="shared system instruction, sent once per request (--tasks mode)")
    ap.add_argument("--system-file", help="read the shared system instruction from this file (--tasks mode; wins over --system)")
    ap.add_argument("--max-out", type=int, default=None,
                    help="output-token ceiling per request (--tasks mode); default = a reasoning-safe floor so reasoning "
                         "can't empty the reply")
    ap.add_argument("--cap", type=float, default=None, help="refuse if projected $ exceeds this (0 = no spend)")
    ap.add_argument("--avg-out", type=float, default=None,
                    help="measured avg output tokens/item for the estimate (else the per-request ceiling is used)")
    ap.add_argument("--endpoint", default="/v1/chat/completions")
    ap.add_argument("--dry-run", action="store_true", help="estimate + cap check only, no submit ($0, no API call)")
    ap.add_argument("--intent", default=None, help="tag this batch's spend with a job-type intent (e.g. "
                    "symgrep-block-index) — else it attributes to '(none)', exactly like an untagged realtime call")
    ap.add_argument("--chain", default=None, help="optional chain id linking this batch to a larger job")
    a = ap.parse_args(rest)
    from . import submit, calls
    _built = None
    try:
        if a.tasks:
            if a.system_file:
                with open(a.system_file) as _sf:
                    system = _sf.read()
            else:
                system = a.system
            _built, n = submit.build_chat_batch_jsonl(a.tasks, a.model, system=system, max_out=a.max_out)
            print(f"[batch-submit] built {n:,} request(s) from tasks via spendguard/models.py "
                  f"(per-model envelope — no hand-rolled params) → {_built}", file=sys.stderr)
            jsonl_path = _built
        else:
            jsonl_path = a.jsonl
        # ATTRIBUTION: run the submit UNDER the caller's intent so the batch's spend (the estimate recorded at submit,
        # and the reconciled actuals) attributes to the job — not '(none)'. Exactly like a realtime calls.context.
        with calls.context(intent=a.intent, chain=a.chain):
            bid = submit.guarded_submit(jsonl_path, a.model, a.cap, batch=True, avg_out_tokens=a.avg_out,
                                        submit=not a.dry_run, endpoint=a.endpoint)
    finally:
        if _built and not a.dry_run:          # keep the built envelope on --dry-run (inspectable); else clean the temp
            try:
                os.unlink(_built)
            except OSError:
                pass
    if a.dry_run:
        if _built:
            print(f"[batch-submit] (dry-run) built envelope kept for inspection: {_built}", file=sys.stderr)
        return 0
    if not bid:
        print("batch-submit: no batch id returned (see gate output above).", file=sys.stderr)
        return 1
    print(bid)
    return 0


def _write_preserving(path, text, force):
    """Write `text` to `path` WITHOUT ever irreversibly losing prior contents: a non-empty existing file is
    refused unless `force`, and even with `force` it is RENAMED aside to <path>.bak_<ts> before the new write —
    so a re-fetch never obliterates an earlier download (a batch output is not free to reproduce)."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        if not force:
            raise SystemExit(f"batch-fetch: refusing to overwrite existing {path} (pass --force to keep it as "
                             f"{path}.bak_<ts>, or choose a fresh --out). Nothing was written.")
        bak = f"{path}.bak_{time.strftime('%Y%m%d_%H%M%S')}"
        os.replace(path, bak)
        print(f"batch-fetch: preserved prior {path} -> {bak}")
    with open(path, "w") as f:
        f.write(text)


def fetch_batch_output(rest):
    ap = argparse.ArgumentParser(prog="spendguard batch-fetch")
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--out", required=True, help="write the completed batch's output .jsonl here")
    ap.add_argument("--force", action="store_true", help="keep an existing --out/.errors as .bak_<ts>, then write")
    ap.add_argument("--intent", default=None, help="tag any spend from this fetch with the batch's job-type intent "
                    "(the download itself is a $0 GET; this keeps attribution consistent with the submit)")
    ap.add_argument("--chain", default=None, help="optional chain id linking this fetch to a larger job")
    a = ap.parse_args(rest)
    from openai import OpenAI
    from .submit import _api_key
    from . import calls
    client = OpenAI(api_key=_api_key("OPENAI_API_KEY"))
    with calls.context(intent=a.intent, chain=a.chain):
        return _fetch_under_context(client, a)


def _fetch_under_context(client, a):
    """The retrieve + download, run inside the caller's intent context (so any spend attributes to the batch's job, not
    '(none)'). Split out so the fetch body is not re-indented wholesale. NOTE: the download is a $0 file GET
    (client.files.content) — the model spend is the BATCH itself, reconciled separately; if the gate ever prices this
    GET as a realtime model call, that is a separate fix (batch-attribution issue #3, still open)."""
    b = client.batches.retrieve(a.batch_id)
    counts = getattr(b, "request_counts", None)
    print(f"batch {a.batch_id}: status={b.status}"
          + (f"  (completed={getattr(counts, 'completed', '?')} failed={getattr(counts, 'failed', '?')} "
             f"total={getattr(counts, 'total', '?')})" if counts else ""))
    if b.status != "completed":
        if b.status in ("failed", "expired", "cancelled"):
            print(f"batch-fetch: batch is {b.status} — it will not complete. "
                  f"{'error_file present' if getattr(b, 'error_file_id', None) else 'no output'}.", file=sys.stderr)
            return 2
        print("batch-fetch: not finished yet — re-run when status=completed.")
        return 3
    if not getattr(b, "output_file_id", None):
        print("batch-fetch: completed but no output_file_id — nothing to download.", file=sys.stderr)
        return 2
    text = client.files.content(b.output_file_id).text
    _write_preserving(a.out, text, a.force)
    n = sum(1 for ln in text.splitlines() if ln.strip())
    print(f"batch-fetch: wrote {n:,} result line(s) -> {a.out}")
    # a partial batch also has an error file — download it beside the output so failures are visible, not lost
    if getattr(b, "error_file_id", None):
        etext = client.files.content(b.error_file_id).text
        _write_preserving(a.out + ".errors", etext, a.force)
        ne = sum(1 for ln in etext.splitlines() if ln.strip())
        print(f"batch-fetch: {ne:,} errored request(s) -> {a.out}.errors")
    return 0
