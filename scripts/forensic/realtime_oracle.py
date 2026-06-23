"""DEV-ONLY realtime oracle (needs OPENAI_ADMIN_KEY / ANTHROPIC_ADMIN_KEY) — the validation target for the
conversation-derived realtime tally that the SHIPPED client uses (no admin key).

The admin usage APIs are ORG-WIDE (the keys are shared beyond our dev work — other devs, the production app). So we
TIMING-MATCH hourly realtime usage to OUR conversations: an hour whose window contains a conversation segment is
attributed to that conversation's org (session_classification); an hour with no conversation is OTHER-ORG and
excluded. Magnitude = tokens × pricing.py (cache-discounted, validated: batch side == the batch ledgers 97-99.9%).
Realtime is split out via OpenAI's per-row `batch` flag and Anthropic's `service_tier`.

  python --env-file? no — run under the gated venv:  .venv.nosync/bin/python scripts/forensic/realtime_oracle.py
"""
import json, urllib.request, urllib.parse, datetime, sys
from collections import defaultdict
from spendguard.config import api_key
from spendguard import pricing, conv
from spendguard.resources import _norm_model

SINCE = "2026-06-01"
START = int(datetime.datetime.fromisoformat(SINCE + "T00:00:00+00:00").timestamp())


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


def openai_hourly():
    """{hour_unix: [ (model, in, cached, out) ]} for REALTIME rows only (batch flag false)."""
    k = api_key("OPENAI_ADMIN_KEY")
    if not k:
        return {}
    url = "https://api.openai.com/v1/organization/usage/completions?" + urllib.parse.urlencode(
        [("start_time", START), ("bucket_width", "1h"), ("limit", "168"), ("group_by[]", "model"), ("group_by[]", "batch")])
    by = defaultdict(list)
    for b in _paged(url, {"Authorization": "Bearer " + k}, "page"):
        hour = int(b.get("start_time") or 0)
        for r in b.get("results", []):
            if r.get("batch"):
                continue                                   # batch is in the ledger; realtime only here
            by[hour].append((_norm_model(r.get("model") or "?"), int(r.get("input_tokens") or 0),
                             int(r.get("input_cached_tokens") or 0), int(r.get("output_tokens") or 0)))
    return by


def anthropic_hourly():
    k = api_key("ANTHROPIC_ADMIN_KEY")
    if not k:
        return {}
    url = "https://api.anthropic.com/v1/organizations/usage_report/messages?" + urllib.parse.urlencode(
        [("starting_at", SINCE + "T00:00:00Z"), ("bucket_width", "1h"), ("limit", "168"),
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


def conversation_hours():
    """{hour_unix: {org: weight}} — which org's conversations were active each hour (from segment timestamps +
    session_classification). The timing key that connects org-wide usage to OUR work."""
    by = defaultdict(lambda: defaultdict(int))
    for s in conv.segments():
        ts = s.get("ts") or ""
        try:
            u = int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        hour = u - (u % 3600)
        c = conv.session_classification(s["sid"]) or {}
        org = c.get("org") or s.get("project_prior") or ""
        if org:
            by[hour][org] += 1
    return by


def main():
    oai, anth = openai_hourly(), anthropic_hourly()
    usage = defaultdict(list)
    for h, rows in oai.items():
        usage[h] += rows
    for h, rows in anth.items():
        usage[h] += rows
    convh = conversation_hours()
    ours = defaultdict(float)          # org -> $ (timing-matched to our conversations)
    other = 0.0                        # org-wide realtime in hours with NO conversation of ours
    ceiling = 0.0
    for hour, rows in usage.items():
        hour_usd = sum(pricing.realtime_cost(m, i, o, cached_in_tok=c) for (m, i, c, o) in rows)
        ceiling += hour_usd
        active = convh.get(hour) or convh.get(hour - 3600) or {}   # allow a 1h lag (logging delay)
        if active:
            org = max(active, key=active.get)              # dominant org active that hour
            ours[org] += hour_usd
        else:
            other += hour_usd
    print("=== REALTIME timing-matched oracle (ours vs other-org) ===")
    print("  org-wide realtime CEILING: $%.2f" % ceiling)
    print("  OURS (hours with our conversations), by org:")
    for org, v in sorted(ours.items(), key=lambda x: -x[1]):
        print("    %-12s $%.2f" % (org, v))
    print("  OURS total: $%.2f" % sum(ours.values()))
    print("  OTHER-ORG (no conversation in that hour): $%.2f" % other)
    print("  (gate-captured realtime was $17.76; conversation-derived tally must reproduce the OURS total)")


if __name__ == "__main__":
    sys.exit(main())
