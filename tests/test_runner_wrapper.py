"""`spendguard run -- <cmd>` — gate a process WITHOUT writing a startup hook into anyone's interpreter.

Why this exists: `install-hook` writes sitecustomize/usercustomize into a venv. That mechanism became a security
anti-pattern on 2026-03-24, when litellm 1.82.8 shipped a malicious `litellm_init.pth` that ran a credential
stealer at every interpreter start. Same mechanism class, same product category. The wrapper does what
`ddtrace-run`/`opentelemetry-instrument` do — bootstrap dir on the CHILD's PYTHONPATH — so:
  • nothing is written into site-packages and nothing persists after the process exits;
  • the effect is scoped to ONE command;
  • the bootstrap is generated locally (never downloaded) and printable with `--show` for review;
  • it CHAINS to a host's own sitecustomize instead of shadowing it;
  • it is fail-open — a broken gate must never stop the user's job.
Offline: no network, no model calls; the child is a plain python -c.
"""
import os, subprocess, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-runner-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import runner

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


print("-- the bootstrap is generated locally, reviewable, and self-contained --")
d = runner.bootstrap_dir()
boot = os.path.join(d, "sitecustomize.py")
src = open(boot).read()
check("bootstrap file written under SPENDGUARD_HOME (not site-packages)",
      os.path.isfile(boot) and os.environ["SPENDGUARD_HOME"] in boot)
check("it installs the gate", "spendguard.install()" in src)
check("it is fail-OPEN (a broken gate never stops the job)", "except Exception" in src)
check("it chains to a host sitecustomize instead of shadowing it", "_host_sitecustomize" in src)
check("no network/download anywhere in the bootstrap",
      not any(w in src for w in ("urllib", "requests", "http", "curl", "pip install")))
check("regenerating is idempotent", runner.bootstrap_dir() == d and open(boot).read() == src)
open(boot, "w").write("# tampered\n")
runner.bootstrap_dir()
check("a tampered bootstrap is restored from source on next run", "spendguard.install()" in open(boot).read())

print("-- env scoping: PYTHONPATH prepend, nothing global --")
e = runner.child_env({"PATH": "/usr/bin", "PYTHONPATH": "/existing/path"})
check("bootstrap dir is PREPENDED (wins over any other sitecustomize)", e["PYTHONPATH"].startswith(d))
check("an existing PYTHONPATH is preserved, not clobbered", "/existing/path" in e["PYTHONPATH"])
check("marks the child so doctor can explain why it's gated", e.get("SPENDGUARD_VIA_RUN") == "1")
check("os.environ itself is untouched", "SPENDGUARD_VIA_RUN" not in os.environ)
e2 = runner.child_env({"PATH": "/usr/bin"})
check("no prior PYTHONPATH → just the bootstrap dir", e2["PYTHONPATH"] == d)

print("-- END-TO-END: a child process is armed BEFORE its own code runs, only under the wrapper --")
PROBE = "import sys; print('ARMED' if 'spendguard' in sys.modules else 'BARE')"
bare = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                      env={**os.environ, "PYTHONPATH": ""})
check("without the wrapper the child is NOT gated", bare.stdout.strip() == "BARE")
wrapped = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=runner.child_env())
check("with the wrapper the gate is armed before user code", wrapped.stdout.strip() == "ARMED")
check("the wrapper adds no stderr noise on a clean run", wrapped.stderr.strip() == "")

print("-- the child's own sitecustomize still runs (we chain, never shadow) --")
host = tempfile.mkdtemp(prefix="host-site-")
open(os.path.join(host, "sitecustomize.py"), "w").write("print('HOST-SITECUSTOMIZE-RAN')\n")
env = runner.child_env()
env["PYTHONPATH"] = env["PYTHONPATH"] + os.pathsep + host
r = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env)
check("host sitecustomize executed", "HOST-SITECUSTOMIZE-RAN" in r.stdout)
check("and the gate is still armed", "ARMED" in r.stdout)

print("-- exit codes + argument handling --")
check("no command → usage, exit 2", runner.main([]) == 2)
check("bare `--` → usage, exit 2", runner.main(["--"]) == 2)
check("--show prints the bootstrap, exit 0", runner.main(["--show"]) == 0)
check("missing binary → 127 (shell convention), not a traceback",
      runner.main(["definitely-not-a-real-binary-xyz"]) == 127)

print("-- the CLI exposes it, and a missing prerequisite is ONE clean line (not a traceback) --")
from spendguard import cli
import inspect
check("`spendguard run` is dispatched", 'cmd == "run"' in inspect.getsource(cli._dispatch))
check("main() wraps dispatch and catches RuntimeError (the KeyMissing base)",
      "except RuntimeError" in inspect.getsource(cli.main) and "_dispatch" in inspect.getsource(cli.main))
check("main() survives a broken pipe (`spendguard report | head`)",
      "BrokenPipeError" in inspect.getsource(cli.main))


def _boom(argv=None):
    raise RuntimeError("OPENAI_API_KEY not found (set it in the environment, or add it to /x/keys.env)")


_real = cli._dispatch
cli._dispatch = _boom
try:
    rc = cli.main(["report"])
finally:
    cli._dispatch = _real
check("a missing key exits 1 cleanly rather than raising", rc == 1)

print("-- a missing command names the one this host DOES have (macOS has no bare `python`) --")
import io, contextlib, shutil
from spendguard import runner as _rn
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    rc = _rn.main(["--", "python", "job.py"]) if not shutil.which("python") else 127
err = buf.getvalue()
check("exits 127 (the shell's own 'not found' code), never 0", rc == 127)
if not shutil.which("python"):
    check("it suggests python3 rather than dead-ending", "python3" in err and "try:" in err)
    check("the suggestion carries the user's own args through", "job.py" in err)
check("but it NEVER substitutes a binary the user didn't name (suggestion only)",
      "execvpe(argv[0]" in inspect.getsource(_rn.main))
check("a genuinely unknown command gets no invented suggestion",
      _rn._near_miss("zzz-not-a-real-command") is None)

print(f"\n{'[FAIL]' if failures else 'OK'} test_runner_wrapper: {failures} failure(s)")
sys.exit(1 if failures else 0)
