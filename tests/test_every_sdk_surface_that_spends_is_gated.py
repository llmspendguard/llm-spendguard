"""EVERY SDK SURFACE THAT CAN SPEND MONEY MUST BE PATCHED AFTER install().

THE DEFECT THIS EXISTS TO STOP. RT_INTERCEPTORS patched `Messages.create`. `adapters._call_once` calls
`messages.stream(...)` for every Anthropic request — a DIFFERENT METHOD on the same class, and one nobody
had added to the table. So the request went out, Anthropic billed it, and no charge row was written.

Measured 2026-08-13: a call through adapters cost $0.000030 and produced charges +0, while the identical
request through `messages.create` produced charges +1. Roughly 2,921 Anthropic calls were never billed to
the local ledger; the floor on the missed amount, counting OUTPUT TOKENS ALONE, is $21.03.

Nothing failed. `gate_calls` kept incrementing, so the gate looked alive, and the receipt simply read low —
and in a spend tool, under-counting reads as thrift. The same hole existed on `openai chat.completions.
stream`, unused by this package but open to anyone who calls it.

WHAT IS CHECKED. After install(), every method named in the interceptor tables is actually wrapped. It
reads the tables rather than a hand-copied list, so a surface added to a table is automatically covered
here, and a surface REMOVED from a table fails loudly instead of silently going unrecorded.

WHAT THIS CANNOT CATCH, stated plainly: a spending surface that exists in an SDK and is in NO table. That
is the exact shape of the original defect, and no test that reads the tables can see it. The defence there
is the streaming-helper sweep below, which asks the installed SDKs which methods look like spending
surfaces and fails on any that no table claims.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import gate  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _resolve(module_path, class_name, method):
    import importlib
    try:
        cls = getattr(importlib.import_module(module_path), class_name)
    except ModuleNotFoundError:
        return "sdk-absent"
    return getattr(cls, method, None)


print("-- every method named in an interceptor table is actually wrapped --")
TABLES = [("RT_INTERCEPTORS", getattr(gate, "RT_INTERCEPTORS", []), 3),
          ("STREAM_INTERCEPTORS", getattr(gate, "STREAM_INTERCEPTORS", []), 3),
          ("INTERCEPTORS", getattr(gate, "INTERCEPTORS", []), 3),
          ("UNIT_INTERCEPTORS", getattr(gate, "UNIT_INTERCEPTORS", []), 3)]
for tname, table, _n in TABLES:
    for spec in table:
        mod, cls_name, method = spec[0], spec[1], spec[2]
        fn = _resolve(mod, cls_name, method)
        if fn == "sdk-absent":
            continue                     # the SDK is not installed here; nothing to gate
        check(f"{tname}: {cls_name}.{method} is gated",
              fn is not None and getattr(fn, "_spend_gated", False),
              f"{mod}.{cls_name}.{method} is in the table but NOT wrapped — calls on it spend money that "
              f"never reaches the ledger.")

print("\n-- no SPEND SURFACE on an installed SDK is left unclaimed (create / parse / stream families) --")
# The original defect in general form: a spending surface that no table names. A hand-written list of specific
# (class, method) pairs is exactly how `.parse`, Responses `.stream`, and legacy `completions.create` hid — the
# list said `.stream` on four classes and nothing else. Instead, enumerate the TOKEN-SPEND resource classes ×
# the spend-family method NAMES, and check EVERY method that actually EXISTS on the installed SDK. Adding a
# method to the family (a future spend helper) or a class here then covers it across the board — a class of
# defect closed, not one instance. Residual, stated honestly: a brand-NEW token-spend RESOURCE-CLASS type must
# be added to RESOURCE_CLASSES; unit-billed surfaces (images / audio / …) are covered by UNIT_INTERCEPTORS and
# their own tests, and genuinely-free surfaces (e.g. moderations) are deliberately not token-spend.
_claimed = {(s[0], s[1], s[2]) for _t, tbl, _n in TABLES for s in tbl}
RESOURCE_CLASSES = [
    ("openai.resources.chat.completions", "Completions"), ("openai.resources.chat.completions", "AsyncCompletions"),
    ("openai.resources.responses", "Responses"), ("openai.resources.responses", "AsyncResponses"),
    ("openai.resources.completions", "Completions"), ("openai.resources.completions", "AsyncCompletions"),
    ("openai.resources.embeddings", "Embeddings"), ("openai.resources.embeddings", "AsyncEmbeddings"),
    ("anthropic.resources.messages", "Messages"), ("anthropic.resources.messages", "AsyncMessages"),
    ("anthropic.resources.beta.messages", "Messages"), ("anthropic.resources.beta.messages", "AsyncMessages"),
]
SPEND_METHODS = ("create", "parse", "stream")         # the token-spend families a helper hides beside
_checked = 0
for mod, cls_name in RESOURCE_CLASSES:
    for method in SPEND_METHODS:
        fn = _resolve(mod, cls_name, method)
        if fn in (None, "sdk-absent"):
            continue                                  # that method / SDK is not present in this env → nothing to gate
        _checked += 1
        claimed = (mod, cls_name, method) in _claimed
        check(f"{cls_name}.{method} is claimed by a table and gated",
              claimed and getattr(fn, "_spend_gated", False),
              f"{mod}.{cls_name}.{method} EXISTS on the installed SDK but is "
              f"{'in NO interceptor table' if not claimed else 'not wrapped'}. A spend surface no table names is "
              f"exactly how ~2,921 Anthropic calls went unbilled: a helper beside `create` that no table claimed.")
check("the surface sweep actually examined installed spend methods (not a no-op)", _checked >= 8,
      f"only {_checked} spend methods were found on the installed SDKs — the enumeration may be resolving nothing.")

print("\nPASS — 0 failure(s)" if not _fails else f"\nFAIL — {len(_fails)} failure(s): " + "; ".join(_fails))
sys.exit(1 if _fails else 0)
