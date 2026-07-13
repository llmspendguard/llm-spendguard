"""Lambda (Lambda Labs GPU cloud) spend adapter (gpu_port.GPUProvider) — GET /api/v1/instances.

Docs (the shapes this adapter parses):
  https://cloud.lambdalabs.com/api/v1/openapi.json   auth "Authorization: Bearer <API-KEY>"; GET /instances →
      {"data": [{id · name · status · region{name,description} · instance_type{name · description ·
      gpu_description · price_cents_per_hour · specs} · hostname · tags …}]}
  https://docs-api.lambda.ai/api/cloud               the Lambda Cloud API reference.

dph_usd = instance_type.price_cents_per_hour / 100 — the PROVIDER's own price field, never a local table.
HONESTY: the listing exposes NO launch/created timestamp, so an instance's runtime is UNKNOWN from a single
listing → every row is {"untimed": True}: visible (id/gpu/status/$-rate all surface), but it contributes
NOTHING to per-day $ math — fabricated hours would be worse than a gap. The documented recovery path for
runtime is Lambda's /api/v1/audit-events (launch/terminate events) or a first-seen snapshot cadence like
vast.ai's resources.snapshot(); neither is wired yet, so the reconcile shows this capture gap loudly instead
of a fake $0-clean ledger. Lambda documents no billing/usage endpoint → no account_total (truth UNKNOWN).
"""
import os
import json
import urllib.request

from . import config

LAMBDA_BASE = "https://cloud.lambdalabs.com/api/v1"
KEY_ENV = "LAMBDA_API_KEY"


def _get(path):
    req = urllib.request.Request(f"{LAMBDA_BASE}/{path}",
                                 headers={"Authorization": f"Bearer {os.environ.get(KEY_ENV, '')}"})
    with urllib.request.urlopen(req, timeout=20, context=config.ssl_context()) as r:
        return json.loads(r.read().decode())


class LambdaProvider:
    name = "lambdalabs"                                    # unambiguous (vs AWS Lambda), parallel to "vastai"

    def configured(self):
        return bool(os.environ.get(KEY_ENV))               # env only (keys.env lands here via config import)

    def instances(self, since_ts=None, now=None):
        """Normalized rows from GET /instances. NEVER raises — [] on any API/network failure (the vast.ai
        doctrine: a transient outage must not zero the set or error the reconcile)."""
        try:
            d = _get("instances")
        except Exception:
            return []
        out = []
        for i in (d or {}).get("data") or []:
            it = i.get("instance_type") or {}
            cents = it.get("price_cents_per_hour")
            row = {"id": str(i.get("id") or ""), "label": i.get("name") or "",
                   "gpu": it.get("gpu_description") or it.get("name") or "?",
                   "dph_usd": (float(cents) / 100.0) if cents not in (None, "") else None,
                   "status": i.get("status"), "region": ((i.get("region") or {}).get("name")) or "",
                   "start_ts": None, "end_ts": None,
                   "untimed": True}                        # no launch timestamp in the listing → runtime UNKNOWN
            if row["dph_usd"] is None:
                row["unpriced"] = True                     # no provider price → visible UNKNOWN, never $0
            out.append(row)
        return out


PROVIDER = LambdaProvider()


def source():
    """reconcile.Source factory for the gpu_port registry — None when unconfigured (silently skipped)."""
    if not PROVIDER.configured():
        return None
    from .gpu_port import ProviderGPUSource
    return ProviderGPUSource(PROVIDER)
