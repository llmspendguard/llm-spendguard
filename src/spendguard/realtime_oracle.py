"""Admin-usage realtime oracle — the HISTORICAL realtime truth, timing-matched to OUR conversations per project.

Realtime $ is NOT in the regular provider key (batch only) and is NOT reconstructable from transcripts (proven: the
tokens were never printed). The ONE source is the provider ADMIN usage API (tokens × pricing.py). Those keys are
ORG-WIDE / shared, so we TIMING-MATCH each hour of realtime usage to the org+project whose conversation segments were
active that hour (session_classification) — an hour with no conversation of ours is OTHER-org and excluded.

This is the package home for that logic (the `scripts/forensic/realtime_oracle.py` printer is a thin wrapper). It is
DEV-only — gated behind SPENDGUARD_ADMIN_ORACLE + the admin keys — and its OUTPUT is RECORDED into the ledger by
`ledger_sync.reconcile_realtime` so the client (no admin key) then pushes it like any other spend. Record once, never
re-derive. The FORWARD path (no admin key) is the gate's inline true-up (records actual tokens at call time).
"""
import json
import urllib.request
import urllib.parse
import datetime
from collections import defaultdict


def _paged(url, headers, page_param, max_pages=80):
    out, page = [], None
    for _ in range(max_pages):
        u = url + ((("&%s=" % page_param) + urllib.parse.quote(page)) if page else "")
        with urllib.request.urlopen(urllib.request.Request(u, headers=headers), timeout=90) as r:
            d = json.loads(r.read())
        out += d.get("data", [])
        nxt = d.get("next_page")
        if d.get("has_more") and nxt:
            page = nxt
        else:
            break
    else:
        # Reached the page cap with the provider STILL paginating. Silently returning a truncated slice would
        # undercount realtime truth and read as "less spend" — the exact failure this module exists to prevent.
        # Fail LOUD; by_project_day catches it and records it in meta['errors'] (a named gap, never a silent $0).
        raise RuntimeError(f"realtime usage exceeded {max_pages} pages and is still paginating — results would be "
                           f"TRUNCATED; raise max_pages or narrow the window (since={url.split('=')[0]}…)")
    return out


def _start_ts(since):
    return int(datetime.datetime.fromisoformat(since + "T00:00:00+00:00").timestamp())


def openai_hourly(since):
    """{hour_unix: [(model, in, cached, out)]} for REALTIME rows only (batch flag false)."""
    from .config import api_key
    from .resources import _norm_model
    k = api_key("OPENAI_ADMIN_KEY")
    if not k:
        return {}
    url = "https://api.openai.com/v1/organization/usage/completions?" + urllib.parse.urlencode(
        [("start_time", _start_ts(since)), ("bucket_width", "1h"), ("limit", "168"),
         ("group_by[]", "model"), ("group_by[]", "batch")])
    by = defaultdict(list)
    for b in _paged(url, {"Authorization": "Bearer " + k}, "page"):
        hour = int(b.get("start_time") or 0)
        for r in b.get("results", []):
            if r.get("batch"):
                continue                                   # batch is already in the ledger; realtime only here
            by[hour].append((_norm_model(r.get("model") or "?"), int(r.get("input_tokens") or 0),
                             int(r.get("input_cached_tokens") or 0), int(r.get("output_tokens") or 0),
                             0))                          # cache-creation slot = 0 (OpenAI bills no separate cache-write)
    return by


def anthropic_hourly(since):
    from .config import api_key
    from .resources import _norm_model
    k = api_key("ANTHROPIC_ADMIN_KEY")
    if not k:
        return {}
    url = "https://api.anthropic.com/v1/organizations/usage_report/messages?" + urllib.parse.urlencode(
        [("starting_at", since + "T00:00:00Z"), ("bucket_width", "1h"), ("limit", "168"),
         ("group_by[]", "model"), ("group_by[]", "service_tier")])
    by = defaultdict(list)
    skipped = 0
    for b in _paged(url, {"x-api-key": k, "anthropic-version": "2023-06-01"}, "page"):
        sa = b.get("starting_at")
        if not sa:
            skipped += 1                                 # skip a malformed bucket rather than abort the whole provider parse…
            continue
        hour = int(datetime.datetime.fromisoformat(sa.replace("Z", "+00:00")).timestamp())
        for r in b.get("results", []):
            if "batch" in (r.get("service_tier") or "").lower():
                continue
            by[hour].append((_norm_model(r.get("model") or "?"),
                             int(r.get("uncached_input_tokens") or r.get("input_tokens") or 0),
                             int(r.get("cache_read_input_tokens") or 0), int(r.get("output_tokens") or 0),
                             int(r.get("cache_creation_input_tokens") or 0)))   # cache-WRITE (billed ~1.25x); was omitted → undercount
    if skipped:
        # …but NEVER silently: a dropped bucket is dropped realtime spend. Leave a trace (the module's rule — a
        # missing unit of work is surfaced, never a silent $0), so the excluded usage is visible, not invisible.
        from .config import warn_once
        warn_once(f"[realtime_oracle] {skipped} anthropic usage bucket(s) missing 'starting_at' were SKIPPED — "
                  f"their realtime usage is EXCLUDED from the total (the provider returned malformed buckets).")
    return by


def _conversation_hours(since):
    """{hour_unix: {(org, project): weight}} — which org+project's conversation segments were active each hour
    (session_classification → the timing key that connects org-wide usage to OUR work)."""
    from . import conv
    by = defaultdict(lambda: defaultdict(int))
    start = _start_ts(since)
    skipped = 0
    for s in conv.segments():
        ts = s.get("ts") or ""
        try:
            u = int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        if u < start:
            continue
        hour = u - (u % 3600)
        sid = s.get("sid")
        if not sid:
            skipped += 1                                 # a segment with no session id can't be classified — skip, don't KeyError-abort the oracle
            continue
        c = conv.session_classification(sid) or {}
        org = (c.get("org") or "").strip()
        proj = (c.get("project") or s.get("project_prior") or "").strip().lower()
        if org and proj:
            by[hour][(org, proj)] += 1
    if skipped:
        from .config import warn_once
        warn_once(f"[realtime_oracle] {skipped} conversation segment(s) without a session id were skipped from the "
                  f"timing-match (their hours are not attributed) — surfaced, not hidden.")
    return by


def by_project_day(since):
    """The realtime TRUTH, timing-matched to OUR conversations → {(project, provider, day): usd} + diagnostics.
    Each hour of org-wide realtime usage is attributed to the (org, project) whose segments were active that hour
    (1h lag tolerated for logging delay); hours with no conversation of ours are OTHER-org and excluded. Magnitude =
    tokens × pricing.py (cache-discounted). Returns (rows, meta) where meta has ours_total/other/ceiling/by_org."""
    from . import pricing
    # A provider's hourly fetch (urlopen → network / HTTP / JSON) can raise; letting it propagate aborted the
    # WHOLE oracle run. Guard each independently and surface the failure in meta['errors'] — a partial oracle is
    # honest (some truth, and a named gap), never a silent $0 that reads as "no realtime spend".
    _errors = {}
    try:
        oai = openai_hourly(since)
    except Exception as e:
        oai, _errors["openai"] = {}, f"{type(e).__name__}: {str(e)[:80]}"
    try:
        anth = anthropic_hourly(since)
    except Exception as e:
        anth, _errors["anthropic"] = {}, f"{type(e).__name__}: {str(e)[:80]}"
    usage = defaultdict(list)
    prov_of = {}
    for h, rows in oai.items():
        usage[h] += [("openai",) + r for r in rows]
    for h, rows in anth.items():
        usage[h] += [("anthropic",) + r for r in rows]
    convh = _conversation_hours(since)
    out = defaultdict(float)               # (project, provider, day) -> $
    by_org = defaultdict(float)
    other = ceiling = 0.0
    for hour, rows in usage.items():
        day = datetime.datetime.fromtimestamp(hour, datetime.timezone.utc).strftime("%Y-%m-%d")
        active = convh.get(hour) or convh.get(hour - 3600) or {}        # 1h lag for logging delay
        total_active = sum(active.values())                             # Σ segment-activity across (org,project)s
        for (prov, m, i, c, o, cc) in rows:
            # An unpriceable model must not abort the whole realtime-truth computation. cost_or_unpriced returns 0
            # and RECORDS the model (note_unpriced) so the gap is surfaced, rather than raising here. `cc` = the
            # Anthropic cache-CREATION tokens now folded in (billed ~1.25x; previously dropped → undercount).
            usd = pricing.cost_or_unpriced(m, i, o, cached_in_tok=c, batch=False, cache_creation_tok=cc)
            ceiling += usd
            if total_active > 0:
                # SPLIT this hour's usage across EVERY (org,project) active that hour, proportional to its
                # activity. Winner-take-all (max) credited a single dominant project and dropped every other
                # active project's share of a shared, org-wide hour — losing attribution the mission depends on.
                for (org, proj), cnt in active.items():
                    share = usd * (cnt / total_active)
                    out[(proj, prov, day)] += share
                    by_org[org] += share
            else:
                other += usd
    rows_out = [{"project": p, "provider": pv, "day": d, "cost": round(v, 6)} for (p, pv, d), v in out.items()]
    meta = {"ours_total": round(sum(by_org.values()), 2), "other_org": round(other, 2),
            "ceiling": round(ceiling, 2), "by_org": {k: round(v, 2) for k, v in by_org.items()},
            "errors": _errors}          # non-empty → a provider fetch failed; the totals are PARTIAL, not complete
    return rows_out, meta
