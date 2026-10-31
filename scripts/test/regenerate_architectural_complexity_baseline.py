"""Regenerate the architectural complexity baseline for the running Python minor."""

import argparse
from pathlib import Path

from spendguard.architectural_fitness import regenerate_architectural_complexity_baseline


def parse_architectural_regeneration_arguments():
    """Collect the required human review narrative for a measured regeneration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--justification", required=True)
    parser.add_argument("--justification-key", required=True)
    return parser.parse_args()


def main():
    arguments = parse_architectural_regeneration_arguments()
    repository_root = Path(__file__).resolve().parents[2]
    regeneration = regenerate_architectural_complexity_baseline(
        repository_root,
        justification=arguments.justification,
        justification_key=arguments.justification_key,
    )
    if regeneration["updated_files"]:
        print(
            f"Python complexity regressions recorded under {regeneration['justification_key']}: "
            f"{', '.join(regeneration['updated_files'])}; extremes {regeneration['measured_extremes']}"
        )
    else:
        print("Python complexity baseline: nothing to update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
