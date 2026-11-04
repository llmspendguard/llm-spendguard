"""Tiny shared CLI UI helpers (zero-dependency).

Keeps the "nothing was spent — re-run with --run" messaging LOUD and IDENTICAL across every caged, estimate-first
command (optimize / mine / reconstruct / review / experiment / promote / conv-synth / cache-test / cascade /
bootstrap). Before this, each printed its own quiet one-liner that was easy to miss — so a user could think a job
ran when it only estimated. One banner, one shape, hard to overlook.
"""
import sys


def estimate_only(action="execute the paid step", flag="--run", cost=None,
                  note="caged by caps.meta — estimate is free", file=sys.stderr):
    """Print a loud, consistent ESTIMATE-ONLY banner for the dry path of a caged command.

    action : what `--run` would actually do (e.g. "judge output quality", "submit the batch").
    flag   : the flag that executes it (default --run; cascade uses --run too).
    cost   : projected USD if it were run (shown when known) — sets expectations before spend.
    note   : the cap/budget context line.
    file   : stream (stderr by default, so the banner stands out from the command's data on stdout).
    """
    lines = ["🟡  ESTIMATE ONLY — nothing was spent."]
    if cost is not None:
        try:
            lines.append(f"    if you run it, projected spend ≈ ${float(cost):.2f}")
        except (TypeError, ValueError):
            pass
    lines.append(f"    ▶ re-run with {flag} to {action}." + (f"  ({note})" if note else ""))
    rule = "  " + "─" * 66
    print("\n" + rule, file=file)
    for ln in lines:
        print("  " + ln, file=file)
    print(rule, file=file)
