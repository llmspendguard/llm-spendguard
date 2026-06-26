"""Unified attribution resolver (conv.resolve) — the ONE engine for batch · realtime · remote.

Step-1 wiring tests (deterministic — they READ a recorded agentic decision; the LLM classification itself is tested in
attribute_segments). Proves all three cost paths resolve via the SAME engine, and that an unclassified segment falls to
the cwd PRIOR (never a regex guess, never blanket 'unattributed').
"""
from spendguard import conv

_SEG = {"seg_id": "S1", "sid": "conv1", "cwd": "/Users/x/Documents/claude/lmm", "project_prior": "lmm",
        "prompt": "classify lmm bc_edges", "batch_ids": ["batch_6a1234567890"], "ts": "2026-06-01T00:00:00Z"}


def _patch(monkeypatch, segs, store):
    monkeypatch.setattr(conv, "segments", lambda tdir=None: segs)
    monkeypatch.setattr(conv, "_seg_get_all", lambda: store)


def test_resolve_unifies_batch_realtime_remote(monkeypatch):
    # ONE recorded agentic determination for the lmm segment
    _patch(monkeypatch, [_SEG], {"S1": {"org": "Healiom", "team": "lmm", "project": "lmm",
                                        "confidence": 95, "source": "llm", "model": "x"}})
    batch = conv.resolve({"batch_id": "batch_6a1234567890"})
    realtime = conv.resolve({"conv_id": "conv1", "cwd": "/Users/x/Documents/claude/lmm", "script": "loinc_stem_pass.py"})
    remote = conv.resolve({"conv_id": "conv1", "cwd": "/Users/x/Documents/claude/lmm", "host": "vast-123"})
    # all three → the SAME determination via the SAME resolver (the unification)
    for r in (batch, realtime, remote):
        assert (r["org"], r["team"], r["project"]) == ("healiom", "lmm", "lmm")   # taxonomy names lowercased (case-insensitive)
        assert r["seg_id"] == "S1" and r["source"] == "llm"
    assert batch["how"] == "batch-map"            # mechanical id match
    assert realtime["how"] == "segment-cwd" and remote["how"] == "segment-cwd"


def test_resolve_unclassified_uses_cwd_prior_not_guess(monkeypatch):
    seg = {**_SEG, "seg_id": "S2", "sid": "conv2", "batch_ids": []}
    _patch(monkeypatch, [seg], {})                # segment not classified yet
    r = conv.resolve({"conv_id": "conv2", "cwd": "/Users/x/Documents/claude/lmm"})   # classify_on_miss=False → no LLM
    assert r["source"] == "prior" and r["project"] == "lmm" and r["how"] == "cwd-prior"
    assert r["evidenced"] is True                 # NEVER unattributed for evidenced spend


def test_resolve_picks_right_segment_within_a_multi_project_session(monkeypatch):
    # one session, two segments in different repos → each event resolves to its OWN segment's determination
    lmm = {**_SEG, "seg_id": "A", "cwd": "/x/lmm", "project_prior": "lmm", "batch_ids": ["batch_aaaaaa111111"]}
    m2a = {**_SEG, "seg_id": "B", "cwd": "/x/manga2anime", "project_prior": "manga2anime",
           "batch_ids": ["batch_bbbbbb222222"], "ts": "2026-06-01T01:00:00Z"}
    _patch(monkeypatch, [lmm, m2a], {
        "A": {"org": "Healiom", "team": "lmm", "project": "lmm", "confidence": 95, "source": "llm"},
        "B": {"org": "Ensight", "team": "manga2anime", "project": "manga2anime", "confidence": 95, "source": "llm"}})
    assert conv.resolve({"conv_id": "conv1", "cwd": "/x/lmm"})["org"] == "healiom"
    assert conv.resolve({"conv_id": "conv1", "cwd": "/x/manga2anime"})["org"] == "ensight"
    assert conv.resolve({"batch_id": "batch_bbbbbb222222"})["project"] == "manga2anime"


def test_resolve_unmatched_is_none_not_misattributed(monkeypatch):
    _patch(monkeypatch, [_SEG], {})
    monkeypatch.setattr(conv, "session_classification", lambda sid: None)
    r = conv.resolve({"conv_id": "unknown", "cwd": "/nowhere"})
    assert r["source"] == "none" and r["org"] == "" and r["evidenced"] is False


def test_guard_reconstruction_feeders_use_resolve_not_session_classification():
    """GUARD (anti-amnesia): the realtime/remote/GPU feeders must attribute via the unified resolve(), NEVER the coarse
    session_classification — the exact bug that mis-attributed lmm -> llm-spendguard. resolve() may use it as a
    last-resort fallback inside conv.py; the spend feeders in resources.py may not call it directly."""
    from spendguard import resources
    src = open(resources.__file__).read()
    assert "session_classification(" not in src, \
        "a reconstruction feeder calls session_classification() directly — route attribution through conv.resolve()"


def test_resolve_cwd_prior_maps_repo_to_org_not_untagged(monkeypatch):
    """GUARD (Bug B): an unclassified segment's cwd-prior must carry org+team via the taxonomy — NOT leave org blank,
    which surfaced real lmm runs as false '(untagged)'. The repo prior 'lmm' → org 'healiom'."""
    seg = {**_SEG, "seg_id": "S9", "sid": "conv9", "project_prior": "lmm", "batch_ids": []}
    _patch(monkeypatch, [seg], {})                              # segment matched but not LLM-classified → cwd-prior path
    monkeypatch.setattr(conv, "_prior_index", lambda: {"lmm": ("healiom", "lmm")})   # taxonomy-derived repo→org
    r = conv.resolve({"conv_id": "conv9", "cwd": "/x/lmm"})
    assert r["how"] == "cwd-prior" and r["source"] == "prior"
    assert r["org"] == "healiom" and r["team"] == "lmm" and r["project"] == "lmm"   # org no longer blank
    assert r["evidenced"] is True


def test_self_analysis_session_detected_for_contam_exclusion(monkeypatch):
    """GUARD (self-contamination): a session whose work IS spendguard (project 'llm-spendguard') must be flagged as a
    self-analysis session, so its realtime tells are treated as ECHOES, not independent evidence — the exact bug where
    lmm's printed $ discussed in a spendguard session got booked as ensight spend. A real workload session is NOT flagged
    (it remains valid evidence)."""
    sg = {**_SEG, "seg_id": "SG", "sid": "sgconv", "cwd": "/x/llm-spendguard", "project_prior": "llm-spendguard"}
    lmm = {**_SEG, "seg_id": "LM", "sid": "lmconv", "cwd": "/x/lmm", "project_prior": "lmm"}
    _patch(monkeypatch, [sg, lmm], {
        "SG": {"org": "Ensight", "team": "llm-spendguard", "project": "llm-spendguard", "confidence": 95, "source": "llm"},
        "LM": {"org": "Healiom", "team": "lmm", "project": "lmm", "confidence": 95, "source": "llm"}})
    assert conv._is_spendguard_session("sgconv") is True       # spendguard's own analysis → echoes, exclude as evidence
    assert conv._is_spendguard_session("lmconv") is False      # a real workload session → valid evidence, keep
    assert conv._is_spendguard_session("") is False            # no session → not self-analysis
