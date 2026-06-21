#!/usr/bin/env python
"""Worklog canvas generator (basis for slack.py `slack push`). PER-ORG two-part canvas over the CANONICAL
4-category $ model — sourced from the PROD rollup (Part 1, single source of truth) + local content (Part 2).

  Part 1 · Spend & value
    HARD $ (real money):  ① LLM API costs (provider × model)   ② Remote compute (provider × machine)
                          + subscription $ you actually pay (the denominator)
    ESTIMATED value (plan-covered, "what it'd cost at API rates"):  ③ est chat value (claude.ai)   ④ est code-chat
                          value (Claude Code)   [⑤ est cowork value — placeholder, no source yet]
  Part 2 · Work done — clean per-(team,project) bullets, SYNTHESIZED from classified chat summaries + code prompts
                          (no titles, no bios). org→team×project everywhere — one taxonomy.

Part 1 numbers come from /tmp/worklog_prod.json (scripts/worklog_pull.mjs against prod). Part 2 work comes from the
local chat cache + Claude Code classifications (prod stores no content by design). The shipped slack.py will pull
Part 1 from a server endpoint with the connection key instead of the admin export.
"""
import json, collections, pathlib, argparse
from spendguard import chat, config, pricing, claudecode

HOME = pathlib.Path.home() / ".spendguard"
PLAN_MONTHLY = float(config._cfg_get("chat", "plan_monthly", 300) or 300)   # assumed Max + ChatGPT Pro; override in config
PRORATE = {"day": 1 / 30.0, "week": 7 / 30.0, "month": 1.0}
_CODE_DIGESTS = None


def _fmt(v):
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def _chat_signals(org, since):
    """Local: chat work signals (auto-summaries) by team→project for Part 2."""
    st = chat._load_state()
    sig = collections.defaultdict(lambda: collections.defaultdict(list))
    for c in st.get("convs", {}).values():
        if c.get("org") != org or max((c.get("days") or {"x": ""}).keys()) < since:
            continue
        team = c.get("team") or "—"
        for a in (c.get("allocation") or [{"project": c.get("project") or "", "pct": 100}]):
            s = chat._clean_summary(c.get("summary", ""))
            if a.get("project") and s:
                sig[team][a["project"]].append(s)
    return sig


def _code_signals(org, since):
    """Local: Claude Code work signals (prompts) by team→project, using the CACHED classifications (no re-classify)."""
    global _CODE_DIGESTS
    try:
        cls = json.loads((HOME / "claudecode_state.json").read_text()).get("cls", {})
    except Exception:
        cls = {}
    if _CODE_DIGESTS is None:
        _CODE_DIGESTS = claudecode._session_digests()
    sig = collections.defaultdict(lambda: collections.defaultdict(list))
    for d in _CODE_DIGESTS:
        a = cls.get(d["sid"])
        if not a or a.get("org") != org or (d["day"] and d["day"] < since):
            continue
        team = a.get("team") or "—"
        proj = a.get("project") or d["project"]
        if d.get("prompt"):
            sig[team][proj].append(d["prompt"])
    return sig


_SYS = (
    "Turn this period's work into a concise TEAM work-log. Each input line is `<key>: <raw signals>`. For each key, "
    "write 2-4 short bullets of what was DONE — concrete outcomes, present tense. STRICT: NO bios, NO "
    "names/companies, NO conversation titles, no 'CEO of', no filler. Output STRICT JSON only, reusing the EXACT "
    'numeric keys: {"0":["bullet",...],"1":[...]}.')


def _synth(items, run):
    from spendguard import adapters, calls, ui
    lines = [f"{k}: " + (" || ".join(s[:200] for s in sigs[:5])[:900]) for k, sigs in items.items()]
    prompt = "Work signals by key:\n" + "\n".join(lines)
    model = config.advisor_model()
    OUT = 110 * len(items) + 400
    if not run:
        from spendguard import ui as _ui
        _ui.estimate_only(action=f"synthesize work bullets for {len(items)} projects",
                          cost=pricing.realtime_cost(model, chat._toklen(_SYS + prompt), OUT))
        return {}
    with calls.context(intent="spendguard:worklog"):
        r = adapters.call(model, prompt, max_tokens=OUT, system=_SYS)
    import re
    txt = r.get("text", "")
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    out = {}
    for km in re.finditer(r'"(\d+)"\s*:\s*\[(.*?)\]', txt, re.S):
        try:
            out[km.group(1)] = json.loads("[" + km.group(2) + "]")
        except Exception:
            out[km.group(1)] = [s.strip().strip('"') for s in re.findall(r'"((?:[^"\\]|\\.)*)"', km.group(2))]
    return out


def generate(org, period, label, run=True):
    prod = json.loads(pathlib.Path("/tmp/worklog_prod.json").read_text())[period]
    since = prod["since"]
    llm, gpu = prod["llm_api"], prod["compute"]
    chat_val = collections.defaultdict(float)
    code_val = collections.defaultdict(float)
    for v in prod["value"]:
        (chat_val if v["channel"] == "claude-ai" else code_val)[v["team"]] += v["usd"]
    LLM = sum(x["usd"] for x in llm); GPU = sum(x["usd"] for x in gpu)
    CHAT = sum(chat_val.values()); CODE = sum(code_val.values())
    sub = PLAN_MONTHLY * PRORATE.get(period, 1.0)

    csig = _chat_signals(org, since); ksig = _code_signals(org, since)
    teams = sorted(set(csig) | set(ksig), key=lambda t: -(chat_val.get(t, 0) + code_val.get(t, 0)))
    units = []
    for team in teams:
        merged = collections.defaultdict(list)
        for src in (csig.get(team, {}), ksig.get(team, {})):
            for p, sg in src.items():
                merged[p].extend(sg)
        for p, sg in merged.items():
            units.append((team, p, sg))
    synth_in = {str(i): u[2] for i, u in enumerate(units) if u[2]}
    bullets = _synth(synth_in, run) if synth_in else {}

    o = []
    o.append(f"> _{org} worklog · **Part 1 → finance/admin · Part 2 → team** · period: **{label}**. Canonical "
             "spendguard rollup — Claude Code · web chat · LLM API · remote compute._\n")
    o.append("## Part 1 · Spend & value")
    o.append("_For finance / admin._\n")
    o.append("### Hard $ — real money")
    o.append(f"**① LLM API costs: {_fmt(LLM)}** _(provider × model)_")
    o.append("| Provider · model | $ |\n|---|---:|")
    for x in llm:
        nm = "batch (model-unattributed)" if x["model"] in ("(provider-batch)", "?") else f"{x['provider']} · {x['model']}"
        o.append(f"| {nm} | {_fmt(x['usd'])} |")
    o.append(f"\n**② Remote compute: {_fmt(GPU)}** _(provider × machine)_")
    if gpu:
        o.append("| Provider · machine | $ |\n|---|---:|")
        for x in gpu:
            mc = x["machine"] if x["machine"] not in ("?",) else "(machine untracked)"
            o.append(f"| {x['provider']} · {mc} | {_fmt(x['usd'])} |")
    else:
        o.append("_(none this period)_")
    o.append(f"\n**Subscription you pay ≈ {_fmt(sub)}** this {period} _(assumed ${PLAN_MONTHLY:.0f}/mo Max + ChatGPT Pro — set in config)_\n")
    o.append("### Estimated value — plan-covered (what it would cost at API rates, not money out)")
    o.append(f"**③ est chat value: {_fmt(CHAT)}** _(claude.ai / desktop)_   ·   **④ est code-chat value: {_fmt(CODE)}** _(Claude Code)_")
    o.append("| Team | est chat value | est code-chat value |\n|---|---:|---:|")
    for t in sorted(set(chat_val) | set(code_val), key=lambda t: -(chat_val.get(t, 0) + code_val.get(t, 0))):
        o.append(f"| {t} | {_fmt(chat_val.get(t,0)) if chat_val.get(t) else '—'} | {_fmt(code_val.get(t,0)) if code_val.get(t) else '—'} |")
    o.append(f"| **Total** | **{_fmt(CHAT)}** | **{_fmt(CODE)}** |")
    o.append("_⑤ est cowork value: no data source yet (no Cowork adapter)._\n")
    o.append("## Part 2 · Work done")
    o.append("_For the team — what shipped, by team → project (chat + code)._\n")
    cur = None
    for i, (team, proj, sg) in enumerate(units):
        if team != cur:
            o.append(f"### {team}"); cur = team
        o.append(f"**{proj}**")
        for b in (bullets.get(str(i)) or [])[:4]:
            o.append(f"- {b}")
        if not bullets.get(str(i)):
            o.append("- _(activity captured)_")
        o.append("")
    o.append("---")
    o.append(f"_spendguard · {label} · ① LLM API + ② remote compute = HARD $ (real money); ③ chat + ④ code-chat = "
             "ESTIMATED plan-covered value (tokens × API price). One taxonomy: every $ and every work item rolls to "
             "org → team × project._")
    return "\n".join(o)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="Healiom")
    ap.add_argument("--period", required=True, choices=["day", "week", "month"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    print(generate(a.org, a.period, a.label, run=a.run))
