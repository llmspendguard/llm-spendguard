"""Resource (non-LLM compute) spend — vast.ai GPU — tracked like LLM: mapped to org/team/project/contributor and
pushed to the SAME server, so the dashboard shows LLM + GPU side by side. One vast.ai account spans MULTIPLE
projects (GLiNER A100 → lmm/Healiom, manga2anime training → Manga2Anime), so each instance's LABEL routes it to
a project — exactly like one provider account spanning orgs. A GPU row carries multiple tags (gpu type, instance,
label) alongside project/org/contributor.

Push path mirrors the LLM roll-up: run `spendguard resources sync` from a repo and it pushes only the GPU spend
whose label maps to THAT repo's project, via that repo's key → the right org. Read-only against vast.ai (free).
"""
import json
import os
import pathlib
import time
import urllib.request

from . import config

VAST_BASE = "https://console.vast.ai/api/v0"

# label substring → project (first match wins). Override/extend via config: resources.vastai.label_map.
DEFAULT_LABEL_MAP = [
    ("manga2anime", "manga2anime"), ("m2a", "manga2anime"), ("sam", "manga2anime"), ("anime", "manga2anime"),
    ("gliner", "lmm"), ("healiom", "lmm"), ("lmm", "lmm"),
]


def _key():
    k = os.environ.get("VAST_API_KEY", "")
    if not k:
        p = pathlib.Path.home() / ".config" / "vastai" / "vast_api_key"
        try:
            k = p.read_text().strip() if p.exists() else ""
        except Exception:
            k = ""
    return k


def _get(path):
    k = _key()
    if not k:
        raise RuntimeError("no vast.ai key (set VAST_API_KEY or ~/.config/vastai/vast_api_key)")
    req = urllib.request.Request(f"{VAST_BASE}/{path}", headers={"Authorization": f"Bearer {k}"})
    with urllib.request.urlopen(req, timeout=20, context=config.ssl_context()) as r:
        return json.loads(r.read().decode())


def project_of(label, label_map=None):
    lab = (label or "").lower()
    for sub, proj in (label_map or DEFAULT_LABEL_MAP):
        if sub in lab:
            return proj
    return ""   # unknown label → untagged (surfaced, not guessed)


def instances():
    d = _get("instances/")
    return d.get("instances", d) if isinstance(d, dict) else (d or [])


def gpu_rows(now=None, label_map=None):
    """Per (project, gpu) cumulative GPU cost-to-date from CURRENTLY-visible instances, attributed by label.
    cost = dph_total × hours since start (running → now; exited keeps its last-seen runtime). Destroyed
    instances aren't listed here — their spend is in the vast.ai invoice total (the GPU reconcile gap, mirroring
    the LLM unattributed gap). Returns rows ready to map into ledger pushes."""
    now = now or time.time()
    agg = {}
    for i in instances():
        dph = float(i.get("dph_total") or 0)
        start = i.get("start_date") or 0
        if not dph or not start:
            continue
        end = i.get("end_date") or now
        hours = max(0.0, (min(end, now) - start) / 3600.0)
        proj = project_of(i.get("label"), label_map)
        gpu = i.get("gpu_name") or "?"
        a = agg.setdefault((proj, gpu), {"project": proj, "gpu": gpu, "instance_ids": [], "labels": set(),
                                         "dph_total": 0.0, "hours": 0.0, "cost": 0.0, "running": 0})
        a["instance_ids"].append(i.get("id"))
        a["labels"].add(i.get("label") or "")
        a["dph_total"] += dph
        a["hours"] += hours
        a["cost"] += dph * hours
        a["running"] += 1 if i.get("actual_status") == "running" else 0
    rows = []
    for r in agg.values():
        r["labels"] = sorted(x for x in r["labels"] if x)
        r["cost"] = round(r["cost"], 4)
        r["hours"] = round(r["hours"], 1)
        rows.append(r)
    return sorted(rows, key=lambda x: -x["cost"])


def sync(dry=False):
    """Push THIS repo's GPU spend (the instances whose label maps to this repo's project) via this repo's key →
    its org. Mirrors the LLM roll-up: provider='vastai', kind='gpu', model=GPU type, project + contributor + tags.
    Run from each repo (lmm pushes its GLiNER A100 → Healiom/LMM; animepipe pushes manga2anime GPU → Manga2Anime)."""
    import datetime
    from . import saas, budget
    c = saas.conn()
    proj = (c.get("project") or budget._project() or "").lower()
    rows = [r for r in gpu_rows() if (r.get("project") or "") == proj]
    day = datetime.date.today().isoformat()
    ref = saas.contributor()
    day_totals = [{
        "day": day, "provider": "vastai", "model": r["gpu"], "kind": "gpu", "channel": "realtime",
        "spend_micros": round(r["cost"] * 1_000_000), "calls": r["running"],
        "member_ref": ref, "project": proj,
        # tag hierarchy: category 'remote-compute' → subtype 'gpu' → the specific GPU + instances
        "tags": ",".join(["remote-compute", "gpu", r["gpu"].replace(" ", ""), "instances:" + "/".join(str(i) for i in r["instance_ids"])]),
    } for r in rows]
    payload = {"visibility": c.get("visibility"), "day_totals": day_totals}
    if dry:
        return payload
    ok, reason = saas.ready()
    if not ok:
        return {"skipped": f"not connected: {reason}"}
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private"}
    return saas._request("POST", "/v1/ledger", payload)


def cmd(argv=None):
    argv = argv or []
    sub = argv[0] if argv else "show"
    if sub == "sync":
        print("resources sync:", sync(dry="--dry" in argv))
        return 0
    rows = gpu_rows()
    print("vast.ai GPU cost-to-date by project (label-attributed):")
    for r in rows:
        print(f"  {(r['project'] or '(untagged)'):12} {r['gpu']:14} ${r['cost']:8.2f}  ({r['hours']}h, {r['running']} running)  {r['instance_ids']}")
    return 0
