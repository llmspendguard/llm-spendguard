"""The SaaS roll-up payload must match the server's /v1/ledger contract, stamp the contributor (member_ref) +
project, and only push rows for the project(s) this connection owns. Pure/offline — no network, no spend."""
from spendguard import saas, budget


def test_rollup_maps_stamps_and_filters_by_project(monkeypatch):
    raw = [
        dict(day="2026-06-15", provider="openai", model="gpt-5.5", kind="batch", project="nlp-pipeline", cost=2.5, calls=4),
        dict(day="2026-06-15", provider="anthropic", model="claude-opus-4-8", kind="realtime", project="nlp-pipeline", cost=1.0, calls=2),
        dict(day="2026-06-14", provider="openai", model="gpt-5.5", kind="meta", project="llm-spendguard", cost=0.10, calls=1),
        dict(day="2026-06-14", provider=None, model=None, kind=None, project="vision-pipeline", cost=0.000001, calls=1),
    ]
    monkeypatch.setattr(budget, "by_dims", lambda since=None: raw)
    # owns project=nlp-pipeline AND the account → nlp-pipeline rows + llm-spendguard (spendguard's own meta rides
    # along for the account owner in legacy static mode); vision-pipeline excluded
    monkeypatch.setattr(saas, "saas_connection",
                        lambda: {"contributor": "dev@example.com", "project": "nlp-pipeline", "owns_account": True})

    rows = saas._rollup_rows()
    assert {r["project"] for r in rows} == {"nlp-pipeline", "llm-spendguard"}     # not vision-pipeline
    assert all(r["member_ref"] == "dev@example.com" for r in rows)
    by = {(r["project"], r["model"], r["channel"]): r for r in rows}
    assert by[("nlp-pipeline", "gpt-5.5", "batch")]["spend_micros"] == 2_500_000 and by[("nlp-pipeline", "gpt-5.5", "batch")]["calls"] == 4
    assert by[("nlp-pipeline", "claude-opus-4-8", "realtime")]["kind"] == "workload"
    assert by[("llm-spendguard", "gpt-5.5", "batch")]["kind"] == "meta"   # spendguard's own meta rode along
    need = {"day", "provider", "model", "kind", "channel", "spend_micros", "calls", "member_ref", "project"}
    assert all(need <= set(r) for r in rows)


def test_rollup_no_filter_sends_all_projects(monkeypatch):
    raw = [
        dict(day="2026-06-15", provider="openai", model="m", kind="batch", project="nlp-pipeline", cost=1.0, calls=1),
        dict(day="2026-06-15", provider="openai", model="m", kind="meta", project="llm-spendguard", cost=0.5, calls=1),
    ]
    monkeypatch.setattr(budget, "by_dims", lambda since=None: raw)
    monkeypatch.setattr(saas, "saas_connection",lambda: {"contributor": "x@y.com"})   # no project set → push everything
    rows = saas._rollup_rows()
    assert len(rows) == 2 and {r["project"] for r in rows} == {"nlp-pipeline", "llm-spendguard"}


def test_contributor_falls_back(monkeypatch):
    monkeypatch.delenv("SPENDGUARD_CONTRIBUTOR", raising=False)
    monkeypatch.setattr(saas, "saas_connection",lambda: {})           # no configured contributor
    monkeypatch.setattr("subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError()))  # no git
    ref = saas.contributor()
    assert ref and ref == ref.lower() and len(ref) <= 128          # never empty, normalized, bounded
    assert ref == saas.config.machine_id()                         # stable persisted usr_<hex> id (no user@host leak)


if __name__ == "__main__":
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from _pytest_fixture_bridge import run_pytest_style
    _sys.exit(run_pytest_style(globals()))
