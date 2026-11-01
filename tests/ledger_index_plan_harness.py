"""Shared EXPLAIN QUERY PLAN assertions for ledger performance and index tests."""


def ledger_plan_details(connection, sql, parameters=()):
    return tuple(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters))


def ledger_plan_searches_with(plan, index_name):
    return any("SEARCH " in detail and index_name in detail for detail in plan)
