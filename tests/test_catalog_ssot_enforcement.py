"""Structural source and observed-ledger guards, including deliberate fault mutations."""
import ast
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, model_catalog, pricing  # noqa: E402

CATALOG_GUARD_ROOT = Path(__file__).resolve().parents[1]
CATALOG_OBSERVATIONS_PATH = Path(__file__).with_name("fixtures") / "catalog_observed_models.json"
LEGACY_PRICE_FILENAME = "prices.json"
FILESYSTEM_READ_METHODS = frozenset({"open", "read_text", "read_bytes"})
# Reviewed public/private module-state bindings. This is an exact architecture interface contract:
# additions require an explicit contract change, regardless of the value's meaning or numeric contents.
PRICING_MODULE_DECLARATION_BINDINGS = frozenset({
    "_ANTHROPIC_CACHE_MODIFIERS", "CACHE_WRITE_5M_MULTIPLIER", "CACHE_WRITE_1H_MULTIPLIER",
    "CACHE_READ_MULTIPLIER", "ONE_MILLION", "PRICING_SOURCE", "PRICING_VERIFIED",
    "STALE_AFTER_DAYS", "PROVIDERS", "AMBIGUOUS_BARE", "LITELLM_BREADTH_FILENAME",
    "PRICING_BREADTH_SNAPSHOT", "PRICING", "UNIT_PRICES", "CONTEXT_LIMITS", "OUTPUT_FLOOR",
    "_OPENROUTER_URL", "_SYS_SAME_MODEL", "_SYS_PRICE_GAP", "UNPRICED_SEEN",
})
GUARD_LEDGER_FILENAME = "observations.sqlite"
GUARD_ADVISOR_SECTION = "advisor"
GUARD_LANE_MODELS_KEY = "lane_models"


def assert_catalog_source_structure(source_units):
    """Enforce fixed file-read grammar and the reviewed pricing module-state interface.

    No literal is classified as a price or source by its meaning. The reserved filename is forbidden
    specifically in filesystem-read call syntax; plain descriptive strings and arbitrary dictionaries
    elsewhere are permitted. All pricing module-state additions require a declaration-contract update.
    """
    violations = []
    for filename, source in source_units:
        tree = ast.parse(source, filename=str(filename))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            filesystem_call = (isinstance(node.func, ast.Name) and node.func.id == "open") or (
                isinstance(node.func, ast.Attribute) and node.func.attr in FILESYSTEM_READ_METHODS)
            if filesystem_call:
                for argument in [*node.args, *([node.func.value] if isinstance(node.func, ast.Attribute) else [])]:
                    if any(isinstance(part, ast.Constant) and isinstance(part.value, str)
                           and Path(part.value).name == LEGACY_PRICE_FILENAME for part in ast.walk(argument)):
                        violations.append(f"{filename}:{node.lineno}: reserved filename in filesystem-read syntax")
        if Path(filename).name == "pricing.py":
            for statement in tree.body:
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    bindings = {part.id for target in targets for part in ast.walk(target) if isinstance(part, ast.Name)}
                    unexpected = bindings - PRICING_MODULE_DECLARATION_BINDINGS
                    if unexpected:
                        violations.append(f"{filename}:{statement.lineno}: undeclared module state {sorted(unexpected)}")
    assert not violations, "\n".join(violations)


def test_catalog_ssot_complete_source_structure():
    units = [(path, path.read_text()) for directory in ("src", "scripts")
             for path in (CATALOG_GUARD_ROOT / directory).rglob("*.py")]
    assert_catalog_source_structure(units)
    assert not Path(model_catalog.DATA_PATH).with_name(LEGACY_PRICE_FILENAME).exists()


def test_catalog_ssot_source_mutations_are_rejected():
    mutations = (
        "from pathlib import Path\nprice_source = Path('prices.json').read_text()\n",
        "PRICE_CARD = {'in_': 1, 'out': 2}\n",
        "OLD_FALLBACK = {'model': (1, 2, 3)}\n",
        "OLD_FALLBACK: dict = {'model': (1, 2, 3)}\n",
    )
    for mutated_source in mutations:
        with pytest.raises(AssertionError):
            assert_catalog_source_structure([("pricing.py", mutated_source)])
    with pytest.raises(AssertionError, match="reserved filename in filesystem-read syntax"):
        assert_catalog_source_structure([("consumer.py", mutations[0])])
    assert_catalog_source_structure([("consumer.py", 'example_output = "prices.json"\ndirections = {"in_": 1, "out": 2}\n')])


def assert_catalog_public_pricing_derivation():
    expected_prices, _, _ = pricing._derive_catalog_pricing_state(pricing.PRICING_BREADTH_SNAPSHOT)
    assert pricing.PRICING == expected_prices, "public pricing state differs from catalog projection"


def test_catalog_ssot_public_pricing_replacement_is_rejected(monkeypatch):
    assert_catalog_public_pricing_derivation()
    authored_replacement = dict(pricing.PRICING)
    authored_replacement[uuid.uuid4().hex] = {}
    monkeypatch.setattr(pricing, "PRICING", authored_replacement)
    with pytest.raises(AssertionError, match="public pricing state differs from catalog projection"):
        assert_catalog_public_pricing_derivation()


def test_catalog_ssot_loader_ignores_poisoned_legacy_leaf(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    (tmp_path / LEGACY_PRICE_FILENAME).write_text(uuid.uuid4().hex)
    assert pricing._load() == pricing._derive_catalog_pricing_state({})[0]


def assert_catalog_observed_prices(model_ids):
    pending = list((config._cfg_get(GUARD_ADVISOR_SECTION, GUARD_LANE_MODELS_KEY, {}) or {}).values())
    configured_models = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        else:
            assert isinstance(value, str), f"invalid configured lane model: {value!r}"
            configured_models.append(value)
    for model_id in [*model_ids, *configured_models]:
        assert model_catalog.model_price(pricing.normalize(model_id)), f"missing catalog price: {model_id}"
        assert pricing.price(model_id)["in_"] is not None


def test_catalog_ssot_seeded_observations_and_price_mutation(tmp_path, monkeypatch):
    observations = json.loads(CATALOG_OBSERVATIONS_PATH.read_text())
    observed = observations["priceable_models"]
    lane_models = observations["lane_models"]
    isolated_config = tmp_path / config.CONFIG_JSON.name
    isolated_config.write_text(json.dumps({GUARD_ADVISOR_SECTION: {GUARD_LANE_MODELS_KEY: lane_models}}))
    monkeypatch.setattr(config, "CONFIG_JSON", isolated_config)
    config.cfg_invalidate()
    ledger = tmp_path / GUARD_LEDGER_FILENAME
    with sqlite3.connect(ledger) as connection:
        connection.execute("CREATE TABLE calls (model TEXT NOT NULL)")
        connection.executemany("INSERT INTO calls VALUES (?)", [(model,) for model in observed])
    snapshot = ledger.read_bytes()
    with sqlite3.connect(f"file:{ledger}?mode=ro", uri=True) as connection:
        models = [row[0] for row in connection.execute("SELECT DISTINCT model FROM calls")]
    assert ledger.read_bytes() == snapshot
    assert_catalog_observed_prices([*models, *lane_models.values()])
    with monkeypatch.context() as lane_mutation:
        missing_lane_model = uuid.uuid4().hex
        lane_mutation.setattr(config._cfg, "_cache", {
            GUARD_ADVISOR_SECTION: {GUARD_LANE_MODELS_KEY: {missing_lane_model: missing_lane_model}}
        })
        with pytest.raises(AssertionError, match="missing catalog price"):
            assert_catalog_observed_prices(models)
    catalog = json.loads(Path(model_catalog.DATA_PATH).read_text())
    for model in observed:
        target = pricing.normalize(model)
        record = model_catalog.model_record(target)
        assert record is not None, f"missing mutation target catalog record: {model} (normalized {target})"
        mutated = json.loads(json.dumps(catalog))
        mutated["models"][record["id"]]["price"] = None
        temporary_catalog = tmp_path / Path(model_catalog.DATA_PATH).name
        temporary_catalog.write_text(json.dumps(mutated))
        with monkeypatch.context() as mutation:
            mutation.setattr(model_catalog, "DATA_PATH", str(temporary_catalog))
            mutation.setattr(model_catalog, "_overlay_path", lambda: None)
            refreshed_prices, refreshed_providers, refreshed_ambiguities = pricing._derive_catalog_pricing_state({})
            mutation.setattr(pricing, "PRICING", refreshed_prices)
            mutation.setattr(pricing, "PROVIDERS", refreshed_providers)
            mutation.setattr(pricing, "AMBIGUOUS_BARE", refreshed_ambiguities)
            with pytest.raises(pricing.PriceUnknownError):
                pricing.price(model)
            with pytest.raises(AssertionError, match="missing catalog price"):
                assert_catalog_observed_prices([model])
    assert_catalog_observed_prices(models)
    assert observations["named_gaps"], "unrecoverable observations must remain explicitly accounted for"
    for gap in observations["named_gaps"]:
        assert gap["name"] and gap["evidence"]
        for model in gap["models"]:
            with pytest.raises(pricing.PriceUnknownError):
                pricing.price(model)


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))
