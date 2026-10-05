"""Shared paid-overage status used by both CLI and MCP surfaces."""
import contextlib
import io
import sqlite3
import time

from . import claudecode, config


def current_overage_status():
    """Return the observable current overage state and historical real-dollar overage by month."""
    with contextlib.redirect_stdout(io.StringIO()):
        windows, _anchor = claudecode._overage_windows(claudecode._overage_events())
    now = time.time()
    con = sqlite3.connect(config.db_path())
    try:
        has_events = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spend_events'").fetchone()
        month = [] if not has_events else con.execute(
            "SELECT substr(occurred_at,1,7) mo, ROUND(SUM(CAST(realtime_usd AS REAL)),2) FROM spend_events "
            "WHERE source='anthropic-invoice' AND intent LIKE 'anthropic-invoice:cc-overage%' "
            "GROUP BY mo ORDER BY mo DESC").fetchall()
    finally:
        con.close()
    return {"on_overage_now": any(begin <= now < reset for (begin, reset) in windows),
            "meaning": "true = the weekly plan cap is hit and this account is paying per-token right now",
            "observed_overage_windows": len(windows),
            "real_overage_by_month_usd": {month_name: dollars for month_name, dollars in month[:6]}}


def overage_cli(_argv=None):
    status = current_overage_status()
    state = "YES" if status["on_overage_now"] else "no"
    print(f"paid overage now: {state} — {status['meaning']}")
    print(f"observed overage windows: {status['observed_overage_windows']}")
    if status["real_overage_by_month_usd"]:
        print("real overage $ by month: " + "  ".join(
            f"{month} ${dollars:.2f}" for month, dollars in status["real_overage_by_month_usd"].items()))
    else:
        print("real overage $ by month: none recorded")
    print("Est sub value is a separate axis and is not included in these billed overage dollars.")
    return 0
