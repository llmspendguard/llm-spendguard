"""estimate_literals.literal_sites hands the judge the WHOLE ENCLOSING FUNCTION, not a fixed ±line window.

The adjudicator (ADJUDICATE_SYS) decides "is this result PRESENTED AS A PRICE?" by "what the RESULT is used for in
the surrounding code". The old `site['code']` was lines[lineno-4 : lineno+3] — a result used more than 3 lines after
the call (e.g. `est = realtime_cost(...)` then `print(f"${est}")` ten lines down) was invisible to the judge, i.e.
evidence truncated before a decision. Now the enclosing function body is sent; a module-level call (no def around it)
keeps the local window since there is no function to show. Offline: a temp source tree, no model, no spend."""
import os
import sys
import tempfile
import pathlib

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import estimate_literals  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

root = pathlib.Path(tempfile.mkdtemp(prefix="sg-estlit-"))

# A cost call whose RESULT is USED ~10 lines later, inside a function. The use (QUOTE_USE_MARKER) is the evidence
# that makes it a quoted price; it is well past the old lineno+3 window.
(root / "quote_far_use.py").write_text(
    "def render_quote(model):\n"
    "    import pricing\n"
    "    est = pricing.realtime_cost(model, 700, 160)\n"
    + "".join(f"    filler_{i} = {i}\n" for i in range(10))
    + "    label = 'estimated cost'\n"
    "    print(f'{label}: ${est:.2f}')   # QUOTE_USE_MARKER — the result shown as a price\n"
    "    return est\n"
)
# A module-level call with no enclosing function — the window fallback applies.
(root / "module_level.py").write_text(
    "import pricing\n"
    "PRICEABLE = bool(pricing.realtime_cost('m', 1000, 1000))   # MODULE_PROBE_MARKER\n"
)

sites = {s["file"]: s for s in estimate_literals.literal_sites(root)}

ck("the far-use site was found", "quote_far_use.py" in sites)
far = sites.get("quote_far_use.py", {})
ck("code includes the CALL line (realtime_cost with the literals)", "realtime_cost(model, 700, 160)" in far.get("code", ""))
ck("code includes the FAR USE ~10 lines later (whole function sent, not a ±3 window)",
   "QUOTE_USE_MARKER" in far.get("code", ""))
ck("symbol is the enclosing function", far.get("symbol") == "render_quote")

ck("the module-level site was found", "module_level.py" in sites)
mod = sites.get("module_level.py", {})
ck("module-level call keeps a local window (its probe line is present)", "MODULE_PROBE_MARKER" in mod.get("code", ""))
ck("module-level symbol falls back to <module>", mod.get("symbol") == "<module>")

print(f"\n{'[FAIL]' if fails else 'OK'} test_estimate_literal_whole_function_context: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
