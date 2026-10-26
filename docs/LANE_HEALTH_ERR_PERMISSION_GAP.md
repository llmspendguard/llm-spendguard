# `lane_health.err` PermissionError — named runtime evidence gap

Static inspection establishes that `lane_health` is a SQLite table created by
`reliability._health_db()` through `budget._ledger_db()`. The database location is derived from
`SPENDGUARD_HOME`; there is no source path, literal, or writer for a `lane_health.err` sidecar in this repository.

The sandbox reproduced an independent database-permission symptom when `spendguard doctor` inspected the default
home: its consumer/registry audit reported `OperationalError: unable to open database file`. The classifier does not
construct or write filesystem paths, so this is not the same root cause as failure-kind attribution.

The exact `lane_health.err` producer and failing operation remain unprovable without the original runtime traceback
and resolved path. A safe source fix would otherwise be guesswork. Reproduce with those two facts while preserving
the affected path's owner, mode, and parent-directory permissions; all automated proof runs must continue to use an
isolated `SPENDGUARD_HOME` rather than the real databases.
