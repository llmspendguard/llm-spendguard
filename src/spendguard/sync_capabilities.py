"""Sync per-model CAPABILITIES from LiteLLM's community catalog into model_catalog.json — the GROUND-TRUTH source for
"does this model accept images / enforce a response schema / call functions / …" plus the token upper bounds.

WHY LiteLLM and not a judge or a hand-list: capability is a verifiable FACT. An LLM judge cannot know models past its
training cutoff (it abstains on exactly the current-gen ids you need), and a hand-curated list silently drifts as
models ship. LiteLLM's `model_prices_and_context_window.json` is CI-updated per release (the SAME dataset sync.py
prices from), so `spendguard sync-capabilities` — also run by the daily price refresh — keeps the catalog current
with no guessing. Reads `litellm.model_cost`, matches a catalog model by its id then its metered_id, and records:
  · `vision` (supports_vision) — read by model_catalog.vision_capable + the best-value vision guard;
  · `capabilities` {response_schema, function_calling, pdf_input, prompt_caching, mode} — the rest of the profile;
  · fills `context_window` / `output_ceiling` (the upper bounds) from max_input/output_tokens ONLY when the catalog
    has none — a curated value is never overwritten.
A model LiteLLM does not cover is LEFT ABSENT (never a guessed flag → the vision guard conservatively keeps the
caller's model). $0 — a local dataset read (no LLM, no network of its own). The whole-file write goes through
config.update_json (atomic + backups), so the SSOT catalog is never truncated.
"""
import argparse
import sys

from . import config, model_catalog

# LiteLLM supports_* field → our short capabilities key. supports_vision is handled separately (top-level `vision`,
# what the guard reads). `mode` (chat/embedding/…) rides along as useful profile data.
_CAP_FIELDS = {
    "supports_response_schema": "response_schema",
    "supports_function_calling": "function_calling",
    "supports_pdf_input": "pdf_input",
    "supports_prompt_caching": "prompt_caching",
    "supports_parallel_function_calling": "parallel_function_calling",
    "supports_tool_choice": "tool_choice",
}


def _litellm_record(model_id, metered_id):
    """LiteLLM's per-model dict for a catalog model — try its id then its metered_id — or None when LiteLLM (the
    installed package's `model_cost` dataset) does not cover it. Never raises."""
    try:
        import litellm
    except Exception:
        return None
    for cand in [x for x in (model_id, metered_id) if x]:
        try:
            rec = litellm.model_cost.get(cand)
        except Exception:
            rec = None
        if isinstance(rec, dict):
            return rec
    return None


def _updates_for(models):
    """Compute the capability updates for every catalog model LiteLLM covers, WITHOUT mutating `models`. Returns
    (updates {mid: {field: value}}, summary). A curated limit (context_window/output_ceiling already has a value) is
    never scheduled for overwrite; capabilities/vision reflect LiteLLM ground truth."""
    updates, covered, vis_t, vis_f, filled = {}, [], [], [], []
    for mid, rec in models.items():
        ll = _litellm_record(mid, rec.get("metered_id"))
        if not ll:
            continue                                       # LiteLLM does not cover it → leave absent (never guess)
        covered.append(mid)
        u = {}
        sv = ll.get("supports_vision")
        if isinstance(sv, bool):
            u["vision"] = sv
            (vis_t if sv else vis_f).append(mid)
        caps = {}
        for f, key in _CAP_FIELDS.items():
            v = ll.get(f)
            if isinstance(v, bool):
                caps[key] = v
        if ll.get("mode"):
            caps["mode"] = ll["mode"]
        if caps:
            u["capabilities"] = caps
        # Upper bounds: fill ONLY when the catalog carries no curated value (never overwrite a verified one).
        cw = (rec.get("context_window") or {}).get("value") if isinstance(rec.get("context_window"), dict) else rec.get("context_window")
        if cw is None and isinstance(ll.get("max_input_tokens"), int):
            u["context_window"] = {"value": ll["max_input_tokens"], "source": "litellm"}
            filled.append(mid + ":ctx")
        oc = (rec.get("output_ceiling") or {}).get("value") if isinstance(rec.get("output_ceiling"), dict) else rec.get("output_ceiling")
        if oc is None and isinstance(ll.get("max_output_tokens"), int):
            u["output_ceiling"] = {"value": ll["max_output_tokens"], "source": "litellm"}
            filled.append(mid + ":out")
        if u:
            updates[mid] = u
    summary = {"covered": len(covered), "uncovered": len(models) - len(covered),
               "vision_true": len(vis_t), "vision_false": len(vis_f), "filled_limits": len(filled)}
    return updates, summary


def sync_capabilities(dry_run=False):
    """Sync LiteLLM capabilities + upper bounds into model_catalog.json for every model LiteLLM covers. Returns the
    summary dict. dry_run computes the summary and writes NOTHING. The write is atomic + backed up (config.update_json)."""
    models = model_catalog.all_records()
    updates, summary = _updates_for(models)
    summary["dry_run"] = bool(dry_run)
    if dry_run or not updates:
        return summary

    def _merge_capability_updates(doc):
        m = (doc or {}).get("models") or {}
        for mid, u in updates.items():
            if mid in m:
                m[mid].update(u)                          # merge capability/vision/limit fields into the record
        return doc

    config.update_json(model_catalog.DATA_PATH, _merge_capability_updates, reason="sync capabilities + upper bounds from LiteLLM")
    return summary


def cmd(argv=None):
    """`spendguard sync-capabilities` — refresh per-model capabilities (vision, response_schema, function_calling, …)
    and upper bounds from LiteLLM into the catalog. argv: optional list (default sys.argv[1:]); flags: --dry-run
    (report coverage, write nothing). Returns 0. Prints a one-line summary. LiteLLM-sourced, $0, no LLM."""
    a = argparse.ArgumentParser(prog="spendguard sync-capabilities")
    a.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = a.parse_args(sys.argv[1:] if argv is None else argv)
    s = sync_capabilities(dry_run=args.dry_run)
    tag = "[DRY-RUN] " if s.get("dry_run") else ""
    print(f"sync-capabilities {tag}: LiteLLM covers {s['covered']}/{s['covered'] + s['uncovered']} catalog models · "
          f"vision true={s['vision_true']} false={s['vision_false']} · filled {s['filled_limits']} missing limit(s)"
          f"{' (nothing written)' if s.get('dry_run') else ' → model_catalog.json'}")
    return 0
