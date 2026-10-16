"""Offline guard: every declared lane model and its static metered crosswalk live in the model catalog."""
import os
from pathlib import Path
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lane-crosswalk-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-lane-crosswalk")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spendguard import adapters, catalog, config, lane_catalog, model_catalog  # noqa: E402


def _declared_lane_models():
    for lane, declaration in (config._cfg_get("advisor", "lane_models", {}) or {}).items():
        if isinstance(declaration, dict):
            for tier, model in declaration.items():
                yield lane, tier, model
        else:
            yield lane, None, declaration


def _authored_lane_models():
    """Catalog lane spellings keep this guard substantive even in its intentionally empty isolated config."""
    for _base_id, record in model_catalog.all_records().items():
        for lane, spellings in (record.get("lane_spellings") or {}).items():
            for use_name in spellings:
                yield lane, None, use_name


def _guarded_lane_models():
    seen = set()
    for row in (*_declared_lane_models(), *_authored_lane_models()):
        if row not in seen:
            seen.add(row)
            yield row


failures = []


def check(label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


original_live_model_ids = catalog.live_model_ids
catalog.live_model_ids = lambda _provider: None
try:
    records = model_catalog.all_records()
    for lane, tier, use_name in _guarded_lane_models():
        label = f"{lane}[{tier}]" if tier is not None else lane
        rec = model_catalog.model_record(use_name)
        check(f"{label} use-name is catalogued", rec is not None, use_name)
        if rec is None:
            continue
        base_id = rec["id"]
        authored_metered_id = rec.get("metered_id") or base_id
        resolved_metered_id, _reasoning = adapters.metered_fallback_id(
            lane_catalog.lane_provider(lane), use_name
        )
        check(f"{label} resolver equals authored static crosswalk",
              resolved_metered_id == authored_metered_id,
              f"catalog={authored_metered_id!r}, resolver={resolved_metered_id!r}")
        metered_rec = records.get(authored_metered_id)
        check(f"{label} metered target is a catalogued id", metered_rec is not None, authored_metered_id)
        if metered_rec is not None:
            check(f"{label} metered target stays with its provider",
                  metered_rec.get("provider") == rec.get("provider"),
                  f"base={rec.get('provider')!r}, metered={metered_rec.get('provider')!r}")
finally:
    catalog.live_model_ids = original_live_model_ids

print(f"\n{'[FAIL]' if failures else 'OK'} test_lane_metered_crosswalk_ssot: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
