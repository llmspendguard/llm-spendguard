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


def _month_start_ts():
    import datetime
    t = datetime.datetime.now(datetime.timezone.utc)
    return datetime.datetime(t.year, t.month, 1, tzinfo=datetime.timezone.utc).timestamp()


def gpu_rows_by_day(since_ts=None, now=None, label_map=None):
    """Per (project, gpu, day) GPU cost — each instance's cost SPLIT across the UTC days it ran (dph × hours that
    day), not lumped on today. Attributed by label → project. since_ts defaults to the start of this month."""
    import datetime
    now = now or time.time()
    since_ts = since_ts if since_ts is not None else _month_start_ts()
    agg = {}
    for i in instances():
        dph = float(i.get("dph_total") or 0)
        start = i.get("start_date") or 0
        if not dph or not start:
            continue
        end = min(i.get("end_date") or now, now)
        t = max(start, since_ts)
        proj = project_of(i.get("label"), label_map)
        gpu = i.get("gpu_name") or "?"
        while t < end:                                     # walk day by day, clipping to each UTC day
            day = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")
            d0 = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()
            de = d0 + 86400
            hours = (min(end, de) - t) / 3600.0
            a = agg.setdefault((proj, gpu, day), {"project": proj, "gpu": gpu, "day": day, "cost": 0.0, "hours": 0.0, "instances": set()})
            a["cost"] += dph * hours
            a["hours"] += hours
            a["instances"].add(i.get("id"))
            t = de
    rows = []
    for a in agg.values():
        a["instances"] = sorted(a["instances"])
        a["cost"] = round(a["cost"], 6)
        a["hours"] = round(a["hours"], 2)
        rows.append(a)
    return rows


def account_gpu_total(since_ts=None):
    """vast.ai account spend since the period — PROXY: invoice charges (prepaid top-ups that fund consumption).
    Approximate (top-ups are lumpy and ± a balance buffer; vast.ai exposes no per-instance billing), but it's the
    account-level GPU truth for the reconcile gap, mirroring the LLM provider-billing gap."""
    since_ts = since_ts if since_ts is not None else _month_start_ts()
    try:
        inv = (_get("users/current/invoices/") or {}).get("invoices", [])
    except Exception:
        return 0.0
    return round(sum(abs(float(i.get("amount") or 0)) for i in inv
                     if not i.get("is_credit") and (i.get("timestamp") or 0) >= since_ts), 2)


def compute_exceeded():
    """Remote-compute (vast.ai) cap status — ALERT-only: launches don't pass through the gate, and we never kill a
    running billed job (your protocol). Returns (scope, cap, spent) for the first breached window, else None.
    Surfaced in the report + dashboard so a breach is visible; pair with `spendguard resources launch` to hard-block
    NEW launches over cap."""
    from . import config
    cm = config.class_cap("compute", "monthly")
    if cm is not None:
        spent = account_gpu_total()                       # month-to-date account charges (proxy)
        if spent > cm:
            return ("compute-monthly", cm, round(spent, 2))
    cd = config.class_cap("compute", "daily")
    if cd is not None:
        try:
            import datetime as _dt
            today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
            st = round(sum(r["cost"] for r in gpu_rows_by_day() if r["day"] == today), 2)
            if st > cd:
                return ("compute-daily", cd, st)
        except Exception:
            pass
    return None


def sync(dry=False):
    """Push THIS repo's GPU spend (instances whose label maps to this repo's project), per-day, via this repo's
    key → its org. The owns_account connection ALSO reconciles: gap = vast.ai account total − Σ attributed →
    pushed as 'unattributed' remote-compute (destroyed/untracked instances), mirroring the LLM unattributed gap."""
    import datetime
    from . import saas, budget
    c = saas.conn()
    proj = (c.get("project") or budget._project() or "").lower()
    ref = saas.contributor()
    allrows = gpu_rows_by_day()
    day_totals = [{
        "day": r["day"], "provider": "vastai", "model": r["gpu"], "kind": "gpu", "channel": "realtime",
        "spend_micros": round(r["cost"] * 1_000_000), "calls": len(r["instances"]),
        "member_ref": ref, "project": proj,
        "tags": ",".join(["remote-compute", "gpu", r["gpu"].replace(" ", ""), "instances:" + "/".join(str(x) for x in r["instances"])]),
    } for r in allrows if (r.get("project") or "") == proj and r["cost"] > 0]
    if c.get("owns_account"):                              # account-level GPU reconcile (one vast.ai account)
        # Only reconcile the blanket gap when this vast.ai account is SINGLE-PROJECT. A shared multi-project account
        # (e.g. lmm GLiNER A100 + manga2anime H200) makes the destroyed/untracked gap CROSS-ORG — dumping it as this
        # org's 'unattributed' would pull another org's GPU spend in (and vast.ai exposes no per-instance billing to
        # split it, and prepaid top-ups are lumpy = a balance buffer, not consumption). So: multi-project account →
        # push only per-project attributed consumption (each org reconciles its own); single-project → the gap is
        # this project's (the "primary task" rule), not 'unattributed'.
        projs_present = {(r.get("project") or "") for r in allrows if r.get("project")}
        if len(projs_present) <= 1:
            gap = round(account_gpu_total() - sum(r["cost"] for r in allrows), 2)
            if gap > 0.5:
                day_totals.append({
                    "day": datetime.date.today().isoformat(), "provider": "vastai", "model": "(destroyed/untracked)",
                    "kind": "gpu", "channel": "realtime", "spend_micros": round(gap * 1_000_000), "calls": 0,
                    "member_ref": ref, "project": proj, "tags": f"remote-compute,gpu,destroyed,{proj}",
                })
    for row in day_totals:                                # per-row id, local↔server cross-check (gpu rows too)
        row["uid"] = saas._row_uid(row)
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
    # show: per-project attributed + the account reconcile gap
    rows = gpu_rows_by_day()
    byproj = {}
    for r in rows:
        byproj[r["project"] or "(untagged)"] = byproj.get(r["project"] or "(untagged)", 0) + r["cost"]
    truth = account_gpu_total()
    attributed = sum(byproj.values())
    print("vast.ai GPU (MTD), label-attributed per project:")
    for p, c in sorted(byproj.items(), key=lambda x: -x[1]):
        print(f"  {p:14} ${c:8.2f}")
    print(f"  {'— attributed':14} ${attributed:8.2f}")
    print(f"  {'account total':14} ${truth:8.2f}  (vast.ai charges; top-up proxy)")
    print(f"  {'→ ungoverned':14} ${max(0, truth - attributed):8.2f}  (destroyed/untracked instances)")
    return 0
