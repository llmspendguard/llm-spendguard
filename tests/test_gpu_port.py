"""GPU-provider PORT (gpu_port) + the RunPod / Modal / Lambda adapters — shared per-UTC-day splitting math
(equivalence-checked against the vast.ai implementation it was extracted from), documented-shape payload
parsing per adapter, the configured()==False silent-skip path, unpriced/untimed honesty (UNKNOWN is visible,
never $0-clean), and registry inclusion in reconcile.all_sources.

OFFLINE-TESTED against the providers' DOCUMENTED response shapes (fixture doc URLs cited inline), NOT
live-verified against real RunPod/Modal/Lambda accounts — no accounts, no network (dead proxy), no spend.
Isolated SPENDGUARD_HOME."""
import os, sys, tempfile, datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-gpuport-")
    # OFFLINE, enforced in-file too (the runner also injects this): any accidental external call dies in ms.
    os.environ["http_proxy"] = os.environ["https_proxy"] = "http://127.0.0.1:9"
    os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    for _k in ("RUNPOD_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "LAMBDA_API_KEY", "VAST_API_KEY"):
        os.environ.pop(_k, None)                     # a real key must never flip an adapter on inside the suite
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
from types import SimpleNamespace

from spendguard import gpu_port, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── shared per-UTC-day splitting math (day_slices / cost_by_day) ─────────────────────────────────────────────
# Anchor to a FIXED UTC midnight (the test_resources_gpu lesson): whole-UTC-day instances off a midnight anchor
# are deterministic on every host clock/timezone; a wall-clock `now` makes a 24h instance straddle two UTC days.
now = datetime.datetime(2026, 6, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

sl = gpu_port.day_slices(now - 3 * 86400, now - 2 * 86400, now - 5 * 86400)
ck("day_slices: whole-UTC-day window → one (day, 24h) slice", sl == [("2026-06-07", 24.0)])
sl2 = gpu_port.day_slices(now - 1.5 * 86400, now, now - 5 * 86400)
ck("day_slices: 36h window splits 12h + 24h across UTC days",
   sl2 == [("2026-06-08", 12.0), ("2026-06-09", 24.0)])
ck("day_slices: since clips the start (only the in-window remainder)",
   gpu_port.day_slices(now - 3 * 86400, now - 2 * 86400, now - 2.5 * 86400) == [("2026-06-07", 12.0)])
ck("day_slices: window fully before since → no slices",
   gpu_port.day_slices(now - 3 * 86400, now - 2 * 86400, now - 86400) == [])

port_rows = [
    {"id": "1", "label": "train-a", "gpu": "H100", "dph_usd": 2.0, "start_ts": now - 3 * 86400, "end_ts": now - 2 * 86400},
    {"id": "2", "label": "train-b", "gpu": "A100", "dph_usd": 3.61, "start_ts": now - 1.5 * 86400, "end_ts": None},  # running
]
cbd = gpu_port.cost_by_day(port_rows, since=now - 5 * 86400, now=now)
ck("cost_by_day: dph × hours per UTC day (24h × $2 → $48 on its day)", abs(cbd.get("2026-06-07", 0) - 48.0) < 1e-6)
ck("cost_by_day: running instance (end None) capped at now, split across days",
   abs(cbd.get("2026-06-08", 0) - 3.61 * 12) < 1e-6 and abs(cbd.get("2026-06-09", 0) - 3.61 * 24) < 1e-6)
ck("cost_by_day: since accepts YYYY-MM-DD", gpu_port.cost_by_day(port_rows, since="2026-06-05", now=now) == cbd)

# EQUIVALENCE with vast.ai — the math was EXTRACTED from resources.gpu_rows_by_day; identical instances must
# land identical per-day $ through both paths (guards the refactor: vast behavior unchanged, one math for all).
from spendguard import resources
resources.instances = lambda: [
    {"id": 1, "gpu_name": "H100", "dph_total": 2.0, "start_date": now - 3 * 86400, "end_date": now - 2 * 86400, "label": "train-a"},
    {"id": 2, "gpu_name": "A100", "dph_total": 3.61, "start_date": now - 1.5 * 86400, "end_date": None, "label": "train-b"},
]
vast_by_day = {}
for r in resources.gpu_rows_by_day(since_ts=now - 5 * 86400, now=now):
    vast_by_day[r["day"]] = round(vast_by_day.get(r["day"], 0.0) + r["cost"], 6)
ck("EQUIVALENCE: vast gpu_rows_by_day per-day $ == shared cost_by_day on the same instances",
   set(vast_by_day) == set(cbd) and all(abs(vast_by_day[d] - cbd[d]) < 1e-6 for d in cbd))

# ── honesty markers: UNKNOWN stays visible, never becomes $0 rows ────────────────────────────────────────────
unk = [{"id": "u1", "label": "x", "gpu": "H100", "dph_usd": None, "start_ts": now - 86400, "end_ts": now, "unpriced": True},
       {"id": "u2", "label": "y", "gpu": "A100", "dph_usd": 1.0, "start_ts": None, "end_ts": None, "untimed": True}]
ck("cost_by_day: unpriced row contributes NOTHING (unknown ≠ $0)", gpu_port.cost_by_day(unk, since=now - 5 * 86400, now=now) == {})

# provider-billed rows (`usd`, e.g. Modal daily report): booked WHOLE to their UTC day, never split/re-derived
usd_rows = [{"id": "ap-1", "label": "m2a-app", "gpu": "?", "dph_usd": None, "usd": 12.34,
             "start_ts": now - 2 * 86400, "end_ts": now - 86400},
            {"id": "ap-1", "label": "m2a-app", "gpu": "?", "dph_usd": None, "usd": 5.0,
             "start_ts": now - 30 * 86400, "end_ts": now - 29 * 86400}]                  # before since → clipped
u = gpu_port.cost_by_day(usd_rows, since=now - 5 * 86400, now=now)
ck("cost_by_day: provider-billed usd row books whole to its UTC day", u == {"2026-06-08": 12.34})

# ── label → project attribution via config resources.<provider>.label_map (the vast pattern, per provider) ──
config.CONFIG_JSON.write_text(json.dumps({"resources": {"runpod": {"label_map": {"m2a": "manga2anime"}},
                                                        "modal": {"label_map": {"m2a": "manga2anime"}}}}))
config._cfg._cache = None                                  # force re-read of the isolated-home config
ck("label_map: read from config resources.<provider>.label_map", gpu_port.label_map("runpod") == [("m2a", "manga2anime")])
ck("label_map: unconfigured provider → EMPTY (no opinionated defaults → no mis-attribution)",
   gpu_port.label_map("lambdalabs") == [])
ck("project_of: unknown label → '' (untagged, surfaced — never guessed)",
   gpu_port.project_of("mystery-box", gpu_port.label_map("runpod")) == "")


# ── RunPod adapter: documented-shape payload parsing ─────────────────────────────────────────────────────────
# Fixture shape per https://graphql-spec.runpod.io/ (Pod: id, name, costPerHr Float, desiredStatus, createdAt
# RFC3339, machine{gpuDisplayName}, runtime{uptimeInSeconds}) and the myself{pods{…}} listing shown at
# https://docs.runpod.io/sdks/graphql/manage-pods . OFFLINE: documented shape, not live-verified.
from spendguard import runpod_adapter
ck("runpod: configured()==False with no key (silent skip)", runpod_adapter.PROVIDER.configured() is False)
ck("runpod: source() is None when unconfigured (never an error, never fake data)", runpod_adapter.source() is None)

_RUNPOD_FIXTURE = {"data": {"myself": {"pods": [
    {"id": "podrunning01", "name": "m2a-train", "costPerHr": 0.69, "desiredStatus": "RUNNING",
     "createdAt": "2026-06-08T04:00:00Z", "machine": {"gpuDisplayName": "RTX 4090"},
     "runtime": {"uptimeInSeconds": 7200}},
    {"id": "podexited002", "name": "old-box", "costPerHr": 1.19, "desiredStatus": "EXITED",
     "createdAt": "2026-06-01T00:00:00Z", "machine": {"gpuDisplayName": "A100 SXM"}, "runtime": None},
    {"id": "podnoprice03", "name": "weird", "costPerHr": None, "desiredStatus": "RUNNING",
     "createdAt": None, "machine": None, "runtime": {"uptimeInSeconds": 60}},
]}}}
runpod_adapter._graphql = lambda q: _RUNPOD_FIXTURE
rp = runpod_adapter.PROVIDER.instances(now=now)
ck("runpod: parses all documented pods (nothing dropped silently)", len(rp) == 3)
run0 = next(r for r in rp if r["id"] == "podrunning01")
ck("runpod: RUNNING pod → start_ts = now − uptimeInSeconds, end open, dph from RunPod's own costPerHr",
   abs(run0["start_ts"] - (now - 7200)) < 1e-6 and run0["end_ts"] is None and run0["dph_usd"] == 0.69
   and run0["gpu"] == "RTX 4090" and run0["label"] == "m2a-train")
ex = next(r for r in rp if r["id"] == "podexited002")
ck("runpod: exited pod (past runtime NOT exposed by the listing) → untimed=True, visible, no fabricated hours",
   ex.get("untimed") is True and ex["start_ts"] is None and ex["dph_usd"] == 1.19)
np_ = next(r for r in rp if r["id"] == "podnoprice03")
ck("runpod: pod without costPerHr → unpriced=True (visible UNKNOWN, never $0-clean)", np_.get("unpriced") is True)
ck("runpod: cost math uses ONLY priced+timed rows (2h × $0.69)",
   abs(sum(gpu_port.cost_by_day(rp, since=now - 5 * 86400, now=now).values()) - 1.38) < 1e-6)
_boom = lambda q: (_ for _ in ()).throw(RuntimeError("api down"))
runpod_adapter._graphql = _boom
ck("runpod: API failure → [] (never raises; a flake must not error the reconcile)",
   runpod_adapter.PROVIDER.instances() == [])
runpod_adapter._graphql = lambda q: _RUNPOD_FIXTURE

os.environ["RUNPOD_API_KEY"] = f"test-fake-{os.getpid()}"   # runtime-built placeholder, not a real credential
ck("runpod: configured()==True once the env key is present (keys.env loads into env)",
   runpod_adapter.PROVIDER.configured() is True)
src = runpod_adapter.source()
ck("runpod: source() → a reconcile.Source named gpu:runpod", src is not None and src.name == "gpu:runpod")
cap = src.captured(since="2026-06-05")
ck("runpod: captured rows carry label-attributed project (config resources.runpod.label_map)",
   cap and all(r["project"] == "manga2anime" for r in cap) and abs(sum(r["cost"] for r in cap) - 1.38) < 1e-6)
ck("runpod: truth UNKNOWN (RunPod exposes no period bill) → None, never a fake $0/covered",
   gpu_port.ProviderGPUSource(runpod_adapter.PROVIDER, conn={"owns_account": True}).truth_total() is None)
ck("runpod: non-owner never anchors a shared account (truth 0.0, the vast doctrine)",
   gpu_port.ProviderGPUSource(runpod_adapter.PROVIDER, conn={"enabled": True, "owns_account": False}).truth_total() == 0.0)


# ── Modal adapter: documented-shape report items ─────────────────────────────────────────────────────────────
# Fixture shape per https://modal.com/docs/reference/modal.billing — workspace_billing_report(start, end,
# resolution="d") → WorkspaceBillingReportItem(object_id, description, environment_name, interval_start UTC
# datetime, cost Decimal, tags). OFFLINE: documented shape, not live-verified.
from decimal import Decimal
from spendguard import modal_adapter
ck("modal: configured()==False with no tokens (silent skip)", modal_adapter.PROVIDER.configured() is False)
ck("modal: source() is None when unconfigured", modal_adapter.source() is None)

_MODAL_ITEMS = [
    SimpleNamespace(object_id="ap-abc123", description="m2a-render", environment_name="main",
                    interval_start=datetime.datetime(2026, 6, 8, tzinfo=datetime.timezone.utc),
                    cost=Decimal("12.34"), tags={}),
    SimpleNamespace(object_id="ap-abc123", description="m2a-render", environment_name="main",
                    interval_start=datetime.datetime(2026, 6, 9, tzinfo=datetime.timezone.utc),
                    cost=Decimal("2.66"), tags={}),
]
modal_adapter._report = lambda since_ts: _MODAL_ITEMS
mi = modal_adapter.PROVIDER.instances(since_ts=now - 5 * 86400)
ck("modal: report items → per-app-per-day rows carrying Modal's own billed $ (usd), no dph re-derivation",
   len(mi) == 2 and mi[0]["usd"] == 12.34 and mi[0]["dph_usd"] is None and mi[0]["label"] == "m2a-render")
ck("modal: each daily row books whole to its own UTC day",
   gpu_port.cost_by_day(mi, since=now - 5 * 86400, now=now) == {"2026-06-08": 12.34, "2026-06-09": 2.66})
ck("modal: account_total = Σ report (the report IS the provider bill)",
   modal_adapter.PROVIDER.account_total(since="2026-06-05") == 15.0)
rows_m = gpu_port.rows_by_day(modal_adapter.PROVIDER, since="2026-06-05", now=now)
ck("modal: rows_by_day attributes app label → project via resources.modal.label_map",
   rows_m and all(r["project"] == "manga2anime" for r in rows_m))
modal_adapter._report = lambda since_ts: (_ for _ in ()).throw(RuntimeError("plan without billing API"))
ck("modal: report failure → [] instances and account_total None (UNKNOWN, never $0)",
   modal_adapter.PROVIDER.instances() == [] and modal_adapter.PROVIDER.account_total() is None)
modal_adapter._report = lambda since_ts: _MODAL_ITEMS


# ── Lambda adapter: documented-shape listing ─────────────────────────────────────────────────────────────────
# Fixture shape per https://cloud.lambdalabs.com/api/v1/openapi.json (GET /instances → {"data":[{id, name,
# status, region{name,description}, instance_type{name, description, gpu_description, price_cents_per_hour,
# specs}, …}]}; docs https://docs-api.lambda.ai/api/cloud). The listing documents NO launch timestamp.
# OFFLINE: documented shape, not live-verified.
from spendguard import lambda_adapter
ck("lambda: configured()==False with no key (silent skip)", lambda_adapter.PROVIDER.configured() is False)
ck("lambda: source() is None when unconfigured", lambda_adapter.source() is None)

_LAMBDA_FIXTURE = {"data": [
    {"id": "0920582c7ff041399e34823a0be62549", "name": "m2a-node-1", "status": "active",
     "region": {"name": "us-east-1", "description": "Virginia, USA"},
     "instance_type": {"name": "gpu_1x_h100_pcie", "description": "1x H100 (80 GB PCIe)",
                       "gpu_description": "H100 (80 GB PCIe)", "price_cents_per_hour": 249,
                       "specs": {"vcpus": 26, "memory_gib": 200, "storage_gib": 512, "gpus": 1}},
     "hostname": "node-1.lambda", "ssh_key_names": ["k1"], "file_system_names": []},
    {"id": "ffff582c7ff041399e34823a0be6ffff", "name": "mystery", "status": "active",
     "region": {"name": "us-west-1", "description": "California, USA"},
     "instance_type": {"name": "odd_type", "price_cents_per_hour": None}},
]}
lambda_adapter._get = lambda path: _LAMBDA_FIXTURE
li = lambda_adapter.PROVIDER.instances()
ck("lambda: dph_usd = the provider's price_cents_per_hour / 100 (never a local price table)",
   li[0]["dph_usd"] == 2.49 and li[0]["gpu"] == "H100 (80 GB PCIe)" and li[0]["label"] == "m2a-node-1")
ck("lambda: NO launch timestamp in the listing → every row untimed=True (visible, no fabricated hours)",
   all(r.get("untimed") is True and r["start_ts"] is None for r in li))
ck("lambda: missing price → unpriced=True as well", li[1].get("unpriced") is True and li[1]["dph_usd"] is None)
ck("lambda: untimed rows contribute NOTHING to per-day $ (unknown ≠ $0)",
   gpu_port.cost_by_day(li, since=now - 5 * 86400, now=now) == {})
lambda_adapter._get = lambda path: (_ for _ in ()).throw(RuntimeError("api down"))
ck("lambda: API failure → [] (never raises)", lambda_adapter.PROVIDER.instances() == [])
lambda_adapter._get = lambda path: _LAMBDA_FIXTURE


# ── registry: the SAME one reconcile.all_sources iterates — vast + the three adapters, plugins composable ────
regs = gpu_port.sources()
ck("registry: vast keeps its historical key ('gpu') + the three port adapters register",
   set(regs) == {"gpu", "gpu:runpod", "gpu:modal", "gpu:lambdalabs"})
ck("registry: unconfigured adapters' factories → None (silently skipped)",
   regs["gpu:modal"]() is None and regs["gpu:lambdalabs"]() is None)
ck("registry: vast factory builds the existing GPUSource (no special-case, same loop)",
   regs["gpu"]().name == "gpu")
gpu_port.register_source("gpu:acme", lambda: None)         # third-party recipe: activate() calls register_source
ck("registry: a plugin-registered source rides the same registry", "gpu:acme" in gpu_port.sources())
gpu_port._SOURCES.pop("gpu:acme")

# reconcile.all_sources: configured providers appear; unconfigured never do; one failing source is isolated.
from spendguard import reconcile, ledger_sync, saas
saas.conn = lambda: {"enabled": True, "owns_account": True, "visibility": "org"}
ledger_sync._provider_total = lambda since: 800.0
ledger_sync._gate_captured_rows = lambda since: [{"cost": 600.0, "project": "lmm"}]
resources.account_gpu_total = lambda since=None: 1000.0
resources.gpu_rows_by_day = lambda *a, **k: [{"cost": 250.0, "project": "lmm"}]
ptmap = {"lmm": ("Healiom", "clinical-ai"), "manga2anime": ("Ensight", "")}
res = reconcile.all_sources(ptmap, since="2026-06-01")
ck("all_sources: configured runpod appears as gpu:runpod; unconfigured modal/lambda are ABSENT (not errors)",
   "gpu:runpod" in res and "gpu:modal" not in res and "gpu:lambdalabs" not in res)
ck("all_sources: vast ('gpu') unchanged through the registry (truth 1000, captured 250)",
   res["gpu"]["truth_total"] == 1000.0 and res["gpu"]["captured"] == 250.0)
ck("all_sources: runpod captured $1.38 attributed to its org via label→project→ptmap",
   res["gpu:runpod"]["captured"] == 1.38 and res["gpu:runpod"]["by_org"].get("Ensight") == 1.38)
ck("all_sources: runpod truth UNKNOWN (no bill exposed) → residual None + explicit warning, never $0-reconciled",
   res["gpu:runpod"]["truth_total"] is None and res["gpu:runpod"]["residual"] is None
   and "UNKNOWN" in (res["gpu:runpod"]["warning"] or ""))
comp = reconcile.completeness(res)
ck("completeness: an unknown-truth provider keeps the verdict INCOMPLETE (unknown never reads as complete)",
   comp["complete"] is False and comp["sources"]["gpu:runpod"]["status"] == "unknown")
gpu_port.register_source("gpu:boom", lambda: (_ for _ in ()).throw(RuntimeError("factory broke")))
res2 = reconcile.all_sources(ptmap, since="2026-06-01")
ck("all_sources: a raising source factory is isolated as {error}; the others still reconcile",
   "error" in res2.get("gpu:boom", {}) and res2["gpu"]["truth_total"] == 1000.0)
gpu_port._SOURCES.pop("gpu:boom")

# ── config_schema: the keys are declared like VAST_API_KEY (secret, env-only; keys.env loads them) ──────────
from spendguard import config_schema
for k in ("RUNPOD_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "LAMBDA_API_KEY"):
    s = next((s for s in config_schema.SETTINGS if s["key"] == k), None)
    ck(f"config_schema: {k} declared (section keys, store env, secret)",
       s is not None and s["section"] == "keys" and s["store"] == "env" and s["secret"] is True)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"gpu_port: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
