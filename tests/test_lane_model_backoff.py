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

print("\n-- a REPEATED model-miss PROMOTES the id into lane_unserved so the PREFLIGHT substitutes it (no metered bleed) --")
# A caller-supplied id a lane never served (glm-5.3-flashx requested on zai-coding, which serves glm-5.3) is otherwise
# only COOLED on each miss — it bleeds to metered+unpriced on the cooldown cycle forever because lane_served_substitute
# reads lane_unserved, and the servability re-measure only tests CONFIGURED lane models. A confirmed (repeat) miss now
# promotes the specific id into lane_unserved so the preflight substitutes it to a served model and stays $0.
from spendguard import catalog                                                        # noqa: E402
resource_state.note_proven_good(resource_state.lane_key("zai-coding"), 100_000)       # within-size misses = model-miss
# seed another unserved id so we can prove the promotion UNIONS (merge_lane_observation replaces, we union at the call)
catalog.merge_lane_observation("zai-coding", [], ["some-other-dead-id"])
adapters._learn_from_fallback("zai-coding", "p", api_failed=False, model="glm-5.3-flashx")   # miss #1 — retryable
fails += ck("first miss does NOT promote (a fluke stays retryable)",
            "glm-5.3-flashx" not in set(catalog.lane_unserved_ids("zai-coding") or []))
adapters._learn_from_fallback("zai-coding", "p", api_failed=False, model="glm-5.3-flashx")   # miss #2 — repeat → promote
_after = set(catalog.lane_unserved_ids("zai-coding") or [])
fails += ck("the REPEATED miss promotes glm-5.3-flashx into lane_unserved", "glm-5.3-flashx" in _after)
fails += ck("the pre-existing unserved id is PRESERVED (union, not clobber)", "some-other-dead-id" in _after)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_model_backoff: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
