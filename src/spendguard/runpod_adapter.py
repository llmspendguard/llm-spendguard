"""RunPod GPU spend adapter (gpu_port.GPUProvider) — pods via RunPod's documented GraphQL API.

Docs (the shapes this adapter parses):
  https://graphql-spec.runpod.io/          Pod: costPerHr Float · name · id · desiredStatus · createdAt
                                           (RFC3339 DateTime) · machine{gpuDisplayName} · runtime{uptimeInSeconds};
                                           Query.myself → User{pods}; auth "Authorization: Bearer <RUNPOD_API_KEY>".
  https://docs.runpod.io/sdks/graphql/manage-pods   the `myself { pods { … } }` listing query.

Cost comes ONLY from RunPod's own `costPerHr` — never a local $/hr table. Billable windows, honestly:
  • RUNNING pod → its CURRENT session uptime (now − runtime.uptimeInSeconds → now, end_ts=None).
  • stopped/exited pod → this listing does NOT expose past GPU runtime, so the row is returned
    {"untimed": True}: visible UNKNOWN, never fabricated hours, never a silent $0. (RunPod's audit log is
    the documented recovery path for past lifecycles, but the GraphQL spec documents `auditLogs` on
    Impersonation — not on User — so it is not queried here.)
  • pod with no costPerHr → {"unpriced": True}, same visibility rule.
RunPod exposes no period-billing total on User (only clientBalance / currentSpendPerHr), so there is no
account_total: the reconcile shows truth UNKNOWN rather than pretending the capture is the bill.
"""
import os
import json
import time
import datetime
import urllib.request

from . import config

RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"
KEY_ENV = "RUNPOD_API_KEY"

# fields per https://graphql-spec.runpod.io/ (Pod, PodMachineInfo, PodRuntime)
_PODS_QUERY = ("query Pods { myself { pods { id name costPerHr desiredStatus createdAt "
               "machine { gpuDisplayName } runtime { uptimeInSeconds } } } }")


def _graphql(query):
    req = urllib.request.Request(
        RUNPOD_GRAPHQL, data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {os.environ.get(KEY_ENV, '')}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=config.ssl_context()) as r:
        return json.loads(r.read().decode())


def _rfc3339_ts(s):
    """RunPod DateTime (RFC3339, e.g. 2007-12-03T10:15:30Z) → unix ts, or None."""
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class RunPodProvider:
    name = "runpod"

    def configured(self):
        return bool(os.environ.get(KEY_ENV))               # env only (keys.env lands here via config import)

    def instances(self, since_ts=None, now=None):
        """Normalized rows from myself.pods. NEVER raises — [] on any API/network failure (a transient outage
        must not zero the GPU set or error the reconcile; the vast.ai doctrine)."""
        try:
            d = _graphql(_PODS_QUERY)
        except Exception:
            return []
        pods = ((((d or {}).get("data") or {}).get("myself") or {}).get("pods")) or []
        now = now or time.time()
        out = []
        for p in pods:
            up = float(((p.get("runtime") or {}).get("uptimeInSeconds")) or 0)
            dph = p.get("costPerHr")
            row = {"id": str(p.get("id") or ""), "label": p.get("name") or "",
                   "gpu": ((p.get("machine") or {}).get("gpuDisplayName")) or "?",
                   "dph_usd": float(dph) if dph not in (None, "") else None,
                   "status": p.get("desiredStatus"), "created_ts": _rfc3339_ts(p.get("createdAt"))}
            if row["dph_usd"] is None:
                row["unpriced"] = True                     # no provider price → visible UNKNOWN, never $0
            if up > 0 and str(p.get("desiredStatus") or "").upper() == "RUNNING":
                row["start_ts"], row["end_ts"] = now - up, None    # current billed session
            else:
                row["start_ts"], row["end_ts"], row["untimed"] = None, None, True   # past runtime not exposed
            out.append(row)
        return out


PROVIDER = RunPodProvider()


def source():
    """reconcile.Source factory for the gpu_port registry — None when unconfigured (silently skipped)."""
    if not PROVIDER.configured():
        return None
    from .gpu_port import ProviderGPUSource
    return ProviderGPUSource(PROVIDER)
