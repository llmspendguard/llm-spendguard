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


def _paged(url, headers, page_param):
    out, page = [], None
    for _ in range(80):
        u = url + ((("&%s=" % page_param) + urllib.parse.quote(page)) if page else "")
        with urllib.request.urlopen(urllib.request.Request(u, headers=headers), timeout=90) as r:
            d = json.loads(r.read())
        out += d.get("data", [])
        nxt = d.get("next_page")
        if d.get("has_more") and nxt:
            page = nxt
        else:
            break
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
                             int(r.get("input_cached_tokens") or 0), int(r.get("output_tokens") or 0)))
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
    for b in _paged(url, {"x-api-key": k, "anthropic-version": "2023-06-01"}, "page"):
        hour = int(datetime.datetime.fromisoformat(b["starting_at"].replace("Z", "+00:00")).timestamp())
        for r in b.get("results", []):
            if "batch" in (r.get("service_tier") or "").lower():
                continue
            by[hour].append((_norm_model(r.get("model") or "?"),
                             int(r.get("uncached_input_tokens") or r.get("input_tokens") or 0),
                             int(r.get("cache_read_input_tokens") or 0), int(r.get("output_tokens") or 0)))
    return by


def _conversation_hours(since):
    """{hour_unix: {(org, project): weight}} — which org+project's conversation segments were active each hour
    (session_classification → the timing key that connects org-wide usage to OUR work)."""
    from . import conv
    by = defaultdict(lambda: defaultdict(int))
    start = _start_ts(since)
    for s in conv.segments():
        ts = s.get("ts") or ""
        try:
            u = int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        if u < start:
            continue
        hour = u - (u % 3600)
        c = conv.session_classification(s["sid"]) or {}
        org = (c.get("org") or "").strip()
        proj = (c.get("project") or s.get("project_prior") or "").strip().lower()
        if org and proj:
            by[hour][(org, proj)] += 1
    return by


def by_project_day(since):
    """The realtime TRUTH, timing-matched to OUR conversations → {(project, provider, day): usd} + diagnostics.
    Each hour of org-wide realtime usage is attributed to the (org, project) whose segments were active that hour
    (1h lag tolerated for logging delay); hours with no conversation of ours are OTHER-org and excluded. Magnitude =
    tokens × pricing.py (cache-discounted). Returns (rows, meta) where meta has ours_total/other/ceiling/by_org."""
    from . import pricing
    oai, anth = openai_hourly(since), anthropic_hourly(since)
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
        dom = max(active, key=active.get) if active else None          # dominant (org, project) that hour
        for (prov, m, i, c, o) in rows:
            usd = pricing.realtime_cost(m, i, o, cached_in_tok=c)
            ceiling += usd
            if dom:
                org, proj = dom
                out[(proj, prov, day)] += usd
                by_org[org] += usd
            else:
                other += usd
    rows_out = [{"project": p, "provider": pv, "day": d, "cost": round(v, 6)} for (p, pv, d), v in out.items()]
    meta = {"ours_total": round(sum(by_org.values()), 2), "other_org": round(other, 2),
            "ceiling": round(ceiling, 2), "by_org": {k: round(v, 2) for k, v in by_org.items()}}
    return rows_out, meta
