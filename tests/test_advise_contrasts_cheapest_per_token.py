"""advise.ranked surfaces a factual CONTRAST: the model cheapest PER TOKEN vs the $/good pick — not a quality verdict.

Addresses caller-feedback #8: a caller measured a model 63.8% wrong that looked cheapest per token, while a pricier
model was the real best value per good answer. The ranking already demotes the cheap one (its low quality drags $/good
up); ranked() now ALSO returns `cheapest_per_token` carrying both models' measured good_rate + $/good, and advise()
prints the contrast, so a reader scanning the $/M-out column also sees the per-GOOD cost. It is PURELY the measured
contrast — it never labels the cheaper model "wrong" (a 99%-good cheaper model is a fine choice; the reader judges from
the numbers). Only populated when quality is labeled and the cheapest-per-token model differs from the pick.

Offline, isolated SPENDGUARD_HOME, zero spend (calls.insert is the ungated corpus seeder).
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-cheapest-pt-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import advise, calls, budget  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()


def seed(provider, model, intent, cost, out_tok, n_good, n_bad):
    # COST goes to the LEDGER (advise's A3 cost basis); QUALITY + out_tok go to the calls corpus. Same per-call cost in
    # both, so per_m_out (ledger cost ÷ corpus out_tok) is unchanged — the ranking is identical, now ledger-grounded.
    for _ in range(n_good + n_bad):
        budget.record_charge(provider, model, "realtime", cost, intent=intent, basis=budget.BASIS_BILLED)
    for _ in range(n_good):
        calls.insert(provider, model, "realtime", cost, out_tok=out_tok, intent=intent,
                     quality="good", quality_conf=1.0, who="test")
    for _ in range(n_bad):
        calls.insert(provider, model, "realtime", cost, out_tok=out_tok, intent=intent,
                     quality="bad", quality_conf=1.0, who="test")


# ── Scenario A: cheap-per-token LOW-quality model vs pricier ACCURATE model ─────────────────────────────────────────
# nano:   $1/M out (cost 0.001, out 1000), 20% good → $/good = 0.010/2 = $0.0050
# gpt-5.5:$2/M out (cost 0.002, out 1000), 90% good → $/good = 0.020/9 ≈ $0.0022  → the PICK
INTENT = "drug-resolver"
seed("openai", "gpt-5-nano", INTENT, 0.001, 1000, n_good=2, n_bad=8)
seed("openai", "gpt-5.5", INTENT, 0.002, 1000, n_good=9, n_bad=1)

r = advise.ranked(INTENT)
ck("the $/good pick is the accurate model (gpt-5.5), not the cheapest-per-token one",
   r["pick"] == "openai:gpt-5.5", extra=f"pick={r['pick']}")
cpt = r.get("cheapest_per_token")
ck("cheapest_per_token is populated", cpt is not None)
if cpt:
    ck("it names the cheapest-per-token model (nano)", cpt["id"] == "openai:gpt-5-nano", extra=repr(cpt))
    ck("it carries nano's measured good_rate (~0.2)", abs((cpt["good_rate"] or 0) - 0.2) < 1e-6, extra=repr(cpt.get("good_rate")))
    ck("it carries nano's per-token price (~$1/M)", abs((cpt["per_m_out"] or 0) - 1.0) < 1e-6, extra=repr(cpt.get("per_m_out")))
    ck("it carries the pick's good_rate for the contrast (~0.9)", abs((cpt["pick_good_rate"] or 0) - 0.9) < 1e-6, extra=repr(cpt))
    ck("it records the pick id", cpt["pick"] == "openai:gpt-5.5", extra=repr(cpt))

# advise() renders a NEUTRAL factual contrast — no 'wrong' verdict
import io  # noqa: E402
import contextlib  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    advise.advise(intent=INTENT)
out = buf.getvalue()
ck("advise() prints the cheapest-per-token contrast", "cheapest per token" in out and "gpt-5-nano" in out, extra=out)
ck("advise() does NOT assert the cheaper model is 'wrong'", "wrong" not in out.lower(), extra=out)
ck("advise() shows the measured good% so the reader judges", "20% good" in out, extra=out)

# ── Scenario B: cheapest-per-token model IS the pick → nothing to contrast ──────────────────────────────────────────
ALIGNED = "aligned-intent"
seed("openai", "gpt-5-mini", ALIGNED, 0.001, 1000, n_good=9, n_bad=1)
seed("openai", "gpt-5.5", ALIGNED, 0.002, 1000, n_good=5, n_bad=5)
r2 = advise.ranked(ALIGNED)
ck("when the cheapest-per-token model is also the pick, the field is empty",
   r2["pick"] == "openai:gpt-5-mini" and r2.get("cheapest_per_token") is None,
   extra=f"pick={r2['pick']} cpt={r2.get('cheapest_per_token')}")

# ── Scenario C: no quality labels → no contrast (nothing to compare on) ─────────────────────────────────────────────
UNLABELED = "unlabeled-intent"
calls.insert("openai", "gpt-5-nano", "realtime", 0.001, out_tok=1000, intent=UNLABELED, who="test")
calls.insert("openai", "gpt-5.5", "realtime", 0.002, out_tok=1000, intent=UNLABELED, who="test")
r3 = advise.ranked(UNLABELED)
ck("an unlabeled intent has no cheapest_per_token contrast", r3.get("cheapest_per_token") is None, extra=repr(r3.get("cheapest_per_token")))

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_advise_contrasts_cheapest_per_token: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)
