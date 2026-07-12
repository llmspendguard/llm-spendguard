"""Learned cost calibration — corrects naive job estimates from OUR OWN captured actuals.

estimate.py projects a job naively (measured sample × pricing.py). The naive form — input ≈
len(prompt)/4, output = max_tokens, flat realtime price — misses in a PREDICTABLE, per-activity way:
models rarely fill max_tokens, tokenizers differ, batch ≠ realtime, caching discounts land. This
module learns those corrections per (label=intent, model, transport) from corpora spendguard
already captures — nothing new is collected:

  calls             (gate forward-capture + backfill)  actual in/out tokens + actual $ per call/batch
  gate_calls        (bulkgate truncation telemetry)    actual out_tok vs the requested max_tokens
  cost_predictions  (this module)                      caller predictions paired to actuals

Learned quantities (quantile distributions, p50/p90 — pure sqlite statistics, zero LLM spend):
  FILL        actual_out ÷ requested max_tokens   (models rarely fill the cap — the biggest error)
  OUT_PER_IN  actual_out ÷ actual_in              (output predictor when no cap obs exist for the cell)
  RESIDUAL    actual $ ÷ pricing.py(model, transport, tokens)   (caching discounts, token-count drift)
  IN_RATIO    actual_in ÷ caller-estimated in     (from PAIRED predictions only; prior 1.0)

Each quantity walks its own specificity chain (exact cell → model → global) with empirical-Bayes
shrinkage toward the parent — sparse cells borrow strength instead of overfitting, and with zero
observations the answer IS the naive one. The level used + n_obs are returned: confidence is part
of the answer, never hidden.

Interface (prices ONLY via pricing.py — never hardcoded):
  estimate(label, n, model, transport, est_in_tokens=…, est_out_max=…)
      → {p50_usd, p90_usd, level, n_obs, basis, naive_usd, …}     est_in/out_max are PER REQUEST
  record_estimate(job_id, label, model, predicted_usd, …)   log a caller's PREDICTION — distinct from
      bulkgate.record_estimate, which authorizes worst-case spend before a run
  pair(now=…)   match predictions to captured actuals: chain==job_id exact (use
      calls.context(chain=job_id) around the run), else label+model inside the pairing window
  backtest()    time-split MAPE, calibrated vs naive — the ship gate ("beats naive or it doesn't ship")

Personalization: the local corpus IS this user's work; predictions carry user+project so the org
axis can aggregate server-side (the SaaS key is org-scoped) — a follow-on, not faked here.
CLI: spendguard calibrate {predict,show,pair,backtest}
"""
import json
import os
import sqlite3
import time
import datetime

from . import pricing

P_LO, P_HI = 0.50, 0.90       # the two quantiles every learned quantity reports
SHRINK_K = 8.0                # EB prior strength: a level with n obs gets weight n/(n+K) vs its parent
MIN_LEVEL_OBS = 5             # a level "carries" the reported confidence once it has this many obs
DEFAULT_PAIR_HORIZON_H = 24   # actuals matched inside this window after a prediction (env override)
MIN_CELL_BACKTEST = 8         # a (label, model, transport) cell needs this many rows to backtest
BACKTEST_TRAIN_FRAC = 0.7     # time-ordered split: earliest 70% train, latest 30% held out
META_PREFIX = "spendguard:"   # our own meta calls are never used to calibrate workloads
MAX_SHARE_CELLS = 400         # cap on cells per push (largest-n first; a truncation is LOGGED, never silent)
MIN_SHARE_OBS = 3             # a cell travels to the org only once it has this many observations
ORG_CACHE = "org_calibration.json"   # fetched org aggregate (HOME-relative); the shrinkage PRIOR


def _pair_horizon_s():
    return float(os.environ.get("SPENDGUARD_PAIR_HORIZON_H") or DEFAULT_PAIR_HORIZON_H) * 3600


def _con():
    from . import config
    c = sqlite3.connect(config.db_path(), timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS cost_predictions(
        job_id TEXT PRIMARY KEY, ts TEXT, label TEXT, model TEXT, transport TEXT,
        n_req INTEGER, est_in INTEGER, est_out_max INTEGER, predicted_usd REAL,
        user TEXT, project TEXT,
        actual_in INTEGER, actual_out INTEGER, actual_usd REAL, actual_calls INTEGER,
        paired_ts TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cp_paired ON cost_predictions(paired_ts)")  # pair() scans unpaired
    return c


def _identity():
    """(user, project) this install attributes predictions to — from saas config, never hardcoded."""
    user = project = None
    try:
        from . import saas, config
        user = saas.contributor()
        project = config.saas_config().get("project")
    except Exception:
        pass
    return user, project


def _quantile(vals, p):
    """Linear-interpolated quantile of a list (no numpy dependency)."""
    s = sorted(vals)
    if not s:
        return None
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _shrink(chain, prior, prior_level="prior", prior_n=0):
    """Empirical-Bayes fold: chain = [(level_name, n, p50, p90), …] most-specific FIRST; prior = (p50, p90) —
    the naive assumption, or the ORG aggregate when one has been fetched (the org's experience is a better
    prior; local stats always sit on top). Folding broad→specific, each level pulls the estimate toward its
    own value with weight n/(n+K). Returns ((p50, p90), level_name, n_obs) where level/n describe the most
    specific LOCAL level meeting MIN_LEVEL_OBS, else the sparse local one, else the prior itself."""
    est = prior
    for _name, n, p50, p90 in reversed(chain):
        if not n:
            continue
        w = n / (n + SHRINK_K)
        est = (w * p50 + (1 - w) * est[0], w * p90 + (1 - w) * est[1])
    level, n_obs = prior_level, prior_n
    for name, n, _p50, _p90 in chain:            # most specific first
        if n >= MIN_LEVEL_OBS:
            level, n_obs = name, n
            break
        if n and level == prior_level:
            level, n_obs = name + "(sparse)", n
    return est, level, n_obs


def _chain_from_obs(named_obs):
    """[(name, [ratios…]), …] → shrink()-ready [(name, n, p50, p90), …]."""
    out = []
    for name, vals in named_obs:
        vals = [v for v in vals if v is not None]
        out.append((name, len(vals), _quantile(vals, P_LO) or 0.0, _quantile(vals, P_HI) or 0.0))
    return out


# ─────────────────────────── observation fetchers (all honor as_of) ───────────────────────────
def _fill_obs(label, model, as_of=None):
    """FILL = out_tok / max_tokens from gate_calls. Levels: exact (sig of label+model) → model → global.
    gate_calls keys by sig (hash of model+template), so the exact cell is recomputed, not stored."""
    from . import bulkgate, config
    epoch_cut = None
    if as_of:
        dt = datetime.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        epoch_cut = dt.timestamp()
    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        def grab(where, args):
            q = ("SELECT CAST(out_tok AS REAL)/max_tokens FROM gate_calls "
                 "WHERE max_tokens > 0 AND out_tok IS NOT NULL " + where)
            if epoch_cut is not None:
                q += " AND ts <= ?"
                args = args + [epoch_cut]
            try:
                return [r[0] for r in con.execute(q, args).fetchall()]
            except sqlite3.OperationalError:
                return []                       # no gate_calls table yet
        exact = grab("AND sig = ?", [bulkgate.sig(pricing.normalize(model), template_id=label)]) + \
            grab("AND sig = ?", [bulkgate.sig(model, template_id=label)])
        return [("exact", exact),
                ("model", grab("AND model = ?", [model])),
                ("global", grab("", []))]
    finally:
        con.close()


def _calls_rows(label, model, transport, as_of=None):
    """calls rows for a cell (workload only, priced, tokenized). model matched post-normalize in python
    because the corpus stores raw ids (e.g. dated snapshots) while callers pass canonical names."""
    from . import config
    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        q = ("SELECT intent, model, kind, in_tok, out_tok, cost, ts FROM calls "
             "WHERE cost > 0 AND in_tok > 0 AND out_tok > 0 AND model IS NOT NULL "
             "AND (intent IS NULL OR intent NOT LIKE ?)")
        args = [META_PREFIX + "%"]
        if as_of:
            q += " AND ts <= ?"
            args.append(as_of)
        want = pricing.normalize(model) if model else None
        rows = []
        for it, m, kind, itok, otok, cost, ts in con.execute(q, args).fetchall():
            if want and pricing.normalize(m) != want:
                continue
            if label is not None and it != label:
                continue
            if transport is not None and kind != transport:
                continue
            rows.append((it, m, kind, itok, otok, cost, ts))
        return rows
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _opi_obs(label, model, as_of=None):
    """OUT_PER_IN levels: exact (label+model) → label → model → global (transport-pooled: token
    behavior is a property of the work, not the channel)."""
    def ratios(la, mo):
        return [o / i for _it, _m, _k, i, o, _c, _ts in _calls_rows(la, mo, None, as_of)]
    return [("exact", ratios(label, model)), ("label", ratios(label, None)),
            ("model", ratios(None, model)), ("global", ratios(None, None))]


def _residual_obs(label, model, transport, as_of=None):
    """RESIDUAL = actual $ ÷ what pricing.py says those tokens cost on that transport. Captures what
    rate tables can't see per-cell (cache hits, provider token-count drift). Unpriced models are
    skipped, never guessed. Levels: exact → model+transport → transport."""
    def ratios(la, mo):
        out = []
        for _it, m, kind, itok, otok, cost, _ts in _calls_rows(la, mo, transport, as_of):
            try:
                base = (pricing.batch_cost if kind == "batch" else pricing.realtime_cost)(m, itok, otok)
            except Exception:
                continue
            if base > 0:
                out.append(cost / base)
        return out
    return [("exact", ratios(label, model)), ("model", ratios(None, model)),
            ("transport", ratios(None, None))]


def _in_ratio_obs(label, model, as_of=None):
    """IN_RATIO = paired actual_in ÷ (est_in × n_req) — exists only once predictions get paired."""
    con = _con()
    try:
        q = ("SELECT label, model, CAST(actual_in AS REAL)/(est_in*n_req) FROM cost_predictions "
             "WHERE paired_ts IS NOT NULL AND actual_calls > 0 AND est_in > 0 AND n_req > 0 "
             "AND actual_in IS NOT NULL")
        args = []
        if as_of:
            q += " AND ts <= ?"
            args.append(as_of)
        rows = con.execute(q, args).fetchall()
    finally:
        con.close()
    want = pricing.normalize(model) if model else None
    exact = [r for la, mo, r in rows if la == label and (not want or pricing.normalize(mo) == want)]
    lab = [r for la, _mo, r in rows if la == label]
    return [("exact", exact), ("label", lab), ("global", [r for _la, _mo, r in rows])]


# ───────────────────────── org-shared prior (fetched aggregate) ─────────────────────────
def _org_cells():
    """The cached org aggregate (list of cells) or []. Written by fetch_shared(); age is reported
    by show(), not enforced — a stale org prior still beats a naive one, and local stats dominate."""
    from . import config
    try:
        return json.loads((config.HOME / ORG_CACHE).read_text()).get("cells") or []
    except Exception:
        return []


def _org_prior(quantity, label, model, transport=""):
    """(p50, p90, n, exact) for the best org-level match of a quantity — exact (label, model[, transport])
    first, else model-pooled (n-weighted across the org's labels). None when the org knows nothing."""
    cells = [c for c in _org_cells() if c.get("quantity") == quantity and c.get("n")]
    if not cells:
        return None
    want = pricing.normalize(model) if model else None

    def m_ok(c):
        return not want or pricing.normalize(c.get("model") or "") == want
    exact = [c for c in cells if c.get("label") == label and m_ok(c)
             and (quantity != "residual" or (c.get("transport") or "") == (transport or ""))]
    pool = exact or [c for c in cells if m_ok(c)]
    if not pool:
        return None
    n = sum(c["n"] for c in pool)
    p50 = sum(c["p50"] * c["n"] for c in pool) / n
    p90 = sum(c["p90"] * c["n"] for c in pool) / n
    return (p50, p90, n, bool(exact))


def _with_org(chain, quantity, label, model, transport="", at=1, naive=(1.0, 1.0)):
    """Place the org's knowledge at the right SPECIFICITY. An exact-label org cell enters the chain at
    position `at` — under this machine's own cell evidence, above its generic cross-label/model pools
    (the label's behavior beats a pool of other work). A model-pooled org match stays the outermost
    prior. Returns (chain, prior, prior_level, prior_n) ready for _shrink."""
    o = _org_prior(quantity, label, model, transport)
    if o and o[3]:
        return chain[:at] + [("org", o[2], o[0], o[1])] + chain[at:], naive, "prior", 0
    if o:
        return chain, (o[0], o[1]), "org", o[2]
    return chain, naive, "prior", 0


# ─────────────────────────────────── the estimator ───────────────────────────────────
def estimate(label, n=1, model=None, transport="batch", est_in_tokens=None, est_out_max=None,
             as_of=None):
    """Predict a planned job's $ from OUR history. est_in_tokens / est_out_max are PER REQUEST
    (what the caller knows: rendered-prompt tokens and the max_tokens it will set); n = requests.
    Returns {p50_usd, p90_usd, level, n_obs, basis, naive_usd, per_request:{in_p50, out_p50}, …}.
    p90 compounds each quantity's p90 (conservative upper band). Zero spend — sqlite + pricing.py."""
    if not model:
        raise ValueError("estimate() needs a model")
    if transport not in ("batch", "realtime"):
        raise ValueError("transport must be 'batch' or 'realtime'")
    p = pricing.price(model)                      # raises for unknown models — never guess a price
    rate_in, rate_out = (p["batch_in"], p["batch_out"]) if transport == "batch" else (p["in_"], p["out"])

    # INPUT per request: caller estimate × learned tokenizer/packing ratio (prior 1.0). If the caller
    # has no estimate, fall back to the cell's own realtime per-call median (batch rows are whole-batch
    # totals — never a per-request stand-in).
    if est_in_tokens is None:
        per_call = [i for _it, _m, k, i, _o, _c, _ts in _calls_rows(label, model, "realtime", as_of)]
        est_in_tokens = _quantile(per_call, P_LO)
        if not est_in_tokens:
            raise ValueError("supply est_in_tokens (render the real prompt and count) — "
                             "no per-call history for this cell to infer it from")
    ir_chain, ir_prior, ir_pl, ir_pn = _with_org(
        _chain_from_obs(_in_ratio_obs(label, model, as_of)), "in_ratio", label, model, at=2)
    (ir50, ir90), _lvl_in, _n_in = _shrink(ir_chain, ir_prior, ir_pl, ir_pn)
    in50, in90 = est_in_tokens * ir50, est_in_tokens * ir90

    # OUTPUT per request — the dominant error source. Prefer learned FILL of the caller's cap
    # (local cell evidence on top, the org's exact-label cells under it, generic pools under that,
    # naive fill=1.0 at the bottom — zero data anywhere → the naive answer); else OUT_PER_IN; else the cap.
    fill_chain, f_prior, f_pl, f_pn = _with_org(
        _chain_from_obs(_fill_obs(label, model, as_of)) if est_out_max else [],
        "fill", label, model, at=1) if est_out_max else ([], (1.0, 1.0), "prior", 0)
    opi_chain, o_prior, o_pl, o_pn = _with_org(
        _chain_from_obs(_opi_obs(label, model, as_of)), "opi", label, model, at=2, naive=None)
    if est_out_max and (any(nn for _l, nn, _a, _b in fill_chain) or f_pn):
        (f50, f90), level, n_obs = _shrink(fill_chain, f_prior, f_pl, f_pn)
        out50, out90 = min(est_out_max * f50, est_out_max), min(est_out_max * f90, est_out_max)
        basis = "fill"
    elif any(nn for _l, nn, _a, _b in opi_chain) or o_pn:
        broadest = next(((a, b) for _l, nn, a, b in reversed(opi_chain) if nn), (0.0, 0.0))
        (o50, o90), level, n_obs = _shrink(opi_chain, o_prior or broadest, o_pl, o_pn)
        out50, out90 = in50 * o50, in90 * o90
        if est_out_max:
            out50, out90 = min(out50, est_out_max), min(out90, est_out_max)
        basis = "out_per_in"
    elif est_out_max:
        out50 = out90 = est_out_max
        level, n_obs, basis = "prior", 0, "cap"
    else:
        raise ValueError("supply est_out_max (your max_tokens) — no output history for this cell")

    # $: transport-correct rates × learned residual (org's exact cells under local, else prior 1.0)
    r_chain, r_prior, r_pl, r_pn = _with_org(
        _chain_from_obs(_residual_obs(label, model, transport, as_of)), "residual", label, model,
        transport, at=1)
    (r50, r90), _lvl_r, _n_r = _shrink(r_chain, r_prior, r_pl, r_pn)
    usd50 = n * (in50 * rate_in + out50 * rate_out) / 1e6 * r50
    usd90 = n * (in90 * rate_in + out90 * rate_out) / 1e6 * r90

    # the naive estimate this corrects: cap fully filled, FLAT realtime price, ratio 1.0
    naive = None
    if est_out_max:
        naive = n * (est_in_tokens * p["in_"] + est_out_max * p["out"]) / 1e6
    return {"label": label, "model": pricing.normalize(model), "transport": transport, "n": n,
            "p50_usd": round(usd50, 4), "p90_usd": round(usd90, 4),
            "level": level, "n_obs": n_obs, "basis": basis,
            "naive_usd": round(naive, 4) if naive is not None else None,
            "per_request": {"in_p50": round(in50, 1), "out_p50": round(out50, 1)}}


# ────────────────────────── prediction log + pairing to actuals ──────────────────────────
def record_estimate(job_id, label, model, predicted_usd, est_in=None, est_out_max=None,
                    n=1, transport="batch", user=None, project=None):
    """Log a caller's PREDICTION for a job (per-request est_in/est_out_max, n requests). The gate
    captures the ACTUALS as the job runs; pair() joins them by job_id — wrap the run in
    calls.context(chain=job_id) for an exact join. Idempotent on job_id. Never raises."""
    try:
        du, dp = _identity()
        con = _con()
        con.execute("INSERT OR REPLACE INTO cost_predictions "
                    "(job_id, ts, label, model, transport, n_req, est_in, est_out_max, predicted_usd, "
                    "user, project) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(job_id), datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                     label, model, transport, int(n), est_in, est_out_max, predicted_usd,
                     user or du, project or dp))
        con.commit()
        con.close()
        return True
    except Exception:
        return False


def pair(now=None):
    """Join predictions to captured actuals once their window has closed. Exact join first
    (calls.chain == job_id, any age); else label+model calls inside [pred_ts, pred_ts+horizon].
    A closed window with no actuals is marked expired (actual_calls=0) — UNKNOWN stays visible,
    it never reads as $0 spent. Idempotent. Returns {paired, expired, pending}."""
    from . import config
    now = time.time() if now is None else now
    horizon = _pair_horizon_s()
    con = _con()
    ccon = sqlite3.connect(config.db_path(), timeout=10)
    paired = expired = pending = 0
    try:
        preds = con.execute("SELECT job_id, ts, label, model FROM cost_predictions "
                            "WHERE paired_ts IS NULL").fetchall()
        for job_id, ts, label, model in preds:
            t0 = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            rows = ccon.execute("SELECT in_tok, out_tok, cost, ts, model FROM calls WHERE chain = ?",
                                (job_id,)).fetchall()
            exact = bool(rows)
            if not exact:
                want = pricing.normalize(model) if model else None
                t_end = datetime.datetime.fromtimestamp(t0 + horizon, tz=datetime.timezone.utc) \
                    .isoformat(timespec="seconds")
                rows = [r for r in ccon.execute(
                    "SELECT in_tok, out_tok, cost, ts, model FROM calls "
                    "WHERE intent = ? AND ts >= ? AND ts <= ?", (label, ts, t_end)).fetchall()
                    if not want or pricing.normalize(r[4] or "") == want]
            if now < t0 + horizon and not exact:
                pending += 1                     # window still open — actuals may still arrive
                continue
            stamp = datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc) \
                .isoformat(timespec="seconds")
            if rows:
                con.execute("UPDATE cost_predictions SET actual_in=?, actual_out=?, actual_usd=?, "
                            "actual_calls=?, paired_ts=? WHERE job_id=?",
                            (sum(r[0] or 0 for r in rows), sum(r[1] or 0 for r in rows),
                             round(sum(r[2] or 0 for r in rows), 6), len(rows), stamp, job_id))
                paired += 1
            else:
                con.execute("UPDATE cost_predictions SET actual_calls=0, paired_ts=? WHERE job_id=?",
                            (stamp, job_id))
                expired += 1
        con.commit()
    finally:
        con.close()
        ccon.close()
    return {"paired": paired, "expired": expired, "pending": pending}


# ─────────────────────────────── backtest: the ship gate ───────────────────────────────
def backtest(as_json=False, min_cell=MIN_CELL_BACKTEST):
    """Held-out replay on the REAL corpus: per (label, model, transport) cell, train on the earliest
    70% (time order), predict each held-out row's $ two ways, score median abs % error (MAPE).
      naive      = row's own in_tok (PERFECT input knowledge — generous to naive) × flat realtime
                   rates, output = the train max (the cap a careful engineer would set).
      calibrated = estimate(…, as_of=train end) with the SAME inputs.
    The deck is stacked FOR naive on input; the learned win must come from output-fill, transport
    and residual — exactly the claimed error sources. Ship gate: calibrated beats naive overall."""
    from . import config
    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        raw = con.execute(
            "SELECT intent, model, kind, in_tok, out_tok, cost, ts FROM calls "
            "WHERE cost > 0 AND in_tok > 0 AND out_tok > 0 AND intent IS NOT NULL "
            "AND model IS NOT NULL AND kind IN ('batch','realtime') AND intent NOT LIKE ?",
            (META_PREFIX + "%",)).fetchall()
    except sqlite3.OperationalError:
        raw = []
    finally:
        con.close()
    cells, skipped_unpriced = {}, 0
    for it, m, kind, itok, otok, cost, ts in raw:
        try:
            canon = pricing.normalize(m)
            pricing.price(canon)
        except Exception:
            skipped_unpriced += 1
            continue
        cells.setdefault((it, canon, kind), []).append((ts, itok, otok, cost, m))

    results, n_pred, c_apes, n_apes = [], 0, [], []
    for (label, model, transport), rows in sorted(cells.items()):
        if len(rows) < min_cell:
            continue
        rows.sort()
        cut = max(int(len(rows) * BACKTEST_TRAIN_FRAC), 1)
        train, test = rows[:cut], rows[cut:]
        if not test:
            continue
        as_of = train[-1][0]
        cap = max(r[2] for r in train)            # the minimal cap that fits everything seen so far
        p = pricing.price(model)
        cell_c, cell_n = [], []
        for _ts, itok, otok, cost, _m in test:
            naive = (itok * p["in_"] + cap * p["out"]) / 1e6
            try:
                cal = estimate(label, n=1, model=model, transport=transport,
                               est_in_tokens=itok, est_out_max=cap, as_of=as_of)["p50_usd"]
            except Exception:
                continue
            cell_n.append(abs(naive - cost) / cost)
            cell_c.append(abs(cal - cost) / cost)
        if not cell_c:
            continue
        n_pred += len(cell_c)
        c_apes += cell_c
        n_apes += cell_n
        results.append({"label": label, "model": model, "transport": transport,
                        "n_test": len(cell_c),
                        "naive_mape": round(_quantile(cell_n, 0.5), 4),
                        "calibrated_mape": round(_quantile(cell_c, 0.5), 4)})
    overall = {"cells": len(results), "test_rows": n_pred, "skipped_unpriced_rows": skipped_unpriced,
               "naive_mape": round(_quantile(n_apes, 0.5), 4) if n_apes else None,
               "calibrated_mape": round(_quantile(c_apes, 0.5), 4) if c_apes else None}
    overall["ship"] = bool(n_apes) and overall["calibrated_mape"] < overall["naive_mape"]
    out = {"overall": overall, "cells": sorted(results, key=lambda r: -r["n_test"])}
    if as_json:
        print(json.dumps(out, indent=1))
        return out
    if not results:
        print("calibrate backtest: no cell has enough labeled history yet "
              f"(need ≥{min_cell} priced rows with an intent) — run `spendguard backfill` / label intents first.")
        return out
    print(f"calibrate backtest — {overall['test_rows']} held-out rows across {overall['cells']} cells "
          f"(median abs % error, lower is better)\n")
    print(f"{'activity':<28}{'model':<22}{'kind':<9}{'n':>4}{'naive':>9}{'learned':>9}")
    for r in out["cells"]:
        print(f"{r['label'][:27]:<28}{r['model'][:21]:<22}{r['transport']:<9}{r['n_test']:>4}"
              f"{r['naive_mape']*100:>8.0f}%{r['calibrated_mape']*100:>8.0f}%")
    if skipped_unpriced:
        print(f"  (skipped {skipped_unpriced} rows on models pricing.py doesn't know — add them, never guess)")
    print(f"\nOVERALL   naive {overall['naive_mape']*100:.0f}%  →  learned {overall['calibrated_mape']*100:.0f}%   "
          + ("✅ learned estimator WINS — ship" if overall["ship"] else "❌ does NOT beat naive — do not ship"))
    return out


# ───────────────────── org sharing: package stats up, pull the aggregate down ─────────────────────
def cell_stats(as_of=None):
    """This install's calibration as SUFFICIENT STATISTICS — per (label, model, transport, quantity)
    only {n, p50, p90}. No prompts, no outputs, no raw calls, no $ amounts; labels ride through de-id
    like every text egress. Largest-n cells first, capped at MAX_SHARE_CELLS (logged, never silent)."""
    from . import config

    def scrub(label):
        try:
            from . import deid
            return deid.redact(label)[:120]
        except Exception:
            return (label or "")[:120]

    cells = []

    def emit(label, model, transport, quantity, vals):
        vals = [v for v in vals if v is not None]
        if len(vals) >= MIN_SHARE_OBS:
            cells.append({"label": scrub(label or ""), "model": pricing.normalize(model),
                          "transport": transport or "", "quantity": quantity, "n": len(vals),
                          "p50": round(_quantile(vals, P_LO), 6), "p90": round(_quantile(vals, P_HI), 6)})

    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        pairs = con.execute(
            "SELECT DISTINCT intent, model, kind FROM calls WHERE intent IS NOT NULL AND model IS NOT NULL "
            "AND cost > 0 AND in_tok > 0 AND out_tok > 0 AND intent NOT LIKE ?",
            (META_PREFIX + "%",)).fetchall()
    except sqlite3.OperationalError:
        pairs = []
    finally:
        con.close()
    for label, model in sorted({(la, mo) for la, mo, _k in pairs}):
        emit(label, model, "", "opi",
             [o / i for _it, _m, _k, i, o, _c, _ts in _calls_rows(label, model, None, as_of)])
        for _l, obs in _fill_obs(label, model, as_of)[:1]:            # the exact-cell fill level only
            emit(label, model, "", "fill", obs)
        for _l, obs in _in_ratio_obs(label, model, as_of)[:1]:
            emit(label, model, "", "in_ratio", obs)
    for label, model, kind in sorted(set(pairs)):
        emit(label, model, kind, "residual",
             [r for _n, rs in _residual_obs(label, model, kind, as_of)[:1] for r in rs])
    cells.sort(key=lambda c: -c["n"])
    if len(cells) > MAX_SHARE_CELLS:
        print(f"  calibrate: sharing top {MAX_SHARE_CELLS} of {len(cells)} cells (largest n first)")
        cells = cells[:MAX_SHARE_CELLS]
    return cells


def push_shared():
    """Package this install's calibration statistics and push them to the org server (visibility-gated,
    de-id'd labels, statistics only). Everyone contributes; everyone's prior improves."""
    from . import saas
    return saas.push_calibration(cell_stats())


def fetch_shared():
    """Pull the org-aggregated calibration (n-weighted across members) and cache it — estimate() then
    shrinks local stats toward the ORG's experience instead of the naive assumption."""
    from . import saas, config
    r = saas.fetch_calibration()
    cells = r.get("cells") or []
    if cells:
        (config.HOME / ORG_CACHE).write_text(json.dumps(
            {"fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "cells": cells}, indent=1))
    return {"cells": len(cells), "members": r.get("members"), **({"skipped": r["skipped"]} if "skipped" in r else {})}


# ─────────────────────────────────── surfacing ───────────────────────────────────
def summary_lines(max_lines=4):
    """Short calibration-confidence lines for advise/optimize output. Empty list if nothing learned.
    (Model-level: gate_calls keys by sig, so per-intent fill needs the model too — that pairing is
    exactly what estimate() reports per prediction.)"""
    lines = []
    try:
        from . import config
        con = sqlite3.connect(config.db_path(), timeout=10)
        q = ("SELECT model, COUNT(*), AVG(CAST(out_tok AS REAL)/max_tokens) FROM gate_calls "
             "WHERE max_tokens > 0 GROUP BY model ORDER BY 2 DESC LIMIT ?")
        for m, cnt, fill in con.execute(q, (max_lines,)).fetchall():
            if m and fill is not None:
                lines.append(f"learned: {m} fills {fill*100:.0f}% of max_tokens on average (n={cnt}) — "
                             f"`spendguard calibrate predict` uses this, the naive cap-price doesn't")
        con.close()
    except Exception:
        pass
    return lines


def show(label=None):
    """Print the calibration state — what has been learned, at what confidence."""
    from . import config
    con = sqlite3.connect(config.db_path(), timeout=10)
    try:
        try:
            cells = con.execute(
                "SELECT intent, model, kind, COUNT(*), SUM(cost) FROM calls "
                "WHERE cost > 0 AND in_tok > 0 AND out_tok > 0 AND intent IS NOT NULL "
                "AND intent NOT LIKE ? " + ("AND intent = ? " if label else "") +
                "GROUP BY 1,2,3 ORDER BY 5 DESC LIMIT 20",
                ((META_PREFIX + "%",) + ((label,) if label else ()))).fetchall()
        except sqlite3.OperationalError:
            cells = []
        print("calibration corpus — per-cell history the estimator learns from "
              "(level+n_obs are returned with every prediction)\n")
        if not cells:
            print("  nothing labeled yet — run `spendguard backfill`, or set calls.context(intent=…) in your jobs.")
        else:
            print(f"{'activity':<28}{'model':<24}{'kind':<9}{'rows':>5}{'$ hist':>10}")
            for it, m, k, n, usd in cells:
                print(f"{(it or '—')[:27]:<28}{m[:23]:<24}{k:<9}{n:>5}{usd:>10,.2f}")
    finally:
        con.close()
    pcon = _con()
    try:
        n_pred, n_paired = pcon.execute(
            "SELECT COUNT(*), SUM(paired_ts IS NOT NULL AND actual_calls > 0) FROM cost_predictions"
        ).fetchone()
    finally:
        pcon.close()
    print(f"\npredictions logged: {n_pred or 0}   paired to actuals: {n_paired or 0}   "
          f"(pair window {_pair_horizon_s()/3600:.0f}h — `spendguard calibrate pair`)")
    org = _org_cells()
    if org:
        try:
            fetched = json.loads((config.HOME / ORG_CACHE).read_text()).get("fetched_at", "?")
        except Exception:
            fetched = "?"
        print(f"org prior: {len(org)} shared cell(s) cached (fetched {fetched[:10]}) — local stats shrink toward the ORG")
    else:
        print("org prior: none cached — `spendguard calibrate fetch` pulls the org's shared experience")
    for ln in summary_lines():
        print("  " + ln)


def main(argv=None):
    import sys
    import argparse
    ap = argparse.ArgumentParser(
        prog="spendguard calibrate",
        description="learned cost estimator — predicts a job's $ from YOUR captured history, "
                    "correcting the naive tokens×price estimate (zero LLM spend)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("predict", help="estimate a planned job from history")
    pr.add_argument("--label", required=True, help="the activity/intent (matches calls.context intent)")
    pr.add_argument("--n", type=int, default=1, help="number of requests")
    pr.add_argument("--model", required=True)
    pr.add_argument("--transport", default="batch", choices=["batch", "realtime"])
    pr.add_argument("--in-tokens", type=float, help="estimated input tokens PER REQUEST")
    pr.add_argument("--out-max", type=float, help="max_tokens you will set PER REQUEST")
    pr.add_argument("--json", action="store_true")
    sh = sub.add_parser("show", help="what has been learned + at what confidence")
    sh.add_argument("--label")
    sub.add_parser("pair", help="join logged predictions to captured actuals (idempotent)")
    sub.add_parser("push", help="share this install's calibration STATISTICS with the org (visibility-gated)")
    sub.add_parser("fetch", help="pull the org aggregate — the org's experience becomes your prior")
    bt = sub.add_parser("backtest", help="held-out MAPE, learned vs naive — the ship gate")
    bt.add_argument("--json", action="store_true")
    bt.add_argument("--min-cell", type=int, default=MIN_CELL_BACKTEST)
    a = ap.parse_args(sys.argv[2:] if argv is None else argv)

    if a.cmd == "predict":
        try:
            r = estimate(a.label, n=a.n, model=a.model, transport=a.transport,
                         est_in_tokens=a.in_tokens, est_out_max=a.out_max)
        except (ValueError, KeyError) as e:
            print(f"calibrate: {e}")
            return 2
        if a.json:
            print(json.dumps(r, indent=1))
        else:
            print(f"{a.label} × {a.n:,} req on {r['model']} ({r['transport']})")
            print(f"  learned estimate: ${r['p50_usd']:,.2f} (p50) … ${r['p90_usd']:,.2f} (p90)   "
                  f"[basis={r['basis']}, level={r['level']}, n_obs={r['n_obs']}]")
            if r["naive_usd"] is not None:
                print(f"  naive (cap-filled, flat realtime price): ${r['naive_usd']:,.2f}")
            print(f"  per request p50: {r['per_request']['in_p50']:,.0f} in / "
                  f"{r['per_request']['out_p50']:,.0f} out tokens")
        return 0
    if a.cmd == "show":
        show(label=a.label)
        return 0
    if a.cmd == "pair":
        r = pair()
        print(f"calibrate pair: {r['paired']} paired · {r['expired']} expired (no actuals in window) · "
              f"{r['pending']} still open")
        return 0
    if a.cmd == "push":
        r = push_shared()
        print(f"calibrate push: {r.get('accepted', 0)} cell(s) shared" if "accepted" in r
              else f"calibrate push: {r.get('skipped', r)}")
        return 0
    if a.cmd == "fetch":
        r = fetch_shared()
        print(f"calibrate fetch: {r['cells']} org cell(s) cached"
              + (f" from {r['members']} member(s)" if r.get("members") else "")
              + (f" — {r['skipped']}" if r.get("skipped") else "")
              + ("; estimate() now shrinks toward the ORG prior" if r["cells"] else ""))
        return 0
    if a.cmd == "backtest":
        out = backtest(as_json=a.json, min_cell=a.min_cell)
        return 0 if out["overall"].get("ship") or not out["cells"] else 1
    return 2
