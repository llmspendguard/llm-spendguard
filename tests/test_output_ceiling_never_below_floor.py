"""THE FLOOR GATE — an output CEILING is NEVER below OUTPUT_FLOOR unless the model AUTHORITATIVELY publishes a lower max.

The ceiling is not the cost: max_output is billed on ACTUAL tokens, so a high ceiling is free and the ONLY failure mode
is a ceiling set too LOW — which truncates a real answer. This has recurred and cost real money (kimi-k3 auto-healed to
2000 and truncated a large reply; glm-5.3 to 13443). The fix is that NO non-authoritative source — a poison-prone learned
fact, a caller value, a per-class recommend — may pull the ceiling below the floor; only a genuinely-published (or
live-catalog) maximum may be lower, because there the model truly cannot produce more.

This gate makes that 100%: it sweeps EVERY priced model and asserts the resolved ceiling clears the floor unless the model
publishes a lower max. A regression (a new poison path, a caller value reaching the ceiling) fails HERE, at the suite,
before it can truncate anything in production. Offline (stubs the network catalog tier; pure resolver arithmetic)."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-floorgate-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import pricing, catalog, adapters  # noqa: E402

FLOOR = pricing.OUTPUT_FLOOR
BACKSTOP = adapters.MAX_TOKEN_CEILING

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# The catalog tier hits the network for a live /models ceiling; stub it OFF so the gate is a pure, offline test of the
# resolver's floor logic (published cache + learned fact + backstop). A live ceiling only ever RAISES authority, never
# lowers below the floor, so stubbing it out is the conservative choice for the gate.
catalog.model_ceiling = lambda vendor, rid: None

print("-- (1) the DEFAULT floors a poison-low learned fact (the kimi-k3 2000 incident, hermetic) --")
_orig_pub, _orig_fact = pricing.max_output_tokens, pricing.max_output
try:
    pricing.max_output_tokens = lambda m: None          # no published ceiling
    pricing.max_output = lambda m: 2000                 # a poisoned auto-heal fact, well below the floor
    ck("DEFAULT learned_floor → poison 2000 raised to the floor (never truncates below it)",
       pricing.output_ceiling("moonshot", "poison-model", BACKSTOP) == FLOOR)
    ck("explicit learned_floor=0 still exposes the raw fact (the escape hatch is intact)",
       pricing.output_ceiling("moonshot", "poison-model", BACKSTOP, learned_floor=0) == 2000)
    pricing.max_output = lambda m: None                 # nothing known at all
    ck("nothing-known → the backstop, itself never below the floor",
       pricing.output_ceiling("x", "unknown-model", BACKSTOP) >= FLOOR)
    pricing.max_output_tokens = lambda m: 8192          # a GENUINELY-published low ceiling
    ck("an AUTHORITATIVE published max below the floor is honoured as-is (the model truly can't do more)",
       pricing.output_ceiling("x", "small-model", BACKSTOP) == 8192)
finally:
    pricing.max_output_tokens, pricing.max_output = _orig_pub, _orig_fact

print("-- (2) the two models that actually truncated in production now clear the floor --")
for prov, m in [("moonshot", "kimi-k3"), ("zai", "glm-5.3")]:
    ck(f"{prov}:{m} ceiling ≥ floor (poison fact can no longer truncate it)",
       pricing.output_ceiling(prov, m, BACKSTOP) >= FLOOR)

print("-- (3) THE SWEEP: EVERY priced model clears the floor, unless it AUTHORITATIVELY publishes a lower max --")
models = sorted(pricing.PRICING.keys())
ck("there are priced models to sweep (the gate is not vacuous)", len(models) > 0)
below = []
for m in models:
    oc = pricing.output_ceiling(None, m, BACKSTOP)
    pub = pricing.max_output_tokens(m)
    # A ceiling below the floor is ONLY acceptable when the model authoritatively PUBLISHES a max that low.
    if oc < FLOOR and not (pub is not None and int(pub) == oc and int(pub) < FLOOR):
        below.append((m, oc, pub))
ck("NO priced model resolves below the floor via a non-authoritative path",
   not below)
if below:
    for m, oc, pub in below[:20]:
        print(f"      ✗ {m}: ceiling={oc} published={pub}  (below {FLOOR} with no authoritative published max)")

print(f"\n{'[FAIL]' if fails else 'OK'} test_output_ceiling_never_below_floor: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
