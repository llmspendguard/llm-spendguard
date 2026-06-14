"""Offline test for the deterministic history miner — NO fs walk, NO db, NO network.

Covers the pure extraction logic: batch-id extraction across artifact shapes, stem cleaning, and
directory→intent derivation. (Full-repo scan + apply are exercised manually / in integration.)
"""
from spendguard.history import _ids_and_meta, _clean_stem, _intent_for


def ids(obj):
    return [bid for bid, _rec in _ids_and_meta(obj)]


print("-- _ids_and_meta (artifact shapes) --")
list_of_dict = [{"id": "msgbatch_AAA", "job_type": "DX_ICD10"}, {"id": "batch_bbb"}]
dict_ids = {"ids": ["batch_111", "batch_222"], "n": 9}
dict_map = {"batch_x": {"foo": 1}, "msgbatch_y": {}, "n": 5}
list_str = ["batch_zzz", "not-an-id"]
assert ids(list_of_dict) == ["msgbatch_AAA", "batch_bbb"], ids(list_of_dict)
assert ids(dict_ids) == ["batch_111", "batch_222"], ids(dict_ids)
assert set(ids(dict_map)) == {"batch_x", "msgbatch_y"}, ids(dict_map)
assert ids(list_str) == ["batch_zzz"], ids(list_str)
# job_type is carried in the record for intent refinement
jt = dict(_ids_and_meta(list_of_dict))["msgbatch_AAA"].get("job_type")
assert jt == "DX_ICD10", jt
print("  [OK] list-of-dict / dict(ids=) / dict-map / list-of-str + job_type")

print("-- _clean_stem --")
for raw, want in [("batch_ids", None), ("mismap_batch_id", "mismap"),
                  ("batch_ids_abbreviations", "abbreviations"), ("tier4_batch_ids", "tier4")]:
    got = _clean_stem(raw)
    assert got == want, f"{raw}: got {got!r}, want {want!r}"
print("  [OK] batch_ids->None · mismap_batch_id->mismap · batch_ids_abbreviations->abbreviations · tier4_batch_ids->tier4")

print("-- _intent_for (dir/stem) --")
cases = [("/r/data/edge_evidence/mismap_batch_id.json", "edge_evidence/mismap"),
         ("/r/data/snomed_mapping/tier4_batch_ids.json", "snomed_mapping/tier4"),
         ("/r/data/crosscheck_strong/batch_ids.json", "crosscheck_strong")]
for path, want in cases:
    got = _intent_for(path, "/r")
    assert got == want, f"{path}: got {got!r}, want {want!r}"
print("  [OK] edge_evidence/mismap · snomed_mapping/tier4 · crosscheck_strong")
print("done.")
