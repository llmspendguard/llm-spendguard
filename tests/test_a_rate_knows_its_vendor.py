"""A rate that cannot be attributed to a vendor cannot be billed to one either.

WHY THIS GUARD EXISTS. Two vendors can publish the same model id at different money, and this table is what
every dollar in the ledger is computed from. Three places used to lose the vendor:

  1. THE LOADER flattened `providers[vendor][models][id]` to `prices[id]`, so when two vendors hosted the
     same id the LAST one loaded silently overwrote the first — both its rate and its attribution. Dict
     iteration order decided whose money was used. It also made _vendor_qualified's ambiguity guard (which
     RAISES rather than pick between disagreeing vendors) permanently unreachable: the collision was
     resolved before anything could see it.

  2. price() reached a bare `PRICING[model]` before consulting the provider — including on the path that
     had JUST parsed the vendor out of a `vendor:model` argument and thrown it away. MEASURED at the time
     of the fix: 17 model ids resolved to another vendor's rate, the worst under-priced 16.7x
     (azure:gpt-4o-mini-audio-preview-2024-12-17 billed at $0.15 against Azure's published $2.50).

  3. THE FALLBACK TABLE inferred its vendors from the model name:
         {m: ("anthropic" if m.startswith("claude") else "openai") for m in _FALLBACK}
     correct for all 23 entries that existed, and a trap for the 24th — an `else` branch that can only ever
     name one vendor, in the table the whole ledger prices from. The first Moonshot or z.ai fallback added
     would have been attributed to OpenAI, silently. An inference that happens to be right is still an
     inference.

None of these fail loudly. Every one produces a plausible number with a wrong vendor behind it.
"""
import sys

from spendguard import model_catalog
from spendguard import pricing as p

failures = []


def check(outcomes, label, ok, extra=""):
    if not ok:
        outcomes.append(label)
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


# Every authored record declares its vendor; every projected rate retains that exact declaration.
catalog_records = model_catalog.all_records()
vendor_guard_catalog_rates = model_catalog.as_price_table()
missing = [mid for mid in catalog_records if not model_catalog.model_record(mid).get("provider")]
check(failures, "every catalog record declares its vendor", bool(catalog_records) and not missing, str(missing))
contradicted = []
for provider, rates in vendor_guard_catalog_rates.items():
    for mid, rate in rates.items():
        record = model_catalog.model_record(mid)
        if not record or record.get("provider") != provider:
            contradicted.append((provider, mid, record))
        check(failures, f"catalog vendor-qualified rate resolves: {provider}:{mid}",
              p._rate_key(p.price(f"{provider}:{mid}")) == p._rate_key(rate))
check(failures, "every projected rate retains its catalog vendor",
      bool(vendor_guard_catalog_rates) and not contradicted, str(contradicted))

# 3. A NAMED VENDOR IS ANSWERED BY THAT VENDOR. Built from the live table, so this keeps testing the real
#    collision set as the price data changes rather than pinning today's example.
bare = {k: v for k, v in p.PRICING.items() if "/" not in k}
disagreeing = []
for k, v in p.PRICING.items():
    if k.count("/") != 1:
        continue
    prov, m = k.split("/")
    if m in bare and p._rate_key(bare[m]) != p._rate_key(v):
        disagreeing.append((prov, m, v))
print(f"       ({len(disagreeing)} ids where a vendor's rate differs from the bare entry — the blast radius)")
wrong = [(prov, m) for prov, m, v in disagreeing
         if p._rate_key(p.price(f"{prov}:{m}")) != p._rate_key(v)]
check(failures, "`vendor:model` is priced at THAT vendor's published rate, never a bare-name entry",
      not wrong, f"{len(wrong)} mispriced, e.g. {wrong[:3]}")

# 4. AN AMBIGUOUS BARE ID HAS NO BARE ANSWER. Not a silently-chosen one, and not an empty result either —
#    the ambiguity is recorded, because "no such model" and "several, name one" are different answers.
for m in p.AMBIGUOUS_BARE:
    check(failures, f"ambiguous id {m!r} has no bare entry to pick from", m not in p.PRICING)
if not p.AMBIGUOUS_BARE:
    print("       (no bare id is currently ambiguous in the loaded table — nothing to assert)")

# 5. THE AMBIGUITY GUARD IS REACHABLE. The loader made it unreachable for years by resolving collisions
#    first; a guard that cannot fire is not a guard.
probe_id = "a-model-two-vendors-price-differently"
saved = dict(p.PRICING)
try:
    p.PRICING[f"vendor-a/{probe_id}"] = dict(in_=1.0, out=2.0, batch_in=0.5, batch_out=1.0)
    p.PRICING[f"vendor-b/{probe_id}"] = dict(in_=9.0, out=9.0, batch_in=4.5, batch_out=4.5)
    # The property with teeth is that it REFUSED, not how the refusal is worded — asserting the message
    # text would just pin today's English. Silently picking one of two vendors' rates is the bug.
    try:
        got = p.price(probe_id)
        check(failures, "two vendors disagreeing on a bare id RAISES rather than picking one", False,
              f"picked in_=${got.get('in_')} from one of the two without saying which")
    except KeyError:
        check(failures, "two vendors disagreeing on a bare id RAISES rather than picking one", True)
    check(failures, "...and naming the vendor still resolves cleanly",
          p.price(f"vendor-b:{probe_id}")["in_"] == 9.0)
finally:
    p.PRICING.clear()
    p.PRICING.update(saved)

print(f"\n{'[FAIL]' if failures else 'OK'} test_a_rate_knows_its_vendor: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
