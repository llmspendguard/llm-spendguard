"""Offline deterministic bound on migration work across operation-scoped cache reads."""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from spendguard import config, semcache

SCOPED_CACHE_ENTRY_COUNT = 12
SCOPED_CACHE_LOOKUP_COUNT = 5
SCOPED_CACHE_TEST_MODEL = "schema-cost-model"


def prove_scoped_semcache_migration_cost():
    with tempfile.TemporaryDirectory() as scratch:
        with patch.dict(os.environ, {"SPENDGUARD_HOME": scratch}), patch.object(config, "HOME", Path(scratch)):
            prompts = [f"schema-cost-prompt-{index}" for index in range(SCOPED_CACHE_ENTRY_COUNT)]
            for prompt in prompts:
                semcache.put(prompt, SCOPED_CACHE_TEST_MODEL, prompt)
            with patch.object(semcache, "_repair_semcache_unique_key",
                              wraps=semcache._repair_semcache_unique_key) as repair_spy:
                for _ in range(SCOPED_CACHE_LOOKUP_COUNT):
                    assert semcache.get_cached(prompts[0], SCOPED_CACHE_TEST_MODEL) == prompts[0]
                lookup_repairs = repair_spy.call_count
            input_path = Path(scratch) / "requests.jsonl"
            output_path = Path(scratch) / "deduplicated.jsonl"
            input_path.write_text("".join(json.dumps({"body": {"prompt": prompt}}) + "\n"
                                          for prompt in prompts))
            with patch.object(semcache, "_repair_semcache_unique_key",
                              wraps=semcache._repair_semcache_unique_key) as dedup_repair_spy:
                result = semcache.dedup_jsonl(input_path, output_path, model=SCOPED_CACHE_TEST_MODEL)
                assert result["cache_hit"] == len(prompts), result
                dedup_repairs = dedup_repair_spy.call_count
            assert lookup_repairs <= 1 and dedup_repairs <= 1, (lookup_repairs, dedup_repairs)
            # Exercise a genuinely missing whole-table key, including a misleading
            # partial unique index: migrate once, then stay cheap on new connections.
            with config.ledger_op("semcache", semcache._ensure_semcache_schema) as cache_schema_handle:
                for columns, unique, name in semcache.table_index_coverages(cache_schema_handle, "semcache"):
                    if columns == semcache._SEMCACHE_UNIQUE_KEY_COLUMNS and unique:
                        cache_schema_handle.execute(f'DROP INDEX "{name}"')
                cache_schema_handle.execute(
                    "CREATE UNIQUE INDEX partial_cache_key ON semcache(model,prompt_hash) WHERE emb IS NOT NULL")
                cache_schema_handle.commit()
            with patch.object(semcache, "_repair_semcache_unique_key",
                              wraps=semcache._repair_semcache_unique_key) as migration_repair_spy:
                for _ in range(SCOPED_CACHE_LOOKUP_COUNT):
                    assert semcache.get_cached(prompts[0], SCOPED_CACHE_TEST_MODEL) == prompts[0]
                assert migration_repair_spy.call_count == 1, migration_repair_spy.call_count


if __name__ == "__main__":
    prove_scoped_semcache_migration_cost()
