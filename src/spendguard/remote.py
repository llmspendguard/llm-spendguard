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

# The doctor line this module reads to decide whether a box may spend. IMPORTED FROM THE EMITTER, not retyped:
# `gate` prints this line and this module parses it, so they must be the same strings by construction. Reading
# back a format we ourselves wrote is parsing; an unrecognised line is answered UNKNOWN below, never "no".
from .gate import ENFORCING_MARKER as _MARKER, ENFORCING_YES as _YES, ENFORCING_NO as _NO


def _ssh_run(run, ssh: str, remote_cmd: str, timeout: int):
    """Run one command on the box. The ssh prefix is SPLIT INTO ARGV and the remote command is passed as a single
    argument, with shell=False — so nothing in either can be interpreted by the LOCAL shell.

    This used to be an f-string into shell=True. The ssh prefix is not hand-typed, it comes from a provider API
    (vast.ai instance records), which means a hostile or merely malformed field was a local command execution on
    the operator's machine — `root@1.2.3.4; rm -rf ~` is a valid-looking string. Splitting to argv removes the
    local shell from the path entirely; the REMOTE shell still interprets remote_cmd, which is what ssh is for
    and what the `||` fallback and redirects here need."""
    argv = shlex.split(ssh) + [remote_cmd]
    return run(argv, shell=False, capture_output=True, text=True, timeout=timeout)


def _enforcing(out: str):
    """(verdict, line) from doctor output — verdict is True / False / None, where None means THE ANSWER WAS NOT
    FOUND. Three states, never two: 'no answer' is not 'no'.

    Scoped to the LINE carrying the marker. It previously read the 24 characters following the marker across
    stdout and stderr CONCATENATED, so any 'YES' within 24 chars — on the next line, or in a warning that got
    glued on from the other stream — satisfied a check whose entire job is to refuse. A fail-open in the one
    function whose docstring promises fail-closed."""
    for ln in (out or "").splitlines():
        if _MARKER not in ln:
            continue
        tail = ln.split(_MARKER, 1)[1]
        # first standalone YES/NO token on that line settles it; anything else on the line is decoration
        for tok in tail.replace(":", " ").replace("—", " ").replace("-", " ").split():
            up = tok.strip().upper()
            if up == _YES:
                return True, ln.strip()
            if up == _NO:
                return False, ln.strip()
        return None, ln.strip()          # marker present, neither answer on it → unknown, not "no"
    return None, ""
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
        # prefer the console script (present in every published build); fall back to `python3 -m` (editable/unreleased)
        'SG="$(command -v spendguard || echo python3 -m spendguard)"',
        '$SG install-hook --user --python "$(command -v python3)" >/dev/null 2>&1 || true',
        # grep is LINE-scoped, so this cannot span lines the way the old python-side 24-char window could;
        # the pattern is built from the same shared constants the emitter uses, so it cannot drift either.
        f'$SG doctor 2>&1 | grep -q {shlex.quote(_MARKER + ".*" + _YES)} '
        '&& echo "[spendguard] box gated" || echo "[spendguard] WARN: gate NOT enforcing"',
    ])


# ── verify: fail-closed enforcement check (the orchestrator aborts if a box isn't gated) ──
def verify(ssh: str, timeout: int = 30, _run=None) -> tuple:
    """Run `<ssh> python3 -m spendguard doctor` and return (ok, detail). FAIL-CLOSED: any error, timeout, or
    uncertainty → ok=False, so callers refuse to launch LLM work on an ungated box. `ssh` is the full prefix,
    e.g. 'ssh -i ~/.ssh/vastai_ed25519 -p 12345 root@1.2.3.4'."""
    run = _run or subprocess.run
    try:
        # console script (published) with `python3 -m` fallback (editable) — run the whole thing ON the box
        r = _ssh_run(run, ssh, "spendguard doctor 2>/dev/null || python3 -m spendguard doctor", timeout)
        # Streams are read SEPARATELY. Concatenating them let a fragment of stderr complete a marker that
        # started in stdout; each stream is now searched on its own, and stdout — where doctor actually
        # prints — is authoritative when both carry an answer.
        saw_marker = ""
        for stream in (getattr(r, "stdout", "") or "", getattr(r, "stderr", "") or ""):
            verdict, line = _enforcing(stream)
            if verdict is not None:
                return verdict, (f"ENFORCING ({line})" if verdict
                                 else f"NOT enforcing — refusing to spend (fail-closed): {line}")
            saw_marker = saw_marker or line
        # Say which of the two unknowns it was. An operator chasing "no marker" looks at the install; one
        # chasing "marker with no answer" looks at a version mismatch. A diagnostic that names the wrong
        # one costs the same hour this whole module exists to save.
        why = (f"'{_MARKER}' present but carried neither {_YES} nor {_NO} ({saw_marker!r}) — likely an older "
               f"spendguard on the box" if saw_marker else f"no '{_MARKER}' line in doctor output")
        return False, (f"{why} (rc={getattr(r, 'returncode', '?')}) — cannot confirm the box is gated, "
                       f"so refusing to spend (fail-closed)")
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
        r = _ssh_run(run, ssh, f"cat {shlex.quote(home)}/realtime_log.jsonl 2>/dev/null", timeout)
        # A FAILED PULL IS NOT AN EMPTY LEDGER. rc was never checked, so an auth failure, an unreachable host,
        # or a missing file all returned {"rows": 0, "usd": 0.0} — indistinguishable from a box that genuinely
        # spent nothing. That is the reading under which you tear a box down and lose its spend for good,
        # believing there was none. Report the failure; the caller must not record this as "synced".
        rc = getattr(r, "returncode", 0)
        if rc:
            return {"error": f"pull failed (rc={rc}: {(getattr(r, 'stderr', '') or '').strip()[:160]}) — "
                             f"spend on this box is UNKNOWN, not zero; do not tear it down on this result",
                    "rows": 0, "usd": 0.0, "project": project, "label": label}
        rows = _parse_rt_log(getattr(r, "stdout", "") or "")
    except Exception as e:
        return {"error": f"pull failed ({e}) — spend on this box is UNKNOWN, not zero",
                "rows": 0, "usd": 0.0, "project": project, "label": label}
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
