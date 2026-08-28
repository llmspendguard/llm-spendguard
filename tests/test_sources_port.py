"""The transcript-source PORT + unified discovery (`spendguard sources`).

"How do we support the tool I use?" should be answered by a plugin, not by editing spendguard — the same shape
`gpu_port` already uses for RunPod/Modal/Lambda. And discovery must never interrogate the user: a first-timer
shouldn't have to know what we support in order to tell us what they run.

Invariants pinned here:
  • a third-party source registered through the port shows up in BOTH `sources` and `scan`, with no special-casing;
  • a broken source warns once and is SKIPPED — it can never break the command or the other sources;
  • discovery is local + free: no network, no LLM, and the user's SOURCE CODE is never read (we look at installed
    packages and known session dirs, which answers better and is far less invasive);
  • keys are never returned or printed — only whether one resolves;
  • LLM vs remote-compute stay separate, as they are everywhere else in spendguard;
  • the `scan` empty state does not dead-end: it falls back to this discovery.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-sources-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import io
import contextlib
import inspect
from spendguard import sources, scan

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


class FakeSource:
    """A third-party adapter, exactly as the port documents it."""
    NAME = "Aider"

    def detect(self):
        return True

    def read(self, days=None):
        return {"sessions": 7, "days": ["2026-07-01", "2026-07-05"], "total_usd": 12.5,
                "projects": {"acme": 12.5}, "models": {"gpt-5.5": 12.5}}


class BrokenSource:
    NAME = "Broken"

    def detect(self):
        raise RuntimeError("its config is corrupt")

    def read(self, days=None):
        return {}


class AbsentSource:
    NAME = "NotInstalled"

    def detect(self):
        return False

    def read(self, days=None):
        raise AssertionError("read() must never be called for a source that doesn't detect")


print("-- the port: register a source, it appears everywhere, with no special-casing --")
sources._register_builtins()
sources.register("aider", lambda: FakeSource())
sources.register("absent", lambda: AbsentSource())
names = [n for n, _s in sources.transcript_sources()]
check("a registered, detected source is discovered", "aider" in names)
check("a source that does NOT detect is excluded (and read() never called)", "absent" not in names)
d = sources.discover_sources()
check("it lands in discover() with its numbers", any(t.get("name") == "Aider" and t.get("total_usd") == 12.5
                                                     for t in d["tools"]))
col = scan.collect()
out = scan.render(col)
check("scan sees it through the port, with zero scan-side changes",
      "Aider" in col and col["Aider"]["total_usd"] == 12.5 and col["Aider"]["projects"] == {"acme": 12.5})
check("and it is rendered as a source line", "Aider" in out and "7 sessions" in out)

print("-- a broken source is skipped with ONE warning, never fatal --")
sources.register("broken", lambda: BrokenSource())
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    names2 = [n for n, _s in sources.transcript_sources()]
check("the broken source is skipped", "broken" not in names2)
check("the good ones still work", "aider" in names2)
check("one warning names it and says it was skipped",
      "broken" in buf.getvalue() and "skipped" in buf.getvalue())
check("discover() still returns a full picture", isinstance(sources.discover_sources().get("providers"), list))

print("-- discovery is local, free, and never reads the user's code --")
src = inspect.getsource(sources)
check("no network primitives", not any(w in src for w in ("urllib", "requests", "socket", "http")))
check("no LLM/adapters call in the discovery path (finding files is mechanical, not agentic)",
      "adapters.call" not in src and "advisor" not in src)
# assert on the CODE, not the docstring — the docstring *explains* the boundary and says "import openai" in prose
code = src.split('"""', 2)[-1]
check("it does NOT scan the user's source files (no *.py globbing, no import-grepping)",
      "*.py" not in code and "import openai" not in code)
check("the boundary is documented, not just observed", "never read the user's SOURCE CODE" in src)

print("-- keys: presence only, never the value --")
os.environ["OPENAI_API_KEY"] = "sk-super-secret-do-not-leak"
provs = sources.providers_paid()
blob = repr(provs) + sources.render(sources.discover_sources())
check("the key VALUE never appears in the data or the render", "sk-super-secret-do-not-leak" not in blob)
oai = [p for p in provs if p["provider"] == "openai"]
check("it reports that a key resolves", oai and oai[0]["resolved"] is True)
check("every provider carries an llm/compute kind", all(p["kind"] in ("llm", "compute") for p in provs))
check("vast.ai is classified as remote compute, not an LLM provider",
      all(p["kind"] == "compute" for p in provs if p["provider"] == "vast"))
check("openai/anthropic are LLM", all(p["kind"] == "llm" for p in provs if p["provider"] in ("openai", "anthropic")))
os.environ.pop("OPENAI_API_KEY", None)

print("-- the render keeps the two axes apart and stays honest --")
r = sources.render(sources.discover_sources())
check("real billed $ and plan-covered value are separate sections",
      "real billed $" in r and "value, not billed $" in r)
check("ungated interpreters are surfaced with the fix", "INTERPRETERS that can spend" in r)
check("it states the no-source-code boundary to the user, not just in code comments",
      "source code is never read" in r)

print("-- `scan` empty state falls back to this discovery instead of dead-ending --")
empty = scan.render({})
check("it does not just say 'nothing' and stop", "spendguard sources" in empty or "providers with a key" in empty)
check("it names the plugin path so an unsupported tool has an answer",
      "entry point" in empty or "spendguard.providers" in empty)

print("-- wired into the CLI + help --")
from spendguard import cli
check("`spendguard sources` dispatches", 'cmd == "sources"' in inspect.getsource(cli._dispatch))
check("it is advertised in the grouped help", "sources" in cli.help_text())

print(f"\n{'[FAIL]' if failures else 'OK'} test_sources_port: {failures} failure(s)")
sys.exit(1 if failures else 0)
