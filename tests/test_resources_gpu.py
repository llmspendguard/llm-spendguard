"""vast.ai GPU reconstruction — per-UTC-day cost split, snapshot → history, and live∪history merge so DESTROYED
instances (gone from the API) stay reconstructable. Pure given injected instance dicts; no network. Isolated home."""
import os, sys, tempfile, datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import resources

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# Anchor to a FIXED UTC midnight so the per-UTC-day split is exact regardless of wall-clock time or the host's
# timezone. gpu_rows_by_day buckets on UTC-midnight boundaries — with a real `time.time()` a 24h instance straddles
# two UTC days (no single $48 day) UNLESS `now` happens to be near UTC midnight. That made this test pass locally
# and FAIL on CI's clock. Whole-UTC-day instances off a midnight anchor are deterministic everywhere.
now = datetime.datetime(2026, 6, 10, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
inst1 = {"id": 1, "gpu_name": "H100", "dph_total": 2.0, "start_date": now - 3 * 86400, "end_date": now - 2 * 86400, "label": "train-a"}
inst2 = {"id": 2, "gpu_name": "A100", "dph_total": 1.0, "start_date": now - 1 * 86400, "end_date": now, "label": "train-b"}
resources.instances = lambda: [inst1, inst2]

rows = resources.gpu_rows_by_day(since_ts=now - 5 * 86400, now=now)
ck("gpu_rows_by_day → per-day rows", len(rows) >= 2)
ck("cost = dph × hours (inst1 ~ $48 over its 24h)", any(abs(r["cost"] - 2.0 * 24) < 2.0 for r in rows))
total = sum(r["cost"] for r in rows)
ck("total ≈ inst1 $48 + inst2 $24", abs(total - (48 + 24)) < 3.0)

# snapshot records both; then inst1 is "destroyed" (gone from the live API) → still reconstructed from history
resources.snapshot()
resources.instances = lambda: [inst2]
ids = {str(i.get("id")) for i in resources._all_instances()}
ck("destroyed instance reconstructed from snapshot history", ids == {"1", "2"})
rows2 = resources.gpu_rows_by_day(since_ts=now - 5 * 86400, now=now)
ck("destroyed instance still in per-day rows", abs(sum(r["cost"] for r in rows2) - 72) < 3.0)

# label_map (config-driven) — empty default means no mis-attribution
ck("DEFAULT_LABEL_MAP empty (no opinionated defaults)", resources.DEFAULT_LABEL_MAP == [])
ck("unlabeled → no project (until user configures)", resources.project_of("train-a") == "")


# ── account-anchored, label-attributed reconcile (replaces the buggy conv-alignment gap-dump) ────────────────────
# _reconcile is PURE + deterministic: rows (project from instance LABEL) + account_total + conn + ptmap →
# {mine, captured, account_total, residual, by_org}. Properties tested: (1) this connection pushes ONLY its own
# project's boxes — a SHARED account can't leak cross-org; (2) every dollar traces to a labelled box (no fabricated
# flat $/day rows); (3) the unrecoverable remainder is an EXPLICIT residual, never dumped on a project; (4) residual
# → 0 when every box is captured (proves the process reconciles to the account given complete inputs).
ptmap = {"lmm": ("Healiom", "clinical-ai"), "manga2anime": ("Ensight", "")}
rows = [
    {"day": "2026-06-10", "gpu": "A100 SXM4", "cost": 250.0, "instances": [1], "project": "lmm"},
    {"day": "2026-06-11", "gpu": "H100 SXM", "cost": 200.0, "instances": [2], "project": "lmm"},
    {"day": "2026-06-12", "gpu": "H200 NVL", "cost": 300.0, "instances": [3], "project": "manga2anime"},  # foreign org
    {"day": "2026-06-12", "gpu": "RTX 3090", "cost": 50.0, "instances": [4], "project": ""},               # unlabeled
]
rec = resources._reconcile(rows, 900.0, {"project": "lmm"}, ptmap)
ck("reconcile: mine = only THIS project's boxes (no cross-org leak)", {r["gpu"] for r in rec["mine"]} == {"A100 SXM4", "H100 SXM"})
ck("reconcile: mine all project=lmm", all(r["project"] == "lmm" for r in rec["mine"]))
ck("reconcile: mine sums to $450 (A100+H100)", round(sum(r["cost"] for r in rec["mine"]), 2) == 450.0)
ck("reconcile: by_org attributes by label", rec["by_org"].get("Healiom") == 450.0 and rec["by_org"].get("Ensight") == 300.0)
ck("reconcile: unlabeled box → (untagged), not a real org", rec["by_org"].get("(untagged)") == 50.0)
ck("reconcile: residual = account − Σ all boxes, explicit (900−800=100)", rec["residual"] == 100.0)

# full recovery: Σ boxes == account_total → residual 0 (the reconcile-to-account property)
ck("reconcile: residual → 0 when every box captured/recovered", resources._reconcile(rows, 800.0, {"project": "lmm"}, ptmap)["residual"] == 0.0)

# record_recovered: a box destroyed before snapshotting is reconstructable → flows through _all_instances
resources.instances = lambda: []                       # no live boxes; rely solely on recovered history
resources.record_recovered({"id": 99, "gpu_name": "H100 SXM", "dph_total": 3.61,
                            "start_date": now - 2 * 86400, "end_date": now - 1 * 86400, "label": "healiom_gpu_h100"})
ck("record_recovered: destroyed box enters _all_instances", "99" in {str(i.get("id")) for i in resources._all_instances()})

# ── discovery: the deterministic parsing layer under the agentic LLM read (tolerant id/gpu/dph extraction) ────────
resources.project_of = lambda label, label_map=None: ("lmm" if "healiom" in (label or "").lower()
                                                      else "manga2anime" if "m2a" in (label or "").lower() else "")
obj = ('{"id": 40272086, "gpu_name": "H100 SXM", "dph_total": 3.61, "start_date": 1781000000, '
       '"end_date": 1781100000, "label": "healiom_gpu_h100", "actual_status": "exited"}')
o1 = resources._parse_instances(obj, seen_ts=1781100000)
ck("_parse_instances: API object → id+gpu+dph+end", o1 and o1[0]["id"] == "40272086" and o1[0]["dph_total"] == 3.61 and o1[0].get("end_date") == 1781100000)
o2 = resources._parse_instances("id=41120359 GTX 1070 Ti status=running $0.099/hr label=m2a-kr", seen_ts=1781000000)
ck("_parse_instances: formatted print → id+gpu+label", o2 and o2[0]["id"] == "41120359" and o2[0].get("label") == "m2a-kr")
# _consolidate classifies by RUNTIME CERTAINTY: real exit → complete ($ reconstructable); running-only → identity
c1 = resources._consolidate(o1, now=1781200000)
ck("_consolidate: real start+exit → complete + project from label", len(c1["complete"]) == 1 and c1["complete"][0]["project"] == "lmm")
c2 = resources._consolidate(o2, now=1781200000)
ck("_consolidate: running box (no real exit) → identity_only, runtime NOT fabricated",
   len(c2["identity_only"]) == 1 and c2["identity_only"][0]["project"] == "manga2anime" and c2["identity_only"][0]["end_date"] is None)

# ── discover_agentic: validates UNTRUSTED LLM output (confidence filter, id-width guard, dedup-by-confidence) ──
import contextlib as _ctx
from spendguard import adapters as _adapters, calls as _calls
resources._gpu_session_excerpts = lambda max_sessions=None: [("sess1", "vast box excerpt")]
_calls.context = lambda **k: _ctx.nullcontext()
_adapters.call = lambda *a, **k: {"cost": 0.0, "error": None, "text": (
    '{"instances":['
    '{"id":"40272086","gpu":"H100 SXM","dph":3.61,"label":"healiom_gpu_h100","project":"lmm","runtime_hours":24,"confidence":90},'
    '{"id":"5","gpu":"X","dph":1,"confidence":95},'               # too-short id → rejected by the \\d{6,10} guard
    '{"id":"40999999","gpu":"A100","dph":1.1,"confidence":40}'    # confidence < 60 → dropped
    ']}')}
resources.instances = lambda: []
_ag = resources.discover_agentic(run=True, record=False, now=now)
_ids = {i["id"] for i in _ag["instances"]}
ck("discover_agentic: keeps the valid high-confidence instance", "40272086" in _ids)
ck("discover_agentic: rejects too-short id (guard)", "5" not in _ids)
ck("discover_agentic: drops confidence<60", "40999999" not in _ids)


# ── phantom-spend guard: a DESTROYED running box (far-future CONTRACT end_date) must be capped at last_seen ──
# vast.ai sets a running box's end_date to contract-expiry (far future). Once destroyed (gone from live), the old
# `if not end_date` cap missed it → it accrued dph × every day since, forever. Cap at last_seen instead.
import json as _json
resources.instances = lambda: []                           # destroyed: gone from the live API
_hist = {"7777": {"id": 7777, "gpu_name": "H100", "dph_total": 2.0,
                  "start_date": now - 3 * 86400, "end_date": now + 300 * 86400,   # far-future contract end
                  "last_seen": now - 2 * 86400, "status": "running"}}
resources._history_path().write_text(_json.dumps(_hist))
_cost = sum(r["cost"] for r in resources.gpu_rows_by_day(since_ts=now - 5 * 86400, now=now))
# real runtime = start (−3d) → last_seen (−2d) = 24h × $2 = $48; NOT capped-at-now (48h=$96) or contract-end (huge)
ck("destroyed running box capped at last_seen (no phantom spend)", abs(_cost - 48.0) < 2.0)

# ── ORG-BASED GPU push: ONE connection pushes EVERY box in its org, each KEEPING ITS OWN timing-matched project ──
# Guards the bug where the GPU push collapsed every box onto a single cwd-derived `project` (dropping Healiom's GPU
# entirely once the connection went org-scoped, and risking an Ensight box leaking into Healiom). The per-instance
# attribution is agentic (timing-match); the push must RESPECT it, not flatten it — same doctrine as the LLM ledger.
from spendguard import attribution as _attr
_attr.taxonomy = lambda *a, **k: ({}, {})
_attr.project_team_map = lambda *a, **k: {"lmm": ("Healiom", "clinical-ai"),
                                          "concept-model": ("Healiom", "clinical-ai"),
                                          "manga2anime": ("Ensight", "engineering")}
orows = [
    {"day": "2026-06-10", "gpu": "H100", "cost": 100.0, "instances": [1], "project": "lmm"},
    {"day": "2026-06-11", "gpu": "A100", "cost": 80.0, "instances": [2], "project": "concept-model"},
    {"day": "2026-06-12", "gpu": "H200", "cost": 300.0, "instances": [3], "project": "manga2anime"},  # Ensight — excluded
]
ro = resources._reconcile(orows, 600.0, {"org": "Healiom", "owns_account": True}, _attr.project_team_map())
ck("org-GPU: mine = ALL Healiom boxes (lmm + concept-model), Ensight box NOT pulled in",
   {r["project"] for r in ro["mine"]} == {"lmm", "concept-model"})
ck("org-GPU: each box keeps its OWN project (not collapsed onto one) → $180 captured for this org",
   round(sum(r["cost"] for r in ro["mine"]), 2) == 180.0)
ck("org-GPU: unknown/empty-scope org → fail-CLOSED (no boxes), never cross-org push-all",
   resources._reconcile(orows, 600.0, {"org": "Nonexistent"}, _attr.project_team_map())["mine"] == [])
ck("org-GPU: legacy single-project conn still scopes to that one project (back-compat)",
   {r["project"] for r in resources._reconcile(orows, 600.0, {"project": "lmm"}, _attr.project_team_map())["mine"]} == {"lmm"})

# ── GPU attribution PRIORITY: the instance LABEL is GROUND TRUTH and WINS over the timing-match ──────────────────
# A vast.ai box runs async — the chat open while it ran is often unrelated work. So a box LABELED m2a-* is manga2anime
# even if a spendguard/lmm conversation was active in its window. Guards the bug where timing-match (primary) sent
# manga2anime's $657 of labeled GPU to llm-spendguard. Timing-match is the fallback ONLY for an UNLABELED box.
import datetime as _dt2
_nn = _dt2.datetime(2026, 6, 10, 0, 0, 0, tzinfo=_dt2.timezone.utc).timestamp()
resources._history_path().write_text("{}")                  # isolate: no leftover recorded boxes
resources.instances = lambda: [
    {"id": 901, "gpu_name": "H100", "dph_total": 2.0, "start_date": _nn - 86400, "end_date": _nn, "label": "m2a-train"},
    {"id": 902, "gpu_name": "A100", "dph_total": 1.0, "start_date": _nn - 86400, "end_date": _nn, "label": "unlabeled-box"}]
resources.project_of = lambda label, label_map=None: ("manga2anime" if "m2a" in (label or "")
                                                      else "lmm" if "healiom" in (label or "") else "")
from spendguard import conv as _conv
_conv.instance_attributions = lambda insts: {"901": {"project": "lmm", "org": "Healiom"},      # timing-match says lmm…
                                             "902": {"project": "lmm", "org": "Healiom"}}      # …for BOTH boxes
_pr = {r["project"] for r in resources.gpu_rows_by_day(since_ts=_nn - 5 * 86400, now=_nn)}
ck("GPU LABEL wins over timing-match: m2a-labeled box → manga2anime (NOT the lmm it timing-matched)", "manga2anime" in _pr)
ck("GPU timing-match is the FALLBACK: an UNLABELED box still uses it → lmm", "lmm" in _pr)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"resources_gpu: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
