"""REAL end-to-end proof of the lane-runtime-PATH fix: spawn codex under a STRIPPED PATH (no nvm) and confirm it
ACTUALLY RUNS.

A unit test of config.lane_exec_env only proves the helper prepends a directory string. It does NOT prove the lane
CLI then runs: the codex binary is a `#!/usr/bin/env node` shebang, so under a GUI/daemon minimal PATH (no nvm) the
spawn fails with `env: node: No such file or directory` — the exact production breakage. Only a live spawn under a
stripped PATH proves node is found next to the binary and codex executes. So this is the test that would have
CAUGHT the bug, and it FAILS if the fix is reverted.

It is a LIVE lane call ($0, plan-billed — a codex subscription prompt, no metered key). It needs the codex lane +
its login, which a public CI runner does not have, so it is OPT-IN and a NAMED SKIP otherwise — never a silent
pass. Enable with SPENDGUARD_LIVE_LANE_TEST=1 on a host where `spendguard lanes --usage` shows codex ready.
"""
import os
import shutil
import sys

if os.environ.get("SPENDGUARD_LIVE_LANE_TEST") != "1":
    print("SKIP (NAMED GAP, not a silent pass): set SPENDGUARD_LIVE_LANE_TEST=1 on a host with a ready codex lane "
          "to run the live minimal-PATH end-to-end proof (a $0 plan call).")
    sys.exit(0)

# Simulate the GUI/daemon minimal PATH BEFORE importing spendguard, so the lane subprocess inherits it: strip nvm
# and everything but the base system dirs. The fix must make codex run anyway by resolving node next to its binary.
os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"

import spendguard                                   # noqa: E402
spendguard.require()
from spendguard import codex_exec                   # noqa: E402


def check(results, label, ok, extra=""):
    results.append(bool(ok))
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


r = []

# Precondition: the test only PROVES something if node is genuinely unreachable via the ambient PATH.
check(r, "precondition: node is NOT on the stripped PATH (so a pass is due to the fix, not ambient node)",
      shutil.which("node") is None, f"shutil.which('node')={shutil.which('node')!r}")

# The codex binary itself resolves independently of PATH (config.resolve_cli: pin -> PATH -> well-known dirs).
check(r, "the codex binary resolves (via resolve_cli, not the stripped PATH)", codex_exec.available(),
      "codex binary not resolvable — set SPENDGUARD_CODEX_BIN or install the codex CLI")

# THE PROOF: run a real codex prompt under the stripped PATH. Without the fix the shebang's `env node` fails and
# run_prompt returns {"error": ...}; with the fix node is found next to the binary and codex answers.
resp = codex_exec.run_prompt("Reply with exactly the token LANE_OK and nothing else.", timeout=150)
_resp_err = resp.get("error") if isinstance(resp, dict) else resp
_resp_text = resp.get("text") if isinstance(resp, dict) else None
check(r, "codex RAN under the stripped PATH — the fix found its co-located node (no 'env: node' failure)",
      isinstance(resp, dict) and not resp.get("error") and bool(resp.get("text")),
      "error=%r" % (_resp_err,))
check(r, "codex returned a usable answer containing the expected token",
      isinstance(resp, dict) and bool(_resp_text) and "LANE_OK" in (_resp_text or ""),
      "text=%r" % (_resp_text,))

# THE WARM-DAEMON PROOF: the `codex app-server` warm daemon must HANDSHAKE and serve under the stripped PATH too.
# The daemon spawns the realpath of the `codex` symlink — a .../node_modules/.../codex.js whose dir has NO node —
# so lane_exec_env must prepend the LAUNCHER (symlink) dir, not realpath's dir, or the shebang's `env node` dies
# and the initialize handshake stalls the full STARTUP_TIMEOUT (~25s) on EVERY call. A pass here is a turn served
# well under that budget, proving the handshake completed (not fell back after a 25s stall).
import time                                                  # noqa: E402
from spendguard import codex_daemon                          # noqa: E402
_t = time.time()
warm = codex_daemon._CodexDaemon().run_warm("Reply with exactly the token WARM_OK and nothing else.",
                                            reasoning="low", timeout=60)
_warm_s = time.time() - _t
_warm_text = warm.get("text") if isinstance(warm, dict) else None
check(r, "the WARM codex app-server handshakes + serves under the stripped PATH (no 25s stall)",
      isinstance(warm, dict) and not warm.get("error") and bool(_warm_text) and _warm_s < 20,
      "%.1fs err=%r text=%r" % (_warm_s, (warm or {}).get("error"), _warm_text))

print(f"\n{'OK' if all(r) else '[FAIL]'} test_live_lane_minimal_path")
sys.exit(0 if all(r) else 1)
