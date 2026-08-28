"""Lane QUOTA — the normalized, cross-lane view of how much subscription quota each lane has left, and the shared
cache that keeps reading each provider's quota surface cheap.

Each lane executor exposes `usage()` → a list of BUCKETS [{bucket, remaining_pct, reset_ts}] (provider TRUTH where
the CLI/API exposes it, None where it does not — never an invented gauge). The surfaces differ per provider and are
read two honest ways:
  • STATUS-POLL (gemini: `agy /usage`; claude-code: `claude -p /usage`) — a pure status command, parsed + cached;
  • OPPORTUNISTIC CAPTURE (codex: the `codex.rate_limits` events the CLI records in its logs sqlite on every real
    call — `exec --json` itself emits only token counts) — read from what real traffic already left behind, no extra
    call; None until traffic has populated it.
  • NO SURFACE (zai-coding): VERIFIED — the z.ai coding-plan endpoint returns no rate-limit headers and there is no
    status command, so usage() stays None (quota UNKNOWN) and routing falls back to the utilisation proxy for it.
    A gauge is never invented where the provider exposes nothing.

This module holds only the PROVIDER-AGNOSTIC pieces: the freshness cache (with the reset-boundary invalidation the
gemini oracle proved out) and the collapse from buckets to a single headroom figure. The per-provider PARSERS live
with their executors; the per-lane AGGREGATION (lanes → usage → headroom) lives in lanes.py, which already imports
every executor (keeping this module import-cycle-free). A lane whose provider exposes nothing is 'quota UNKNOWN',
which display and routing must treat as DISTINCT from 'exhausted'.
"""
import time


def cached_usage(cache, ttl_s, fetch):
    """Return fetch()'s buckets, cached in `cache` (a dict with keys 'at'/'val') for ttl_s — with the SAME two
    freshness bounds the gemini quota oracle proved out: the TTL, AND the soonest reset the cached snapshot itself
    reported. Once we are past that reset the quota has refilled by definition, so a stale-EXHAUSTED read can never
    mask a recovered lane (the harmful direction). fetch() returns buckets or None; any exception → None (fail safe,
    ordinary handling), and the result (including None) is cached so a burst of callers costs one read."""
    now = time.time()
    val = cache.get("val")
    fresh = now - cache.get("at", 0.0) < ttl_s
    if fresh and val:                                  # invalidate a cached snapshot once past the reset it promised
        soonest = min((float(b.get("reset_ts") or 0) for b in val), default=0.0)
        if soonest and now >= soonest:
            fresh = False
    if fresh:
        return val
    try:
        val = fetch()
    except Exception:
        val = None
    cache["at"], cache["val"] = now, val
    return val


def bucket_headroom(buckets):
    """Collapse a lane's buckets to one headroom figure: {remaining_pct (the MINIMUM — the tightest/binding bucket),
    reset_ts (soonest reset among the buckets AT that minimum — when the binding constraint refills)}. None when
    buckets is None (quota UNKNOWN — not the same as 0% remaining). A bucket with no remaining_pct is treated as
    full (100), so a partial report never invents scarcity."""
    if not buckets:
        return None
    rem = min(int(b.get("remaining_pct", 100)) for b in buckets)
    resets = [float(b["reset_ts"]) for b in buckets
              if int(b.get("remaining_pct", 100)) == rem and b.get("reset_ts")]
    return {"remaining_pct": rem, "reset_ts": (min(resets) if resets else None)}
