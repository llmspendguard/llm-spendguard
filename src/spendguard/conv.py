"""Layer 2 — post-event CONVERSATION mining (where the *decisions* live).

git history and ledgers say what happened; the conversation says WHY and whether it worked. This mines
session transcripts (Claude Code .jsonl, or any text) for the cost decisions and outcomes the live
recorder never captured — "$231 surprise", "hardcoded constant was the gap", "pack 25-40/req", "don't
cancel" — and turns them into confidence-scored insights + graph events.

Two stages:
  index   DETERMINISTIC, zero spend, CACHED (~/.spendguard/conv_index.json keyed by file mtime+size, so
          the slow full scan runs once). Extracts high-signal events (a run batch-id mention, or a $-figure
          co-occurring with a cost-decision keyword), writes `conversation_event` nodes + `comments_on`
          edges to the runs they discuss.
  synth   CAGED LLM synthesis (config.advisor_model, intent spendguard:conv-synth → caps.meta). Feeds the
          top deduped decision snippets to the reasoner → source='conversation' insights. ESTIMATE-ONLY
          unless --run. Sends curated, truncated snippets of YOUR OWN transcripts to the model.

CLI: `spendguard mine-conv {index,synth} [--transcripts PATH] [--limit N] [--run]`.
"""
import os, re, json, glob
from . import config, calls, learn

_DEFAULT_TDIR = os.path.expanduser("~/.claude/projects")
_CACHE = os.path.join(str(config.HOME), "conv_index.json")

_BID = re.compile(r"(batch_[0-9a-f]{20,}|msgbatch_[0-9A-Za-z]{18,})")
_COST = re.compile(r"\$[0-9]{2,}(?:\.[0-9]+)?")
_SIG = re.compile(r"(pack|cheaper|expensive|cancel|wrong|waste|re-?run|hardcod|overcharg|surprise|"
                  r"too much|batch|realtime|estimate|cap\b|budget|reasoning|mini|nano|opus|haiku)", re.I)
_SNIP = 320


# ─────────────────────────── transcript parsing ───────────────────────────
def _text_of(obj):
    m = obj.get("message") if isinstance(obj, dict) else None
    c = (m or {}).get("content") if isinstance(m, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                parts.append(b["text"])
            elif b.get("type") == "tool_result":
                tc = b.get("content")
                if isinstance(tc, str):
                    parts.append(tc)
                elif isinstance(tc, list):
                    parts += [x.get("text", "") for x in tc if isinstance(x, dict)]
        return "\n".join(parts)
    return ""


def _events_in(path, run_ids):
    """High-signal events in one transcript: a run batch-id mention, or a $-figure + decision keyword."""
    out = []
    try:
        lines = open(path, errors="ignore").read().splitlines()
    except Exception:
        return out
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        txt = _text_of(obj)
        if not txt:
            continue
        bids = [b for b in set(_BID.findall(txt)) if b in run_ids]
        costs = _COST.findall(txt)
        sigs = sorted(set(s.lower() for s in _SIG.findall(txt)))
        if not bids and not (costs and sigs):
            continue
        role = obj.get("type") or (obj.get("message") or {}).get("role")
        # window the snippet around the first signal so we keep the relevant sentence
        anchor = 0
        m = _COST.search(txt) or _SIG.search(txt)
        if m:
            anchor = max(0, m.start() - 80)
        out.append(dict(role=role, ts=obj.get("timestamp"), runs=bids,
                        costs=costs[:6], sigs=sigs[:8], text=txt[anchor:anchor + _SNIP].strip()))
    return out


# ─────────────────────────── index (cached, deterministic) ───────────────────────────
def _run_ids():
    with learn._lock:
        return {r[0] for r in learn._db().execute("SELECT id FROM graph_nodes WHERE type='run'").fetchall()}


def build_index(tdir=None, rebuild=False):
    """Scan transcripts → cached event index. Reuses cached per-file events when mtime+size unchanged."""
    tdir = tdir or _DEFAULT_TDIR
    files = sorted(glob.glob(os.path.join(tdir, "**", "*.jsonl"), recursive=True)) if os.path.isdir(tdir) else [tdir]
    cache = {}
    if os.path.exists(_CACHE) and not rebuild:
        try:
            cache = json.load(open(_CACHE)).get("files", {})
        except Exception:
            cache = {}
    run_ids = _run_ids()
    out = {}
    scanned = 0
    for p in files:
        try:
            st = os.stat(p)
        except Exception:
            continue
        sig = {"mtime": int(st.st_mtime), "size": st.st_size}
        prev = cache.get(p)
        if prev and prev.get("mtime") == sig["mtime"] and prev.get("size") == sig["size"]:
            out[p] = prev
            continue
        ev = _events_in(p, run_ids)
        out[p] = {**sig, "events": ev}
        scanned += 1
    os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
    json.dump({"files": out, "tdir": tdir}, open(_CACHE, "w"))
    return out, scanned


# NOTE: these keyword patterns are EXAMPLES showing how a snippet's vocabulary → a project tag (two distinct
# example domains: an NLP/text pipeline and a vision/image pipeline). Adjust them to YOUR projects' domain
# keywords — the defaults won't match an unrelated codebase.
_PROJECT_RULES = [
    ("nlp-pipeline", r"embedding|entity|\bner\b|extract|classif|corpus|ontolog|tokeniz|sentiment"),
    ("vision-pipeline", r"image|frame|video|segment|detect|caption|render|\bvae\b"),
]


def _project_of(snippet):
    s = (snippet or "").lower()
    for proj, pat in _PROJECT_RULES:
        if re.search(pat, s):
            return proj
    return ""


def attribute_usage(since="2026-06-01", tdir=None):
    """Proper accounting: match actual provider USAGE (per-batch cost) to PROJECTS via the conversation that ran
    each batch (batch id → transcript → content), instead of a blanket 'unattributed' bucket. Uses the
    timestamps/content already indexed. Returns {total, batches, linked, by_project:{proj:$}}. Free (no spend)."""
    from . import backfill
    costs = {}
    for _prov, _model, cost, _it, _ot, day, bid in (backfill._openai_rows() + backfill._anthropic_rows()):
        if (day or "") >= since:
            costs[bid] = costs.get(bid, 0.0) + cost
    links = batch_links(tdir)
    by_project = {}
    linked = 0
    for bid, c in costs.items():
        if bid in links:
            linked += 1
            p = _project_of(links[bid]["snippet"]) or "linked-unclear"
        else:
            p = "no-conversation"
        by_project[p] = round(by_project.get(p, 0.0) + c, 4)
    return {"total": round(sum(costs.values()), 2), "batches": len(costs), "linked": linked, "by_project": by_project}


def attribute_cmd(argv=None):
    r = attribute_usage()
    print(f"usage→project by conversation evidence (MTD): ${r['total']:.2f} · {r['batches']} batches · {r['linked']} linked")
    for p, c in sorted(r["by_project"].items(), key=lambda x: -x[1]):
        pct = (100 * c / r["total"]) if r["total"] else 0
        print(f"  {p:22} ${c:9.2f}  ({pct:.0f}%)")
    print("  (linked-unclear / no-conversation = candidates for the gated LLM-residual tagger)")
    return 0


def batch_links(tdir=None):
    """{batch_id: {conv, path, snippet, ts}} — which conversation (transcript = a Claude Code session id)
    references each batch id. The bridge from a recovered per-request call (call_io.batch) to its chat context."""
    index, _ = build_index(tdir)
    links = {}
    for path, rec in index.items():
        conv = os.path.splitext(os.path.basename(path))[0]    # transcript filename = the session/conversation id
        for ev in rec.get("events", []):
            for bid in ev.get("runs", []):
                links.setdefault(bid, {"conv": conv, "path": path, "snippet": (ev.get("text") or "")[:200], "ts": ev.get("ts")})
    return links


def batch_context(batch_id, turns=10, maxchars=3500, tdir=None):
    """The real pre/post conversation context for a batch: up to `turns` message turns BEFORE and AFTER the batch
    was first mentioned in its transcript — the 'why before / outcome after', not just a one-line snippet. Each
    side capped at maxchars. Returns {before, at, after} (empty if the batch isn't in any transcript)."""
    info = batch_links(tdir).get(batch_id)
    if not info:
        return {"before": "", "at": "", "after": ""}
    try:
        lines = open(info["path"], errors="ignore").read().splitlines()
    except Exception:
        return {"before": "", "at": "", "after": ""}
    seq, hit = [], None
    for ln in lines:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        txt = _text_of(obj)
        if not txt:
            continue
        role = obj.get("type") or (obj.get("message") or {}).get("role") or "?"
        seq.append((role, " ".join(txt.split())))
        if hit is None and batch_id in txt:
            hit = len(seq) - 1
    if hit is None:
        return {"before": "", "at": "", "after": ""}
    fmt = lambda chunk: "\n".join(f"[{r}] {t}" for r, t in chunk)[-maxchars:]
    return {"before": fmt(seq[max(0, hit - turns):hit]),
            "at": seq[hit][1][:300],
            "after": fmt(seq[hit + 1:hit + 1 + turns])[:maxchars]}


def batch_contexts(tdir=None, turns=10, maxchars=3500):
    """{batch_id: {conv, before, at, after}} for every batch referenced in a transcript — ONE pass over the files
    (efficient bulk version of batch_context, for linking the whole corpus). ~`turns` turns before/after each."""
    index, _ = build_index(tdir)
    run_ids = _run_ids()
    out = {}
    fmt = lambda chunk: "\n".join(f"[{r}] {t}" for r, t in chunk)[-maxchars:]
    for path in index.keys():
        conv = os.path.splitext(os.path.basename(path))[0]
        try:
            lines = open(path, errors="ignore").read().splitlines()
        except Exception:
            continue
        seq, hits = [], {}
        for ln in lines:
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            txt = _text_of(obj)
            if not txt:
                continue
            seq.append((obj.get("type") or (obj.get("message") or {}).get("role") or "?", " ".join(txt.split())))
            for bid in set(_BID.findall(txt)):
                if bid in run_ids and bid not in hits and bid not in out:
                    hits[bid] = len(seq) - 1
        for bid, h in hits.items():
            out[bid] = {"conv": conv, "before": fmt(seq[max(0, h - turns):h]),
                        "at": seq[h][1][:300], "after": fmt(seq[h + 1:h + 1 + turns])[:maxchars]}
    return out


def _all_events(index):
    for rec in index.values():
        for ev in rec.get("events", []):
            yield ev


def _score(ev):
    return len(ev.get("costs", [])) * 2 + len(ev.get("sigs", [])) + (3 if ev.get("runs") else 0) \
        + (2 if ev.get("role") == "user" else 0)   # the user's own cost statements are gold


def index_cmd(tdir=None, apply=False, rebuild=False):
    index, scanned = build_index(tdir, rebuild=rebuild)
    events = list(_all_events(index))
    mentioned = set(r for ev in events for r in ev.get("runs", []))
    print(f"mine-conv index — {len(index)} transcripts ({scanned} (re)scanned, rest cached)")
    print(f"  {len(events):,} high-signal events; {len(mentioned)} of our runs discussed")
    top = sorted(events, key=_score, reverse=True)[:8]
    for ev in top:
        tag = f"runs={len(ev['runs'])} " if ev.get("runs") else ""
        print(f"    [{ev.get('role','?'):<9}] {tag}{ev['text'][:110]}")
    if not apply:
        print("  report-only. --apply writes conversation_event nodes + comments_on edges (no spend).")
        return dict(events=len(events), discussed=len(mentioned))
    added = 0
    with learn._lock:
        learn._db().execute("DELETE FROM graph_edges WHERE rel='comments_on'")
        learn._db().execute("DELETE FROM graph_nodes WHERE type='conversation_event'")
        learn._db().commit()
    for ev in sorted(events, key=_score, reverse=True)[:200]:   # cap node blast radius
        nid = learn.add_node("conversation_event", ev["text"][:80],
                             attrs={"role": ev.get("role"), "costs": ev.get("costs"), "sigs": ev.get("sigs")},
                             ts=ev.get("ts"))
        for rid in ev.get("runs", []):
            learn.add_edge(nid, rid, "comments_on")
        added += 1
    print(f"  applied: {added} conversation_event nodes (+ comments_on edges to discussed runs).")
    return dict(events=len(events), discussed=len(mentioned), nodes=added)


# ─────────────────────────── synth (caged LLM) ───────────────────────────
_SYS = ("You mine a software team's chat for durable COST lessons about LLM usage. Given dated decision "
        "snippets, output STRICT JSON and nothing else: a list of AT MOST {{k}} objects "
        '{"intent": str|null, "lesson": str, "confidence": 0..1, "evidence": str}. A lesson is a reusable '
        "rule the team learned (e.g. packing, batch vs realtime, model choice, never cancel-to-save, "
        "price-basis errors). Quote the snippet that supports it in evidence. Keep lessons under 220 chars. "
        "Higher confidence when a snippet states an outcome/number; lower when speculative. No fences.")


def _dedup_top(events, k):
    seen, picked = set(), []
    for ev in sorted(events, key=_score, reverse=True):
        norm = re.sub(r"\s+", " ", ev["text"].lower())[:60]
        if norm in seen:
            continue
        seen.add(norm)
        picked.append(ev)
        if len(picked) >= k:
            break
    return picked


def synth(tdir=None, run=False, limit=40):
    from .submit import _count_tokens
    model = config.advisor_model()
    index, _ = build_index(tdir)
    events = _dedup_top(list(_all_events(index)), limit)
    print(f"mine-conv synth — reasoner = {model} (realtime), caged by intent spendguard:*")
    if not events:
        print("  no decision snippets indexed. Run `spendguard mine-conv index` first. 0 spend.")
        return dict(requests=0, cost=0.0)
    body = "\n".join(f"- ({ev.get('ts','?')[:10]} {ev.get('role','?')}) {ev['text']}" for ev in events)
    sys = _SYS.replace("{{k}}", str(min(10, max(4, limit // 4))))   # .replace, not .format — _SYS has literal JSON braces
    prompt = f"Decision snippets ({len(events)}):\n{body}"
    in_tok = _count_tokens(sys + prompt, model)
    out_tok = 1500
    from . import pricing
    cost = pricing.realtime_cost(model, in_tok, out_tok)
    print(f"  {len(events)} snippets fed.  ESTIMATE (zero paid calls):")
    print(f"    realtime {model} 1 call · in~{in_tok:,} out≤{out_tok:,} -> ~${cost:.4f}")
    from . import budget
    print(f"  meta budget: ${config.meta_cap():.2f}/day · spent today ${budget.meta_spent_today():.4f}")
    if not run:
        from . import ui; ui.estimate_only(action="synthesize from the conversations", cost=cost)
        return dict(requests=1, in_tok=in_tok, out_tok=out_tok, cost=cost, model=model)

    from . import adapters
    with calls.context(intent="spendguard:conv-synth"):
        r = adapters.call(model, prompt, max_tokens=out_tok, system=sys)
    if r["error"]:
        print(f"  ERROR: {r['error']}")
        return dict(error=r["error"])
    added = _persist_insights_conv(r["text"])
    print(f"  synthesized {added} conversation-sourced insight(s). Cost ${r['cost']:.4f}.")
    return dict(insights=added, cost=r["cost"], model=model)


def _persist_insights_conv(text):
    from .advisor import _parse_insights
    data = _parse_insights(text)
    if data is None:
        learn.add_insight(None, text.strip()[:500], source="conversation", confidence=0.4)
        return 1
    added = 0
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or not it.get("lesson"):
            continue
        iid = learn.add_insight(it.get("intent"), str(it["lesson"])[:500],
                                evidence=str(it.get("evidence", ""))[:500], source="conversation",
                                confidence=float(it.get("confidence", 0.6)))
        learn.add_node("insight", str(it["lesson"])[:80],
                       attrs={"confidence": it.get("confidence"), "source": "conversation"}, id=iid)
        added += 1
    return added


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard mine-conv")
    ap.add_argument("op", choices=["index", "synth"])
    ap.add_argument("--transcripts", help="transcript file or dir (default: ~/.claude/projects)")
    ap.add_argument("--apply", action="store_true", help="(index) write conversation_event nodes + edges")
    ap.add_argument("--rebuild", action="store_true", help="(index) ignore cache, full re-scan")
    ap.add_argument("--limit", type=int, default=40, help="(synth) max snippets to feed the reasoner")
    ap.add_argument("--run", action="store_true", help="(synth) actually spend (default: estimate). Capped by caps.meta.")
    a = ap.parse_args(argv)
    if a.op == "index":
        index_cmd(a.transcripts, apply=a.apply, rebuild=a.rebuild)
    else:
        synth(a.transcripts, run=a.run, limit=a.limit)
    return 0
