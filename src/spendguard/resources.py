"""Resource (non-LLM compute) spend — vast.ai GPU — tracked like LLM: mapped to org/team/project/contributor and
pushed to the SAME server, so the dashboard shows LLM + GPU side by side. One vast.ai account spans MULTIPLE
projects (NLP training → nlp-pipeline/Acme, vision training → vision-pipeline), so each instance's LABEL routes it to
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

# NOTE: the old conversation-alignment gap reconstruction (`_gpu_alignment` + `_GPU_KW`) was REMOVED. It spread the
# blanket account gap across conversation-matched projects with a flat per-day weight — which (a) dumped a SHARED
# account's gap cross-org (manga2anime's destroyed H200 landed on Healiom) and (b) fabricated flat $/day rows not tied
# to any real box. Replaced by `_reconcile` below: account-anchored + label-attributed (every dollar traces to an
# instance label → project → org), with the unrecoverable remainder surfaced as an EXPLICIT residual, never dumped.

VAST_BASE = "https://console.vast.ai/api/v0"

# label substring → project (first match wins). EMPTY by default on purpose: opinionated defaults would silently
# mis-attribute a stranger's instance (e.g. "embed-test" → some project that isn't theirs). Each user sets their own
# in config `resources.vastai.label_map` ({substring: project}), e.g. {"train": "ml-pipeline", "render": "video"}.
DEFAULT_LABEL_MAP = []


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


def _label_map():
    """Config `resources.vastai.label_map` ({substring: project}) FIRST (user-specific, e.g. {"train": "ml-pipeline",
    "render": "video"}), then DEFAULT_LABEL_MAP. So labels actually map to projects (the GPU ground truth)."""
    cfg = config._cfg_get("resources", "vastai", {}) or {}
    m = cfg.get("label_map") or {} if isinstance(cfg, dict) else {}
    return [(str(k).lower(), v) for k, v in m.items()] + DEFAULT_LABEL_MAP


def project_of(label, label_map=None):
    lab = (label or "").lower()
    for sub, proj in (label_map if label_map is not None else _label_map()):
        if sub in lab:
            return proj
    return ""   # unknown label → untagged (surfaced, not guessed)


def instances():
    d = _get("instances/")
    return d.get("instances", d) if isinstance(d, dict) else (d or [])


def _history_path():
    return config.HOME / "resources_history.json"


def _load_history():
    try:
        return json.loads(_history_path().read_text())
    except Exception:
        return {}


def snapshot():
    """Record each LIVE instance's reconstruction state (id → gpu/dph/start/end/label/last_seen), so DESTROYED
    instances stay reconstructable per-day. vast.ai exposes NO per-day consumption AND drops destroyed instances
    from the API (the invoice/CSV export is top-ups only) — so we must persist their state while they're live.
    Idempotent (latest state per id). Runs on every `saas sync` + can be cron'd (`resources snapshot`)."""
    hist = _load_history()
    now = time.time()
    rec = 0
    for i in instances():
        iid = str(i.get("id") or "")
        if not iid or not float(i.get("dph_total") or 0) or not i.get("start_date"):
            continue
        hist[iid] = {"id": i.get("id"), "gpu_name": i.get("gpu_name"), "dph_total": float(i.get("dph_total") or 0),
                     "start_date": i.get("start_date"), "end_date": i.get("end_date"),
                     "label": i.get("label") or "", "status": i.get("actual_status"), "last_seen": now}
        rec += 1
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        _history_path().write_text(json.dumps(hist))
    except Exception:
        pass
    return {"recorded": rec, "total_tracked": len(hist)}


def _all_instances():
    """Live instances UNION recorded history — so destroyed instances (gone from the API) are reconstructed from
    their last snapshot. Live state overrides history; a destroyed instance's runtime is capped at last_seen."""
    live = {str(i.get("id")): i for i in instances() if i.get("id")}
    merged = []
    hist = _load_history()
    for iid, h in hist.items():
        if iid in live:
            continue
        h = dict(h)
        if not h.get("end_date"):                          # destroyed while running → cap at last seen
            h["end_date"] = h.get("last_seen")
        merged.append(h)
    merged.extend(live.values())
    return merged


def gpu_rows(now=None, label_map=None):
    """Per (project, gpu) cumulative GPU cost-to-date from CURRENTLY-visible instances, attributed by label.
    cost = dph_total × hours since start (running → now; exited keeps its last-seen runtime). Destroyed
    instances aren't listed here — their spend is in the vast.ai invoice total (the GPU reconcile gap, mirroring
    the LLM unattributed gap). Returns rows ready to map into ledger pushes."""
    now = now or time.time()
    agg = {}
    for i in _all_instances():
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
    for i in _all_instances():
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


def _reconcile(allrows, account_total, conn, ptmap):
    """Account-anchored, label-attributed GPU reconcile (PURE → testable). Every row's project comes from its
    instance LABEL (the GPU ground truth); `ptmap` maps project → (org, team). This connection pushes ONLY its own
    project's boxes (`mine`); the account remainder is surfaced as an EXPLICIT `residual` (= account_total − Σ all
    recorded boxes), NEVER dumped on a project/org — so a SHARED vast.ai account can't leak cross-org and no flat
    per-day rows are fabricated. `by_org` is a diagnostic of where the recorded spend landed. residual → 0 only when
    every box is captured/recovered AND account_total is true consumption (top-ups carry a balance buffer)."""
    proj = (conn.get("project") or "").lower()
    captured = round(sum(r["cost"] for r in allrows), 2)
    residual = round((account_total or 0) - captured, 2)
    by_org = {}
    for r in allrows:
        org = ptmap.get((r.get("project") or "").lower(), ("", ""))[0] or "(untagged)"
        by_org[org] = round(by_org.get(org, 0.0) + r["cost"], 2)
    mine = [r for r in allrows if (r.get("project") or "") == proj and r["cost"] > 0]
    return {"mine": mine, "captured": captured, "account_total": round(account_total or 0, 2),
            "residual": residual, "by_org": by_org}


def record_recovered(box):
    """Record a box DESTROYED before snapshotting began, reconstructed from evidence (e.g. session transcripts), so
    it flows through the normal reconcile. Same shape as a live instance + `source:"recovered"` (id, gpu_name,
    dph_total, start_date, end_date, label). Idempotent by id. The runtime is an ESTIMATE — the durable fix is
    continuous `snapshot()` so boxes are captured live, not reconstructed after the fact."""
    hist = _load_history()
    b = dict(box)
    b["source"] = "recovered"
    b.setdefault("last_seen", b.get("end_date"))
    hist[str(b["id"])] = b
    try:
        _history_path().write_text(json.dumps(hist))
    except Exception:
        pass
    return {"recovered": str(b["id"]), "total_tracked": len(hist)}


def sync(dry=False):
    """Push THIS repo's GPU spend (instances whose LABEL maps to this repo's project), per-day, via this repo's key
    → its org (`_reconcile` → `mine`). Account-anchored: the unrecoverable remainder is returned as an EXPLICIT
    `residual` (account total − Σ recorded boxes), surfaced for visibility but NEVER dumped on a project/org (a
    shared vast.ai account would otherwise leak cross-org). snapshot() runs first so live boxes are captured."""
    from . import saas, budget
    c = saas.conn()
    proj = (c.get("project") or budget._project() or "").lower()
    ref = saas.contributor()
    snapshot()                                             # RECORD live instances first (so destroyed ones survive)
    from . import attribution
    _ptmap = attribution.project_team_map(attribution.taxonomy()[0])
    _team = lambda p: _ptmap.get((p or "").lower(), ("", ""))[1]
    allrows = gpu_rows_by_day()
    rec = _reconcile(allrows, account_gpu_total() if c.get("owns_account") else 0, c, _ptmap)
    day_totals = [{
        "day": r["day"], "provider": "vastai", "model": r["gpu"], "kind": "gpu", "channel": "realtime",
        "spend_micros": round(r["cost"] * 1_000_000), "calls": len(r["instances"]),
        "member_ref": ref, "project": proj, "team": _team(proj),
        "tags": ",".join(["remote-compute", "gpu", r["gpu"].replace(" ", ""), "team:" + _team(proj),
                          "instances:" + "/".join(str(x) for x in r["instances"])]),
    } for r in rec["mine"]]
    for row in day_totals:                                # per-row id, local↔server cross-check (gpu rows too)
        row["uid"] = saas._row_uid(row)
    reconcile = {"account_total": rec["account_total"], "captured": rec["captured"],
                 "residual": rec["residual"], "by_org": rec["by_org"]}
    payload = {"visibility": c.get("visibility"), "day_totals": day_totals}
    if dry:
        return {**payload, "reconcile": reconcile}
    ok, reason = saas.ready()
    if not ok:
        return {"skipped": f"not connected: {reason}", "reconcile": reconcile}
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private", "reconcile": reconcile}
    if not day_totals:                                    # nothing attributed to THIS project → don't 422 the push
        return {"skipped": "no attributed GPU for this project — label your vast.ai instances (include the project "
                "in the instance label) or set resources.vastai.label_map", "reconcile": reconcile}
    res = saas._request("POST", "/v1/ledger", payload)
    if isinstance(res, dict):
        res["reconcile"] = reconcile
    return res


def cmd(argv=None):
    argv = argv or []
    sub = argv[0] if argv else "show"
    if sub == "snapshot":                                  # record live instances → history (cron this; runs on sync too)
        print("resources snapshot:", snapshot())
        return 0
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
    residual = round(truth - attributed, 2)
    print(f"  {'→ residual':14} ${residual:8.2f}  (account − attributed; should ≈ unspent balance buffer)")
    # Process self-check: a LARGE residual means a project/tenant is UNDER-recovered (destroyed boxes not yet
    # reconstructed) — surface it loudly so it gets attributed, never silently dumped or ignored.
    if truth and residual > max(25.0, 0.10 * truth):
        print(f"  ⚠  residual is {residual / truth * 100:.0f}% of the account — a project/tenant is UNDER-recovered. "
              "Recover its destroyed boxes (resources.record_recovered, evidence-anchored) so the gap lands on the "
              "right org, not floating. Durable fix: schedule snapshot() so boxes are captured live.")
    return 0
