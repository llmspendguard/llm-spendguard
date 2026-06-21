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
import re
import collections
import urllib.request

from . import config

_GPU_KW = re.compile(r"vast\.?ai|\bgpu\b|gliner|\b(?:h100|h200|a100|3090|4090|rtx)\b|\bcuda\b|fine-?tun|"
                     r"training run|gpu (?:box|instance|fleet|fanout)", re.I)   # GPU-specific; NOT bare 'training'


def _gpu_alignment(since):
    """Cross-connect GPU spend to the CONVERSATIONS that ran it, for attribution — the same conversation→project
    mapping LLM batches use (conv.attribute_usage). chat convs + code sessions mentioning GPU work, carrying their
    CLASSIFIED (org, team, project), → {(day, project): {"w", "org", "team"}}. So the destroyed-instance gap lands
    on the project that actually ran the GPU (e.g. an NER training run, a video-generation job), dated to those days
    + org-routable. vast.ai has no per-day consumption AND no audit log, so conversations are the only context.
    RESTRICTED to GPU-capable scope (projects/teams that actually have GPU instances, by label) so a stray keyword
    can't land GPU on a non-GPU project."""
    from . import attribution
    taxo, _ = attribution.taxonomy()
    ptmap = attribution.project_team_map(taxo)
    gpu_projs, gpu_teams = set(), set()                     # the ground truth: where GPU instances actually ran
    for i in _all_instances():
        p = (project_of(i.get("label")) or "").lower()
        if p:
            gpu_projs.add(p)
            org, team = ptmap.get(p, ("", ""))
            if team:
                gpu_teams.add(((org or "").lower(), team.lower()))
    agg = {}

    def add(day, org, team, proj):
        if not proj or day < since:
            return
        proj, org, team = proj.lower(), (org or "").lower(), (team or "").lower()
        if gpu_projs and not (proj in gpu_projs or (org, team) in gpu_teams):
            return                                          # not a GPU-capable project/team → exclude (no false-positive)
        e = agg.setdefault((day, proj), {"w": 0, "org": org, "team": team})
        e["w"] += 1
    try:
        from . import chat
        for c in chat._load_state().get("convs", {}).values():
            blob = (c.get("title", "") + " " + c.get("summary", "") + " " + c.get("first_user", "")).lower()
            if _GPU_KW.search(blob):
                for day in (c.get("days") or {}):
                    add(day, c.get("org"), c.get("team"), c.get("project") or c.get("ai_project"))
    except Exception:
        pass
    try:
        from . import claudecode
        cls = claudecode.load_cls()
        for d in claudecode._session_digests():
            if d.get("day", "") >= since and _GPU_KW.search((d.get("prompt") or "").lower()):
                a = cls.get(d["sid"]) or {}
                add(d["day"], a.get("org"), a.get("team"), a.get("project") or d.get("project"))
    except Exception:
        pass
    return agg

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


def sync(dry=False):
    """Push THIS repo's GPU spend (instances whose label maps to this repo's project), per-day, via this repo's
    key → its org. The owns_account connection ALSO reconciles: gap = vast.ai account total − Σ attributed →
    pushed as 'unattributed' remote-compute (destroyed/untracked instances), mirroring the LLM unattributed gap."""
    import datetime
    from . import saas, budget
    c = saas.conn()
    proj = (c.get("project") or budget._project() or "").lower()
    ref = saas.contributor()
    snapshot()                                             # RECORD live instances first (so destroyed ones survive)
    from . import attribution
    _ptmap = attribution.project_team_map(attribution.taxonomy()[0])
    _team = lambda p: _ptmap.get((p or "").lower(), ("", ""))[1]
    allrows = gpu_rows_by_day()
    day_totals = [{
        "day": r["day"], "provider": "vastai", "model": r["gpu"], "kind": "gpu", "channel": "realtime",
        "spend_micros": round(r["cost"] * 1_000_000), "calls": len(r["instances"]),
        "member_ref": ref, "project": proj, "team": _team(proj),
        "tags": ",".join(["remote-compute", "gpu", r["gpu"].replace(" ", ""), "team:" + _team(proj),
                          "instances:" + "/".join(str(x) for x in r["instances"])]),
    } for r in allrows if (r.get("project") or "") == proj and r["cost"] > 0]
    if c.get("owns_account"):                              # account-level GPU reconcile (one vast.ai account)
        # Only reconcile the blanket gap when this vast.ai account is SINGLE-PROJECT. A shared multi-project account
        # (e.g. nlp-pipeline A100 + vision-pipeline H200) makes the destroyed/untracked gap CROSS-ORG — dumping it as this
        # org's 'unattributed' would pull another org's GPU spend in (and vast.ai exposes no per-instance billing to
        # split it, and prepaid top-ups are lumpy = a balance buffer, not consumption). So: multi-project account →
        # push only per-project attributed consumption (each org reconciles its own); single-project → the gap is
        # this project's (the "primary task" rule), not 'unattributed'.
        projs_present = {(r.get("project") or "") for r in allrows if r.get("project")}
        if len(projs_present) <= 1:
            gap = round(account_gpu_total() - sum(r["cost"] for r in allrows), 2)
            if gap > 0.5:
                # The remaining gap = instances destroyed BEFORE snapshotting began (unrecoverable from vast.ai —
                # no per-day consumption, no audit log). CROSS-CONNECT to conversations: attribute by the (day,
                # project, org, team) of the GPU-work convs/sessions, the same way LLM batches map via conv.
                # attribute_usage. Org-routed (only this connection's org) so each project's GPU lands in its own org.
                since = datetime.date.fromtimestamp(_month_start_ts()).isoformat()
                conn_org = (c.get("org") or "").lower()
                align = {k: v for k, v in _gpu_alignment(since).items()
                         if not (conn_org and v["org"] and v["org"] != conn_org)}    # org-route
                tot = sum(v["w"] for v in align.values())
                if tot:
                    for (day, aproj), v in align.items():
                        amt = gap * v["w"] / tot
                        if amt > 0.005:
                            day_totals.append({
                                "day": day, "provider": "vastai", "model": "(reconstructed, conv-aligned)", "kind": "gpu",
                                "channel": "realtime", "spend_micros": round(amt * 1_000_000), "calls": 0,
                                "member_ref": ref, "project": aproj, "team": v["team"],
                                "tags": f"remote-compute,gpu,reconstructed,team:{v['team']}"})
                else:
                    day_totals.append({
                        "day": datetime.date.today().isoformat(), "provider": "vastai", "model": "(destroyed/untracked)",
                        "kind": "gpu", "channel": "realtime", "spend_micros": round(gap * 1_000_000), "calls": 0,
                        "member_ref": ref, "project": proj, "tags": f"remote-compute,gpu,destroyed,{proj}"})
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
    if not day_totals:                                    # nothing attributed to THIS project → don't 422 the push
        return {"skipped": "no attributed GPU for this project — label your vast.ai instances (include the project "
                "in the instance label) or set resources.vastai.label_map; destroyed instances are unrecoverable per-project"}
    return saas._request("POST", "/v1/ledger", payload)


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
    print(f"  {'→ ungoverned':14} ${max(0, truth - attributed):8.2f}  (destroyed/untracked instances)")
    return 0
