"""Enforce the spend gate on DISTRIBUTED / REMOTE compute (vast.ai boxes, any SSH-reachable host).

The gate only governs the interpreter it's loaded in — a freshly-spun-up box's `python3` is UNGATED until it's
provisioned, so remote LLM scripts can spend silently. This makes remote gating STRUCTURAL, in three layers:

  • onstart : the boot snippet that installs + hooks spendguard so EVERY `python3` on the box is gated from boot —
              bake it into the instance's onstart / provisioning script, so it covers ALL scripts, not one.
  • verify  : a FAIL-CLOSED check — SSH in, run `doctor`, and return ok=False on any error/uncertainty, so the
              orchestrator (or the box itself) REFUSES to spend rather than spend ungated.
  • sync    : pull the box's local ledger (realtime + gate logs) and roll the spend into the local ledger under the
              box's project — so an ephemeral box's spend is attributed to the org and survives teardown. Idempotent
              (re-syncing the same box replaces, never double-counts — never destroy a box mid-spend as cost control).

The principle: gate at PROVISION, verify before SPEND, sync before TEARDOWN — enforcement moves from "remembered
per script" to structural-by-construction. `spendguard remote {onstart|verify|sync}`.
"""
import json
import shlex
import subprocess

_DEFAULT_HOME = "/root/.spendguard"
_PKG = "llm-spendguard"
_GIT = "git+https://github.com/llmspendguard/llm-spendguard"


# ── onstart: the provision-time gate (pure; no SSH — you bake the output into the box's onstart) ──
def onstart_snippet(home: str = _DEFAULT_HOME, from_git: bool = False) -> str:
    """Bash to install + hook spendguard so every python3 on the box is gated from boot. Idempotent + secret-free
    (attribution happens at `sync` time, so no key lives on the box). `--from-git` pulls latest main instead of PyPI."""
    src = _GIT if from_git else _PKG
    return "\n".join([
        "# --- spendguard: gate every python3 on this box (idempotent, secret-free) ---",
        f"export SPENDGUARD_HOME={shlex.quote(home)}",
        f"python3 -c 'import spendguard' 2>/dev/null || pip install -q {src}",
        'python3 -m spendguard install-hook --user --python "$(command -v python3)" >/dev/null 2>&1 || true',
        'python3 -m spendguard doctor 2>&1 | grep -q "ENFORCING HERE.*YES" '
        '&& echo "[spendguard] box gated" || echo "[spendguard] WARN: gate NOT enforcing"',
    ])


# ── verify: fail-closed enforcement check (the orchestrator aborts if a box isn't gated) ──
def verify(ssh: str, timeout: int = 30, _run=None) -> tuple:
    """Run `<ssh> python3 -m spendguard doctor` and return (ok, detail). FAIL-CLOSED: any error, timeout, or
    uncertainty → ok=False, so callers refuse to launch LLM work on an ungated box. `ssh` is the full prefix,
    e.g. 'ssh -i ~/.ssh/vastai_ed25519 -p 12345 root@1.2.3.4'."""
    run = _run or subprocess.run
    try:
        r = run(f"{ssh} python3 -m spendguard doctor", shell=True, capture_output=True, text=True, timeout=timeout)
        out = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
        tail = out.split("ENFORCING HERE", 1)[1][:24] if "ENFORCING HERE" in out else ""
        ok = bool(tail) and "YES" in tail
        return ok, ("ENFORCING" if ok else "NOT enforcing — refusing to spend (fail-closed)")
    except Exception as e:
        return False, f"verify failed ({e}) — fail-closed"


# ── sync: roll the box's ledger up to the org before teardown (idempotent) ──
def _parse_rt_log(text: str):
    """Parse a realtime_log.jsonl (per-day-per-model rollup) → [{day, model, provider, cost}]. Tolerant."""
    rows = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("cost"):
            rows.append({"day": o.get("day") or "", "model": o.get("model") or "?",
                         "provider": o.get("provider") or "?", "cost": float(o.get("cost") or 0)})
    return rows


def sync(ssh: str, project: str, label: str = None, home: str = _DEFAULT_HOME, timeout: int = 60, _run=None) -> dict:
    """Pull the box's realtime_log.jsonl and roll its spend into the LOCAL ledger tagged `project` (so the org sees
    it and it survives teardown). Idempotent: keyed by conv_id `remote:<label>` — re-syncing the same box REPLACES
    its prior rows, never double-counts. `label` defaults to the ssh target. Returns {rows, usd, project}."""
    run = _run or subprocess.run
    label = label or ssh.split()[-1] if ssh else "remote"
    try:
        r = run(f"{ssh} cat {shlex.quote(home)}/realtime_log.jsonl 2>/dev/null",
                shell=True, capture_output=True, text=True, timeout=timeout)
        rows = _parse_rt_log(getattr(r, "stdout", "") or "")
    except Exception as e:
        return {"error": f"pull failed ({e})", "rows": 0, "usd": 0.0, "project": project}
    from . import budget
    n, usd = budget.ingest_remote(label, project, rows)
    return {"rows": n, "usd": round(usd, 4), "project": project, "label": label}


# ── CLI ──
def cmd(argv=None):
    argv = list(argv or [])
    sub = argv[0] if argv else ""

    def _opt(flag, default=None):
        return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else default

    if sub == "onstart":
        print(onstart_snippet(home=_opt("--home", _DEFAULT_HOME), from_git="--from-git" in argv))
        return 0
    if sub == "verify":
        ssh = _opt("--ssh")
        if not ssh:
            print("usage: spendguard remote verify --ssh '<ssh prefix>'"); return 2
        ok, detail = verify(ssh)
        print(f"[spendguard remote] {ssh.split()[-1] if ssh else ''}: {detail}")
        return 0 if ok else 1                     # non-zero → fail-closed: the orchestrator aborts the launch
    if sub == "sync":
        ssh = _opt("--ssh"); project = _opt("--project")
        if not ssh or not project:
            print("usage: spendguard remote sync --ssh '<ssh prefix>' --project <name> [--label X] [--home P]")
            return 2
        res = sync(ssh, project, label=_opt("--label"), home=_opt("--home", _DEFAULT_HOME))
        print(f"[spendguard remote] sync {res.get('label')}: {res.get('rows', 0)} rows · "
              f"${res.get('usd', 0):.2f} → project {project}" + (f"  ({res['error']})" if res.get("error") else ""))
        return 0
    print("usage: spendguard remote {onstart [--from-git] | verify --ssh '<prefix>' | sync --ssh '<prefix>' --project X}")
    return 2
