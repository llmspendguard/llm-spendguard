"""`spendguard batch-submit` / `batch-fetch` — the GATED human-run halves of an OpenAI Batch-API job.

A caller (e.g. symgrep's bulk describe bootstrap) prepares a request .jsonl out-of-band; the SPEND and the
result download run HERE, under the enforcing gate, one deliberate command each:

  spendguard batch-submit --jsonl reqs.jsonl --model gpt-5.6-luna --cap 5 --dry-run   # $0 estimate (no API call)
  spendguard batch-submit --jsonl reqs.jsonl --model gpt-5.6-luna --cap 5             # SUBMIT (metered, capped)
  spendguard batch-fetch  --batch-id batch_abc --out out.jsonl                        # poll; download when done

submit reuses submit.guarded_submit (estimate -> enforce cap -> submit); fetch polls batches.retrieve and, on
completion, downloads the output (and any error file) so the caller can ingest it. Nothing hardcoded: jsonl,
model, cap, avg-out, batch id, and out path are all arguments.
"""
import argparse
import os
import sys
import time


def submit_batch_jsonl(rest):
    ap = argparse.ArgumentParser(prog="spendguard batch-submit")
    ap.add_argument("--jsonl", required=True, help="request .jsonl (each line a /v1/chat/completions batch item)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--cap", type=float, default=None, help="refuse if projected $ exceeds this (0 = no spend)")
    ap.add_argument("--avg-out", type=float, default=None,
                    help="measured avg output tokens/item for the estimate (else the max_tokens ceiling is used)")
    ap.add_argument("--endpoint", default="/v1/chat/completions")
    ap.add_argument("--dry-run", action="store_true", help="estimate + cap check only, no submit ($0, no API call)")
    a = ap.parse_args(rest)
    from . import submit
    bid = submit.guarded_submit(a.jsonl, a.model, a.cap, batch=True, avg_out_tokens=a.avg_out,
                                submit=not a.dry_run, endpoint=a.endpoint)
    if a.dry_run:
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
    a = ap.parse_args(rest)
    from openai import OpenAI
    from .submit import _api_key
    client = OpenAI(api_key=_api_key("OPENAI_API_KEY"))
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
