"""saas.py pure transforms — the scrubbed /v1 payload builders extracted from the HTTP push (fetch→transform→load).
build_rollup_rows / build_guarded_rows / _project_filter / _conn_project_base take plain data + config and emit the
exact contract rows, with NO network/DB — so the filter, the kind/channel mapping, the $→micros, the contributor
stamping, and the scrubbing (only contract fields ever leave) are all unit-testable offline. Script-style."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-saas-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import saas

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── _project_filter: None = all · list/single → set · owns_account widens to the shared llmseg/unattributed ──
ck("_project_filter: no project → None (push all)", saas._project_filter({}) is None)
ck("_project_filter: projects list → lowercased set", saas._project_filter({"projects": ["LMM", "SlideKit"]}) == {"lmm", "slidekit"})
ck("_project_filter: single project", saas._project_filter({"project": "Lmm"}) == {"lmm"})
ck("_project_filter: owns_account adds the shared llmseg + unattributed",
   saas._project_filter({"project": "lmm", "owns_account": True}) == {"lmm", "llmseg", "unattributed"})
ck("_conn_project_base: does NOT widen (guarded sources aren't shared)",
   saas._conn_project_base({"project": "lmm", "owns_account": True}) == {"lmm"})

# ── build_rollup_rows: filter + kind/channel map + $→micros + contributor stamp + uid + scrub ──
raw = [
    {"day": "2026-06-22", "provider": "openai", "model": "gpt-5.5", "kind": "batch", "cost": 2.5, "calls": 3, "project": "lmm"},
    {"day": "2026-06-22", "provider": "openai", "model": "o", "kind": "realtime", "cost": 1.0, "calls": 1, "project": "lmm"},
    {"day": "2026-06-22", "provider": "anthropic", "model": "opus", "kind": "meta", "cost": 0.5, "calls": 2, "project": "llmseg"},
    {"day": "2026-06-22", "provider": "openai", "model": "x", "kind": "batch", "cost": 9.0, "calls": 1, "project": "manga2anime"},
    {"day": "2026-06-22", "provider": "openai", "model": "(provider-batch)", "kind": "batch", "cost": 7.0, "calls": 0, "project": "unattributed"},
]
rows = saas.build_rollup_rows(raw, "alice@x.test", {"lmm", "llmseg", "unattributed"})
projs = [r["project"] for r in rows]
ck("rollup: filters to the connection's projects (manga2anime dropped)", "manga2anime" not in projs and "lmm" in projs)
lmm_batch = next(r for r in rows if r["project"] == "lmm" and r["channel"] == "batch")
ck("rollup: $→micros (2.5 → 2_500_000)", lmm_batch["spend_micros"] == 2_500_000)
ck("rollup: realtime kind→workload + channel realtime", next(r for r in rows if r["model"] == "o")["channel"] == "realtime")
ck("rollup: meta kind→meta", next(r for r in rows if r["project"] == "llmseg")["kind"] == "meta")
ck("rollup: contributor stamped on workload", lmm_batch["member_ref"] == "alice@x.test")
ck("rollup: unattributed gap carries NO contributor", next(r for r in rows if r["project"] == "unattributed")["member_ref"] == "")
ck("rollup: every row has a uid == _row_uid(row)", all(r["uid"] == saas._row_uid(r) for r in rows))
allowed = {"day", "provider", "model", "kind", "channel", "spend_micros", "calls", "member_ref", "project", "uid"}
ck("rollup: SCRUBBED — only contract fields leave (no prompt/content)", all(set(r) <= allowed for r in rows))
ck("rollup: flt=None pushes everything", len(saas.build_rollup_rows(raw, "a", None)) == len(raw))

# ── build_guarded_rows: filter (empty base = all) + cumulants pass through ──
grows = [
    {"day": "2026-06-22", "project": "lmm", "source": "cache", "n": 5, "k1": 10.0, "k2": 2.0, "k3": 0.1, "k4": 0.01},
    {"day": "2026-06-22", "project": "manga2anime", "source": "block", "n": 2, "k1": 4.0, "k2": 1.0, "k3": 0.0, "k4": 0.0},
]
g = saas.build_guarded_rows(grows, {"lmm"})
ck("guarded: filters to base (manga2anime dropped)", len(g) == 1 and g[0]["project"] == "lmm")
ck("guarded: cumulants pass through", g[0]["k1"] == 10.0 and g[0]["n"] == 5)
ck("guarded: empty base → push all", len(saas.build_guarded_rows(grows, set())) == 2)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"saas_payload: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
