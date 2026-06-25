#!/usr/bin/env python
"""Reconstruct ungated REALTIME LLM spend from conversations via the Batch API (cheap, async, admin-free).

The realtime calls the gate never recorded are SPARSE and described many ways (live call-code, worker pools, OR just a
printed cost line like 'typed 840 · $0.483'). We FIND them agentically with a cheap-ish model, then opus CONSOLIDATEs
fragments into DISTINCT executed runs and prices them (printed-$ = ground truth, else estimate). BATCH spend is already
EXACT from the regular batch API, so batch-known chunks are removed BEFORE the LLM sees them and any found run matching
a known batch is subtracted — no double-count. Admin API is NOT used (dev cross-check only).

  submit  : gather last-2-month, batch-known-removed, realtime-tell chunks → submit ONE Sonnet find batch.
  status  : poll the batch.
  collect : parse run-fragments → opus-consolidate (run-identity) → price → print realtime $ by org.

Run UNDER the gated venv (`.venv/bin/python`). Estimate-first done + approved (~$15 Batch API, tell-filtered scope).
"""
import sys, os, json, re
import spendguard; spendguard.require()
from spendguard import conv, resources, config, calls, pricing

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "realtime_find_batch_state.json")
SINCE = "2026-04-25"                                   # last 2 months (from 2026-06-25)
FIND_MODEL = "claude-sonnet-4-6"                       # haiku failed recall; sonnet = 3/3 in eval
CONSOLIDATE_MODEL = "claude-opus-4-8"


def gather(tell_only=True):
    """Last-2-month chunks, batch-known removed; tell_only keeps only realtime-tell chunks (call-code OR printed-$)."""
    raw = list(conv.session_chunks(max_chars=14000, since=SINCE))
    sent = [(s, e) for (s, e) in raw if not (resources._BATCH_CTX.search(e) and not resources._RT_TELL.search(e))]
    if tell_only:
        sent = [(s, e) for (s, e) in sent if resources._RT_TELL.search(e)]
    return sent


def submit(tell_only=True):
    import anthropic
    key = config.api_key("ANTHROPIC_API_KEY")
    chunks = gather(tell_only=tell_only)
    reqs, mapping = [], {}
    for i, (sid, ex) in enumerate(chunks):
        cid = "c%05d" % i
        mapping[cid] = sid
        reqs.append({"custom_id": cid, "params": {"model": FIND_MODEL, "max_tokens": 500,
                     "system": resources._RT_FIND_SYS, "messages": [{"role": "user", "content": ex}]}})
    c = anthropic.Anthropic(api_key=key)
    with calls.context(intent="spendguard:realtime_find_batch_submit"):
        batch = c.messages.batches.create(requests=reqs)
    json.dump({"batch_id": batch.id, "mapping": mapping, "model": FIND_MODEL, "n": len(chunks),
               "tell_only": tell_only, "since": SINCE}, open(STATE, "w"))
    print("SUBMITTED %s | %d requests | scope: since %s, batch-known removed, tell_only=%s"
          % (batch.id, len(chunks), SINCE, tell_only))


def status():
    import anthropic
    st = json.load(open(STATE))
    c = anthropic.Anthropic(api_key=config.api_key("ANTHROPIC_API_KEY"))
    b = c.messages.batches.retrieve(st["batch_id"])
    print("batch %s | status=%s | counts=%s" % (st["batch_id"], b.processing_status, b.request_counts))
    return b.processing_status


def _parse_frags():
    import anthropic
    st = json.load(open(STATE))
    mapping = st["mapping"]
    c = anthropic.Anthropic(api_key=config.api_key("ANTHROPIC_API_KEY"))
    frags = []
    for res in c.messages.batches.results(st["batch_id"]):
        if res.result.type != "succeeded":
            continue
        txt = "".join(b.text for b in res.result.message.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        try:
            runs = (json.loads(m.group(0)).get("runs") if m else []) or []
        except Exception:
            runs = []
        for rn in runs:
            if str(rn.get("kind") or "realtime").lower() == "batch":
                continue                              # batch is counted in the batch ledger
            rn["sid"] = mapping.get(res.custom_id, "")
            frags.append(rn)
    json.dump(frags, open(STATE + ".frags", "w"))
    return frags


def _parse_runs(txt):
    """TOLERANT per-object parse — salvage run dicts even when the JSON array was truncated at max_tokens
    (runs are FLAT objects, no nested braces, so {[^{}]*} matches each)."""
    out = []
    for mm in re.finditer(r"\{[^{}]*\}", txt or ""):
        try:
            o = json.loads(mm.group(0))
            if isinstance(o, dict) and o.get("model"):
                out.append(o)
        except Exception:
            pass
    return out


def collect():
    if status() != "ended":
        print("batch not ended yet — re-run `collect` later")
        return
    fp = STATE + ".frags"
    frags = json.load(open(fp)) if os.path.exists(fp) else _parse_frags()   # reuse saved frags — no re-batch
    print("realtime run-fragments found:", len(frags))
    if not frags:
        return
    from spendguard import adapters
    def consolidate(items):
        with calls.context(intent="spendguard:realtime_find_batch_consolidate"):
            r = adapters.call(CONSOLIDATE_MODEL, json.dumps(items)[:90000], max_tokens=4000,
                              system=resources._RT_CONSOLIDATE_SYS)
        return _parse_runs(r.get("text", ""))           # tolerant parse — never lose a group to truncation
    GROUP = 25                                           # small groups so each group's run-list fits under max_tokens (no truncation loss)
    group_runs = []
    for i in range(0, len(frags), GROUP):
        group_runs.extend(consolidate(frags[i:i + GROUP]))
    merged = {}                                          # MECHANICAL cross-group run-identity (no final-merge truncation)
    for rn in group_runs:
        ms = resources._norm_model(rn.get("model")); rn["model"] = ms
        pu = rn.get("printed_usd")
        key = (str(rn.get("name") or "")[:40].lower(), ms, round(float(pu), 2) if pu not in (None, "", 0) else None)
        merged.setdefault(key, rn)
    runs = list(merged.values())
    # PRICE (printed-$ = ground truth, else estimate) + attribute via session_classification
    rows, by_org = [], {}
    for rn in runs:
        ms = resources._norm_model(rn.get("model"))
        printed = rn.get("printed_usd")
        if printed not in (None, "", 0):
            try:
                usd, basis = round(float(printed), 2), "printed"
            except Exception:
                usd, basis = 0.0, "printed"
        else:
            try:
                usd = round(pricing.realtime_cost(ms, int(rn.get("total_in") or 0), int(rn.get("total_out") or 0)), 2)
            except Exception:
                usd = 0.0
            basis = "estimated"
        if usd <= 0:
            continue
        sid0 = (rn.get("sessions") or [None])[0]
        sc = (conv.session_classification(sid0) if sid0 else {}) or {}
        org = sc.get("org") or "(untagged)"
        by_org[org] = round(by_org.get(org, 0.0) + usd, 2)
        rows.append({"name": rn.get("name"), "model": ms, "usd": usd, "basis": basis, "org": org,
                     "calls": rn.get("calls"), "reasoning": (rn.get("reasoning") or "")[:120]})
    total = round(sum(r["usd"] for r in rows), 2)
    pr = round(sum(r["usd"] for r in rows if r["basis"] == "printed"), 2)
    json.dump({"total": total, "printed": pr, "by_org": by_org, "runs": rows}, open(STATE + ".result", "w"), indent=2)
    print("\n=== RECONSTRUCTED REALTIME (tell-filtered, last 2 months) ===")
    print("  distinct runs: %d  |  TOTAL realtime $%.2f  (printed-$ ground truth $%.2f / estimated $%.2f)"
          % (len(rows), total, pr, total - pr))
    print("  by org:", by_org)
    for r in sorted(rows, key=lambda x: -x["usd"])[:15]:
        print("   %-30s %-9s %-9s $%7.2f | %s" % (str(r["name"])[:30], r["model"], r["basis"], r["usd"], r["reasoning"]))


_RECLASSIFY_SYS = (
    "You AUDIT reconstructed 'realtime LLM run' records to REMOVE wrong entries before totaling realtime API spend. "
    "Decide what each run REALLY is:\n"
    "- REALTIME: a genuine realtime (non-batch) CHAT/COMPLETION API run (gpt-5.5 / opus / sonnet / haiku completions). KEEP.\n"
    "- EMBEDDING: an embedding/vectorize job (text-embedding-*, 'embedding', 'embed', 'vectorize'). EXCLUDE — embeddings run "
    "via Batch API and cost ~30-100x LESS than completions; pricing one as a gpt-5.5 completion is the classic over-count.\n"
    "- BATCH: a Batch API run ('batch'/'batches'/'msgbatch'/'.batches.'/24h window). EXCLUDE — already in the batch ledger.\n"
    "- META: spendguard's OWN operation — a billing/usage/costs API pull, a reconcile, a cost-audit read. EXCLUDE — not a workload run.\n"
    "Also set inflated=true when basis='estimated' and the $ is implausibly high for the described work (e.g. embeddings or "
    "huge token counts priced as completions). Input: runs [{idx,name,model,usd,basis,reasoning}]. "
    'Output STRICT JSON: {"tags":[{"idx":<int>,"tag":"REALTIME|EMBEDDING|BATCH|META","inflated":<bool>}]}.')


def _parse_tags(txt):
    out = []
    for mm in re.finditer(r"\{[^{}]*\}", txt or ""):
        try:
            o = json.loads(mm.group(0))
            if isinstance(o, dict) and "idx" in o and o.get("tag"):
                out.append(o)
        except Exception:
            pass
    return out


def clean():
    from spendguard import adapters
    res = json.load(open(STATE + ".result"))
    runs = res["runs"]
    tag_by_idx = {}
    G = 80
    for i in range(0, len(runs), G):
        grp = [{"idx": i + j, "name": (r.get("name") or "")[:60], "model": r.get("model"), "usd": r.get("usd"),
                "basis": r.get("basis"), "reasoning": (r.get("reasoning") or "")[:90]} for j, r in enumerate(runs[i:i + G])]
        with calls.context(intent="spendguard:realtime_reclassify"):
            rr = adapters.call(CONSOLIDATE_MODEL, json.dumps(grp), max_tokens=4000, system=_RECLASSIFY_SYS)
        for t in _parse_tags(rr.get("text", "")):
            tag_by_idx[int(t["idx"])] = t
    # apply: sum REALTIME only; bucket the rest
    buckets = {"REALTIME": 0.0, "EMBEDDING": 0.0, "BATCH": 0.0, "META": 0.0, "?": 0.0}
    by_org, kept, inflated = {}, [], []
    for idx, r in enumerate(runs):
        t = tag_by_idx.get(idx, {"tag": "?", "inflated": False})
        tag = t.get("tag", "?")
        buckets[tag] = round(buckets.get(tag, 0.0) + r["usd"], 2)
        if tag == "REALTIME":
            if t.get("inflated") and r.get("basis") == "estimated":
                inflated.append(r); continue          # drop inflated estimates (conservative); list them for review
            org = r.get("org") or "(untagged)"
            by_org[org] = round(by_org.get(org, 0.0) + r["usd"], 2)
            kept.append(r)
    total = round(sum(r["usd"] for r in kept), 2)
    printed = round(sum(r["usd"] for r in kept if r.get("basis") == "printed"), 2)
    out = {"realtime_total": total, "printed": printed, "by_org": by_org, "excluded": buckets,
           "kept": kept, "inflated_dropped": inflated}
    json.dump(out, open(STATE + ".clean", "w"), indent=2)
    # stable cache the reconcile loop READS (admin-free realtime axis). TIGHTEN here: printed-$ = ground truth (full),
    # soft estimates halved (they overshoot ~2x). Org-level for now; per-project/per-month needs collect to keep those.
    def _tight(r):
        return r["usd"] if r.get("basis") == "printed" else round(r["usd"] * 0.5, 2)
    cache = {"since": SINCE, "total": round(sum(_tight(r) for r in kept), 2),
             "note": "2-month window; org-level; per-project + per-month split is a refinement",
             "rows": [{"org": r.get("org") or "(untagged)",
                       "provider": "openai" if "gpt" in (r.get("model") or "").lower() else "anthropic",
                       "usd": _tight(r)} for r in kept]}
    cache_path = os.path.expanduser("~/.spendguard/realtime_reconstruction.json")
    json.dump(cache, open(cache_path, "w"), indent=2)
    print("  wrote reconcile cache:", cache_path, "(tightened total $%.2f)" % cache["total"])
    print("=== CLEANED REALTIME (embeddings/batch/meta removed, inflated estimates dropped) ===")
    print("  CORRECTED realtime $%.2f  (printed-$ %.2f / estimated %.2f)  across %d runs"
          % (total, printed, total - printed, len(kept)))
    print("  by org:", by_org)
    print("  EXCLUDED: embeddings $%.0f · batch $%.0f · spendguard-meta $%.0f · inflated-est dropped $%.0f (%d)"
          % (buckets["EMBEDDING"], buckets["BATCH"], buckets["META"],
             round(sum(r["usd"] for r in inflated), 2), len(inflated)))
    print("  top kept realtime runs:")
    for r in sorted(kept, key=lambda x: -x["usd"])[:12]:
        print("   %-32s %-9s %-9s $%8.2f" % (str(r.get("name"))[:32], r.get("model"), r.get("basis"), r["usd"]))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"submit": submit, "status": status, "collect": collect, "clean": clean}.get(cmd, status)()
