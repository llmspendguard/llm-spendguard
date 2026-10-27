"""Offline metadata-only schema checks after queue and gate migrations complete."""
import sqlite3

from spendguard import bulkgate, lane_queue

SCOPED_SCHEMA_REPEAT_COUNT = 5
SCOPED_SCHEMA_POPULATED_COUNT = 12


class SchemaMigrationAttemptCounter:
    def __init__(self, connection):
        self.connection = connection
        self.alter_attempts = []

    def execute(self, sql, *args):
        if sql.startswith("ALTER TABLE"):
            self.alter_attempts.append(sql)
        return self.connection.execute(sql, *args)

    def commit(self):
        return self.connection.commit()


def prove_scoped_queue_gate_metadata_guards():
    for ensure_schema in (lane_queue._ensure_queue_schema, bulkgate._ensure_gate_schema):
        with sqlite3.connect(":memory:") as connection:
            ensure_schema(connection)
            if ensure_schema is lane_queue._ensure_queue_schema:
                connection.executemany(
                    "INSERT INTO lane_queue(intent,task,result_migrated) VALUES(?,?,1)",
                    ((str(index), str(index)) for index in range(SCOPED_SCHEMA_POPULATED_COUNT)),
                )
            counter = SchemaMigrationAttemptCounter(connection)
            for _ in range(SCOPED_SCHEMA_REPEAT_COUNT):
                ensure_schema(counter)
            assert not counter.alter_attempts, counter.alter_attempts
            if ensure_schema is lane_queue._ensure_queue_schema:
                migration_plan = connection.execute(
                    "EXPLAIN QUERY PLAN SELECT id,result FROM lane_queue WHERE result_migrated=0"
                ).fetchall()
                assert all("SCAN lane_queue" not in row[3] for row in migration_plan), migration_plan
                connection.execute("INSERT INTO lane_queue(intent,task,result) VALUES(?,?,?)",
                                   ("late-legacy", "legacy-task", '{}'))
                ensure_schema(connection)
                assert connection.execute(
                    "SELECT result_migrated FROM lane_queue WHERE intent=?", ("late-legacy",)
                ).fetchone()[0] == 1


if __name__ == "__main__":
    prove_scoped_queue_gate_metadata_guards()
