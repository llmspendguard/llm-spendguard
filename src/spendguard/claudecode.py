"""Claude Code adapter — mine ~/.claude/projects/*.jsonl into spend + work-done, INCREMENTALLY.

Claude Code meters every turn (message.usage: input/output/cache tokens + model) and records the work (tool_use:
Edit/Write/Bash/…, the cwd→project, git branch). This reads those transcripts and turns them into the same ledger
rows the rest of spendguard uses — so Claude Code spend shows on the dashboard next to API + batch + GPU, and the
work shows in the work-done view, EVEN ON A SUBSCRIPTION (CC reports tokens regardless of how it's billed).

INCREMENTAL + idempotent (this is the "track what's analyzed, update only the new part" the user asked for):
  * Per-session WATERMARK (`state.sessions[path] = {lines, mtime}`) — only NEW lines since last run are read, so a
    growing conversation is re-mined cheaply and never double-counted.
  * A local per-(project, model, day) ACCUMULATOR (`state.ledger`) — new lines add to it; we push the FULL day
    totals, so the server upsert (keyed by row uid) stays correct as sessions grow.
Cost ≈ pricing.realtime_cost(model, input+cache_create+cache_read, output, cached=cache_read). Project = cwd name.
"""
import os, json, glob, pathlib, datetime

from . import config, conv, pricing

_TOOL_FILE_KEYS = ("file_path", "path", "notebook_path")


def _projects_dir():
    return os.environ.get("SPENDGUARD_CC_DIR") or str(pathlib.Path.home() / ".claude" / "projects")


def _state_path():
    return config.state_path("claudecode")     # one naming rule for every adapter


def _load_state():
    """This adapter's persisted state, or its own empty shape. FOUR copies of this existed, each a bare
    `except: return {...}` — which is also how a TRUNCATED state file (from the non-atomic writes that used
    to sit opposite them) presented as "nothing saved yet" rather than as damage."""
    return config.load_state("claudecode", {"sessions": {}, "ledger": {}})


def _save_state(st):
    """Persist this adapter's state. THREE byte-identical copies of this existed (chat, claudecode, codex),
    each non-atomic and each swallowing its own failure — so a crash mid-write left a TRUNCATED state file
    that the loader's bare `except: return {}` then reported as "no state", and a save that never happened
    looked exactly like one that did. config.save_state writes atomically, keeps an Emacs-style `~` backup,
    quarantines a corrupt file instead of wedging on it, and says so when it cannot write."""
    return config.save_state("claudecode", st)


def load_cls():
    """Per-session classifications {sid: {org, team, project}} — public accessor for other modules (resources GPU
    alignment, the worklog) so they don't read claudecode's state file by hardcoded name."""
    return _load_state().get("cls", {})


def _project_of(cwd):
    """Bucket by the REPO (git-root basename), not the session's cwd — so subdirs (lmm/scripts/fanout) collapse to
    the repo (lmm) and match how actual-$ is tagged, instead of fragmenting est-value across dozens of cwd names."""
    return config.project_of_cwd(cwd, "claude-code")   # codex._project_of was the same but for its default


def _row_cost(model, u):
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    cc = int(u.get("cache_creation_input_tokens") or 0)
    # COST uses the full breakdown (cache_read priced at the discounted cached rate). The RETURNED token split is
    # HONEST and un-lumped: in = new input + cache CREATION (both full-priced), cached = cache READ (discounted).
    # Claude Code re-reads the whole context every turn, so cr dominates — lumping it into `in` would report a
    # misleadingly huge "input" (20B+) when it's mostly cheap cache reads. Returns (cost, in, out, cached_read).
    try:
        # realtime_cost returns None for a model with no price — not an exception, so a surrounding
        # try/except never sees it and the None reaches the caller's arithmetic. Unknown contributes
        # nothing to a total rather than taking the scan down; the unpriced model is surfaced elsewhere.
        return (pricing.realtime_cost(model, inp + cc + cr, out, cr) or 0.0), inp + cc, out, cr
    except Exception:
        return 0.0, inp + cc, out, cr


def _scan_new_lines(path, from_line):
    """Yield parsed records from `from_line` onward. Returns (records, total_lines)."""
    recs, n = [], 0
    try:
        with open(path, "r", errors="ignore") as f:
            for n, line in enumerate(f, 1):
                if n <= from_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return recs, n


def update(st=None):
    """Read NEW transcript lines into the local accumulator (spend + work per project/model/day). Pure-ish: mutates
    + returns state; no network. Returns (state, summary-of-this-pass)."""
    st = st or _load_state()
    sessions = st.setdefault("sessions", {})
    ledger = st.setdefault("ledger", {})
    counted = st.setdefault("counted_ids", {})     # message.id → 1: each assistant API response counted ONCE across
    added_cost, added_lines, touched = 0.0, 0, 0   # ALL files (resume/branch/compaction replays messages into new ones)
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        prev = sessions.get(path) or {"lines": 0, "mtime": 0}
        if mtime <= prev.get("mtime", 0) and prev.get("lines"):
            continue                                       # unchanged since last pass → skip (the watermark)
        recs, total = _scan_new_lines(path, prev.get("lines", 0))
        if not recs and total <= prev.get("lines", 0):
            sessions[path] = {"lines": total or prev.get("lines", 0), "mtime": mtime}
            continue
        touched += 1
        for r in recs:
            msg = r.get("message") or {}
            mid = msg.get("id")
            # Count each assistant API response ONCE. Resume/branch/compaction REPLAYS earlier messages into NEW
            # transcript files; without this dedup the same message.id (its usage + tool_use) is summed once per
            # file → est-value AND work-done inflate ~2.4x. message.id is the globally-unique API response id.
            if mid:
                if mid in counted:
                    continue
                counted[mid] = 1
            u = msg.get("usage") or {}
            model = msg.get("model")
            day = (r.get("timestamp") or "")[:10] or datetime.date.today().isoformat()
            proj = _project_of(r.get("cwd"))
            if u and model:
                cost, intok, outtok, crtok = _row_cost(model, u)
                key = f"{proj}|{model}|{day}"
                e = ledger.setdefault(key, {"project": proj, "model": model, "day": day,
                                            "cost": 0.0, "in_tok": 0, "out_tok": 0, "cached_tok": 0, "turns": 0})
                e["cost"] += cost; e["in_tok"] += intok; e["out_tok"] += outtok
                e["cached_tok"] = e.get("cached_tok", 0) + crtok; e["turns"] += 1   # .get: old state entries predate the field
                added_cost += cost
            content = msg.get("content")
            if isinstance(content, list):                   # work-done: tool usage + files touched
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        wkey = f"{proj}|work"
                        w = ledger.setdefault(wkey, {"project": proj, "_work": True, "tools": {}, "files": []})
                        w["tools"][b.get("name", "?")] = w["tools"].get(b.get("name", "?"), 0) + 1
                        inp = b.get("input") or {}
                        for fk in _TOOL_FILE_KEYS:
                            if inp.get(fk):
                                fn = os.path.basename(str(inp[fk]))
                                if fn not in w["files"]:
                                    w["files"].append(fn)
        added_lines += (total - prev.get("lines", 0))
        sessions[path] = {"lines": total, "mtime": mtime}
    return st, {"sessions_updated": touched, "new_lines": added_lines, "new_cost": round(added_cost, 4)}


def _turn_usage(msg):
    """Raw, UN-lumped token split for ONE assistant message.usage → (in_tok, out_tok, cache_read, cache_write).
    Deliberately not lumped like _row_cost (which folds cache-creation into `in` for a compact display): the ledger
    stores cache_read_tok / cache_write_tok as their OWN columns, and that per-turn split is exactly what the
    context-trajectory + compaction signal (Phase 3) reads back — context = in + cache_read + cache_write."""
    u = msg.get("usage") or {}
    return (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
            int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))


def ingest_events(days=None, limit=None, reset=False, dry=False):
    """Write each Claude Code assistant TURN into the local spend_events ledger, so the app's OWN turns become
    queryable next to gate/batch/GPU spend. The ledger had 0 claude-code rows — precisely why token burn AFTER the
    weekly plan cap was exhausted could not be tracked down: it overflowed to real API $ that no gate call ever saw.
    Each turn is booked kind="est_chat" → est_chat_usd (plan-covered VALUE, billed=False, kept OUT of real $);
    Phase 2 reconciles the slice past the weekly cap into a separate realtime/billed stream (never summed with this).

    Idempotent: a STABLE dedup_key ("cc:<message.id>") makes the ledger book each assistant API response ONCE — across
    resume/branch/compaction replays (same id in several files) AND across re-runs (record_event returns early on an
    existing id). Incremental via a per-session watermark SEPARATE from update()'s, so the two miners never advance
    each other's cursor. CHUNK-safe: each session is isolated in its own try, so one malformed transcript cannot wedge
    the run. Pure parse + arithmetic — $0, no LLM, no network.

    days  — only ingest turns on/after today-`days` (bounds a first measured run to recent activity).
    limit — only the `limit` most-recently-modified transcripts (bounds the SCAN, not just the write).
    reset — delete prior source="claude-code" rows + clear the watermark first (dev re-ingest).
    dry   — count the turns + est-value that WOULD be written, write nothing (measure before a large backfill)."""
    from . import budget
    st = _load_state()
    wm = st.setdefault("ledger_sessions", {})          # OWN watermark {path:{lines,mtime}} — distinct from update()'s
    if reset and not dry:
        try:
            budget._ledger().delete(where={"source": "claude-code"}, actor="claude-code:ingest", reason="reset re-ingest")
            print("claude-code ingest: cleared prior claude-code rows + watermark")
        except Exception as e:
            print(f"claude-code ingest: reset delete failed ({type(e).__name__}: {str(e)[:80]}) — continuing")
        wm.clear()
    if not os.path.isdir(_projects_dir()):
        print(f"no Claude Code session directory at {_projects_dir()} — nothing to ingest (NOT an empty ledger; set "
              f"SPENDGUARD_CC_DIR if your sessions live elsewhere).")
        return 0
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    paths = glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)
    if limit:                                           # bound the SCAN to the most-recently-active transcripts
        paths = sorted(paths, key=lambda p: (os.path.getmtime(p) if os.path.exists(p) else 0), reverse=True)[:int(limit)]
    else:
        paths = sorted(paths)
    seen = set()                                        # skip re-submitting a replayed message.id within THIS run
    rows = 0; val = 0.0; sessions = 0; skipped = 0; unpriced = 0; no_id = 0; pre_cut = 0
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        prev = wm.get(path) or {"lines": 0, "mtime": 0}
        if not reset and not dry and mtime <= prev.get("mtime", 0) and prev.get("lines"):
            skipped += 1
            continue                                    # unchanged since last ingest → the watermark
        conv_id = os.path.basename(path)[:-6] if path.endswith(".jsonl") else os.path.basename(path)
        try:
            recs, total = _scan_new_lines(path, 0 if (reset or dry) else prev.get("lines", 0))
            proj = None; booked = 0
            for r in recs:
                if proj is None and r.get("cwd"):
                    proj = _project_of(r.get("cwd"))
                msg = r.get("message") or {}
                mid = msg.get("id"); u = msg.get("usage"); model = msg.get("model")
                if not (u and model):
                    continue                             # user msg / tool result / non-metered record — not a dropped turn
                if not mid:
                    no_id += 1                           # usage present but NO message.id → no stable dedup_key is possible,
                    continue                             # so it can't be booked idempotently — COUNTED, never silent (measured
                if mid in seen:                          # 0 today; a transcript-format change must SURFACE here, not vanish)
                    continue
                seen.add(mid)
                ts = r.get("timestamp") or ""
                if cutoff and ts[:10] and ts[:10] < cutoff:
                    pre_cut += 1                          # older than the --days window: intentionally out of scope, but COUNTED
                    continue
                intok, outtok, cr, cc = _turn_usage(msg)
                try:                                     # realtime_cost RETURNS None for some unpriced models but RAISES
                    cost = pricing.realtime_cost(model, intok + cc + cr, outtok, cr) or 0.0   # KeyError for others (the
                except Exception:                        # '<synthetic>' marker conv-synth writes) — treat any as unpriced,
                    cost = 0.0                           # PER TURN, so it never drops the whole session
                if cost <= 0:                            # unpriced/zero-token turn: a $0 est_chat row is illegal AND
                    unpriced += 1                        # meaningless as est-value — count it, don't book it
                    continue
                rows += 1; val += cost; booked += 1
                if dry:
                    continue
                budget._record_spend_event(
                    "anthropic", model, "est_chat", float(cost),
                    conv_id=conv_id, project=proj or "claude-code", occurred_at=ts or None,
                    in_tok=intok, out_tok=outtok, cache_read_tok=cr, cache_write_tok=cc,
                    source="claude-code", basis="reconstructed",
                    intent="claude-code:turn", actor="claudecode.ingest_events",
                    dedup_key="cc:" + str(mid))
            if booked:
                sessions += 1
            if not dry:
                wm[path] = {"lines": total, "mtime": mtime}
                if sessions and sessions % 200 == 0:     # CHUNK: checkpoint the watermark periodically
                    _save_state(st)
        except Exception as e:                           # per-session isolation — a bad transcript never wedges the run
            print(f"claude-code ingest: skip {os.path.basename(path)} ({type(e).__name__}: {str(e)[:80]})")
    if not dry:
        _save_state(st)
    tag = "WOULD ingest (dry)" if dry else "ingested"
    print(f"claude-code {tag}: {rows:,} turns · ${val:,.2f} est-value · {sessions} sessions · {skipped} unchanged · "
          f"{unpriced} unpriced-skipped · {no_id} no-id-skipped · {pre_cut} pre-cutoff-skipped")
    if not dry:
        print("  → source=\"claude-code\" rows in spend_events (est_chat_usd, billed=False); query per conversation "
              "WHERE conv_id=<session-uuid>. Re-run is idempotent (stable dedup_key cc:<message.id>).")
    return 0


def _parse_iso(ts):
    """Parse a transcript/ledger ISO timestamp to a tz-aware datetime, or None. Handles a trailing 'Z'."""
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _week_start(dt, anchor):
    """UTC datetime of the weekly-window START containing `dt`. With `anchor` (a datetime), windows are
    [anchor+7k, anchor+7(k+1)); else the ISO calendar week (Monday 00:00 UTC). Parsing a fixed cadence, not a
    meaning judgement — the reset schedule is DECLARED (config), never inferred."""
    if anchor:
        k = int((dt - anchor).total_seconds() // (7 * 86400))
        return anchor + datetime.timedelta(days=7 * k)
    d = dt.astimezone(datetime.timezone.utc)
    return (d - datetime.timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def reconcile_overflow(cap_usd=None, anchor=None, dry=False):
    """Reconstruct billing_state for the Claude Code app turns: which of them billed for REAL once the weekly plan
    cap was exhausted. LOCAL reconstruction is the anchor (never the Admin API): walk each weekly window's turns in
    TIMESTAMP order, cap-fill cumulative est-value against the DECLARED weekly cap, and book the slice PAST the cap as
    a SEPARATE realtime/billed stream (source="claude-code-overflow"), tagged by conv_id. The est-value stream
    (source="claude-code", est_chat_usd) is left untouched — the two are DIFFERENT axes and are never summed:
        est-value of conv X   = SUM(est_chat_usd)  WHERE source='claude-code'          AND conv_id=X
        REAL overflow $ of X  = SUM(realtime_usd)   WHERE source='claude-code-overflow' AND conv_id=X
    This is a reconciliation (idempotent by delete+rebook), so re-run it whenever the cap changes. The cap is a
    DECLARED proxy (the plan meters in its own opaque unit); unset → nothing is reconstructed (no fabrication).
    Pure arithmetic over ledger rows — $0, no LLM, no network. Admin cost_report is a DEV-ONLY calibration aid,
    never in this path."""
    import sqlite3
    from . import budget
    cap = cap_usd if cap_usd is not None else config._cfg_get("subscription", "claude_code_weekly_cap_usd", None)
    try:
        cap = float(cap) if cap is not None else None
    except (TypeError, ValueError):
        cap = None
    if not cap or cap <= 0:
        print("claude-code overflow: no weekly cap declared (subscription.claude_code_weekly_cap_usd) — every turn "
              "stays plan_covered; nothing is reconstructed as billed overflow. Declare the est-value $/week your plan "
              "covers (calibrate it against the Admin cost_report, dev-only) to reconstruct which turns overflowed to "
              "real API $.")
        return 0
    anchor_s = anchor if anchor is not None else config._cfg_get("subscription", "claude_code_week_anchor", None)
    anchor_dt = _parse_iso(anchor_s) if anchor_s else None
    con = sqlite3.connect(config.db_path())
    try:
        rows = con.execute("SELECT occurred_at, conv_id, model, est_chat_usd FROM spend_events "
                           "WHERE source='claude-code' AND est_chat_usd IS NOT NULL ORDER BY occurred_at").fetchall()
    finally:
        con.close()
    windows = {}                                            # window_start_iso -> [(dt, conv, model, est_value), …]
    bad = 0
    for occ, conv, model, val in rows:
        dt = _parse_iso(occ)
        try:
            v = float(val)
        except (TypeError, ValueError):
            v = 0.0
        if dt is None or v <= 0:
            bad += 1                                         # a ledger row with no parseable ts / no positive value — COUNTED
            continue
        windows.setdefault(_week_start(dt, anchor_dt).isoformat(), []).append((dt, conv or "", model or "?", v))
    overflow = {}                                           # (window_iso, conv, model) -> overflow_usd
    per_window = []                                         # (window_iso, covered_usd, overflow_usd)
    for wiso, turns in sorted(windows.items()):
        turns.sort(key=lambda t: t[0])
        cum = 0.0; over_w = 0.0
        for dt, conv, model, v in turns:
            before = cum
            cum += v
            over = max(0.0, cum - max(cap, before))         # exact split of the crossing turn: only the part past the cap
            if over > 0:
                overflow[(wiso, conv, model)] = overflow.get((wiso, conv, model), 0.0) + over
                over_w += over
        per_window.append((wiso, min(cum, cap), over_w))
    total_over = sum(overflow.values())
    if not dry:                                             # reconciliation = delete + rebook (idempotent by construction)
        try:
            budget._ledger().delete(where={"source": "claude-code-overflow"},
                                    actor="claude-code:overflow", reason="reconcile overflow")
        except Exception as e:
            print(f"claude-code overflow: clearing prior rows failed ({type(e).__name__}: {str(e)[:60]}) — continuing")
        for (wiso, conv, model), over in overflow.items():
            if over <= 0:
                continue
            budget._record_spend_event("anthropic", model, "realtime", float(round(over, 6)),
                                       conv_id=conv, project="claude-code", occurred_at=wiso,
                                       source="claude-code-overflow", basis="reconstructed",
                                       intent="claude-code:overflow", actor="claudecode.reconcile_overflow",
                                       dedup_key=f"cc-of:{wiso}:{conv}:{model}")
    print(f"claude-code overflow{' (dry)' if dry else ''} — weekly cap ${cap:,.0f} est-value/window"
          f"{' · anchor ' + anchor_dt.isoformat() if anchor_dt else ' · ISO calendar weeks'}")
    print(f"  {len(windows)} window(s) · RECONSTRUCTED overflow: Real ${total_over:,.2f} (API overflow) "
          f":: est-value stream unchanged (the two are NEVER summed)"
          + (f" · {bad} ledger row(s) skipped (unparseable ts/value)" if bad else ""))
    for wiso, covered, over in sorted(per_window, reverse=True)[:8]:
        print(f"    {wiso[:10]}  covered ~${covered:,.0f} of ${cap:,.0f}  ·  overflow ${over:,.2f}"
              + ("  ⚠ OVER CAP" if over > 0 else ""))
    byconv = {}
    for (wiso, conv, model), over in overflow.items():
        byconv[conv] = byconv.get(conv, 0.0) + over
    if byconv:
        print("  top conversations by REAL overflow $ (what billed after the cap):")
        for conv, over in sorted(byconv.items(), key=lambda x: -x[1])[:6]:
            print(f"    ${over:8.2f}  {conv[:24]}")
    if not dry:
        print("  → source=\"claude-code-overflow\" rows (realtime_usd, billed=1). Real $ for a conversation AFTER the "
              "cap = SUM(realtime_usd) WHERE source='claude-code-overflow' AND conv_id=<uuid>.")
    return 0


def overflow_by_conversation():
    """{conv_id: real overflow $} from the reconstructed source='claude-code-overflow' rows — the billed slice past
    the weekly cap, per conversation. Read-only; empty until `claude-code overflow` has run with a declared cap."""
    import sqlite3
    con = sqlite3.connect(config.db_path())
    try:
        rows = con.execute("SELECT conv_id, SUM(CAST(realtime_usd AS REAL)) FROM spend_events "
                           "WHERE source='claude-code-overflow' GROUP BY conv_id").fetchall()
    finally:
        con.close()
    return {c or "": float(v or 0.0) for c, v in rows}


def _cc_conv_rows(min_day=None):
    """{conv_id: [(occurred_at, model, in_tok, cache_read, cache_write), …] ordered by ts} from the claude-code
    est-value rows. One focused SELECT; read-only. context per turn = in_tok + cache_read + cache_write."""
    import sqlite3
    q = ("SELECT conv_id, occurred_at, model, COALESCE(in_tok,0), COALESCE(cache_read_tok,0), "
         "COALESCE(cache_write_tok,0) FROM spend_events WHERE source='claude-code'")
    args = []
    if min_day:
        q += " AND occurred_at >= ?"; args.append(min_day)
    q += " ORDER BY conv_id, occurred_at"
    con = sqlite3.connect(config.db_path())
    try:
        rows = con.execute(q, args).fetchall()
    finally:
        con.close()
    out = {}
    for conv, occ, model, i, cr, cw in rows:
        out.setdefault(conv or "", []).append((occ, model or "?", int(i or 0), int(cr or 0), int(cw or 0)))
    return out


def _cache_read_rate(model):
    """$/token for a cache READ of `model` (the recurring cost of re-reading retained context each turn): the
    canonical `cached_in` rate if priced, else None. From pricing only — never an invented number."""
    try:
        from . import pricing
        p = pricing.price(model) or {}
        cin = p.get("cached_in")
        return (float(cin) / 1e6) if cin is not None else None
    except Exception:
        return None


def measured_compaction_ratio(min_drop=2.0):
    """Median k = context_before / context_after over OBSERVED large context DROPS across claude-code conversations.
    A compaction (or /clear, or topic reset) sharply drops the re-read context between consecutive turns, so the
    reduction factor is MEASURABLE from the ledger itself — never assumed. Only drops of at least `min_drop`× count
    (a real reset, not turn-to-turn wobble). Returns (k, n_events); (None, 0) when none is observed — so 'compacting
    cuts it ~k×' is only ever shown from measured evidence."""
    ks = []
    for conv, seq in _cc_conv_rows().items():
        prev = None
        for _occ, _model, i, cr, cw in seq:
            ctx = i + cr + cw
            if prev and ctx > 0 and prev / ctx >= min_drop:
                ks.append(prev / ctx)
            if ctx > 0:
                prev = ctx
    if not ks:
        return None, 0
    ks.sort()
    n = len(ks)
    return (ks[n // 2] if n % 2 else (ks[n // 2 - 1] + ks[n // 2]) / 2.0), n


def context_trajectory(conv_id):
    """Per-conversation context stats from the ledger: {turns, current, max, mean, recurring_read_usd_per_turn,
    model} where context = in_tok + cache_read + cache_write per turn. `current` = the last turn's context (what each
    further turn re-reads); recurring_read_usd_per_turn = last turn's cache_read × the model's cache-read rate — the $
    this open session spends EVERY turn just re-reading its retained context. None where unpriced/empty."""
    seq = _cc_conv_rows().get(conv_id) or []
    if not seq:
        return {"turns": 0, "current": 0, "max": 0, "mean": 0.0, "recurring_read_usd_per_turn": None, "model": None}
    ctxs = [i + cr + cw for _o, _m, i, cr, cw in seq]
    _occ, last_model, _li, last_cr, _lcw = seq[-1]
    rate = _cache_read_rate(last_model)
    recur = (last_cr * rate) if rate is not None else None
    return {"turns": len(seq), "current": ctxs[-1], "max": max(ctxs), "mean": sum(ctxs) / len(ctxs),
            "recurring_read_usd_per_turn": recur, "model": last_model}


def compaction_candidates(min_context=None, min_turns=None, min_day=None):
    """Conversations sustaining a large re-read context — the ones expensive to keep OPEN. A conversation qualifies
    when its most-recent `min_turns` turns ALL have context (in+cache_read+cache_write) >= `min_context`. For each:
    current context, mean, recurring re-read $/turn, and (only when k is MEASURED) the $/turn a compaction would save.
    Config: advisor.compaction_context_tokens, advisor.compaction_min_turns. The threshold only PRE-FILTERS which
    sessions to surface; whether the retained context is still worth its cost is an agentic call, not made here.
    Returns (candidates sorted by recurring $/turn desc, (k, k_n), scan-stats {examined, flagged, too_few_turns,
    below_threshold} — so the scan is transparent and never silently truncates)."""
    min_context = int(min_context if min_context is not None
                      else config._cfg_get("advisor", "compaction_context_tokens", 100000))
    min_turns = int(min_turns if min_turns is not None
                    else config._cfg_get("advisor", "compaction_min_turns", 5))
    k, k_n = measured_compaction_ratio()
    out = []
    examined = short = below = 0                             # every conversation NOT flagged is COUNTED — the scan is
    for conv, seq in _cc_conv_rows(min_day=min_day).items():  # transparent (no silent truncation of the candidate set)
        examined += 1
        if len(seq) < min_turns:
            short += 1
            continue
        if any((i + cr + cw) < min_context for _o, _m, i, cr, cw in seq[-min_turns:]):
            below += 1                                      # not SUSTAINED above the threshold (one big turn ≠ bloat)
            continue
        traj = context_trajectory(conv)
        recur = traj["recurring_read_usd_per_turn"]
        saved = (recur * (1.0 - 1.0 / k)) if (recur is not None and k) else None
        out.append({"conv_id": conv, "turns": traj["turns"], "current": traj["current"], "mean": traj["mean"],
                    "recurring_read_usd_per_turn": recur, "compact_k": k, "saved_usd_per_turn": saved})
    out.sort(key=lambda r: (r["recurring_read_usd_per_turn"] or 0.0), reverse=True)
    return out, (k, k_n), {"examined": examined, "flagged": len(out), "too_few_turns": short, "below_threshold": below}


def context_cmd(conv_id=None, top=10):
    """`claude-code context` — the compaction view: open conversations whose sustained re-read context makes them
    expensive to keep alive, each with its $/turn re-read cost and (when k is measured from real compactions) the
    $/turn a compaction would save. With --conv <uuid>, one conversation's trajectory."""
    if conv_id:
        t = context_trajectory(conv_id)
        r = t["recurring_read_usd_per_turn"]
        print(f"conversation {_conv_label(conv_id, _sidebar_titles(), 40)} ({conv_id[:12]}) — "
              f"{t['turns']} turns · model {t.get('model') or '?'}")
        print(f"  context tokens: current {t['current']:,} · max {t['max']:,} · mean {t['mean']:,.0f}")
        print(f"  recurring re-read cost: {('$%.4f/turn' % r) if r is not None else '—'} at the current context "
              f"(what every further turn costs just to re-read what's retained)")
        return 0
    cands, (k, k_n), stats = compaction_candidates()
    ktxt = (f"measured compaction ratio k≈{k:.1f}× (from {k_n} observed context drops)" if k
            else "compaction ratio not yet measured (no context-drop events seen) — savings shown as —")
    thr = int(config._cfg_get("advisor", "compaction_context_tokens", 100000))
    print(f"claude-code context — compaction candidates (re-read context ≥ {thr:,} tok sustained); {ktxt}")
    print(f"  scanned {stats['examined']:,} conversations → {stats['flagged']} candidate(s) "
          f"({stats['below_threshold']:,} below threshold · {stats['too_few_turns']:,} too few turns)")
    if not cands:
        print("  (no open conversation is sustaining a context above the threshold)")
        return 0
    titles = _sidebar_titles()
    for c in cands[:top]:
        r = c["recurring_read_usd_per_turn"]; s = c["saved_usd_per_turn"]
        rtxt = ("$%.4f/turn" % r) if r is not None else "—"
        stxt = (" → compacting saves ~$%.4f/turn" % s) if s is not None else ""
        print(f"  current {c['current']:>9,} tok · {c['turns']:>4} turns · re-read {rtxt}{stxt}  ·  "
              f"{_conv_label(c['conv_id'], titles, 30)}")
    print("  ↑ the threshold only PRE-FILTERS; whether a session's retained context is still worth its per-turn cost "
          "is an agentic judgement (read the session), never decided by this number.")
    return 0


def _sidebar_store_dir():
    """The desktop app's session STORE dir (where sidebar titles live), distinct from the transcript dir
    (_projects_dir). Env-overridable; defaults to the macOS Application Support location."""
    return os.environ.get("SPENDGUARD_CC_SESSIONS_DIR") or os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code-sessions")


def _sidebar_titles():
    """{transcript-uuid: human SIDEBAR title} from the desktop app's session store (each record's cliSessionId ==
    the transcript uuid). A transcript can have several session json files (resumes/bridges); keep the most recently
    active one. Best-effort → {} when the store is absent (a non-desktop / headless host) — the caller then falls
    back to the uuid, so a missing store degrades the LABEL, never the numbers."""
    out = {}
    for p in glob.glob(os.path.join(_sidebar_store_dir(), "**", "local_*.json"), recursive=True):
        try:
            with open(p, "r", errors="replace") as f:
                o = json.load(f)
        except Exception:
            continue
        cli = o.get("cliSessionId")
        title = o.get("title")
        if not cli or not title:
            continue
        la = o.get("lastActivityAt") or 0
        if cli not in out or la > out[cli][0]:
            out[cli] = (la, title)
    return {k: v[1] for k, v in out.items()}


def _conv_label(conv_id, titles, width=34):
    """Human label for a conversation: its sidebar title if the desktop store has one, else the raw uuid. Titles are
    resolved at READ time (they change; denormalizing a stale copy into the ledger would rot)."""
    t = titles.get(conv_id)
    return (t[:width] if t else (conv_id or "?")[:width])


def conversations_cmd(top=15):
    """`claude-code conversations` — the unified per-conversation view: top conversations by est-value, each labeled by
    its human SIDEBAR TITLE, with turns, current max re-read context, and (when reconciled) the REAL $ that overflowed
    the weekly cap. The est-value and the real-overflow axes are shown SEPARATELY, never summed."""
    import sqlite3
    con = sqlite3.connect(config.db_path())
    try:
        rows = con.execute(
            "SELECT conv_id, ROUND(SUM(CAST(est_chat_usd AS REAL)),2), COUNT(*), "
            "MAX(COALESCE(in_tok,0)+COALESCE(cache_read_tok,0)+COALESCE(cache_write_tok,0)) "
            "FROM spend_events WHERE source='claude-code' GROUP BY conv_id").fetchall()
    finally:
        con.close()
    titles = _sidebar_titles()
    over = overflow_by_conversation()
    data = [{"conv": c or "", "est": float(e or 0.0), "turns": int(n or 0), "maxctx": int(mx or 0),
             "over": over.get(c or "", 0.0)} for c, e, n, mx in rows]
    data.sort(key=lambda r: -r["est"])
    any_over = any(r["over"] > 0 for r in data)
    head = ("  ::  Real overflow $ shown as a SEPARATE column (reconciled; never summed with est-value)" if any_over
            else "  (run `claude-code overflow --cap-usd <$/week>` to add the Real overflow $ column)")
    print(f"claude-code conversations — top {top} of {len(data)} by est-value{head}")
    for r in data[:top]:
        otxt = (f"  ·  Real overflow ${r['over']:,.2f}" if r["over"] > 0 else "")
        print(f"  ${r['est']:8.2f} est · {r['turns']:>5} turns · {r['maxctx']:>9,} max ctx · "
              f"{_conv_label(r['conv'], titles)}{otxt}")
    return 0


def show(days=None):
    st, passinfo = update()
    _save_state(st)
    cutoff = None
    if days:
        cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    spend = [v for v in st["ledger"].values() if not v.get("_work") and (not cutoff or v["day"] >= cutoff)]
    work = [v for v in st["ledger"].values() if v.get("_work")]
    # Stamp the est-value windows (from the FULL ledger, not the day-filtered `spend`) so `spendguard receipt` and the
    # in-chat footer can show plan-usage cheaply. billed=false → it stays out of actual-$; channel keyed so claude.ai
    # doesn't clobber it. Best-effort.
    try:
        from . import receipt
        _cls = st.get("cls", {})       # ORG → TEAM → PROJECT, the agentic classification (same as the server push)
        receipt.stamp_est_value(
            [{"day": d["day"], "spend_micros": round(d["cost"] * 1_000_000), "billed": False,
              "org": (_cls.get(d["sid"]) or {}).get("org") or "",
              "team": (_cls.get(d["sid"]) or {}).get("team") or "",
              "project": (_cls.get(d["sid"]) or {}).get("project") or d["project"]}
             for d in _session_digests() if d.get("day") and d.get("cost", 0) > 0],
            source="claude-code")
    except Exception:
        pass
    byproj = {}
    for r in spend:
        p = byproj.setdefault(r["project"], {"cost": 0.0, "turns": 0, "models": set()})
        p["cost"] += r["cost"]; p["turns"] += r["turns"]; p["models"].add(r["model"])
    total = sum(p["cost"] for p in byproj.values())
    span = sorted(r["day"] for r in spend)
    rng = f"{span[0]} → {span[-1]} ({len(set(span))} days)" if span else "no data"
    print(f"Claude Code USAGE VALUE — {len(st['sessions'])} sessions · {rng}{' · last %sd' % days if days else ' · ALL-TIME'}\n")
    print(f"  {'project':<22}{'value $':>10}{'turns':>9}  models")
    for proj, p in sorted(byproj.items(), key=lambda x: -x[1]["cost"]):
        wk = next((w for w in work if w["project"] == proj), None)
        print(f"  {proj[:21]:<22}{('$%.2f' % p['cost']):>10}{p['turns']:>9}  {', '.join(sorted(m for m in p['models'] if m))[:40]}")
        if wk:
            tools = ", ".join(f"{k}×{v}" for k, v in sorted(wk["tools"].items(), key=lambda x: -x[1])[:5])
            print(f"  {'':<22}└ work: {tools}  ·  {len(wk['files'])} files touched")
    print(f"\n  {'TOTAL VALUE':<22}{('$%.2f' % total):>10}")
    print("  ⚠ this is USAGE VALUE (tokens × API pricing) — what it WOULD cost at API rates, NOT $ billed. On a")
    print("    subscription it's covered by the flat plan: \"~$X of value for your $Y/mo plan\". `claude-code sync`")
    print("    pushes it as channel=claude-code, billed=false, so the dashboard keeps it OUT of actual spend.")
    return 0


def _session_digests(days=None):
    """Per-SESSION digests (the cwd is an umbrella, so each session is classified on its own content)."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    out = []
    seen = set()                                           # count each message.id ONCE across resume/branch replays
    # AN UNREADABLE DIRECTORY IS NOT AN EMPTY ONE. glob returns [] for a path that is missing, renamed by a
    # Claude Code upgrade, or unreadable — indistinguishable from "you did no work this period". Both callers
    # then printed a header with no rows, so a broken source looked exactly like an honest zero and the user
    # had nothing to act on. chat.work names the difference; this did not.
    if not os.path.isdir(_projects_dir()):
        print(f"no Claude Code session directory at {_projects_dir()} — nothing could be READ, which is not "
              f"the same as no work. Set SPENDGUARD_CLAUDE_PROJECTS if your sessions live elsewhere.")
        return 0
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        d = _digest(path, seen)
        if d["cost"] <= 0 and not d["tools"]:
            continue
        if cutoff and d["day"] and d["day"] < cutoff:
            continue
        d["sid"] = os.path.basename(path)[:48]
        out.append(d)
    return out


def classify(run=False, days=None, recls=False):
    """Classify Claude Code sessions into org→team×project via the SHARED classifier + taxonomy (NOT the cwd repo
    name — that's an umbrella). Caged, estimate-first. Stored per session in state.cls; reused by day_totals/sync."""
    from . import attribution
    st = _load_state()
    cls = st.setdefault("cls", {})
    # Re-classify a session if it's unclassified OR its cached confidence is 0/missing. A 0 means it was never given a
    # real confidence (stale cache from before confidence-capture, or a genuinely low-confidence read) → the
    # convergence loop re-does it, so CC attributions never silently sit at confidence 0 (the chat path always has one).
    todo = [d for d in _session_digests(days) if d.get("prompt")
            and (recls or not (cls.get(d["sid"]) or {}).get("confidence"))]
    if not todo:
        print("claude-code: nothing to classify (run `claude-code show` to mine first; --reclassify to redo).")
        return 0
    taxo, _ = attribution.taxonomy()
    items = [{"id": d["sid"], "text": f"[{d['project']}] {d['prompt']}"} for d in todo]
    res = attribution.classify_items(items, taxo, run)
    if not run:
        return 0
    cls.update(res)
    _save_state(st)
    print(f"claude-code: classified {len(res)}/{len(todo)} sessions into org→team×project.")
    return 0


def day_totals(member_ref, org_label=None):
    """Per-(team, project, model, day) CC rows → server (channel=claude-code, billed=false). Each session maps to its
    CLASSIFIED org→team×project (state.cls); `team` rides along for org→team scope attribution. org_label keeps only
    sessions whose classified org matches (or are unclassified) — for org-routed push."""
    st = _load_state()
    cls = st.get("cls", {})
    agg = {}
    for d in _session_digests():
        if d["cost"] <= 0:
            continue
        a = cls.get(d["sid"])
        if a is None:
            if org_label:                                  # org-routed push: skip unclassified (avoid cross-org pollution)
                continue
            a = {}                                         # local view: include with cwd fallback (no team)
        org = a.get("org", "")
        # A RECORD WITH NO ORG IS NOT A RECORD FOR THIS ORG. The `and org` made the comparison run only when
        # org was truthy, so a session classified with an empty org passed the filter into an ORG-ROUTED
        # push — the cross-org pollution the branch above declines to risk for an UNCLASSIFIED session, let
        # through for a classified one whose org happens to be blank.
        if org_label and (org or "").lower() != org_label.lower():
            continue
        team = (a.get("team") or "").lower()
        proj = (a.get("project") or d["project"] or "claude-code").lower()
        model = d.get("model") or ""
        key = f"{team}|{proj}|{model}|{d['day']}"
        e = agg.setdefault(key, {"team": team, "project": proj, "model": model, "day": d["day"],
                                 "cost": 0.0, "in": 0, "out": 0, "cached": 0, "n": 0})
        e["cost"] += d["cost"]; e["in"] += d.get("in_tok", 0); e["out"] += d.get("out_tok", 0)
        e["cached"] += d.get("cached_tok", 0); e["n"] += 1
    return [{"day": e["day"], "provider": "anthropic", "model": e["model"], "kind": "workload",
             "channel": "claude-code", "billed": False, "spend_micros": round(e["cost"] * 1_000_000),
             "calls": e["n"], "in_tokens": e["in"], "out_tokens": e["out"], "cached_in_tokens": e["cached"],
             "member_ref": member_ref, "project": e["project"], "team": e["team"],
             "tags": ("team:" + e["team"]) if e["team"] else ""}
            for e in agg.values() if e["day"]]


def sync(dry=False):
    """Push Claude Code spend (channel=claude-code) → the server. Honors visibility + contributor; ORG-ROUTED by the
    session's classified org (only rows whose org matches THIS connection — or are unclassified — push here)."""
    from . import saas
    c = saas.saas_connection()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private"}
    cok, cwhy = saas.contributor_ok()
    if not cok:
        return {"skipped": cwhy}
    rows = day_totals(saas.contributor(), org_label=c.get("org"))
    for r in rows:
        r["uid"] = saas._row_uid(r)
    if dry:
        return {"day_totals": rows}
    if not rows:
        return {"skipped": "no Claude Code spend for this connection's org"}
    try:
        # day_totals() is the COMPLETE per-session est-value set → declare a replace so the server prunes this
        # contributor's orphaned claude-code rows (re-classification / dedup re-bucketing) instead of stacking them.
        return saas._request("POST", "/v1/ledger", {"visibility": c.get("visibility"), "day_totals": rows,
                                                    "replace": [{"channel": "claude-code", "billed": False}]})
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/ledger endpoint yet"}
        raise


def _iso_period(day, by):
    """Kept as a one-line alias so this module's callers read locally, but the PERIOD RULE lives in exactly
    one place. chat and claudecode each had this identical wrapper, and before that each had its own real
    implementation — one of which was missing 'ytd'."""
    from . import attribution
    return attribution.iso_period(day, by)


def _digest(path, seen=None, ask_verdicts=None):
    """Full per-session digest = a WORK ROW: project, primary day, models, value$, turns, tools, files, and the
    first user prompt (what was ASKED — the 'what the spend was for'). Re-reads the whole session (on-demand).
    Pass a shared `seen` set across sessions to count each assistant message.id ONCE — resume/branch/compaction
    replays earlier messages into new transcript files, so without it the est-value/work double-counts (~2.4x)."""
    proj = None; days = {}; models = set(); cost = 0.0; turns = 0; tools = {}; files = []; prompt = ""; branch = ""
    in_tok = out_tok = cached_tok = 0; modelcost = {}
    recs, _ = _scan_new_lines(path, 0)
    for r in recs:
        if proj is None and r.get("cwd"):
            proj = _project_of(r.get("cwd"))
        if not branch and r.get("gitBranch"):
            branch = r.get("gitBranch")
        day = (r.get("timestamp") or "")[:10]
        msg = r.get("message") or {}
        if not prompt and (r.get("type") == "user" or msg.get("role") == "user"):
            c = msg.get("content")
            t = c if isinstance(c, str) else (" ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text") if isinstance(c, list) else "")
            t = (t or "").strip().replace("\n", " ")
            # THE SAME THREE SUBSTRINGS LIVED HERE AND IN conv._is_user_ask, deciding the same thing — "is
            # this a real human ask?" — mechanically, in two places. This is the session's prompt: the answer
            # becomes "what the spend was for" in every work-done row and story. Now one decider, in conv.
            if conv._is_user_ask(r, t, ask_verdicts):
                prompt = t[:200]
        mid = msg.get("id")
        if mid is not None and seen is not None:           # count each API response ONCE across replayed files
            if mid in seen:
                continue
            seen.add(mid)
        u = msg.get("usage") or {}; model = msg.get("model")
        if u and model:
            cu, ai, bo, cr = _row_cost(model, u); cost += cu; turns += 1; models.add(model)
            in_tok += ai; out_tok += bo; cached_tok += cr; modelcost[model] = modelcost.get(model, 0) + cu
            if day:
                days[day] = days.get(day, 0) + cu
        c = msg.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools[b.get("name", "?")] = tools.get(b.get("name", "?"), 0) + 1
                    inp = b.get("input") or {}
                    for fk in _TOOL_FILE_KEYS:
                        if inp.get(fk) and os.path.basename(str(inp[fk])) not in files:
                            files.append(os.path.basename(str(inp[fk])))
    primary = max(days, key=days.get) if days else ((recs[0].get("timestamp") or "")[:10] if recs else "")
    dominant = max(modelcost, key=modelcost.get) if modelcost else (sorted(models)[0] if models else "")
    return {"project": proj or "claude-code", "day": primary, "models": sorted(models), "cost": round(cost, 4),
            "turns": turns, "tools": tools, "files": files, "prompt": prompt, "branch": branch,
            "in_tok": in_tok, "out_tok": out_tok, "cached_tok": cached_tok, "model": dominant}


def work(by="week", days=None):
    """Conversation-derived WORK DONE — per-session rows (what was asked + cost) bucketed by day/week/month/quarter."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    digs = []
    seen = set()                                           # count each message.id ONCE across resume/branch replays
    # AN UNREADABLE DIRECTORY IS NOT AN EMPTY ONE. glob returns [] for a path that is missing, renamed by a
    # Claude Code upgrade, or unreadable — indistinguishable from "you did no work this period". Both callers
    # then printed a header with no rows, so a broken source looked exactly like an honest zero and the user
    # had nothing to act on. chat.work names the difference; this did not.
    if not os.path.isdir(_projects_dir()):
        print(f"no Claude Code session directory at {_projects_dir()} — nothing could be READ, which is not "
              f"the same as no work. Set SPENDGUARD_CLAUDE_PROJECTS if your sessions live elsewhere.")
        return 0
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        d = _digest(path, seen)
        if d["cost"] <= 0 and not d["tools"]:
            continue
        if cutoff and d["day"] and d["day"] < cutoff:
            continue
        digs.append(d)
    buckets = {}
    for d in digs:
        p = _iso_period(d["day"], by)
        b = buckets.setdefault(p, {"value": 0.0, "sessions": 0, "rows": []})
        b["value"] += d["cost"]; b["sessions"] += 1; b["rows"].append(d)
    print(f"WORK DONE — by {by}{' · last %sd' % days if days else ''} · from Claude Code conversations (value = usage $)\n")
    for p in sorted(buckets, reverse=True):
        b = buckets[p]
        print(f"  ▸ {p}  —  ${b['value']:.2f} value · {b['sessions']} sessions")
        for d in sorted(b["rows"], key=lambda x: -x["cost"])[:8]:
            tl = " · ".join(f"{k}×{v}" for k, v in sorted(d["tools"].items(), key=lambda x: -x[1])[:3])
            print(f"     {('$%.2f' % d['cost']):>8}  {d['project'][:13]:<14} {(d['prompt'] or '(no prompt captured)')[:66]}")
            if tl:
                print(f"     {'':>8}  {'':<14} └ {tl} · {len(d['files'])} files")
        print()
    print("  ↑ per-session ROWS (what was asked + $). NEXT: a caged LLM 'story' synthesis per period + push to the dashboard.")
    return 0


def _toklen(s):
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(s))
    except Exception:
        return max(1, len(s) // 4)


_STORY_SYS = (
    "You turn a developer's AI-assisted work SESSIONS into a WORK LOG. Each session line is: [project] what was "
    "asked | tools used | files. Output STRICT JSON only (no prose outside it):\n"
    '{"story": "<3-5 sentence first-person-plural narrative of what got DONE this period — concrete, no fluff, '
    'no activity counts>",\n'
    ' "insights": [{"type": "finding|decision|gotcha|next", "project": "<proj>", "text": "<a WORK/domain insight: '
    'something LEARNED, a DECISION made, a GOTCHA discovered, or a NEXT step — about the work itself, NOT about '
    'how to use LLMs/cost better>"}]}\n'
    "Give 3-8 insights, substance over activity (what we now KNOW). These are the org's private knowledge.")


def story(by="week", days=7, run=False):
    """Caged synth over the period's work rows → a narrative STORY + private WORK INSIGHTS (findings/decisions/
    gotchas/next — distinct from cost/LLM-usage learnings). Estimate-first; the LLM call is caged under caps.meta."""
    from . import config, adapters, calls, pricing, ui
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    digs = []
    seen = set()                                           # count each message.id ONCE across resume/branch replays
    # AN UNREADABLE DIRECTORY IS NOT AN EMPTY ONE. glob returns [] for a path that is missing, renamed by a
    # Claude Code upgrade, or unreadable — indistinguishable from "you did no work this period". Both callers
    # then printed a header with no rows, so a broken source looked exactly like an honest zero and the user
    # had nothing to act on. chat.work names the difference; this did not.
    if not os.path.isdir(_projects_dir()):
        print(f"no Claude Code session directory at {_projects_dir()} — nothing could be READ, which is not "
              f"the same as no work. Set SPENDGUARD_CLAUDE_PROJECTS if your sessions live elsewhere.")
        return 0
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        d = _digest(path, seen)
        if (d["cost"] > 0 or d["tools"]) and (not cutoff or not d["day"] or d["day"] >= cutoff):
            digs.append(d)
    if not digs:
        print("no sessions in range — nothing to synthesize."); return 0
    lines = []
    for d in sorted(digs, key=lambda x: -x["cost"])[:40]:
        tl = ",".join(f"{k}×{v}" for k, v in sorted(d["tools"].items(), key=lambda x: -x[1])[:4])
        lines.append(f"- [{d['project']}] {(d['prompt'] or '(no prompt)')[:160]} | tools: {tl} | {len(d['files'])} files")
    prompt = f"Sessions ({len(digs)}, last {days}d):\n" + "\n".join(lines)
    model = config.advisor_model()
    OUT = 1500
    est = pricing.realtime_cost(model, _toklen(_STORY_SYS + prompt), OUT)
    print(f"work story + insights — {model} (caged under caps.meta ${config.meta_cap():.2f}/day)")
    print(f"  ESTIMATE (zero paid calls): {len(digs)} sessions · in~{_toklen(_STORY_SYS + prompt):,} out≤{OUT} -> ~${est:.4f}")
    if not run:
        ui.estimate_only(action="synthesize the work story + private insights", cost=est)
        return 0
    with calls.context(intent="spendguard:worklog"):     # caged → meta budget, excluded from the workload corpus
        r = adapters.call(model, prompt, max_tokens=OUT, system=_STORY_SYS)
    if r.get("error"):
        print("  error:", r["error"]); return 1
    from .chat import _parse_story                         # tolerant parse (recovers story + insights if truncated)
    data = _parse_story(r.get("text", ""))
    print("\n=== WORK STORY ===\n" + (data.get("story") or r.get("text", "")[:800]))
    print("\n=== WORK INSIGHTS (private — your IP, never pooled) ===")
    for ins in (data.get("insights") or []):
        print(f"  [{ins.get('type', '?'):<8}] ({ins.get('project', '?')}) {ins.get('text', '')}")
    # `or 0`, not `.get(..., 0)`: the default only applies when the KEY IS ABSENT. adapters.call returns
    # cost=None for a model with no price, which reaches :.4f and raises — after the call was paid for.
    print(f"\n  (caged cost ${(r.get('cost') or 0):.4f}; intent spendguard:worklog)")
    return 0


def main(argv=None):
    argv = argv or []
    if "--rebuild" in argv:                        # re-bucket: clear the accumulator + watermarks, re-mine at repo level
        st = _load_state(); st["ledger"] = {}; st["sessions"] = {}; st["counted_ids"] = {}; _save_state(st)
        print("claude-code: state reset — re-mining all transcripts with repo-level buckets + per-message-id dedup")
        argv = [a for a in argv if a != "--rebuild"]
    sub = argv[0] if argv else "show"
    if sub == "sync":
        print("claude-code sync:", sync(dry="--dry" in argv))
        return 0
    days = None
    if "--days" in argv:
        try:
            days = int(argv[argv.index("--days") + 1])
        except (ValueError, IndexError):
            pass
    by = "week"
    if "--by" in argv:
        try:
            by = argv[argv.index("--by") + 1]
        except IndexError:
            pass
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (ValueError, IndexError):
            pass
    if sub == "ingest":                                 # per-turn rows → local spend_events ledger (queryable, idempotent)
        return ingest_events(days=days, limit=limit, reset="--reset" in argv, dry="--dry" in argv)
    if sub == "overflow":                               # reconstruct billing_state — which turns billed past the weekly cap
        cap_usd = None
        if "--cap-usd" in argv:
            try:
                cap_usd = float(argv[argv.index("--cap-usd") + 1])
            except (ValueError, IndexError):
                pass
        anchor = argv[argv.index("--anchor") + 1] if "--anchor" in argv and argv.index("--anchor") + 1 < len(argv) else None
        return reconcile_overflow(cap_usd=cap_usd, anchor=anchor, dry="--dry" in argv)
    if sub == "context":                                # compaction view: sustained-large-context conversations + $/turn
        conv_id = argv[argv.index("--conv") + 1] if "--conv" in argv and argv.index("--conv") + 1 < len(argv) else None
        return context_cmd(conv_id=conv_id, top=limit or 10)
    if sub in ("conversations", "convs"):               # unified per-conversation view, labeled by human sidebar title
        return conversations_cmd(top=limit or 15)
    if sub == "classify":                               # classify sessions into org→team×project (caged, est-first)
        return classify(run="--run" in argv, days=days, recls="--reclassify" in argv)
    if sub == "work":                                   # conversation-derived work rows, bucketed by period
        return work(by=by, days=days)
    if sub == "story":                                  # caged narrative + private work-insights (estimate-first)
        return story(by=by, days=days or 7, run="--run" in argv)
    return show(days=days)
