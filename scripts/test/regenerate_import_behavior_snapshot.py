"""Regenerate the import-time behavioral-equivalence golden snapshot."""

from pathlib import Path

from spendguard.import_behavior_snapshot import regenerate_import_behavior_snapshot


def main():
    repository_root = Path(__file__).resolve().parents[2]
    regenerated_entries = regenerate_import_behavior_snapshot(repository_root)
    print(f"Import behavior snapshot regenerated for {len(regenerated_entries)} entry points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
