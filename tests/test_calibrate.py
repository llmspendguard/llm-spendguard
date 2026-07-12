"""Learned cost calibration (calibrate.py) — the estimator must LEARN from the captured corpus and
BEAT the naive cap-filled/flat-price estimate, degrade honestly to naive with no data, shrink sparse
cells toward their parent, pair predictions to actuals idempotently, and pass the backtest ship gate
on a corpus with realistic error shapes (30% cap fill, batch discount, caching residual).
Offline, isolated SPENDGUARD_HOME, zero LLM spend — prices come from the REAL pricing.py table
(never hardcoded here: expectations are computed FROM price())."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-calibrate-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import datetime
import sqlite3
from spendguard import calibrate, config, pricing, bulkgate

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


MODEL = "gpt-5.5"                      # a model the canonical table always prices
P = pricing.price(MODEL)               # expectations derive from the real table — never hardcoded $
LABEL = "edge_typing"
FILL = 0.30                            # seeded truth: outputs fill 30% of the cap
CAP = 1000
IN_TOK = 2000
DAY0 = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)


def iso(day, sec=0):
    return (DAY0 + datetime.timedelta(days=day, seconds=sec)).isoformat(timespec="seconds")


# ── seed the calls corpus: 20 batch rows, out = 30% of in-cap analog, cost = batch price × 0.9 residual ──
con = sqlite3.connect(config.db_path())
con.execute("""CREATE TABLE IF NOT EXISTS calls(
    id TEXT PRIMARY KEY, ts TEXT, chain TEXT, intent TEXT, caller TEXT, provider TEXT, model TEXT, kind TEXT,
    in_tok INTEGER, out_tok INTEGER, cost REAL, latency REAL,
    prompt_hash TEXT, prompt_snip TEXT, output_snip TEXT, finish TEXT,
    quality TEXT, quality_src TEXT, quality_conf REAL)""")
RESIDUAL = 0.9                          # caching etc. lands actual $ at 90% of the table's batch price
for i in range(20):
    out = int(CAP * FILL)
    cost = pricing.batch_cost(MODEL, IN_TOK, out) * RESIDUAL
    con.execute("INSERT INTO calls (id, ts, intent, model, kind, in_tok, out_tok, cost) VALUES (?,?,?,?,?,?,?,?)",
                (f"c{i}", iso(i), LABEL, MODEL, "batch", IN_TOK, out, cost))
con.commit()
con.close()

# fill observations land via the REAL bulkgate path (out vs requested max_tokens)
SIG = bulkgate.sig(MODEL, template_id=LABEL)
for i in range(20):
    bulkgate.note_response(SIG, MODEL, int(CAP * FILL), max_tokens=CAP, finish_reason="stop")

# ── estimate on the exact cell: learned ≪ naive, correct level/confidence reported ──
r = calibrate.estimate(LABEL, n=100, model=MODEL, transport="batch",
                       est_in_tokens=IN_TOK, est_out_max=CAP)
naive_expected = 100 * (IN_TOK * P["in_"] + CAP * P["out"]) / 1e6
ck("naive baseline = cap-filled × flat realtime price (from pricing.py)",
   abs(r["naive_usd"] - round(naive_expected, 4)) < 1e-6)
ck("learned p50 is well under naive (fill 30% + batch rate + 0.9 residual learned)",
   r["p50_usd"] < 0.55 * r["naive_usd"])
approx = 100 * (IN_TOK * P["batch_in"] + CAP * FILL * P["batch_out"]) / 1e6 * RESIDUAL
ck("learned p50 lands near the seeded truth (±20% — shrinkage pulls slightly to naive)",
   abs(r["p50_usd"] - approx) / approx < 0.20)
ck("p90 ≥ p50 (a band, not a point)", r["p90_usd"] >= r["p50_usd"])
ck("basis/level/confidence surfaced", r["basis"] == "fill" and r["level"] == "exact" and r["n_obs"] >= 20)

# ── zero-history cell degrades to the naive answer, honestly labeled ──
r0 = calibrate.estimate("never_seen", n=10, model="claude-haiku-4-5", transport="realtime",
                        est_in_tokens=500, est_out_max=200)
ck("unknown label on unknown model reports its (sparse/global) evidence level, never fakes 'exact'",
   r0["level"] != "exact")

# ── as_of replay: before the seeded data existed, the exact cell has nothing learned —
# the ONLY correction left is the static transport rate (batch table price, cap fully filled) ──
r_past = calibrate.estimate(LABEL, n=1, model=MODEL, transport="batch",
                            est_in_tokens=IN_TOK, est_out_max=CAP, as_of=iso(-10))
cap_at_batch = (IN_TOK * P["batch_in"] + CAP * P["batch_out"]) / 1e6
ck("as_of replay sees no history → cap-filled at transport rates, honestly labeled prior/cap",
   r_past["level"] == "prior" and r_past["basis"] == "cap"
   and abs(r_past["p50_usd"] - round(cap_at_batch, 4)) < 1e-6)

# ── shrinkage: a 2-obs sibling label pulls toward parent evidence, not its own noise ──
con = sqlite3.connect(config.db_path())
for i in range(2):
    out = int(CAP * 0.95)              # tiny sample that happens to look cap-filling
    cost = pricing.batch_cost(MODEL, IN_TOK, out)
    con.execute("INSERT INTO calls (id, ts, intent, model, kind, in_tok, out_tok, cost) VALUES (?,?,?,?,?,?,?,?)",
                (f"s{i}", iso(25 + i), "sparse_sibling", MODEL, "batch", IN_TOK, out, cost))
con.commit()
con.close()
rs = calibrate.estimate("sparse_sibling", n=1, model=MODEL, transport="batch",
                        est_in_tokens=IN_TOK, est_out_max=CAP)
ck("sparse cell (n=2) borrows the model-level fill evidence (n=20) instead of its own noise",
   rs["level"] == "model" and rs["per_request"]["out_p50"] < 0.6 * CAP)
# the shrinkage math itself: 2 obs at 0.95 vs prior 1.0 → w=2/(2+8) → 0.99, tagged sparse
(s50, _s90), s_level, s_n = calibrate._shrink([("exact", 2, 0.95, 0.95)], (1.0, 1.0))
ck("EB shrinkage: n=2 keeps only n/(n+K) of its own signal and says so",
   abs(s50 - 0.99) < 1e-9 and s_level == "exact(sparse)" and s_n == 2)

# ── record_estimate → pair: exact join by chain=job_id, idempotent, feeds IN_RATIO ──
ok = calibrate.record_estimate("job-42", LABEL, MODEL, predicted_usd=1.23,
                               est_in=IN_TOK, est_out_max=CAP, n=4, transport="batch")
ck("record_estimate returns True (never raises)", ok is True)
con = sqlite3.connect(config.db_path())
for i in range(4):                      # the run's actuals, captured by the gate with chain=job_id
    con.execute("INSERT INTO calls (id, ts, chain, intent, model, kind, in_tok, out_tok, cost) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"j{i}", iso(30, i), "job-42", LABEL, MODEL, "batch", int(IN_TOK * 1.1),
                 int(CAP * FILL), pricing.batch_cost(MODEL, int(IN_TOK * 1.1), int(CAP * FILL))))
con.commit()
con.close()
pr = calibrate.pair(now=(DAY0 + datetime.timedelta(days=31)).timestamp())
ck("prediction paired to its 4 chain-matched calls", pr["paired"] == 1)
pcon = sqlite3.connect(str(config.db_path()))
arow = pcon.execute("SELECT actual_calls, actual_in FROM cost_predictions WHERE job_id='job-42'").fetchone()
pcon.close()
ck("actuals aggregated onto the prediction", arow == (4, int(IN_TOK * 1.1) * 4))
pr2 = calibrate.pair(now=(DAY0 + datetime.timedelta(days=32)).timestamp())
ck("re-pair is a no-op (idempotent)", pr2["paired"] == 0)
obs = calibrate._in_ratio_obs(LABEL, MODEL)
ck("paired prediction feeds IN_RATIO calibration (actual 1.1× the caller's estimate)",
   obs[0][1] and abs(obs[0][1][0] - 1.1) < 0.01)

# ── expired prediction: closed window with no actuals stays visible as UNKNOWN, never $0-clean ──
calibrate.record_estimate("job-ghost", "never_ran", MODEL, predicted_usd=9.99, est_in=100, est_out_max=50)
import time as _time
pr3 = calibrate.pair(now=_time.time() + calibrate._pair_horizon_s() + 60)   # ts stamps wall-clock now
ck("closed window with no actuals marks expired (not paired, not silently dropped)", pr3["expired"] >= 1)

# ── backtest ship gate: learned beats naive on this corpus, honest table printed ──
bt = calibrate.backtest(as_json=True, min_cell=8)
ck("backtest covers the seeded cell", bt["overall"]["cells"] >= 1 and bt["overall"]["test_rows"] >= 3)
ck("SHIP GATE: calibrated MAPE beats naive on held-out rows",
   bt["overall"]["ship"] and bt["overall"]["calibrated_mape"] < bt["overall"]["naive_mape"])

# ── wiring: CLI dispatch, report auto-pair, advise surfacing, estimate.py --label hook ──
import inspect
import importlib
from spendguard import cli, report, advise
naive_est = importlib.import_module("spendguard.estimate")   # __init__ exports estimate() the function
ck("CLI wired: `spendguard calibrate`", '"calibrate"' in inspect.getsource(cli.main))
ck("report auto-pairs predictions each run", "calibrate" in inspect.getsource(report._run))
ck("advise surfaces calibration confidence", "summary_lines" in inspect.getsource(advise.advise))
ck("naive `spendguard estimate --label` prints the learned correction",
   "--label" in inspect.getsource(naive_est.main) and "calibrate" in inspect.getsource(naive_est.main))
# the CLI dispatch must reach the SUBMODULE's main — __init__ re-exports pricing.estimate (a function)
# that shadows `from . import estimate`, which broke `spendguard estimate` silently (found 2026-07-11)
import io
import contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli.main(["estimate", "--items", "10", "--prefix-tok", "1", "--in-per-item", "1",
                   "--out-per-item", "1", "--models", MODEL, "--packs", "1"])
ck("`spendguard estimate` CLI dispatch actually runs (shadowed-submodule regression guard)",
   rc in (0, None) and "CHEAPEST" in buf.getvalue())

# ── ORG-SHARED learning: stats package up, aggregate prior down, local stats on top ──
stats = calibrate.cell_stats()
ck("cell_stats packages the seeded cells (statistics only)",
   any(c["label"] == LABEL and c["quantity"] == "fill" and c["n"] >= 20 for c in stats))
ALLOWED_KEYS = {"label", "model", "transport", "quantity", "n", "p50", "p90"}
ck("nothing but sufficient statistics leaves — no prompts/outputs/$ in the payload",
   stats and all(set(c) <= ALLOWED_KEYS for c in stats)
   and all(isinstance(c[k], (int, float)) for c in stats for k in ("n", "p50", "p90")))

from spendguard import saas
sent = {}
saas.push_calibration = lambda cells=None: sent.update(n=len(cells or [])) or {"accepted": len(cells or [])}
r_push = calibrate.push_shared()
ck("push_shared sends the packaged cells through the one saas door", r_push["accepted"] == sent["n"] > 0)

# an ORG that fills only 20% of caps on this model — a label THIS machine has never seen
ORG_LABEL = "org_only_summarize"
saas.fetch_calibration = lambda: {"members": 3, "cells": [
    {"label": ORG_LABEL, "model": MODEL, "transport": "", "quantity": "fill", "n": 500, "p50": 0.20, "p90": 0.35},
    {"label": ORG_LABEL, "model": MODEL, "transport": "batch", "quantity": "residual", "n": 400, "p50": 0.95, "p90": 1.0},
]}
r_fetch = calibrate.fetch_shared()
ck("fetch_shared caches the org aggregate", r_fetch["cells"] == 2 and (config.HOME / calibrate.ORG_CACHE).exists())
r_org = calibrate.estimate(ORG_LABEL, n=10, model=MODEL, transport="batch",
                           est_in_tokens=IN_TOK, est_out_max=CAP)
ck("a label unseen locally uses the ORG prior (level=org, its n reported)",
   r_org["level"] == "org" and r_org["n_obs"] == 500 and r_org["basis"] == "fill")
ck("org prior corrects the estimate (fill 20% ≪ cap-filled naive)", r_org["p50_usd"] < 0.5 * r_org["naive_usd"])
r_local = calibrate.estimate(LABEL, n=100, model=MODEL, transport="batch",
                             est_in_tokens=IN_TOK, est_out_max=CAP)
ck("a locally-rich cell KEEPS its local evidence on top of the org prior (level stays exact)",
   r_local["level"] == "exact" and abs(r_local["p50_usd"] - r["p50_usd"]) / r["p50_usd"] < 0.15)
ck("report wires the org loop (push + fetch ride the daily report)",
   "push_shared" in inspect.getsource(report._run) and "fetch_shared" in inspect.getsource(report._run))

# ── rails: pricing only via pricing.py — no $/token literals in the module ──
src = inspect.getsource(calibrate)
ck("no hardcoded prices in calibrate.py (rates only via pricing.price/_cost)",
   "1e6" in src and not any(tok in src for tok in ("2.50", "15.00", "$/1M", "PRICE =")))

print(("[OK]" if not fails else "[FAIL]") + " calibrate: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
