"""Per-(lane, model) backoff — when a lane REJECTS a specific model (e.g. codex 400s gpt-5-mini on a ChatGPT plan)
the lane must stop intercepting THAT model on every call, WITHOUT cooling the whole lane (gpt-5.5 keeps riding
codex) and WITHOUT mistaking a genuine SIZE limit for a model rejection. The decision comes from the API-fallback
OUTCOME (did the API answer the same prompt?), never from parsing the lane's error text. Offline: exercises the
in-process learning primitives directly — no LLM, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanemodel-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, resource_state                                        # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []


def _ceil(lane):
    return resource_state.size_ceiling(resource_state.lane_key(lane))


resource_state._reset()                          # size_ceiling + proven-good + cooldowns all live in the unified store

print("-- a MODEL rejection (WITHIN a proven-good size; the API then answered) backs off THAT (lane, model) only --")
resource_state.note_proven_good(resource_state.lane_key("codex"), 100_000)   # codex has answered prompts this big
kind = adapters._learn_from_fallback("codex", "a small prompt", api_failed=False, model="gpt-5-mini")
fails += ck("classified 'model-cooled' (a within-size miss backs off the model, lane kept, not cooled)", kind == "model-cooled")
fails += ck("the rejected model is now cooling on codex", adapters._lane_model_cooling("codex", "gpt-5-mini"))
fails += ck("a DIFFERENT model still rides codex (gpt-5.5 unaffected)", not adapters._lane_model_cooling("codex", "gpt-5.5"))
fails += ck("the WHOLE codex lane is NOT cooled — only the one model", not adapters._lane_cooling("codex"))

print("\n-- a genuine SIZE limit (ABOVE a proven-good size) learns a ceiling, does NOT per-model-cool --")
adapters._lane_note_ok("gemini", "x" * 1_000)         # gemini has PROVEN it answers 1000-char prompts
kind2 = adapters._learn_from_fallback("gemini", "x" * 200_000, api_failed=False, model="gemini-3-flash")
fails += ck("size-limited fail (above proven-good) = 'unsuitable'", kind2 == "unsuitable")
fails += ck("it learned a routing size ceiling", _ceil("gemini") is not None)
fails += ck("it did NOT per-model-cool (the model is fine, the prompt was too big)",
            not adapters._lane_model_cooling("gemini", "gemini-3-flash"))

print("\n-- a lane that is DOWN (the API also failed) cools the WHOLE lane, not one model --")
kind3 = adapters._learn_from_fallback("zai-coding", "p", api_failed=True, model="glm-5.3")
fails += ck("api-also-failed = 'down'", kind3 == "down")
fails += ck("the whole lane is cooled", adapters._lane_cooling("zai-coding"))
fails += ck("not recorded as a per-model rejection", not adapters._lane_model_cooling("zai-coding", "glm-5.3"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_model_backoff: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
