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
import os, re, json, glob, hashlib
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


def attribute_usage(since="2026-06-01", tdir=None):
    """Proper accounting: match actual provider USAGE (per-batch cost) to PROJECTS via the AGENTIC per-subconversation
    attribution — batch id → the segment that ran it → its LLM-classified project (cwd as a prior the LLM confirms).
    NEVER a regex keyword guess. Reads the cached classification (run `spendguard accounting --run` to refresh it).
    Returns {total, batches, linked, by_project:{proj:$}}. Free (reads the cache)."""
    from . import backfill
    costs = {}
    for _prov, _model, cost, _it, _ot, day, bid in (backfill._openai_rows() + backfill._anthropic_rows()):
        if (day or "") >= since:
            costs[bid] = costs.get(bid, 0.0) + cost
    bmap = batch_project_map(tdir)
    by_project = {}
    linked = 0
    for bid, c in costs.items():
        b = bmap.get(bid)
        if b:
            linked += 1
            p = b.get("project") or b.get("prior") or "linked-unclear"
        else:
            p = "no-conversation"            # batch ran outside any indexed transcript → genuinely no evidence
        by_project[p] = round(by_project.get(p, 0.0) + c, 4)
    return {"total": round(sum(costs.values()), 2), "batches": len(costs), "linked": linked, "by_project": by_project}


def attribute_cmd(argv=None):
    run = "--run" in (argv or [])
    attribute_segments(run=run)              # run=False → print the estimate (no spend); --run → classify (caged)
    r = attribute_usage()
    print(f"usage→project by AGENTIC per-subconversation attribution (MTD): ${r['total']:.2f} · "
          f"{r['batches']} batches · {r['linked']} linked")
    for p, c in sorted(r["by_project"].items(), key=lambda x: -x[1]):
        pct = (100 * c / r["total"]) if r["total"] else 0
        print(f"  {p:22} ${c:9.2f}  ({pct:.0f}%)")
    print("  (no-conversation = batch ran outside any indexed transcript; `accounting --run` refreshes the LLM pass)")
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


# ─────────────────────────── AGENTIC per-subconversation attribution ───────────────────────────
# A transcript (one Claude Code session) can span SEVERAL projects. Attribution must therefore work at the
# SUBCONVERSATION level: split the session at each user ask, and classify each segment on its own — with the
# repo/cwd as a PRIOR the LLM confirms or overrides (never a regex keyword guess). Spend (a batch id, a realtime
# span) attributes to the segment that produced it. Magnitude still comes from provider truth; the LLM only decides
# WHERE it lands. Cached per segment id, so re-runs don't re-pay.

def _basename(cwd):
    return os.path.basename(str(cwd or "").rstrip("/"))


def _seg_id(sid, ts, prompt):
    return hashlib.sha1("|".join([sid or "", ts or "", (prompt or "")[:120]]).encode()).hexdigest()[:16]


def _is_user_ask(obj, txt):
    """A genuine user ask (starts a new subconversation) — not a tool_result echo, system line, or interrupt."""
    t = obj.get("type") or (obj.get("message") or {}).get("role")
    if t != "user":
        return ""
    s = (txt or "").strip()
    if not s or s.startswith("<") or "tool_result" in s[:40] or "[Request interrupted" in s:
        return ""
    return s


def segment_records(records, sid="", cwd_default=""):
    """PURE (no file IO, offline-testable): split ONE transcript's records into subconversation segments. A segment
    opens at each user ask and runs until the next; it carries the ask (prompt), the cwd PRIOR, the batch ids that
    appear in its span, and the time. Returns a list of segment dicts."""
    segs, cur, cwd = [], None, cwd_default

    def _flush():
        if cur and (cur["batch_ids"] or cur["prompt"]):
            segs.append(cur)

    for obj in records:
        if not isinstance(obj, dict):
            continue
        if obj.get("cwd"):
            cwd = obj.get("cwd")
        txt = _text_of(obj)
        ts = obj.get("timestamp") or ""
        ask = _is_user_ask(obj, txt)
        if ask:
            _flush()
            cur = {"sid": sid, "cwd": cwd, "project_prior": _basename(cwd), "prompt": ask[:200],
                   "batch_ids": set(), "ts": ts, "day": (ts or "")[:10]}
        if cur is None:                                   # spend/text before any user ask → a headless segment
            cur = {"sid": sid, "cwd": cwd, "project_prior": _basename(cwd), "prompt": "",
                   "batch_ids": set(), "ts": ts, "day": (ts or "")[:10]}
        if not cur["cwd"] and cwd:
            cur["cwd"] = cwd; cur["project_prior"] = _basename(cwd)
        for bid in _BID.findall(txt or ""):
            cur["batch_ids"].add(bid)
    _flush()
    for s in segs:
        s["seg_id"] = _seg_id(s["sid"], s["ts"], s["prompt"])
        s["batch_ids"] = sorted(s["batch_ids"])
    return segs


def segments(tdir=None):
    """All subconversation segments across every transcript (file IO shell over segment_records). The session cwd is
    read from the records themselves (reliable), not the path slug (which is lossy for hyphenated dir names)."""
    tdir = tdir or _DEFAULT_TDIR
    files = sorted(glob.glob(os.path.join(tdir, "**", "*.jsonl"), recursive=True)) if os.path.isdir(tdir) else [tdir]
    out = []
    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        try:
            recs = [json.loads(ln) for ln in open(path, errors="ignore").read().splitlines() if ln.strip()]
        except Exception:
            continue
        out += segment_records([r for r in recs if isinstance(r, dict)], sid=sid)
    return out


# ── persistence: agentic decisions live in the BASE SQLITE (learn._db / config.db_path()) so we NEVER redo / re-pay
#    for them. source ∈ llm | human (priors are free + recomputed, never stored). 'human' is final and beats the llm. ──
_SEG_TAU = 70   # below this confidence (or a non-llm/human source) → the convergence loop re-runs the segment


def _content_hash(seg):
    return hashlib.sha1("|".join([seg.get("cwd") or "", (seg.get("prompt") or "")[:200],
                                  ",".join(seg.get("batch_ids") or [])]).encode()).hexdigest()[:16]


def _seg_get_all():
    """{seg_id: {project,org,team,confidence,source,model}} — every recorded agentic/human attribution decision."""
    from . import learn
    out = {}
    try:
        with learn._lock:
            for r in learn._db().execute(
                    "SELECT seg_id, project, org, team, confidence, source, model FROM seg_attribution"):
                out[r[0]] = {"project": r[1] or "", "org": r[2] or "", "team": r[3] or "",
                             "confidence": int(r[4] or 0), "source": r[5] or "", "model": r[6] or ""}
    except Exception:
        pass
    return out


def _seg_put_cls(seg_id, cls, source="llm", model="", seg=None):
    """Record ONE attribution decision in the base sqlite: conversation id (sid) · subconversation (seg_id+prompt) ·
    the LLM's full DETERMINATION (cls as JSON — what it classified/extracted) · model · ts. So we never re-pay AND
    can re-derive / selectively re-run when the model or prompt changes. An llm write NEVER overwrites a human one."""
    from . import learn
    with learn._lock:
        db = learn._db()
        cur = db.execute("SELECT source FROM seg_attribution WHERE seg_id=?", (seg_id,)).fetchone()
        if cur and cur[0] == "human" and source != "human":
            return                                         # human is final
        seg = seg or {}
        db.execute(
            "INSERT OR REPLACE INTO seg_attribution"
            "(seg_id,content_hash,sid,cwd,prompt,project,org,team,confidence,source,model,ts,batch_ids,determination) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seg_id, _content_hash(seg), seg.get("sid", ""), seg.get("cwd", ""), (seg.get("prompt") or "")[:300],
             (cls.get("project") or ""), (cls.get("org") or ""), (cls.get("team") or ""),
             int(cls.get("confidence") or 0), source, model, learn._now(),
             json.dumps(seg.get("batch_ids") or []), json.dumps(cls)))
        db.commit()


def seg_record(seg_id):
    """Full recorded row for ONE segment incl. the stored DETERMINATION + model + ts — for audit / re-derive /
    deciding whether to re-run. Returns None if not recorded."""
    from . import learn
    with learn._lock:
        r = learn._db().execute(
            "SELECT seg_id,sid,cwd,prompt,project,org,team,confidence,source,model,ts,determination "
            "FROM seg_attribution WHERE seg_id=?", (seg_id,)).fetchone()
    if not r:
        return None
    keys = ("seg_id", "sid", "cwd", "prompt", "project", "org", "team", "confidence", "source", "model", "ts", "determination")
    out = dict(zip(keys, r))
    try:
        out["determination"] = json.loads(out["determination"]) if out["determination"] else None
    except Exception:
        pass
    return out


def _load_seg_cache():
    """Back-compat alias: the recorded decisions keyed by seg_id (now sqlite-backed, was JSON)."""
    return _seg_get_all()


def _save_seg_cache(d):
    """Back-compat bulk writer (seg_id -> cls) → sqlite. Entries default to source='llm' unless they carry 'source'."""
    for seg_id, cls in (d or {}).items():
        _seg_put_cls(seg_id, cls, source=(cls.get("source") or "llm"))


def attribute_segments(tdir=None, run=False, recls=False, spend_only=True, tau=_SEG_TAU):
    """AGENTIC: classify each subconversation into org→team×project via the shared LLM classifier (cwd as a PRIOR it
    confirms/overrides), and RECORD each decision in the base sqlite so we never re-pay. Estimate-first (run=False →
    estimate only). Scopes to SPEND-BEARING segments by default (the core-mission $ attribution), and re-classifies
    only segments that are absent / not-llm-or-human / below confidence τ — the convergence loop's small step.
    Returns the recorded store (run=True) or an estimate summary (run=False)."""
    from . import attribution
    segs = segments(tdir)
    if spend_only:
        segs = [s for s in segs if s["batch_ids"]]         # only segments that OWN spend
    store = {} if recls else _seg_get_all()

    def _needs(s):
        if recls:
            return True
        cur = store.get(s["seg_id"])
        return (cur is None) or (cur.get("source") not in ("llm", "human")) or (cur.get("confidence", 0) < tau)

    todo = [s for s in segs if s["prompt"] and _needs(s)]
    if not todo:
        return _seg_get_all() if run else {"estimate_only": True, "segments": len(segs), "to_classify": 0}
    taxo, _ = attribution.taxonomy()
    items = [{"id": s["seg_id"], "text": f"[repo:{s['project_prior']}] {s['prompt']}"} for s in todo]
    res = attribution.classify_items(items, taxo, run)     # estimate-first lives INSIDE classify_items
    if not run:
        return {"estimate_only": True, "segments": len(segs), "to_classify": len(todo)}
    by_id = {s["seg_id"]: s for s in todo}
    model = config.advisor_model()
    for sid_, cls in res.items():
        _seg_put_cls(sid_, cls, source="llm", model=model, seg=by_id.get(sid_))
    return _seg_get_all()


def batch_project_map(tdir=None):
    """{batch_id: {org,team,project,confidence,source,prior,seg_id,evidenced}} — AGENTIC per-subconversation
    attribution of every batch to its project, via the segment that ran it + the RECORDED decision (base sqlite).
    When a segment isn't classified yet, the cwd PRIOR (the repo) is the project — NEVER a regex keyword guess,
    NEVER a blanket 'unattributed' for evidenced spend. `spendguard accounting --run` populates the agentic
    decisions (then cached forever in sqlite)."""
    store = _seg_get_all()
    out = {}
    for s in segments(tdir):
        cls = store.get(s["seg_id"])
        for bid in s["batch_ids"]:
            if bid in out:
                continue
            if cls and (cls.get("project") or cls.get("org")):
                out[bid] = {**cls, "org": _canon(cls.get("org")), "team": _canon(cls.get("team")),
                            "project": _canon(cls.get("project")), "prior": s["project_prior"],
                            "seg_id": s["seg_id"], "evidenced": True}
            else:                                          # evidenced (we KNOW the repo) but not yet LLM-classified
                out[bid] = {"org": "", "team": "", "project": (s["project_prior"] or "").lower(),
                            "confidence": 0, "source": "prior", "prior": s["project_prior"],
                            "seg_id": s["seg_id"], "evidenced": True}
    return out


def session_classification(sid):
    """The dominant {org,team,project} for a whole CONVERSATION (sid), from its classified segments. This is the
    shared primitive for attributing NON-batch units that link to a session rather than a batch-id — a vast.ai GPU
    instance launched in that session, or remote realtime (Haiku/Sonnet) run by that session's fleet. Same agentic
    classifier as batch; just rolled up to the session. Highest-confidence classified segment wins (ties → latest).
    Returns None if the session has no agentic/human classification yet."""
    from . import learn
    rows = []
    with learn._lock:
        for r in learn._db().execute(
                "SELECT project,org,team,confidence,ts FROM seg_attribution "
                "WHERE sid=? AND source IN ('llm','human') AND (project<>'' OR org<>'')", (sid,)):
            rows.append(r)
    if not rows:
        return None
    rows.sort(key=lambda r: (int(r[3] or 0), r[4] or ""), reverse=True)
    p, o, t, _c, _ts = rows[0]
    return {"project": _canon(p), "org": _canon(o), "team": _canon(t)}


def _match_segment(evidence, segs, store):
    """Find the SEGMENT that ran a spend event — MECHANICAL only (exact id/cwd match; never a meaning decision):
    explicit seg_id → the batch_id's segment → within the session, the segment whose cwd matches the run (tie-break:
    the one referencing the script, else highest-confidence-classified, else latest). Returns the segment or None."""
    sid = evidence.get("conv_id") or evidence.get("sid")
    seg_id, bid, cwd, script = evidence.get("seg_id"), evidence.get("batch_id"), evidence.get("cwd"), evidence.get("script") or ""
    if seg_id:
        for s in segs:
            if s["seg_id"] == seg_id:
                return s
    if bid:
        for s in segs:
            if bid in (s.get("batch_ids") or []):
                return s
    if sid:
        insess = [s for s in segs if s.get("sid") == sid]
        cand = insess
        if cwd:
            cm = [s for s in insess if s.get("cwd") and
                  (s["cwd"] == cwd or cwd.startswith(s["cwd"]) or s["cwd"].startswith(cwd))]
            cand = cm or insess
        if len(cand) == 1:
            return cand[0]
        if len(cand) > 1:
            if script:
                sm = [s for s in cand if script in (s.get("prompt") or "")]
                cand = sm or cand
            cand.sort(key=lambda s: (int((store.get(s["seg_id"]) or {}).get("confidence", 0)), s.get("ts") or ""), reverse=True)
            return cand[0]
    return None


def _canon(s):
    """Canonical taxonomy name — case-INSENSITIVE: lowercased. org/team/project are matched + returned lowercased so
    'Ensight'/'ensight' (or any case) never split in rollups. Display can prettify; identity is lowercase."""
    return (s or "").strip().lower()


def resolve(evidence, tdir=None, classify_on_miss=False):
    """UNIFIED agentic attribution for ANY spend event (batch · realtime · remote/GPU) — the ONE engine all three cost
    paths share. Map the event to the SEGMENT that ran it (mechanical: batch_id / seg_id / cwd within the session), then
    return that segment's RECORDED agentic determination (org/team/project; cwd was the PRIOR the LLM confirmed). On an
    unclassified segment: classify_on_miss=True → classify it now (LLM, recorded); else the cwd PRIOR (the repo, source
    ='prior') — NEVER a regex keyword guess, NEVER blanket 'unattributed' for evidenced spend. Generalises
    batch_project_map. `evidence` keys: batch_id | conv_id/sid + cwd + script | seg_id | host/label."""
    store = _seg_get_all()
    segs = segments(tdir)
    seg = _match_segment(evidence, segs, store)
    if seg is not None:
        cls = store.get(seg["seg_id"])
        if cls and (cls.get("project") or cls.get("org")):
            return {"org": _canon(cls.get("org")), "team": _canon(cls.get("team")), "project": _canon(cls.get("project")),
                    "confidence": int(cls.get("confidence") or 0), "source": cls.get("source") or "llm",
                    "how": "batch-map" if evidence.get("batch_id") else "segment-cwd",
                    "seg_id": seg["seg_id"], "prior": seg.get("project_prior"), "evidenced": True}
        if classify_on_miss and seg.get("prompt"):
            cls = _classify_one_segment(seg)                  # AGENTIC, recorded
            if cls and (cls.get("project") or cls.get("org")):
                return {"org": _canon(cls.get("org")), "team": _canon(cls.get("team")), "project": _canon(cls.get("project")),
                        "confidence": int(cls.get("confidence") or 0), "source": "llm", "how": "llm",
                        "seg_id": seg["seg_id"], "prior": seg.get("project_prior"), "evidenced": True}
        return {"org": "", "team": "", "project": (seg.get("project_prior") or "").lower(), "confidence": 0,
                "source": "prior", "how": "cwd-prior", "seg_id": seg["seg_id"],
                "prior": seg.get("project_prior"), "evidenced": True}
    sc = session_classification(evidence.get("conv_id") or evidence.get("sid") or "")
    if sc:
        return {**sc, "confidence": 0, "source": "session", "how": "session-fallback",
                "seg_id": None, "prior": None, "evidenced": bool(evidence.get("cwd"))}
    return {"org": "", "team": "", "project": "", "confidence": 0, "source": "none", "how": "none",
            "seg_id": None, "prior": None, "evidenced": False}


def _classify_one_segment(seg):
    """Classify ONE segment via the shared LLM classifier + record it (resolve's agentic miss path)."""
    from . import attribution
    taxo, _ = attribution.taxonomy()
    res = attribution.classify_items(
        [{"id": seg["seg_id"], "text": f"[repo:{seg.get('project_prior')}] {seg.get('prompt')}"}], taxo, run=True)
    cls = (res or {}).get(seg["seg_id"])
    if cls:
        _seg_put_cls(seg["seg_id"], cls, source="llm", model=config.advisor_model(), seg=seg)
    return cls


# COST signals only — NOT model names. Model names (haiku/opus/gpt) appear everywhere and drown the actual cost
# evidence in noise; the run-OUTPUT cost lines (per-clip $, USAGE prints, aggregate/total cost, token totals) are
# what's reconstructable. lmm's UNGATED realtime is mostly ESTIMATES in chat (rate tables, "~$X running"), which a
# forensic tool must NOT book as spend — so lmm realtime is the GATE-CAPTURED figure, not a chat reconstruction.
_RT_EVIDENCE = re.compile(
    r"(\$\s?[0-9]+\.[0-9]+\s*/\s*clip|===\s*USAGE\s*===|[0-9]+\s+in\s*/\s*[0-9]+\s+out|aggregate cost|total cost|"
    r"loop_results|calls?\s*/\s*clip|input_tokens|output_tokens|haiku|sonnet)", re.I)


def remote_llm_excerpts(tdir=None, max_sessions=None, window=600):
    """Per-session excerpts of the RECORDED remote-LLM cost evidence — per-clip $ rates, '=== USAGE ===' token
    prints, aggregate-cost lines, calls/clip — i.e. the numbers the vast.ai boxes PRINTED into the transcript while
    running Haiku/Sonnet. This is the input to the agentic remote-realtime reconstruction (the box calls never hit
    the local gate). Returns [(sid, excerpt)] for sessions that carry such evidence."""
    tdir = tdir or _DEFAULT_TDIR
    files = sorted(glob.glob(os.path.join(tdir, "**", "*.jsonl"), recursive=True)) if os.path.isdir(tdir) else [tdir]
    out = []
    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        hits = []
        try:
            for ln in open(path, errors="ignore"):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                txt = _text_of(obj)
                if txt and _RT_EVIDENCE.search(txt):
                    m = _RT_EVIDENCE.search(txt)
                    a = max(0, m.start() - 120)
                    hits.append(" ".join(txt[a:a + window].split()))
        except Exception:
            continue
        if hits:
            out.append((sid, "\n".join(hits[:14])[:4500]))
        if max_sessions and len(out) >= max_sessions:
            break
    return out


def session_chunks(tdir=None, max_chars=14000, max_sessions=None, sids=None, since=None):
    """Per-session CHUNKS of the substantive transcript (assistant text + tool OUTPUTS — where the work, the LLM-call
    invocations, and their printed usage/results live), NOT regex-pre-filtered. This is the input to the AGENTIC
    realtime reconstruction: the caged LLM READS the conversation and decides what's a realtime run + its tokens.
    Regex pre-selection (remote_llm_excerpts) is too naive — it hides the lmm classification / co-occurrence / stats
    sessions that did the BULK of the realtime work (Opus + GPT-5.5 too, not just haiku/sonnet). Mechanical content
    selection (tool outputs + text, capped) only BOUNDS the input; the model still does all the finding. Yields
    (sid, chunk)."""
    tdir = tdir or _DEFAULT_TDIR
    files = sorted(glob.glob(os.path.join(tdir, "**", "*.jsonl"), recursive=True)) if os.path.isdir(tdir) else [tdir]
    n = 0
    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        if sids is not None and sid not in sids:
            continue
        buf = []
        try:
            for ln in open(path, errors="ignore"):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if since and (obj.get("timestamp") or "")[:10] < since:
                    continue                                     # time-window: skip messages older than `since` (YYYY-MM-DD)
                # RICHER than _text_of: include tool_use COMMANDS/scripts (the python/curl that MADE the API calls —
                # the "not shown messages" with the call + per-item prompt) AND tool_result OUTPUTS (printed usage /
                # scale / results) AND assistant text (the work narrative + item counts). All three are needed for the
                # LLM to find a realtime run and multiply per-call tokens × loop scale.
                m = obj.get("message") or {}
                cc = m.get("content")
                parts = []
                if isinstance(cc, str):
                    parts.append(cc)
                elif isinstance(cc, list):
                    for b in cc:
                        if not isinstance(b, dict):
                            continue
                        ty = b.get("type")
                        if ty == "text" and b.get("text"):
                            parts.append(b["text"])
                        elif ty == "tool_use":
                            inp = b.get("input") or {}
                            parts.append((inp.get("command") or inp.get("code") or inp.get("content") or json.dumps(inp))[:6000])
                        elif ty == "tool_result":
                            tc = b.get("content")
                            parts.append(tc if isinstance(tc, str) else (" ".join(x.get("text", "") for x in tc if isinstance(x, dict)) if isinstance(tc, list) else ""))
                t = "\n".join(p for p in parts if p)
                if t and len(t) > 30:
                    buf.append(" ".join(t.split())[:6000])       # cap any single huge paste so one read can't dominate
        except Exception:
            continue
        blob = "\n".join(buf)
        if len(blob) < 200:
            continue
        for i in range(0, len(blob), max_chars):
            yield sid, blob[i:i + max_chars]
        n += 1
        if max_sessions and n >= max_sessions:
            break


_RT_USAGE = re.compile(
    r"(\d{2,8})\s*in\s*/\s*(\d{1,8})\s*out"                       # "276 in / 154 out"
    r"|input_tokens['\"]?\s*[:=]\s*(\d+)[^\n]{0,60}?output_tokens['\"]?\s*[:=]\s*(\d+)", re.I)
_RT_MODELS = (("opus", "claude-opus-4-8"), ("sonnet", "claude-sonnet-4-6"), ("haiku", "claude-haiku-4-5"),
              ("gpt-5", "gpt-5.5"), ("gpt5", "gpt-5.5"), ("gpt", "gpt-5.5"))


def realtime_token_tally(tdir=None):
    """SHIPPED (NO admin key): derive realtime LLM $ from the CONVERSATIONS — the admin-key-free counterpart to the
    timing oracle. For each transcript: extract every realtime call's printed token usage ('=== USAGE === N in / M
    out', input_tokens/output_tokens), skip batch (batch-id context), attach the model from nearby text, PRICE via
    pricing.py, and attribute the session to its org via session_classification. Returns {total, by_org, calls}.
    NOTE: only counts usage the transcript actually PRINTED — a lower bound vs the admin oracle when runs logged only
    samples; the durable fix for full coverage is inline capture (gate) going forward."""
    from . import pricing
    tdir = tdir or _DEFAULT_TDIR
    files = sorted(glob.glob(os.path.join(tdir, "**", "*.jsonl"), recursive=True)) if os.path.isdir(tdir) else [tdir]
    by_org, total, calls = {}, 0.0, 0
    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        try:
            text = open(path, errors="ignore").read()
        except Exception:
            continue
        sess = {}
        for m in _RT_USAGE.finditer(text):
            a, b = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4)))
            if a < 10 or a > 5_000_000:
                continue
            win = text[max(0, m.start() - 120):m.start() + 60].lower()
            if "msgbatch_" in win or re.search(r"batch_[0-9a-f]{6,}", win) or ".batches." in win:
                continue                                          # batch usage display → counted in the ledger
            model = next((c for k, c in _RT_MODELS if k in win), None)
            if not model:
                continue
            e = sess.setdefault(model, [0, 0]); e[0] += a; e[1] += b; calls += 1
        if not sess:
            continue
        org = (session_classification(sid) or {}).get("org") or "(unattributed)"
        for model, (i, o) in sess.items():
            try:
                c = pricing.realtime_cost(model, i, o)
            except Exception:
                c = 0.0
            total += c
            by_org[org] = round(by_org.get(org, 0.0) + c, 4)
    return {"total": round(total, 2), "by_org": {k: round(v, 2) for k, v in by_org.items()}, "calls": calls}


def instance_attributions(instances, tdir=None):
    """TIMING MATCH for GPU: join each vast.ai instance to the conversation that was running while it was up. vast.ai
    gives the authoritative COST + run window ([start_date, end_date] unix); this resolves the ATTRIBUTION by finding
    the conversations ACTIVE in that window (segments whose transcript ts falls inside it) and taking their
    session_classification. A segment that mentions the instance id or label is weighted far higher (a direct
    reference beats mere temporal overlap). ONE pass over the transcripts for the whole instance list. Returns
    {instance_id: {org,team,project,match}}. The combine-vast.ai-cost-with-LLM-attribution join."""
    import datetime
    ts_segs = []
    for s in segments(tdir):
        try:
            u = datetime.datetime.fromisoformat((s.get("ts") or "").replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        ts_segs.append((u, s["sid"], (s.get("prompt") or "").lower()))
    out = {}
    for inst in instances or []:
        iid = str(inst.get("id") or "")
        start = float(inst.get("start_date") or 0)
        end = float(inst.get("end_date") or 0) or (start + 1)
        hints = [h for h in (iid.lower(), str(inst.get("label") or "").lower()) if h]
        score = {}
        for u, sid, blob in ts_segs:
            if not (start <= u <= end):
                continue
            score[sid] = score.get(sid, 0) + (5 if any(h in blob for h in hints) else 1)
        for sid, _w in sorted(score.items(), key=lambda x: -x[1]):
            c = session_classification(sid)
            if c:
                out[iid] = {**c, "match": "timing"}
                break
    return out


def batch_contexts(tdir=None, turns=10, maxchars=3500):
    """{batch_id: {conv, before, at, after}} for every batch referenced in a transcript — ONE pass over the files
    (one pass over the transcripts, for linking the whole corpus). ~`turns` turns before/after each."""
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
