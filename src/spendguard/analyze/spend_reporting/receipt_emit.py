"""Side-effecting receipt output orchestration."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

from ... import config
from .receipt_render import _PREFIX, render_flow, render_line
from .receipt_tally import level, tally


def _sinks():
    """WHERE auto-emitted receipts go: env SPENDGUARD_RECEIPTS_SINK → config receipts.sinks → 'stderr'.
    Comma-separated; each is 'stderr' | 'stdout' | 'file:<path>'. The file sink is how a host with no in-chat hook
    (Codex, an editor, a tmux/menubar widget) can display the tally — point it at a log and tail/render that."""
    v = os.getenv("SPENDGUARD_RECEIPTS_SINK") or config._cfg_get("receipts", "sinks", "stderr") or "stderr"
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _out(text: str) -> None:
    if not text:
        return
    for sink in _sinks():
        try:
            if sink == "stderr":
                print(text, file=sys.stderr, flush=True)
            elif sink == "stdout":
                print(text, file=sys.stdout, flush=True)
            elif sink.startswith("file:"):
                p = pathlib.Path(os.path.expanduser(sink[5:]))
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a") as f:
                    f.write(text + "\n")
        except Exception:
            pass


def _emit_contract_line(intent, t):
    """One line, on stderr, when a flow's realtime outputs did not match the declared shape."""
    import sys
    bits = f"{t.get('parsed', 0)}/{t.get('n', 0)} parsed"
    if t.get("salvaged"):
        bits += f" · {t['salvaged']} salvaged (fence/preamble)"
    if t.get("failed"):
        bits += f" · {t['failed']} FAILED — first: {t.get('first') or '?'}"
    print(f"{_PREFIX}contract [{intent or 'flow'}]: {bits}", file=sys.stderr)


def emit_flow(intent, chain, start, contract=None) -> None:
    """Emit a per-flow receipt at a flow boundary (_call_corpus_accessors.resolve_calls_context exit). `start` is the (rowid, usd) snapshot taken
    at flow enter. Guarded + level-gated; only acts at 'flow'/'verbose'. Silent if the flow neither called nor spent.
    NEVER raises into the caller's code path.

    `contract` is the flow's realtime output tally (see _call_corpus_accessors.resolve_calls_context). A flow whose outputs stopped matching
    their declared shape is reported UNCONDITIONALLY — a spend receipt that stays quiet while every response is
    unparseable is the same DONE-not-CORRECT failure the batch gate was fixed for."""
    try:
        try:                                      # a flow may have just delegated to a ledger-valued lane; freshen it
            from ... import lane_value
            lane_value.refresh_lane_value_if_stale()
        except Exception:
            pass
        lvl = level()
        if contract and contract.get("failed"):        # a broken contract is news at ANY verbosity
            _emit_contract_line(intent, contract)
        if lvl not in ("flow", "verbose"):
            return
        start_rid, start_usd = (start or (0, 0.0))
        agg = None
        try:
            from spendguard.store.call_corpus import call_corpus_accessors as _call_corpus_accessors
            agg = _call_corpus_accessors.resolve_calls_flow_agg(start_rid, chain)
        except Exception:
            pass
        actual = None
        try:
            import spendguard.manage.admission.admission_accessors as budget
            actual = max(0.0, budget.resolve_spent_all_time() - (start_usd or 0.0))   # memoized baseline (matches the flow-enter snapshot)
        except Exception:
            pass
        n = (agg or {}).get("n") or 0
        # if the flow did nothing measurable, stay quiet (don't spam empty receipts)
        if not n and not actual and not (agg or {}).get("incomplete_count"):
            return
        if contract and contract.get("n") and not contract.get("failed"):
            _emit_contract_line(intent, contract)     # clean runs say so: "397/397 parsed" is the evidence
        flow = {"intent": intent, "n": n,
                "in_tok": (agg or {}).get("in_tok"),
                "out_tok": (agg or {}).get("out_tok"),
                # a MEASURED $0 (a subscription/plan-served flow — $0 billed) is the truth, not "unmeasured": only
                # fall back to the estimated cost when actual could not be measured at all (None), never when it is 0.
                "actual": actual if actual is not None else (agg or {}).get("cost"),
                "est": (agg or {}).get("est"),
                "incomplete_count": (agg or {}).get("incomplete_count"),
                "input_cost_floor": (agg or {}).get("input_cost_floor"),
                "caller": (agg or {}).get("caller")}
        proj = None                                  # scope the tally to THIS flow's repo (the relevant scope)
        try:
            import spendguard.manage.admission.admission_accessors as budget
            proj = budget.resolve__project() or None
        except Exception:
            pass
        _out(render_flow(flow, lvl, tally(project=proj)))
    except Exception:
        pass


_STATUSLINE_CACHE_S = 10        # the status line renders often; recompute the (ledger-reading) tally at most once per
#                                 this many seconds and serve a cached line otherwise — bounds the per-render WORK.


def _statusline_cache_path():
    from ... import config
    return config.HOME / "statusline_tally.cache"


def _write_statusline_cache(line):
    """Persist the rendered tally line for the FAST status line (spendguard/statusline.py) to read with no import.
    The per-turn --stop-hook writes it, so a status line that repaints many times between turns is a cheap file read."""
    from ...ledger_writer import ledger_bulk_scope_matches
    if ledger_bulk_scope_matches(config.db_path()):
        return
    try:
        _statusline_cache_path().write_text(line)
    except Exception:
        pass


def _cached_tally_line():
    """The rendered global tally LINE, recomputed at most once per _STATUSLINE_CACHE_S and cached to disk; otherwise
    the cached line is returned. So even the heavy `spendguard receipt --statusline` path does the tally work (ledger
    read + lane-value refresh) at most once per window. The FAST path (the standalone statusline.py the installer
    writes) skips this whole process — it reads the cache directly with no spendguard import."""
    from ...ledger_writer import ledger_bulk_scope_matches
    if ledger_bulk_scope_matches(config.db_path()):
        # Show live pending state only to its scope; never reuse or publish a
        # cross-scope rendered tally while its inputs are uncommitted.
        return render_line(tally())
    cache = _statusline_cache_path()
    try:
        if (time.time() - cache.stat().st_mtime) < float(_STATUSLINE_CACHE_S):
            return cache.read_text()
    except Exception:
        pass
    try:                                          # cache MISS → refresh the ledger-valued lanes, then recompute + store
        from ... import lane_value
        lane_value.refresh_lane_value_if_stale()
    except Exception:
        pass
    line = render_line(tally())
    _write_statusline_cache(line)
    return line


def _compaction_hint():
    """{threshold_tokens, k} for the status-line compaction nudge: the config re-read-context threshold + the MEASURED
    compaction ratio (a ledger query via claudecode). Account-level, not per-session. The --stop-hook calls this every
    turn, so it SELF-CACHES for an hour (k barely moves). Best-effort → safe defaults; never raises."""
    from ... import config
    from ...ledger_writer import ledger_bulk_scope_matches
    pending_compaction_scope = ledger_bulk_scope_matches(config.db_path())
    hp = config.HOME / "compaction_hint.json"
    try:
        if not pending_compaction_scope and (time.time() - hp.stat().st_mtime) < 3600:
            return json.loads(hp.read_text())
    except Exception:
        pass
    try:
        thr = int(config._cfg_get("advisor", "compaction_context_tokens", 100000) or 100000)
    except Exception:
        thr = 100000
    k = None
    try:
        from ... import compaction
        kk, _kn = compaction.measured_explicit_compaction_ratio()             # REAL per-event k× (pre/post) once compactions are recorded
        if not kk:
            from ... import claudecode
            kk, _kn = claudecode.measured_compaction_ratio()   # else the ledger context-drop heuristic
        k = round(float(kk), 1) if kk else None
    except Exception:
        k = None
    hint = {"threshold_tokens": thr, "k": k}
    if pending_compaction_scope:
        return hint
    try:                                                  # through update_json (atomic + `~` backup + refuses to
        config.update_json(hp, lambda _d: hint, reason="compaction-hint cache", keep_backups=0)
    except Exception:
        pass
    return hint


# Re-nudge only after the live context grows this fraction beyond the last nudge: the Stop-hook interrupt fires
# ONE notice per threshold crossing, then stays silent until the context moves materially OR the session is
# compacted (ctx falls back under the threshold, clearing the mark so the next crossing nudges once). A nudge that
# repeats every turn with no new information is an interrupt, not a receipt.
_RENUDGE_GROWTH_FRAC = 0.5


def _nudge_state_path(home):
    return os.path.join(home, "compaction_nudge_state.json")


def _nudge_should_fire(home, session_key, ctx, threshold):
    """Debounce the Stop-hook compaction nudge. Returns True at most ONCE per threshold crossing: on the first
    crossing, and again only after `ctx` grows _RENUDGE_GROWTH_FRAC beyond the last nudge. Under the threshold it
    clears the session's mark (a compaction / fresh start resets it) and returns False. State writes go through
    config.update_json (atomic, backed up, refuses a corrupt file) — never a hand-rolled whole-file write.
    Hook-safe: any read/write failure fails to False (stay silent) rather than raising or looping."""
    from ... import config
    path = _nudge_state_path(home)
    try:
        state = {}
        try:
            with open(path) as fh:                 # a READ, not a write — safe to hand-roll
                state = json.load(fh)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        last = state.get(session_key)
        if ctx < threshold:                        # user acted / fresh / compacted → clear so the next crossing nudges
            if last is not None:
                config.update_json(path, lambda d: {k: v for k, v in dict(d or {}).items() if k != session_key},
                                   reason="compaction-nudge-reset", keep_backups=0)
            return False
        if last is not None and ctx < int(last) * (1.0 + _RENUDGE_GROWTH_FRAC):
            return False                           # over threshold, but not materially larger than the last notice
        config.update_json(path, lambda d: {**dict(d or {}), session_key: ctx},
                           reason="compaction-nudge-fire", keep_backups=0)
        return True
    except Exception:
        return False


def _compaction_nudge(info, home, once=False):
    """The per-SESSION compaction nudge string (or '') — tails the CURRENT session's transcript for the re-read context
    of its last turn, and if that is at/above the cached threshold, returns a '/compact' suggestion carrying the
    measured k×. With `once=True` (the Stop-hook INTERRUPT) it fires at most once per threshold crossing (see
    _nudge_should_fire); the passive status line leaves it False and shows the notice whenever over threshold. The k×
    is shown ONLY when it rounds to a real saving (>=2x) — '~1x' is identical cost and claims a saving that is not
    there, so it is suppressed. Cheap (tail only, ~64KB). Mirrored inline in the standalone statusline. Never raises."""
    try:
        tp = info.get("transcript_path") or info.get("transcriptPath")
        if not tp or not os.path.exists(tp):
            return ""
        with open(tp, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 65536))
            tail = f.read().decode("utf-8", "ignore")
        ctx, model = 0, None
        for ln in reversed(tail.splitlines()):
            if '"usage"' not in ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            u = ((o.get("message") or {}).get("usage")) or {}
            if u:
                ctx = (int(u.get("input_tokens", 0) or 0) + int(u.get("cache_read_input_tokens", 0) or 0)
                       + int(u.get("cache_creation_input_tokens", 0) or 0))
                model = (o.get("message") or {}).get("model")      # for the $/turn re-read figure below
                break
        if not ctx:
            return ""
        hint = {}
        try:
            with open(os.path.join(home, "compaction_hint.json")) as hf:
                hint = json.load(hf)
        except Exception:
            hint = {}
        threshold = int(hint.get("threshold_tokens") or 100000)
        session_key = str(info.get("session_id") or info.get("sessionId") or os.path.basename(tp))
        if once:
            if not _nudge_should_fire(home, session_key, ctx, threshold):
                return ""
        elif ctx < threshold:
            return ""
        # The terse per-turn nudge carries NO savings multiplier: "~1x cheaper" claimed a saving that was not there,
        # and whether a given ratio is a saving worth showing is a judgement a per-turn hook must not hard-code. The
        # token count is the signal; the measured k× lives in the guided /compact detail, not this one-liner.
        # Add the $/turn re-read cost (est-value on the plan) so a resident-context bloat is visible as DOLLARS, not
        # just tokens — the "a $0.50/turn session is impossible to miss" surface. ctx is already the resident size;
        # × the model's cache-read rate is the recurring re-read cost. The per-SOURCE breakdown (which tool output is
        # the lever) is a $0 drill-down on `spendguard claude-code context --conv <id>`.
        dollar = ""
        try:
            from ... import claudecode
            _rate = claudecode._cache_read_rate(model) if model else None
            if _rate is not None:
                dollar = " (~$%.2f/turn re-read)" % (ctx * _rate)
        except Exception:
            dollar = ""
        return "⚠ %dK tok/turn%s · /compact (guided)" % (ctx // 1000, dollar)
    except Exception:
        return ""
