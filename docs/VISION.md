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

## Schema rule (all paths)

Keep structured-output schemas **strict-expressible**: an ARRAY of `{key,value}` objects, never a dynamic-key map
(`additionalProperties: <schema>`). On the OpenAI path a map is refused with a typed
`adapters.SchemaNotStrictExpressible` (path named); on other vendors it is fragile (the model invents keys). The
detector is `adapters.strict_map_violation(schema)` → the offending path, or `None` if it is safe.

## Cost

Images are priced by **pixels** (`content_tokens`: Anthropic `(w×h)/750`, OpenAI tiles), not the text tokenizer.
A vision call bills the metered API (`executor="api"`, `cost > 0`) — it is never `$0`-lane-served.
