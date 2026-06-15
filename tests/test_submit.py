"""Offline test for the submit gate (estimate + cap) and the packing/cache projection. Isolated home
(guarded_submit writes an audit json into SPENDGUARD_HOME). NO network — submit=False throughout."""
import os, sys, json, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-submit-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import submit
import spendguard.estimate as estimate   # NB: `spendguard.estimate` the name resolves to the pricing
#                                          function (exported in __init__); the CLI estimator is the submodule.


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


# a small OpenAI /v1/chat/completions batch jsonl
path = tempfile.mktemp(suffix=".jsonl")
with open(path, "w") as f:
    for x in ("aspirin", "metformin", "lisinopril"):
        f.write(json.dumps({"custom_id": x, "method": "POST", "url": "/v1/chat/completions",
                            "body": {"model": "gpt-5.5", "messages": [{"role": "user", "content": "classify " + x}],
                                     "max_tokens": 100}}) + "\n")

print("-- estimate_jsonl_cost --")
est = submit.estimate_jsonl_cost(path, "gpt-5.5")
check("counts 3 requests", est["requests"] == 3)
check("batch mode + positive cost", est["mode"] == "batch" and est["cost"] > 0)
check("out ceiling = 3 × max_tokens (conservative)", est["out_tok"] == 300)

print("-- guarded_submit cap enforcement (submit=False; refusals raise BEFORE any write/submit) --")
try:
    submit.guarded_submit(path, "gpt-5.5", cap_dollars=0.0000001, submit=False)
    check("over $ cap → RuntimeError", False)
except RuntimeError:
    check("over $ cap → RuntimeError", True)
try:
    submit.guarded_submit(path, "gpt-5.5", cap_dollars=1000, submit=False, request_cap=1)
    check("over request_cap → RuntimeError", False)
except RuntimeError:
    check("over request_cap → RuntimeError", True)
check("under cap → passes (None, submit=False)",
      submit.guarded_submit(path, "gpt-5.5", cap_dollars=1000, submit=False) is None)

print("-- estimate.project (packing + caching) --")
p1 = estimate.project("gpt-5.5", 100, prefix=0, in_per=10, out_per=5, pack=1, mode="batch", assume_cache=False)
p30 = estimate.project("gpt-5.5", 100, prefix=0, in_per=10, out_per=5, pack=30, mode="batch", assume_cache=False)
check("packing 30/req cuts request count", p30["n_req"] < p1["n_req"])  # 4 vs 100
# big repeated prefix: caching is cheaper than not, when above the model's min + reused
big = estimate.project("gpt-5.5", 100, prefix=4000, in_per=10, out_per=5, pack=1, mode="batch", assume_cache=True)
nocache = estimate.project("gpt-5.5", 100, prefix=4000, in_per=10, out_per=5, pack=1, mode="batch", assume_cache=False)
check("caching a big reused prefix is cheaper", big["cost"] < nocache["cost"])
print("done.")
