"""Embedding execution surfaces and checkpoint lifecycle."""

import time

from ... import config
from spendguard.store.reference_data.model_catalog import embedding_input_limits
from .execution_accessors import PROVIDERS
from . import execution_accessors as _execution_accessors


_oai_compat_client = _execution_accessors.resolve__oai_compat_client()


_EMBED_MAX_BATCH = 128            # inputs per /embeddings request — conservative, well under provider caps (OpenAI
#                                   2048, Gemini smaller); overridable via max_batch=. A NAMED default, never magic.
_EMBED_TIMEOUT_S = 60             # per-request wall bound so a hung embeddings call can't wedge a whole corpus run.
_DEFAULT_EMBED_MODEL = "text-embedding-3-small"   # LAST-RESORT fail-safe ONLY if the catalog curates no OpenAI embedding
#                                                   model (a broken catalog) — never the normal path (see below).
EMBED_CHECKPOINT_DIRNAME = "embed-checkpoints"
# Six hours comfortably exceeds a normal minutes-to-low-hours multi-chunk embed run, while moving legacy bulk
# promptly enough that probe snapshots do not carry it for the seven-day retention window.
EMBED_CHECKPOINT_INFLIGHT_GRACE_HOURS = float(
    config._cfg_get("safety", "embed_checkpoint_inflight_grace_hours", 6.0)
)
EMBED_CHECKPOINT_SECONDS_PER_HOUR = 60.0 * 60.0
EMBED_CHECKPOINT_INFLIGHT_GRACE_SECONDS = (
    EMBED_CHECKPOINT_INFLIGHT_GRACE_HOURS * EMBED_CHECKPOINT_SECONDS_PER_HOUR
)


def _embed_checkpoint_dir():
    from spendguard.platform import platform_accessors as _platform_accessors
    return _platform_accessors.resolve_mark_distribution_ephemeral_dir(config.HOME / EMBED_CHECKPOINT_DIRNAME)


def _embed_checkpoint_path(basename):
    legacy_path = config.HOME / basename
    checkpoint_dir = _embed_checkpoint_dir()
    return legacy_path if legacy_path.exists() else checkpoint_dir / basename


def migrate_cold_legacy_embed_checkpoints(keep_path=None):
    """Move cold realtime checkpoints; return moved, deduplicated, retained, skipped, and error."""
    import os as _os

    checkpoint_dir = _embed_checkpoint_dir()
    keep = _os.path.abspath(keep_path) if keep_path else None
    cutoff = time.time() - EMBED_CHECKPOINT_INFLIGHT_GRACE_SECONDS
    out = {"moved": 0, "deduplicated": 0, "retained": 0, "skipped": 0, "error": None}
    try:
        names = _os.listdir(str(config.HOME))
    except FileNotFoundError:
        return out
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return out
    for name in names:
        # Batch envelopes stay at top level for collect-path safety; new ones already use the marked directory.
        if not (name.startswith("embed_") and name.endswith(".jsonl")) or name.startswith("embed_batch_"):
            continue
        source = _os.path.join(str(config.HOME), name)
        target = _os.path.join(str(checkpoint_dir), name)
        try:
            if not _os.path.isfile(source):
                continue
            if (keep and _os.path.abspath(source) == keep) or _os.stat(source).st_mtime >= cutoff:
                out["retained"] += 1
                continue
            if _os.path.exists(target):
                _os.unlink(source)
                out["deduplicated"] += 1
            else:
                _os.replace(source, target)
                out["moved"] += 1
        except OSError:
            out["skipped"] += 1
    return out


def _embed_default_model():
    """The embed model to use when a caller passes none: config `advisor.embed_model` if set, else the CATALOG's cheapest
    OpenAI embedding model (model_catalog.embedding_models — the SSOT for "which models embed"). No embedding-model id is
    hardcoded at a call site; adding/retiring one is a catalog ROW. The bare literal is a fail-safe used ONLY when the
    catalog curates no OpenAI embedding model, never the normal path."""
    try:
        ov = config._cfg_get("advisor", "embed_model", None)
        if ov:
            return ov
        from ... import model_catalog
        oai = model_catalog.embedding_models("openai")
        return oai[0][1] if oai else _DEFAULT_EMBED_MODEL     # (provider, model_id), cheapest input first
    except Exception:
        return _DEFAULT_EMBED_MODEL


def embed(texts, model=None, *, dimensions=None, max_batch=None, timeout_s=None, checkpoint=None):
    """The first-class REALTIME embedding surface — embed a list of texts on an OpenAI-COMPATIBLE provider (openai
    + gemini today; both speak /embeddings), gated + priced + metered exactly like adapters.call (the gate already
    intercepts .embeddings.create). `model` defaults to the OpenAI default (config advisor.embed_model, else
    text-embedding-3-small); pass a gemini id (e.g. 'gemini-embedding-001') to embed there, so the SAME corpus can
    be embedded on both for an A/B.

    ROBUST BULK (the CHUNK-never-single-shot rule, all four parts):
      • CHUNKED into requests of at most `max_batch` inputs;
      • DURABLE BY DEFAULT — a MULTI-chunk run auto-checkpoints to a jsonl under HOME's marked checkpoint dir (a single request is atomic;
        more than one is crash-losable), each chunk APPENDED (keyed by sha256(text) — CONTENT, not position) BEFORE
        the next runs, so a crash RESUMES and never re-pays, and a changed list re-embeds only new texts;
      • per-request TIMEOUT (default _EMBED_TIMEOUT_S) so a hung call can't wedge the run;
      • per-input SIZE GUARD + per-chunk ISOLATION — an oversized text is marked failed for that item (never sent to
        400 its chunk), and one chunk's failure marks its items and KEEPS GOING (the rest still embed + checkpoint).

    Returns a dict — NEVER raises (except a deliberate spend stop, which PROPAGATES) — {vectors, model, dims, n,
    checkpoint, failed, error}. `vectors` is ALIGNED to inputs (None where an item failed) and `failed` lists
    {i, reason}; it is never a SILENTLY short list — a None + a failed[] entry, or an explicit error. Re-run to
    retry only the failed items (the checkpoint already holds the rest)."""
    import json as _json, hashlib as _hl
    import spendguard.manage.admission.admission_accessors as _gp
    model = model or _embed_default_model()
    prov = _gp.resolve__provider_of(model)               # provider_for (prefix) → priced catalog; the canonical resolver
    raw = model.split(":", 1)[1] if ":" in model else model
    items = list(texts or [])
    timeout_s = timeout_s or _EMBED_TIMEOUT_S
    base = {"vectors": [], "model": raw, "dims": dimensions, "n": len(items),
            "checkpoint": checkpoint, "failed": [], "error": None}
    if not prov or prov == _gp.UNKNOWN_PROVIDER:
        return {**base, "error": f"cannot resolve a provider for {model!r} — pass 'provider:model'"}
    if not items:
        return base
    keys = [_hl.sha256(t.encode("utf-8", "replace")).hexdigest() for t in items]
    _n = max(1, int(max_batch or _EMBED_MAX_BATCH))
    from ... import model_catalog as _mc
    _ceiling = _mc.embed_batch_ceiling(raw)                        # provider-enforced cap from the catalog SSOT (None if uncurated)
    _input_limits = embedding_input_limits(raw)
    _input_token_limit = _input_limits["token_limit"]
    if _ceiling and _n > _ceiling:
        _n = _ceiling                                             # a caller CANNOT exceed it by accident — over the cap 400s the
        #                                                           WHOLE chunk (every input in it fails), so clamp, never trust max_batch
    if checkpoint is None and len(items) > _n:
        _ck = _hl.sha256(f"{raw}|{len(items)}|{keys[0]}|{keys[-1]}".encode()).hexdigest()[:10]
        checkpoint = str(_embed_checkpoint_path(f"embed_{raw}_{_ck}.jsonl"))  # durable by default for a multi-request run
        try:
            migrate_cold_legacy_embed_checkpoints(keep_path=checkpoint)
            gc_embed_checkpoints(apply=True, keep_path=checkpoint, include_legacy_top_level=False)  # BOUND the marked
        except Exception:                                             # OWN temp files (unlike codex's, which is why that
            pass                                                      # gc is opt-in). A gc failure never breaks the run.
    base["checkpoint"] = checkpoint
    done = {}                                                          # sha(text) -> vector, seeded from the checkpoint
    if checkpoint:
        try:
            with open(checkpoint) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        row = _json.loads(line)
                        done[row["k"]] = row["v"]
        except FileNotFoundError:
            pass
    _extra = {"dimensions": dimensions} if dimensions else {}
    import spendguard.manage.admission.admission_accessors as _g
    _stop = _g.resolve_deliberate_stop_types()
    try:
        c = _oai_compat_client(prov, timeout_s)
    except Exception as e:
        return {**base, "error": str(e)[:200]}                        # client build (unknown provider / no key)
    failed = []
    todo = []                                                         # not-yet-embedded AND within the size guard
    from spendguard.analyze.cost_estimation import provider_tokens as _provider_tokens
    for i, k in enumerate(keys):
        if k in done:
            continue
        _token_count = _provider_tokens.count_exact_model_text_tokens(items[i], provider=prov, model=raw)
        if _input_token_limit is not None and _token_count is not None and _token_count > _input_token_limit:
            failed.append({"i": i, "reason":
                           f"input {_token_count} tokens > {_input_token_limit} token limit — split it"})
            continue
        if _input_token_limit is not None and _token_count is None:
            _fallback_char_limit = _input_limits["char_limit"]
            if len(items[i]) > _fallback_char_limit:
                failed.append({"i": i, "reason":
                               f"input {len(items[i])} chars > {_fallback_char_limit} conservative character "
                               "limit — split it"})
                continue
        todo.append(i)
    def _embed_one(grp):
        """Embed one group, writing vectors to `done`/checkpoint and failures to `failed`. On a (non-stop) REJECTION,
        BISECT and recurse — the PROVIDER's accept/reject is the oracle for the workable batch size, so NO error text
        is parsed (a 400 may state a token limit AND a batch limit in one message; picking the right number would be
        a judgement, not a parse). When a group is already a single input and STILL fails, it is recorded as failed
        WITH THE PROVIDER'S OWN REASON — we have bisected as far as we can, so the reason itself (not an inference of
        ours about why) tells the caller what is wrong, and re-running retries only the failed items. Never
        double-pays: a rejected request billed nothing. Returns the largest sub-group size that SUCCEEDED (0 if
        none), so the outer loop can shrink _n for the rest of the run."""
        try:
            r = c.embeddings.create(model=raw, input=[items[i] for i in grp], **_extra)   # gate meters this
        except Exception as e:
            if isinstance(e, _stop):
                raise                                                 # a spend refusal / cap HALTS — never isolated
            if len(grp) <= 1:
                failed.append({"i": grp[0], "reason": str(e)[:160]})  # bisected to one input, still refused → record its reason
                return 0
            mid = len(grp) // 2
            return max(_embed_one(grp[:mid]), _embed_one(grp[mid:]))  # BISECT: the provider decides the workable size
        # OpenAI-compat /embeddings returns `data` in INPUT ORDER (a documented guarantee), and OpenAI stamps a
        # chunk-relative `index` while gemini/voyage return index=None — so key by the stamped index when present, else
        # the enumeration position (equal to the index by that ordering guarantee). `int(d.index)` alone raised
        # TypeError: int(None) on gemini/voyage, crashing every embedding call to those providers.
        vecs = {(int(d.index) if getattr(d, "index", None) is not None else j): list(d.embedding)
                for j, d in enumerate(r.data)}                         # index is CHUNK-relative → map to the group
        _fh = open(checkpoint, "a") if checkpoint else None           # append this chunk BEFORE the next → durable
        try:
            for j, i in enumerate(grp):
                v = vecs.get(j)
                if v is None:
                    # the provider accepted the input but returned NO vector for it (a short r.data) — record WHICH
                    # item, so every None in `vectors` has a failed[] entry (the 'never a silently-short list' rule).
                    failed.append({"i": i, "reason": "provider returned no vector for this input (omitted from r.data)"})
                    continue
                done[keys[i]] = v
                if _fh:
                    _fh.write(_json.dumps({"k": keys[i], "v": v}) + "\n")
        finally:
            if _fh:
                _fh.close()
        return len(grp)
    i0 = 0                                                            # chunk at _n; a group that only succeeds SMALLER
    while i0 < len(todo):                                             # shrinks _n for the rest (adapts to a real cap)
        grp = todo[i0:i0 + _n]
        w = _embed_one(grp)
        i0 += len(grp)
        if 0 < w < len(grp):                                         # REJECTED: the group only went through at a size
            #                                                          SMALLER than the one attempted. Compare to len(grp),
            #                                                          NOT _n — a partial final chunk or a 1-input query
            #                                                          embed succeeds WHOLE at w < _n and is NOT a rejection
            #                                                          (comparing to _n mis-read those as a cap-of-w).
            config.warn_once(f"[spendguard] embed {raw}: batches of {len(grp)} were rejected; the workable size here "
                             f"was {w} — running the rest at {w}/request. Add capabilities.embed_max_batch for {raw} "
                             f"through model_catalog.author_catalog_records(layer='seed') to size it right up front.")
            _n = w
    out = [done.get(k) for k in keys]
    n_missing = sum(1 for v in out if v is None)
    dims = next((len(v) for v in out if v is not None), dimensions)
    return {**base, "vectors": out, "dims": dims, "failed": failed,
            "error": (f"{n_missing}/{len(items)} inputs unembedded (see failed[]); re-run to retry only those"
                      if n_missing else None)}


def gc_embed_checkpoints(max_age_days=None, max_total_gb=None, apply=False, keep_path=None,
                         include_legacy_top_level=True):
    """Bound the `embed_<model>_<hash>.jsonl` resume checkpoints that embed() writes in its marked dir, plus legacy
    top-level HOME checkpoints. Measured
    2026-10-08: 1,016 files / 19.65 GiB had accumulated in ~/.spendguard, 1,014 of them inside one month — embed()
    created a checkpoint per multi-chunk run and NOTHING ever pruned one.

    Why a RETENTION BOUND and not delete-on-success: a checkpoint is keyed by sha256(text), so re-running the same
    corpus is served entirely from it and re-pays NOTHING. Deleting on clean completion would hand that saving back
    (and break the resume guarantee tests/test_embed_surface.py asserts). So a checkpoint stays useful for a window
    and is pruned once it is COLD — recent ones are never touched.

    TWO bounds, both applied (age first, then total size):
      • AGE — older than `max_age_days` (safety.embed_checkpoint_max_age_days, default 7) is cold → prune;
      • SIZE — if what SURVIVES the age bound still exceeds `max_total_gb`
        (safety.embed_checkpoint_max_gb, default 5), prune OLDEST-FIRST until under the cap, so one month of heavy
        embedding cannot fill the disk even when every file is recent.

    Unlike codex_exec.gc_shell_snapshots (opt-in, because it deletes in CODEX's dir), this prunes spendguard's OWN
    temp files, so embed() calls it on create. DRY-RUN by default; deletes only with apply=True. `keep_path` is
    never pruned — the in-flight run's own checkpoint, which both bounds must ignore.

    `embed_batch_*.jsonl` is EXCLUDED: that is a submitted batch's request envelope, still needed to collect its
    results — a different artefact that happens to share the prefix. Honest reporting (never masks a failure as a
    clean sweep): a listing failure sets `error`; a per-file stat/unlink failure is COUNTED in `skipped`. Returns
    {dir, examined, stale, over_cap, bytes, deleted, skipped, apply, cutoff_days, max_total_gb, error}."""
    import os as _os
    if max_age_days is None:
        max_age_days = float(config._cfg_get("safety", "embed_checkpoint_max_age_days", 7.0))
    if max_total_gb is None:
        max_total_gb = float(config._cfg_get("safety", "embed_checkpoint_max_gb", 5.0))
    checkpoint_dir = _embed_checkpoint_dir()
    out = {"dir": str(checkpoint_dir), "examined": 0, "stale": 0, "over_cap": 0, "bytes": 0, "deleted": 0,
           "skipped": 0, "apply": bool(apply), "cutoff_days": max_age_days, "max_total_gb": max_total_gb,
           "error": None}
    keep = _os.path.abspath(keep_path) if keep_path else None
    cutoff = time.time() - max(0.0, float(max_age_days)) * 86400.0
    cold, warm = [], []                                   # (mtime, size, path) — cold = past the age bound
    scan_dirs = (checkpoint_dir, config.HOME) if include_legacy_top_level else (checkpoint_dir,)
    for scan_dir in scan_dirs:
        try:
            names = _os.listdir(str(scan_dir))
        except FileNotFoundError:
            continue
        except OSError as e:                              # a real scan failure is surfaced, not a false 'empty, clean'
            out["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            return out
        for name in names:
            # FORMAT match on spendguard's own naming, not a judgement: embed_<model>_<hash>.jsonl, excluding the
            # embed_batch_ envelopes (a submitted batch still needs those to collect).
            if not (name.startswith("embed_") and name.endswith(".jsonl")) or name.startswith("embed_batch_"):
                continue
            p = _os.path.join(str(scan_dir), name)
            try:
                if not _os.path.isfile(p):
                    continue
                st = _os.stat(p)
            except OSError:
                out["skipped"] += 1                       # could not stat → COUNTED, never silently ignored
                continue
            if keep and _os.path.abspath(p) == keep:
                continue                                  # the in-flight run's own checkpoint — both bounds skip it
            out["examined"] += 1
            (cold if st.st_mtime < cutoff else warm).append((st.st_mtime, int(st.st_size), p))
    doomed = list(cold)
    out["stale"] = len(cold)
    warm.sort()                                           # oldest first — the size bound evicts the coldest survivors
    _surviving = sum(s for _m, s, _p in warm)
    _cap = max(0.0, float(max_total_gb)) * (1024 ** 3)
    for m, s, p in warm:
        if _surviving <= _cap:
            break
        doomed.append((m, s, p))
        out["over_cap"] += 1
        _surviving -= s
    out["bytes"] = sum(s for _m, s, _p in doomed)
    if apply:
        for _m, _s, p in doomed:
            try:
                _os.unlink(p)
                out["deleted"] += 1
            except OSError:
                out["skipped"] += 1                       # doomed but could not delete (perms/locked) → COUNTED
    return out


def embed_batch(texts, model=None, *, dimensions=None, jsonl_path=None, cap_dollars=None, submit=True,
                preflight=True):
    """The first-class BATCH embedding surface — the ASYNC Batch API (~50% cheaper) for a LARGE corpus, gated
    estimate-first + capped by the SAME chokepoint every batch submission passes (submit.guarded_submit). Builds a
    /v1/embeddings JSONL (one input per line, custom_id 'emb-<i>' = input index) at a durable path, estimates +
    cap-checks it ($0, no paid call), then submits. Returns {batch_id, jsonl, requests, error}; submit=False
    writes + estimates only. Collect results later with callio.guarded_collect(batch_id) — vectors map by custom_id.

    Batch availability is a CONFIG CAPABILITY on the provider spec (`batch`), never a hardcoded id check: OpenAI
    declares it (its async Batch API). A provider that does not (gemini's OpenAI-compat endpoint exposes no
    /batches — its async batch is a separate native API) returns a clear error pointing to embed() (chunked
    realtime) until its native batch path is wired — a LOUD boundary, never a silent metered fallback."""
    import json as _json, hashlib as _hl
    import spendguard.manage.admission.admission_accessors as _gp
    model = model or _embed_default_model()
    prov = _gp.resolve__provider_of(model)               # provider_for (prefix) → priced catalog; the canonical resolver
    raw = model.split(":", 1)[1] if ":" in model else model
    items = list(texts or [])
    base = {"batch_id": None, "jsonl": None, "requests": len(items), "error": None}
    if not PROVIDERS.get(prov or "", {}).get("batch"):
        return {**base, "error": f"{prov} does not declare the async Batch API (provider spec 'batch') — use embed() "
                                 f"(chunked realtime) for {prov}, or wire + declare its native batch path."}
    if not items:
        return base
    if not jsonl_path:                                               # a stable, content-derived name (so a re-run reuses it)
        _tag = _hl.sha256(f"{raw}|{len(items)}|{items[0][:80]}|{items[-1][:80]}".encode("utf-8", "replace")).hexdigest()[:10]
        jsonl_path = str(_embed_checkpoint_path(f"embed_batch_{raw}_{_tag}.jsonl"))
    try:
        with open(jsonl_path, "w") as fh:
            for i, t in enumerate(items):
                body = {"model": raw, "input": t}
                if dimensions:
                    body["dimensions"] = dimensions
                fh.write(_json.dumps({"custom_id": f"emb-{i}", "method": "POST",
                                      "url": "/v1/embeddings", "body": body}) + "\n")
        from . import submit as _submit
        bid = _submit.guarded_submit(jsonl_path, model=raw, cap_dollars=cap_dollars, batch=True,
                                     submit=submit, endpoint="/v1/embeddings", preflight=preflight)
        return {**base, "batch_id": bid, "jsonl": jsonl_path}
    except Exception as e:
        import spendguard.manage.admission.admission_accessors as _g2
        if isinstance(e, _g2.resolve_deliberate_stop_types()):
            raise                                                     # a cap refusal HALTS — never a silent partial
        return {**base, "jsonl": jsonl_path, "error": str(e)[:200]}


_EMBED_COMPARE_SAMPLE = 200       # texts embedded per model for an A/B — a sample is enough for a STRUCTURE compare


def embed_compare(texts, models=None, *, sample=None):
    """A/B two or more embedding models on the SAME texts — the cheap way to decide openai vs gemini (vs voyage)
    BEFORE committing a corpus + a Qdrant collection to one. Embeds a SAMPLE of `texts` on each model (default: the
    OpenAI default + gemini-embedding-001), then reports per-model {dims, rate_per_1m, n_failed} and — PAIRWISE — how
    much the models AGREE on neighbourhood STRUCTURE: the Pearson correlation of their off-diagonal cosine-similarity
    matrices (≈1.0 = the models call the same texts near/far; low = they disagree on what is similar). A quality
    signal that needs NO labelled retrieval set. Makes REAL (gated) embedding calls on the sample — small, not free.

    Returns {sample_n, models: [{model, dims, rate_per_1m, n_failed, error}],
             agreement: [{a, b, structure_corr, pairs}]}."""
    from ... import pricing as _pr

    def _cosine_sim(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return (dot / (na * nb)) if na and nb else 0.0

    def _pearson(xs, ys):
        n = len(xs)
        if n < 2:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs) ** 0.5
        vy = sum((y - my) ** 2 for y in ys) ** 0.5
        return (cov / (vx * vy)) if vx and vy else None

    if models:
        models = list(models)
    else:
        # DEFAULT A/B slate from the catalog SSOT (no hardcoded embedding ids): the OpenAI default + the cheapest
        # embedding model of every OTHER embedding-capable provider (gemini, voyage, …). Adding a provider's embedder to
        # the catalog auto-joins this comparison; it is never a literal here.
        from ... import model_catalog as _mc
        _default = _embed_default_model()
        models, _seen = [_default], {(_mc.provider_of(_default) or "openai")}
        for _prov, _mid in _mc.embedding_models():
            if _prov not in _seen:
                _seen.add(_prov)
                models.append(f"{_prov}:{_mid}")
    items = list(texts or [])
    _s = int(sample or _EMBED_COMPARE_SAMPLE)
    items = items[:_s] if len(items) > _s else items
    per, sims = [], {}
    for m in models:
        r = embed(items, m)                                          # gated + metered; a sample, so bounded spend
        vecs = r.get("vectors") or []
        try:
            rate = _pr.price(m.split(":", 1)[-1]).get("in_")
        except Exception:
            rate = None
        per.append({"model": m, "dims": r.get("dims"), "rate_per_1m": rate,
                    "n_failed": len(r.get("failed") or []), "error": r.get("error")})
        sims[m] = {(i, j): _cosine_sim(vecs[i], vecs[j])            # off-diagonal cosine over pairs both models have
                   for i in range(len(vecs)) for j in range(i + 1, len(vecs))
                   if vecs[i] is not None and vecs[j] is not None}
    agreement = []
    for a in range(len(models)):
        for b in range(a + 1, len(models)):
            ma, mb = models[a], models[b]
            common = [k for k in sims.get(ma, {}) if k in sims.get(mb, {})]
            agreement.append({"a": ma, "b": mb, "pairs": len(common),
                              "structure_corr": _pearson([sims[ma][k] for k in common],
                                                         [sims[mb][k] for k in common])})
    return {"sample_n": len(items), "models": per, "agreement": agreement}
