"""cli reconcile dispatch is honest about the provider (Cat-4 CLI/exit honesty):

  * an unknown provider word is an ERROR (exit 2), not a silent fall-through to openai that prints the wrong
    provider's numbers under the name the user typed.
  * a leading FLAG (e.g. `reconcile --since 2026-08-01`) is NOT eaten as the provider — it reaches the
    provider tool as an argument, and the default provider (openai) still applies.
  * `anthropic` / `all` / (omitted→openai) route to the right place with their args intact.

Offline, isolated home; the provider tools' main()/report() are stubbed so nothing hits the network.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-clirec-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import cli                      # noqa: E402
import spendguard.reconcile_openai as roa       # noqa: E402
import spendguard.reconcile_anthropic as raa    # noqa: E402
import spendguard.reconcile as rec              # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


seen = {}
roa.main = lambda: (seen.__setitem__("openai", list(sys.argv)), 0)[1]
raa.main = lambda: (seen.__setitem__("anthropic", list(sys.argv)), 0)[1]
rec.report = lambda ptmap=None, since=None: seen.__setitem__("all", {"since": since})   # capture the window passed

# an unknown provider word → error exit 2, and NOTHING dispatched
seen.clear()
rc = cli._dispatch(["reconcile", "bogus"])
ck("an unknown provider word exits 2 (not a silent openai run)", rc == 2 and not seen, f"rc={rc} seen={seen}")

# a leading flag is PRESERVED (reaches the tool), not eaten as the provider; default provider = openai
seen.clear()
cli._dispatch(["reconcile", "--since", "2026-08-01"])
ck("a leading --since flag reaches the openai tool (not eaten as the provider)",
   seen.get("openai") == ["reconcile", "--since", "2026-08-01"], f"got {seen}")

# explicit anthropic routes to the anthropic tool, with its args
seen.clear()
cli._dispatch(["reconcile", "anthropic", "--by-day"])
ck("`reconcile anthropic --by-day` routes to the anthropic tool with its args",
   seen.get("anthropic") == ["reconcile", "--by-day"], f"got {seen}")

# an omitted provider defaults to openai
seen.clear()
cli._dispatch(["reconcile"])
ck("`reconcile` (no provider) defaults to openai", seen.get("openai") == ["reconcile"], f"got {seen}")

# `all` routes to the unified report
seen.clear()
cli._dispatch(["reconcile", "all"])
ck("`reconcile all` routes to the unified report", seen.get("all") == {"since": None}, f"got {seen}")

# `all --since DATE` now PASSES the window through — it used to be parsed into prov_args and silently dropped
seen.clear()
cli._dispatch(["reconcile", "all", "--since", "2026-08-01"])
ck("`reconcile all --since DATE` passes the window to the unified report (was silently ignored)",
   (seen.get("all") or {}).get("since") == "2026-08-01", f"got {seen}")

print(f"\n{'[FAIL]' if fails else 'OK'} test_cli_reconcile_dispatch: {fails} failure(s)")
sys.exit(1 if fails else 0)
