"""Enable `python -m spendguard …` — identical to the `spendguard` console script, but works even where the
console script isn't on PATH (e.g. provisioning an ephemeral GPU box: `python3 -m spendguard install-hook …`)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
