"""Guard: find scripts that hardcode an OpenAI price disagreeing with pricing.py.

Catches the class of bug that caused the cost surprise: a gpt-5.5 rate literal
that isn't the canonical (5.00/30.00 realtime, 2.50/15.00 batch). Run it before
trusting any script's "$ estimate", and after editing prices.

  python scripts/audit_price_constants.py        # report
  python scripts/audit_price_constants.py --ci    # exit 1 if any gpt-5.5 mispricing found
"""
import os, re, sys, glob

from .pricing import PRICING

SCRIPTS = os.getenv("SPENDGUARD_AUDIT_DIR") or os.getcwd()  # dir of code to scan for stray price literals
# Match the price pair attached SPECIFICALLY to a gpt-5.5 / gpt-5.5-pro dict key,
# e.g.  "gpt-5.5": (1.25, 10.0)  — not other models that share the line.
KEYED = re.compile(r"""["'](gpt-?5\.5(?:-pro)?)["']\s*:\s*\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)""", re.I)
ALLOWED = {
    "gpt-5.5": {(PRICING["gpt-5.5"]["in_"], PRICING["gpt-5.5"]["out"]),
                (PRICING["gpt-5.5"]["batch_in"], PRICING["gpt-5.5"]["batch_out"])},
    "gpt-5.5-pro": {(PRICING["gpt-5.5-pro"]["in_"], PRICING["gpt-5.5-pro"]["out"]),
                    (PRICING["gpt-5.5-pro"]["batch_in"], PRICING["gpt-5.5-pro"]["batch_out"])},
}

# Specific known-wrong literals that have burned us — banned in any form, any dict shape.
BANNED = [
    (re.compile(r"\(\s*15\.0+\s*,\s*75\.0+\s*\)"), "old-Opus rate (15/75) — opus-4.8 is (5.0, 25.0)"),
    (re.compile(r"\(\s*15\.0+\s*,\s*120\.0+\s*\)"), "wrong gpt-5.5-pro (15/120) — should be (30,180) rt / (15,90) batch"),
    (re.compile(r"[\"']gpt-?5\.5[\"']\s*:\s*\{[^}]*[\"']out[\"']\s*:\s*40"), "gpt-5.5 out=40 — should be 30"),
    (re.compile(r"[\"']gpt-?5\.5[\"']\s*:\s*\(\s*1\.25\s*,\s*10"), "gpt-5.5 priced as old gpt-5 (1.25/10) — should be (5,30) rt"),
]

def main():
    ci = "--ci" in sys.argv
    hits = []
    for path in sorted(glob.glob(os.path.join(SCRIPTS, "*.py"))):
        if os.path.basename(path) in ("pricing.py", "audit_price_constants.py", "reconcile_openai_spend.py"):
            continue
        for i, line in enumerate(open(path, errors="ignore"), 1):
            for m in KEYED.finditer(line):
                key = m.group(1).lower()
                pair = (float(m.group(2)), float(m.group(3)))
                if pair not in ALLOWED.get(key, set()):
                    hits.append((os.path.basename(path), i, f"{key}={pair}", line.strip()[:90]))
            for rx, msg in BANNED:
                if rx.search(line):
                    hits.append((os.path.basename(path), i, msg, line.strip()[:90]))

    if not hits:
        print("OK: no gpt-5.5 price literal disagrees with canonical pricing.py")
        return 0
    print(f"FOUND {len(hits)} gpt-5.5 price literal(s) not matching canonical pricing.py "
          f"(gpt-5.5 realtime 5.0/30.0 or batch 2.5/15.0; pro 30.0/180.0 or 15.0/90.0):\n")
    for fn, ln, pair, txt in hits:
        print(f"  {fn}:{ln}  {pair}   {txt}")
    print("\nFix: replace with `from pricing import batch_cost` or use the canonical pair.")
    return 1 if ci else 0

if __name__ == "__main__":
    sys.exit(main())
