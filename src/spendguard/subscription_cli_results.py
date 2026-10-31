"""Provider CLI response contracts, independent of workspace and process ownership."""
import json
import time


def antigravity_completed_response(completed, started_at, decode_result, decode_usage, error_result):
    """Decode the completed CLI transport without changing its provider error/usage contract."""
    if completed.returncode != 0:
        return error_result(completed.stderr or completed.stdout or "agy exited non-zero")   # quota→a reset window rides retry_after_s
    obj = decode_result(completed.stdout)
    if obj is None:
        return {"error": "agy produced no parseable json result"}
    if obj.get("status") != "SUCCESS":
        return error_result(obj.get("error") or f"agy status={obj.get('status')!r}")
    text = (obj.get("response") or "").strip()
    if not text:
        return {"error": "agy produced no response text"}
    in_tok, out_tok = decode_usage(obj)
    return {"text": text, "in_tok": in_tok, "out_tok": out_tok, "latency": time.time() - started_at, "error": None}


def kimi_warm_prompt_result(full, model, reasoning, timeout, durable_dispatch_id, run_warm, append_evidence):
    """Try ACP once; preserve durable evidence, hard refusals, and cold fallback semantics."""
    _t0 = time.time()
    try:
        _r = run_warm(full, model=model, reasoning=reasoning, timeout=timeout)
    except Exception as _e:                            # an exception must NEVER bypass the cold/API fallback below
        if durable_dispatch_id:
            raise
        _r = {"error": f"kimi daemon raised: {str(_e)[:150]}"}
    if durable_dispatch_id:
        append_evidence(
            durable_dispatch_id, stdout=json.dumps(_r) + "\n")
    if _r.get("text") and not _r.get("error"):
        _txt = _r["text"]
        return {"text": _txt, "in_tok": len(full) // 4, "out_tok": len(_txt) // 4,   # est: the ACP stream has no usage
                "latency": round(time.time() - _t0, 2), "error": None}
    if _r.get("tool_error"):
        # A HARD refusal/cancel. A cold `kimi -p` would refuse the same way, so return the error NOW and let the
        # adapter fall back to the metered API + back off the lane. NEVER surface the refusal text as `text`. (A
        # merely TRANSIENT daemon problem — would-not-start / dead pipe / timeout — has no tool_error, so it still
        # falls through to one cold `kimi -p` below.)
        return {"error": (_r.get("error") or "kimi acp refused the request")[:200]}
    return None


def kimi_assistant_response_text(stdout):
    """The final assistant answer from `kimi -p --output-format stream-json`: JSONL where the answer rides
    {"role":"assistant","content":…} and meta/tool events ride role='meta'. Keep the LAST non-empty assistant
    content (the final message, like codex's --output-last-message); content is a string or a list of text blocks.
    Mechanical extraction of a fixed event shape; '' when no assistant message is present."""
    text = ""
    for ln in (stdout or "").splitlines():
        s = ln.strip()
        if not s.startswith("{"):
            continue
        try:
            d = json.loads(s)
        except Exception:
            continue
        if d.get("role") != "assistant":
            continue
        c = d.get("content")
        if isinstance(c, list):
            c = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") in (None, "text"))
        if isinstance(c, str) and c.strip():
            text = c                                          # keep the latest non-empty assistant message (final answer)
    return text


