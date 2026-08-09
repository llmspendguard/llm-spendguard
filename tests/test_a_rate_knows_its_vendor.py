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

from spendguard import pricing as p

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


# 1. THE DATA CARRIES ITS OWN VENDOR — no entry may rely on being inferred.
missing = [m for m, r in p._FALLBACK.items() if not r.get("provider")]
check("every built-in rate declares its vendor", not missing, f"undeclared: {missing}")

# 2. AND THE DECLARATION IS CONFIRMED BY THE AUTHORITATIVE TABLE, not by a name rule. The breadth layer
#    keys models as `vendor/model`; ask it rather than reading the id. This is the check that would catch a
#    mis-stamped entry — including one stamped by the very prefix rule this file exists to have removed.
contradicted, unconfirmable = [], []
for m, r in p._FALLBACK.items():
    hosts = {k.split("/")[0] for k in p.PRICING if k.count("/") == 1 and k.split("/", 1)[1] == m}
    if not hosts:
        unconfirmable.append(m)
    elif r["provider"] not in hosts:
        contradicted.append((m, r["provider"], sorted(hosts)))
check("no declared vendor is contradicted by the vendor-qualified table",
      not contradicted, str(contradicted[:4]))
print(f"       ({len(p._FALLBACK) - len(unconfirmable)}/{len(p._FALLBACK)} confirmed against the "
      f"vendor-qualified table; {len(unconfirmable)} had no qualified entry to check against)")

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
check("`vendor:model` is priced at THAT vendor's published rate, never a bare-name entry",
      not wrong, f"{len(wrong)} mispriced, e.g. {wrong[:3]}")

# 4. AN AMBIGUOUS BARE ID HAS NO BARE ANSWER. Not a silently-chosen one, and not an empty result either —
#    the ambiguity is recorded, because "no such model" and "several, name one" are different answers.
for m in list(p.AMBIGUOUS_BARE)[:5]:
    check(f"ambiguous id {m!r} has no bare entry to pick from", m not in p.PRICING)
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
        check("two vendors disagreeing on a bare id RAISES rather than picking one", False,
              f"picked in_=${got.get('in_')} from one of the two without saying which")
    except KeyError:
        check("two vendors disagreeing on a bare id RAISES rather than picking one", True)
    check("...and naming the vendor still resolves cleanly",
          p.price(f"vendor-b:{probe_id}")["in_"] == 9.0)
finally:
    p.PRICING.clear()
    p.PRICING.update(saved)

print(f"\n{'[FAIL]' if failures else 'OK'} test_a_rate_knows_its_vendor: {failures} failure(s)")
sys.exit(1 if failures else 0)
