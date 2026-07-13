"""Modal spend adapter (gpu_port.GPUProvider) — workspace billing report via Modal's DOCUMENTED usage API.

Docs (the shapes this adapter consumes):
  https://modal.com/docs/reference/modal.billing   modal.billing.workspace_billing_report(start, end=None,
      resolution="d") → WorkspaceBillingReportItem: object_id · description · environment_name ·
      interval_start (UTC datetime) · cost (Decimal) · tags. Team/Enterprise plans.
  https://modal.com/docs/guide/billing             the report is the workspace's billed usage.
  https://modal.com/docs/reference/modal.config    auth = MODAL_TOKEN_ID + MODAL_TOKEN_SECRET.

Modal exposes NO public REST usage endpoint and no per-instance $/hr — its documented programmatic usage
surface is the SDK's billing report, so this adapter calls that (lazy import; SDK absent → unconfigured →
silently skipped). Each report row is Modal's own BILLED $ per app per UTC day: rows carry {"usd": …} (exact
provider truth — never re-derived from a rate) with label = the app description, so config
`resources.modal.label_map` routes apps → projects. GPU type isn't exposed by the report → gpu "?" (visible
unknown). `account_total` sums the same report — for Modal the report IS the bill, so captured reconciles to
truth by construction (residual ≈ 0 states "capture is the bill", not a rigged fixture).
"""
import os
import datetime

from . import config  # noqa: F401  (keys.env → os.environ at import, the same route every provider key takes)

TOKEN_ID_ENV, TOKEN_SECRET_ENV = "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"
_RESOLUTION = "d"                                          # daily rows — one UTC day per report item


def _report(since_ts):
    """Documented SDK call (see module docstring). Isolated for tests: canned items monkeypatch here."""
    import modal.billing
    start = datetime.datetime.fromtimestamp(since_ts, tz=datetime.timezone.utc)
    return modal.billing.workspace_billing_report(start=start, resolution=_RESOLUTION)


class ModalProvider:
    name = "modal"

    def configured(self):
        """Both documented token halves present (env only; keys.env lands there via config import) AND the
        SDK importable — its billing API is the documented usage surface, so no SDK means nothing to read."""
        if not (os.environ.get(TOKEN_ID_ENV) and os.environ.get(TOKEN_SECRET_ENV)):
            return False
        try:
            import modal.billing  # noqa: F401
            return True
        except Exception:
            return False

    def instances(self, since_ts=None, now=None):
        """Normalized rows from the billing report — one row per (app object, UTC day), carrying Modal's own
        billed $ (`usd`). NEVER raises: [] on any failure (plan without the API, network, auth) so a transient
        problem can't zero a ledger or error the reconcile."""
        from . import gpu_port
        since_ts = since_ts if since_ts is not None else gpu_port.month_start_ts()
        try:
            items = _report(since_ts) or []
        except Exception:
            return []
        out = []
        for it in items:
            iv = getattr(it, "interval_start", None)
            if iv is None:
                continue
            if iv.tzinfo is None:                          # docs: timestamps are UTC (naive = UTC)
                iv = iv.replace(tzinfo=datetime.timezone.utc)
            ts = iv.timestamp()
            out.append({"id": str(getattr(it, "object_id", "") or ""),
                        "label": str(getattr(it, "description", "") or ""),
                        "gpu": "?",                        # the report is per-app $; GPU type isn't exposed
                        "dph_usd": None,                   # no hourly rate — the report IS the billed $
                        "usd": float(getattr(it, "cost", 0) or 0),
                        "start_ts": ts, "end_ts": ts + 86400,          # resolution="d" → one UTC day per row
                        "environment": str(getattr(it, "environment_name", "") or "")})
        return out

    def account_total(self, since=None):
        """Σ of the same billed report over the window — Modal's report IS the provider bill. None (UNKNOWN,
        never $0) when it can't be read."""
        from . import gpu_port
        rows = self.instances(since_ts=gpu_port._since_ts(since))
        if not rows:
            return None
        return round(sum(r["usd"] for r in rows), 2)


PROVIDER = ModalProvider()


def source():
    """reconcile.Source factory for the gpu_port registry — None when unconfigured (silently skipped)."""
    if not PROVIDER.configured():
        return None
    from .gpu_port import ProviderGPUSource
    return ProviderGPUSource(PROVIDER)
