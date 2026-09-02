"""Compaction lifecycle — spendguard's COST view of Claude Code context compaction.

Two hooks feed this (wired by receipt._install_claude_code):
  • PreCompact       → record_precompact(info): records the compaction EVENT (its trigger + the context it compacted
                       at) AND returns the preservation GUIDANCE to inject (so both auto and manual compaction keep
                       the task goal / decisions+rationale / paths+ids — the review measured auto loss at 19%,
                       manual at 0%). The guidance text is CONFIG-PATHED (advisor.precompact_guidance_file →
                       the review's generated precompact_guidance.txt), so re-running the review updates it; a small
                       bundled fallback is used only if that file is absent. Nothing hardcoded.
  • SessionStart(compact) → record_sessionstart(info): records the POST-compaction context on the same event, so we
                       measure the REAL per-event k× = pre_context / post_context (grounding the status-line nudge in
                       measured events instead of the ledger-drop heuristic).

Everything here is best-effort and NEVER raises — a hook must not break the session. $0, pure parse + tiny state.
"""
import datetime
import json
import os

from . import config

_STATE = "compaction_events"
_MAX_EVENTS = 500

_FALLBACK_GUIDANCE = (
    "Before summarizing, preserve VERBATIM in a pinned section: (1) the current task goal/sequence with "
    "[DONE]/[NEXT]/[TODO] markers; (2) every user decision, redirect, or constraint AND its rationale (not just the "
    "choice); (3) all file paths, line numbers, ids, ports, numbers exactly as written; (4) the immediate next "
    "action to run; (5) any open question awaiting a reply. Do not paraphrase decisions or drop rationale.")


def _iso_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _load_events():
    try:
        return list(config.load_state(_STATE, {"events": []}).get("events") or [])
    except Exception:
        return []


def _save_events(events):
    try:
        config.save_state(_STATE, {"events": events[-_MAX_EVENTS:]}, loud=False)
    except Exception:
        pass


def _guidance_file():
    """The tailored guidance path — config advisor.precompact_guidance_file, else the review's generated deliverable
    if present (a discovery default, not a hardcoded answer), else None."""
    p = config._cfg_get("advisor", "precompact_guidance_file", None)
    if p:
        p = os.path.expanduser(str(p))
        if os.path.exists(p):
            return p
    cand = os.path.expanduser("~/Documents/claude/compaction_review/precompact_guidance.txt")
    return cand if os.path.exists(cand) else None


def guidance_text():
    """The preservation directive to inject at PreCompact — from the config-pathed file, else the bundled fallback."""
    f = _guidance_file()
    if f:
        try:
            t = open(f).read().strip()
            if t:
                return t
        except Exception:
            pass
    return _FALLBACK_GUIDANCE


_FALLBACK_SNIPPET = (
    "/compact Preserve verbatim: the current task goal + the immediate next action; every decision I made and WHY "
    "(the rationale, not just the choice); all file paths, ids, numbers, ports. Keep open questions awaiting my "
    "reply. Collapse exploration and tool dumps to their conclusions only.")


def _snippet_file():
    p = config._cfg_get("advisor", "compact_snippet_file", None)
    if p:
        p = os.path.expanduser(str(p))
        if os.path.exists(p):
            return p
    cand = os.path.expanduser("~/Documents/claude/compaction_review/compact_snippet.txt")
    return cand if os.path.exists(cand) else None


def compact_snippet():
    """The ready-to-paste EFFECTIVE '/compact <instructions>' command (preserve goal/decisions/paths, collapse
    exploration) — from the config-pathed file (advisor.compact_snippet_file → the review's compact_snippet.txt),
    else a bundled fallback. This is what makes a MANUAL /compact lossless when typed explicitly; the PreCompact hook
    injects the SAME intent automatically on every compaction, so even a bare /compact is guided once the hook is on."""
    f = _snippet_file()
    if f:
        try:
            t = open(f).read().strip()
            if t:
                return t
        except Exception:
            pass
    return _FALLBACK_SNIPPET


def _recent_convo(transcript_path, max_chars=6000):
    """A compact digest of the RECENT conversation for the advisor — the last user/assistant messages, tailed from
    the transcript and bounded so the advisory LLM call stays cheap. '' when unreadable."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 400000))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    out = []
    for ln in tail.splitlines():
        if '"role"' not in ln and '"type"' not in ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        m = o.get("message") or {}
        role = m.get("role") or o.get("type")
        if role not in ("user", "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, list):
            txt = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        else:
            txt = c if isinstance(c, str) else ""
        txt = (txt or "").strip().replace("\n", " ")
        if txt:
            out.append(f"[{role}] {txt[:400]}")
    return "\n".join(out[-24:])[-max_chars:]


def _parse_advise(text):
    """Pull the advisor's JSON verdict out of its reply — PARSING a known shape, not deciding (the decision is the
    LLM's). {} if none."""
    import re
    try:
        m = re.search(r"\{.*\}", text or "", re.S)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


_ADVISE_SYS = (
    "You decide whether a long AI-assisted coding conversation should be COMPACTED right now, and if so HOW to compact "
    "it without losing what matters. Compaction summarizes the conversation to reclaim context; done mid-implementation "
    "or carelessly it drops decisions and the task goal (measured: ~19% of auto-compactions lose data). Look at what "
    "the conversation is actually DOING. Output STRICT JSON only, no prose:\n"
    '{"should_compact": true|false,\n'
    ' "reason": "<one sentence: are we at a natural seam (a sub-task just finished) or mid-implementation, and why '
    'now or not>",\n'
    ' "compact_command": "/compact <instructions TAILORED to THIS conversation: name its actual current task goal + '
    'status, every decision made and WHY, the specific file paths/ids/numbers in play, the immediate next action, and '
    'any open question; tell the summarizer to collapse exploration and tool output to conclusions only>"}')


def agentic_advise(transcript_path, run=False, model=None):
    """AGENTIC compaction advisor: read what the conversation is DOING and decide (a) whether NOW is a good moment to
    compact (a natural seam vs mid-implementation) and (b) a compact_command TAILORED to what THIS conversation must
    preserve. The judgement is the LLM's (not a threshold). Runs UNDER spendguard's own gate, caged (intent
    spendguard:compact-advise), estimate-first. Returns the verdict dict on run; prints the estimate + returns None
    otherwise. Never raises out to a hook."""
    from . import config, adapters, calls, pricing, ui, claudecode
    convo = _recent_convo(transcript_path)
    if not convo:
        print("compact --tailor: no readable recent conversation at that transcript.")
        return None
    model = model or config._cfg_get("advisor", "judge_model", "claude-haiku-4-5")
    OUT = 700
    est = pricing.realtime_cost(model, claudecode._toklen(_ADVISE_SYS + convo), OUT) or 0.0
    # the ECONOMICS — computable WITHOUT the call: a bloated session re-reads $/turn, and compacting cuts that ~k×.
    # Spending cents (this advice) to save dollars (the re-read) is always a good trade — so make it explicit, up front.
    econ = ""
    try:
        _ctx, cr, mdl = _tail_context(transcript_path)
        p = pricing.price(mdl or model) or {}
        rate = (float(p["cached_in"]) / 1e6) if p.get("cached_in") is not None else None
        if rate and cr:
            reread = cr * rate
            k = measured_k()[0] or 11.0
            econ = (f"this session re-reads ~${reread:.4f}/turn · compacting (~{k:.0f}×) saves "
                    f"~${reread * (1 - 1 / k):.4f}/turn — cents to save dollars.")
    except Exception:
        econ = ""
    if not run:
        if econ:
            print("  " + econ)
        ui.estimate_only(action="agentically decide whether/how to compact THIS conversation", cost=est)
        return None
    with calls.context(intent="spendguard:compact-advise"):     # caged → meta budget, gated like every LLM call
        r = adapters.call(model, convo, max_tokens=OUT, system=_ADVISE_SYS)
    if r.get("error"):
        print("compact --tailor error:", r["error"])
        return None
    data = _parse_advise(r.get("text", ""))
    print(f"agentic compaction advice ({model}, caged):")
    print(f"  compact now? {'YES' if data.get('should_compact') else 'NO'} — {data.get('reason', '')}")
    print("  → " + (data.get("compact_command") or compact_snippet()))
    if econ:
        print(f"  economics: advice ${(r.get('cost') or 0):.4f} spent · " + econ)
    return data


def _tail_context(transcript_path):
    """(context_tokens, cache_read_tokens, model) of the LAST usage-bearing turn in the transcript, or (0, 0, None).
    Reads only the last ~64KB — cheap enough for a hook. context = input + cache_read + cache_write."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 65536))
            tail = f.read().decode("utf-8", "ignore")
        for ln in reversed(tail.splitlines()):
            if '"usage"' not in ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            m = o.get("message") or {}
            u = m.get("usage") or {}
            if u:
                cr = int(u.get("cache_read_input_tokens", 0) or 0)
                ctx = int(u.get("input_tokens", 0) or 0) + cr + int(u.get("cache_creation_input_tokens", 0) or 0)
                return ctx, cr, m.get("model")
    except Exception:
        pass
    return 0, 0, None


def record_precompact(info):
    """Record the compaction event (trigger + pre-context) and RETURN the preservation guidance. Never raises."""
    try:
        tp = info.get("transcript_path") or info.get("transcriptPath") or ""
        ctx, cr, model = _tail_context(tp) if tp and os.path.exists(tp) else (0, 0, None)
        evs = _load_events()
        evs.append({"ts": _iso_now(), "session": info.get("session_id") or info.get("sessionId") or "",
                    "trigger": info.get("trigger") or info.get("compaction_type") or "unknown", "model": model,
                    "pre_context": ctx, "pre_cache_read": cr, "post_context": None, "k": None})
        _save_events(evs)
    except Exception:
        pass
    return guidance_text()


def record_sessionstart(info):
    """If this SessionStart is a post-COMPACT restart, fill the last matching event's post_context + k× (pre/post)."""
    try:
        if (info.get("source") or "") != "compact":
            return
        tp = info.get("transcript_path") or info.get("transcriptPath") or ""
        post, _cr, _m = _tail_context(tp) if tp and os.path.exists(tp) else (0, 0, None)
        if post <= 0:
            return
        sid = info.get("session_id") or info.get("sessionId") or ""
        evs = _load_events()
        for ev in reversed(evs):                            # the most recent open event for this session
            if ev.get("session") == sid and ev.get("post_context") is None:
                ev["post_context"] = post
                if ev.get("pre_context") and post > 0:
                    ev["k"] = round(ev["pre_context"] / post, 2)
                _save_events(evs)
                return
    except Exception:
        pass


def measured_k():
    """(k, n) — the MEDIAN real compaction ratio from RECORDED events that have both pre and post (k = pre/post),
    or (None, 0) if none yet. This is the measured-from-events k× the nudge prefers over the ledger-drop heuristic."""
    ks = sorted(float(ev["k"]) for ev in _load_events() if ev.get("k"))
    if not ks:
        return None, 0
    n = len(ks)
    return (ks[n // 2] if n % 2 else (ks[n // 2 - 1] + ks[n // 2]) / 2.0), n


def event_summary():
    """Compaction stats for a report: counts by trigger + the measured k×. Read-only."""
    evs = _load_events()
    by_trigger = {}
    for ev in evs:
        t = ev.get("trigger") or "unknown"
        by_trigger[t] = by_trigger.get(t, 0) + 1
    k, n = measured_k()
    return {"events": len(evs), "by_trigger": by_trigger, "measured_k": k, "k_events": n}
