# Vision (image) calls — the reliable path

Two failures compounded in a real image labeler and are worth stating up front, because they are the traps this
page exists to close:

1. **The vision request rode a text lane and cold-400'd.** The subscription lanes (`agy`/`codex`/`claude-code`)
   are text-only print-mode CLIs with **no image channel**. A vision call MUST ride the metered API.
2. **`no_substitution=True` is NOT a vision fix.** It pins the vendor (suppresses bandit swap / failover); it does
   **not** skip the lane. Only passing `images=` skips the lane (by construction) — that is the real mechanism.
3. **A dynamic-key MAP schema came back empty.** OpenAI strict mode forces `additionalProperties=false` +
   `required=`every declared property, so a map (`labels:{<id>:<label>}`) has no allowed keys and the model
   "correctly" returns `{}`. Use an **ARRAY of `{key,value}`** (`results:[{id,label}]`) instead.

## One vision call — `adapters.vision`

```python
from spendguard import adapters
r = adapters.vision(
    "openai:gpt-5-nano",                 # a vision-capable model (or "gemini:gemini-3-flash", "anthropic:…")
    "Classify each item shown. Return results:[{id,label}].",
    images=["/path/to/img.png"],         # file PATH(s) or data: URL(s) — REQUIRED, non-empty
    schema={"type":"object","required":["results"],"properties":{"results":{"type":"array","items":{
        "type":"object","required":["id","label"],
        "properties":{"id":{"type":"string"},"label":{"type":"string"}}}}}},
    sig="my-labeler",                    # names the call-class so the OUTPUT budget is measured, not guessed
)
# r["executor"] == "api"   (never a lane — vision skips them by construction)
# r["cost"]     == the metered $ (images priced by PIXELS, not the text tokenizer)
```

`adapters.vision` is the discoverable entry for what `adapters.call(images=…)` does. It **requires** a non-empty
`images` (a vision call with no image is a bug, not a text call) and runs the schema guard.

## Many images — `bulk_delegate(images_for=…, vision_model=…)`

The lane bulk fan can't do vision (lanes are text-only), so bulk vision fans across the metered API — governed by
the dispatch governor, checkpointed/resumed by content key, arity-checked, exactly like the lane fan:

```python
from spendguard import lane_balance
rows = lane_balance.bulk_delegate(
    tasks, "label-assets",
    images_for=lambda task: [task["image_path"]],   # task → the image(s) it labels
    vision_model="openai:gpt-5-nano",               # REQUIRED for a vision fan
    schema=SCHEMA,
    expect_ids=lambda task: task["ids"],            # packed envelope? arity-checked → a dropped id is a retried MISS
    checkpoint="run.jsonl", chunk_size=50,
)
```

Guards that fire (each an error row with a structured `reason` code, never a silent success):
`no_vision_model`, `no_image`, `image_unreadable`, `image_too_big` (total per-image cap). A deliberate stop
(`DispatchTimeout`) **halts** the fan rather than being downgraded to a row.

## From an MCP consumer — `spendguard_vision`

A governed, estimate-first, idempotent metered vision call:

- **No `budget_usd`** → returns only the **estimate** (0 spend).
- **`budget_usd`** → runs, refused if the estimate exceeds it.
- A dynamic-key map schema is refused **before any spend**; total image bytes are capped before load; the call is
  deadline-bounded; and a **paid result is cached by request content**, so an identical re-request is `$0`
  (`no_cache: true` forces a fresh call).

## Many vendors, one image — `crossllm.ask_vision` (durable panel)

To label one image across several vision models (consensus / adjudication), use a **durable** panel — not the
synchronous `crossllm.ask` fan, which loses paid results on a crash:

```python
from spendguard import crossllm
panel = crossllm.ask_vision(
    "Label the defect in this photo.",
    images=["/path/to/photo.jpg"],
    vendors=["openai:gpt-5-nano", "gemini:gemini-3-flash", "anthropic:claude-…"],
    schema=SCHEMA, budget_usd=0.05, checkpoint="panel.jsonl",
)
# panel["results"] == [{vendor, text, cost, executor, error}, …]   (one per vendor)
```

Each vendor's call is **checkpointed per-vendor**, so a crash mid-panel resumes without re-paying the vendors that
already answered. Estimate-first: omit `budget_usd` to get only the summed estimate (0 spend); with it, the panel is
refused (`BudgetRefused`, a deliberate stop) before any spend if the estimate exceeds it. Under the hood it is
`bulk_delegate(images_for=…, model_for=…, prompt_for=…)` — the same durable core, one task per vendor.

## Schema rule (all paths)

Keep structured-output schemas **strict-expressible**: an ARRAY of `{key,value}` objects, never a dynamic-key map
(`additionalProperties: <schema>`). On the OpenAI path a map is refused with a typed
`adapters.SchemaNotStrictExpressible` (path named); on other vendors it is fragile (the model invents keys). The
detector is `adapters.strict_map_violation(schema)` → the offending path, or `None` if it is safe.

## Cost

Images are priced by **pixels** (`content_tokens`: Anthropic `(w×h)/750`, OpenAI tiles), not the text tokenizer.
A vision call bills the metered API (`executor="api"`, `cost > 0`) — it is never `$0`-lane-served.

## `timeout_s` and the vision transport

An explicit `timeout_s` is **safe to pass on a vision call** — it bounds the request by wall-clock (a daemon-thread
join + `c.close()` that actually cancels billing) WITHOUT handing the Anthropic SDK an httpx client timeout.

This matters because an httpx client timeout on an Anthropic **vision** request (large image body) surfaces as a
spurious `"Connection error."` and the call returns `None`. It bit a bakeoff on 2026-09-27: a `claude-opus-4-8`
slate for `7thsense-vision-caption` failed **40/40** with "Connection error." while the non-Anthropic arms
(gpt-5-nano/mini, gemini) succeeded — the httpx timeout, not opus, was the fault. `adapters._call_once` now OMITS
the httpx client timeout for an `images=` call (keeping the wall-clock bound via the thread-join) so any caller's
`timeout_s` works on vision. Text calls keep the httpx timeout (fast-connect cancel). Regression guard:
`tests/test_anthropic_vision_timeout.py`. (History: production callers such as 7thsense's `vision/openai_backend.py`
had learned to never forward `timeout` to `adapters.vision` for exactly this reason; that workaround is no longer
required.)

**Still bounded when the transport hangs.** Dropping the httpx timeout for vision left the daemon-thread join +
`c.close()` in `_call_once._anth_msg` as the ONLY wall-clock bound — so that bound is *proven*, not assumed.
`tests/test_anthropic_vision_hang_bounded.py` fakes an Anthropic vision transport whose streamed read blocks far past
the deadline and asserts the call is cut at ~`timeout_s` with a `_CallDeadline` (never a hang), that the client was
built WITHOUT the httpx timeout, and that `c.close()` fired (the real billing-cancel). Removing the httpx timeout did
not remove the ceiling.

**Surface parity.** The fix lives in `_call_once`, and every real-time vision entry funnels through it via
`adapters.call(images=…)`: `adapters.vision` (forwards `images=` / `timeout_s=`), `bulk_delegate(images_for=…)` (its
`_run_task_on_api` calls `adapters.call(images=…, timeout_s=deadline_s)`), and `crossllm.ask_vision` (which *is*
`bulk_delegate` under the hood) — so all three inherit the omit-timeout behaviour. The named-entry inheritance is
guarded in `tests/test_anthropic_vision_hang_bounded.py` (Part 3). The Anthropic **Batch** submit path
(`experiment._promote_batch` / `submit.guarded_submit`) constructs timeout-FREE clients — it never handed the SDK a
per-call httpx timeout, so it was never subject to this trap (the only timeout-bearing Anthropic client construction in
the code is the `not images`-guarded one in `_call_once`).

**Live proof.** `scripts/reliability/anthropic_vision_timeout_live_proof.py` makes ONE real
`adapters.call('anthropic:claude-opus-4-8', …, images=[<generated 16×16 PNG>], timeout_s=120)` and asserts a real
caption returns with `error=None` — the layer the offline fakes cannot exercise. It pins the vendor
(`no_substitution=True`) and asserts `served_by(r) == "anthropic"`, because the model IS the measurement: unpinned, the
lane bandit swaps in another vendor (observed: `gemini-3.8-flash`) and the "proof" exercises the wrong SDK. Estimate-first:
with no flag it prints a $0 estimate (~$0.003 worst-case at the default `--cap 0.25`); `--live` makes the one metered call.
Confirmed live 2026-09-28: `served_by=anthropic`, `substituted=false`, `error=null`, a real caption returned (billed
$0.00086, out 30 tok, 1.66 s) — the fix holds against the live Anthropic SDK, no `"Connection error."`.
