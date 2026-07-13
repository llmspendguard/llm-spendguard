"""GPU-provider PORT — the explicit contract every remote-compute spend source implements, plus the ONE
per-UTC-day cost-splitting math (extracted from the vast.ai implementation, which remains the reference
adapter: resources.py calls back into `day_slices` here, so vast and every other provider split identically).

A provider adapter (runpod_adapter / modal_adapter / lambda_adapter / a third-party plugin) implements
`GPUProvider` and registers a `reconcile.Source` factory via `register_source` — the SAME registry
`reconcile.all_sources` iterates for vast.ai, so `spendguard reconcile all` includes any configured provider
with zero special-casing. An UNCONFIGURED provider (no key) is silently skipped: never an error, never fake data.

NORMALIZED instance row (what every adapter's `instances()` returns):
  id (str) · label (str — routes to a project via config `resources.<provider>.label_map`, exactly like
  vast.ai labels) · gpu (str, "?" when the provider doesn't expose it) · dph_usd (float $/hour, ONLY from the
  PROVIDER's own billing field — never a local price table) · start_ts (unix, None when the provider doesn't
  expose when the box started) · end_ts (unix, None = still running).
  Optional honesty markers — UNKNOWN stays visible, never $0-clean:
    usd:      the provider's own BILLED $ for this row (e.g. a Modal daily billing-report row). When present
              it IS the cost — booked whole to the UTC day of start_ts (adapters emitting `usd` must emit
              per-day rows; splitting a billed total would fabricate).
    unpriced: True — the provider exposed no price for this row. The row stays visible; it contributes
              NOTHING to $ math (unknown ≠ $0).
    untimed:  True — the provider exposed no runtime window (e.g. Lambda's listing has no launch timestamp).
              Same rule: visible, never fabricated hours.
"""
import sys
import time
import datetime
import importlib
from typing import Protocol

from . import config


class GPUProvider(Protocol):
    """The port. `configured()` = key material present in the environment (keys.env is loaded into os.environ
    at import by config.load_key_files, same route as VAST_API_KEY) — False means SKIP silently. `instances()`
    returns normalized rows (module docstring) and NEVER raises: any API/network failure returns [] so a
    transient outage can't zero a ledger or error the reconcile (the vast.ai doctrine). `since_ts` is an
    optional window hint for report-style providers (Modal); listing-style providers ignore it."""
    name: str

    def configured(self) -> bool: ...

    def instances(self, since_ts=None) -> list: ...


# ── the ONE per-UTC-day splitting math (vast.ai's, extracted — resources.gpu_rows_by_day calls this) ──────────

def day_slices(start_ts, end_ts, since_ts=None):
    """Split the run window [max(start_ts, since_ts), end_ts) across UTC days → [(YYYY-MM-DD, hours_that_day)].
    Walks day by day clipping to each UTC midnight — identical to the loop vast.ai always used, now shared so
    every provider's dph×hours lands on the same days."""
    t = max(start_ts, since_ts) if since_ts is not None else start_ts
    out = []
    while t < end_ts:
        day = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")
        d0 = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()
        de = d0 + 86400
        out.append((day, (min(end_ts, de) - t) / 3600.0))
        t = de
    return out


def month_start_ts():
    """Start of the current UTC month — the default reconcile window every GPU source shares (vast's default)."""
    t = datetime.datetime.now(datetime.timezone.utc)
    return datetime.datetime(t.year, t.month, 1, tzinfo=datetime.timezone.utc).timestamp()


def _since_ts(since):
    """Window start as a unix ts: None → current UTC month start; 'YYYY-MM-DD' → that UTC midnight; number → as-is."""
    if since is None:
        return month_start_ts()
    if isinstance(since, (int, float)):
        return float(since)
    return datetime.datetime.strptime(str(since)[:10], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()


def _utc_day(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def cost_by_day(instances, since=None, now=None):
    """{YYYY-MM-DD: $} for NORMALIZED instance rows. Provider-billed rows (`usd` present) book whole to the UTC
    day of start_ts (they are already per-interval — splitting a billed $ would fabricate). Rate rows split
    dph_usd × hours across UTC days via `day_slices` — the SAME math vast.ai uses. Unpriced/untimed rows
    contribute NOTHING (unknown ≠ $0); they stay visible in the adapter's instances()."""
    now = now or time.time()
    since = _since_ts(since)
    out = {}
    for i in instances:
        start = i.get("start_ts")
        if i.get("usd") is not None:                       # the provider's own billed $ — exact, never re-derived
            if start and start >= since:
                day = _utc_day(start)
                out[day] = round(out.get(day, 0.0) + float(i["usd"]), 6)
            continue
        dph = i.get("dph_usd")
        if not dph or not start:                           # unpriced / untimed → UNKNOWN, not $0 (visible on the row)
            continue
        end = min(i.get("end_ts") or now, now)
        for day, hours in day_slices(float(start), end, since):
            out[day] = round(out.get(day, 0.0) + float(dph) * hours, 6)
    return out


# ── label → project attribution (the vast.ai pattern, per provider) ───────────────────────────────────────────

def label_map(provider):
    """Config `resources.<provider>.label_map` ({substring: project}) → [(substring_lower, project)]. EMPTY by
    default ON PURPOSE (an opinionated default would silently mis-attribute a stranger's instance — the same
    doctrine as vast's DEFAULT_LABEL_MAP). Each user sets their own, e.g. {"train": "ml-pipeline"}."""
    cfg = config._cfg_get("resources", provider, {}) or {}
    m = cfg.get("label_map") or {} if isinstance(cfg, dict) else {}
    return [(str(k).lower(), v) for k, v in m.items()]


def project_of(label, lmap):
    """First label_map substring match wins; unknown label → "" (untagged — surfaced, never guessed)."""
    lab = (label or "").lower()
    for sub, proj in lmap:
        if sub in lab:
            return proj
    return ""


def rows_by_day(provider, since=None, now=None):
    """Per (project, gpu, day) $ rows for a port adapter — the SAME row shape resources.gpu_rows_by_day emits
    for vast ({project, gpu, day, cost, hours, instances}), so provider rows flow through reconcile/rollups
    identically. Attribution: instance label → project via `resources.<provider>.label_map`. Unpriced/untimed
    rows are EXCLUDED from $ (unknown ≠ $0) but stay visible via the adapter's instances()."""
    now = now or time.time()
    since_ts = _since_ts(since)
    lmap = label_map(provider.name)
    agg = {}

    def add(proj, gpu, day, cost, hours, iid):
        a = agg.setdefault((proj, gpu, day), {"project": proj, "gpu": gpu, "day": day,
                                              "cost": 0.0, "hours": 0.0, "instances": set()})
        a["cost"] += cost
        a["hours"] += hours
        if iid:
            a["instances"].add(str(iid))

    for i in provider.instances(since_ts=since_ts):
        proj = project_of(i.get("label"), lmap)
        gpu = i.get("gpu") or "?"
        start = i.get("start_ts")
        if i.get("usd") is not None:                       # provider-billed per-day row (e.g. Modal report)
            if start and start >= since_ts:
                end = min(i.get("end_ts") or now, now)
                add(proj, gpu, _utc_day(start), float(i["usd"]), max(0.0, (end - start) / 3600.0), i.get("id"))
            continue
        dph = i.get("dph_usd")
        if not dph or not start:
            continue
        end = min(i.get("end_ts") or now, now)
        for day, hours in day_slices(float(start), end, since_ts):
            add(proj, gpu, day, float(dph) * hours, hours, i.get("id"))
    rows = []
    for a in agg.values():
        a["instances"] = sorted(a["instances"])
        a["cost"] = round(a["cost"], 6)
        a["hours"] = round(a["hours"], 2)
        rows.append(a)
    return rows


# ── reconcile.Source over a port adapter (the same shape vast's GPUSource plugs into reconcile.run) ───────────

class ProviderGPUSource:
    """reconcile.Source for a GPUProvider: captured = per-day rows priced ONLY from the provider's own fields,
    attributed by label → project; truth = the provider's account-level bill when it exposes one and this
    connection owns the account (`account_total(since)`, optional on the provider) — else None, an EXPLICIT
    UNKNOWN the reconcile surfaces (a missing bill must never read as $0 / fully reconciled). Gap recovery is
    explicit per provider, so attribute_gap returns [] (the vast GPUSource doctrine)."""

    def __init__(self, provider, conn=None):
        from . import saas
        self.provider = provider
        self.name = f"gpu:{provider.name}"
        self._conn = conn if conn is not None else saas.conn()

    def conn(self):
        return self._conn

    def truth_total(self, since=None):
        if not self._conn.get("owns_account"):             # shared-account anchor: only the owner reconciles
            return 0.0
        fn = getattr(self.provider, "account_total", None)
        return fn(since) if fn else None                   # None = no bill exposed → UNKNOWN, surfaced

    def captured(self, since=None):
        return [r for r in rows_by_day(self.provider, since) if r["cost"] > 0]

    def attribute_gap(self, gap, since=None):
        return []


# ── the registry reconcile.all_sources iterates (vast.ai + port adapters + third-party plugins) ───────────────

_SOURCES = {}          # key → zero-arg factory returning a reconcile.Source, or None = unconfigured → skipped
_BUILTINS_DONE = False


def register_source(key, factory):
    """Add a GPU spend source to the registry `reconcile.all_sources` iterates. `factory` is zero-arg and
    returns a reconcile.Source, or None when its provider isn't configured (then it is silently skipped —
    never an error, never fake data). Third-party packages call this from their `spendguard.providers`
    entry-point activate(), riding `spendguard reconcile all` with zero special-casing."""
    _SOURCES[key] = factory


def _register_builtins():
    """vast.ai keeps its historical registry key ("gpu" — dashboards/tests key on it); the port adapters
    register as gpu:<provider>. Fail-open per adapter (the provider_plugins doctrine): a broken adapter module
    warns and is skipped — it can never break the reconcile or the other providers."""
    global _BUILTINS_DONE
    if _BUILTINS_DONE:
        return
    _BUILTINS_DONE = True

    def _vast():
        from . import resources                            # lazy: avoids module cycles
        return resources.GPUSource()

    register_source("gpu", _vast)
    for mod_name in ("runpod_adapter", "modal_adapter", "lambda_adapter"):
        try:
            mod = importlib.import_module(f".{mod_name}", __package__)
            register_source(f"gpu:{mod.PROVIDER.name}", mod.source)
        except Exception as e:
            print(f"[spendguard] WARN gpu adapter {mod_name} failed to load (skipped): {e}", file=sys.stderr)


def sources():
    """{registry_key: factory} for every registered GPU spend source, built-ins first. reconcile.all_sources
    calls each factory inside its own try (one failing source becomes {error}, the rest still reconcile)."""
    _register_builtins()
    return dict(_SOURCES)
