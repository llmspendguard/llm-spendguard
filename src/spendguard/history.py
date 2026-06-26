"""Layer 2 — post-event mining + graph enrichment (DETERMINISTIC, zero spend).

The backfill seeds `run` nodes from billed batches, but the *meaning* (which job an batch was) and
the *causal structure* live OUTSIDE the ledger — in the repo's artifacts, git history, and the
conversation. This module mines those to reconstruct what the live recorder couldn't:

  reconstruct_intents(repo)  scan <repo>/**/*batch_id*.json artifacts → {batch_id: intent} (dir/stem,
                             refined by per-record job_type). Recovers the dimension the advisor reasons
                             over. report by default; apply=True writes intent onto calls + run nodes.
  enrich_graph()             add causal/temporal edges to the learning graph: `preceded` (runs of an
                             intent in time order) + `derived_from` (mined insight → the runs it cites).
  mine_git(repo)             walk git history of the batch-id artifacts → cost/fix/revert signals as
                             weak quality hints + script_version nodes.

All zero-spend. The LLM-judged reconstruction (ambiguous conversation/outcome) lives in advisor.py and
is caged by caps.meta. CLI: `spendguard mine-history {intents,graph,git} [--repo PATH] [--apply]`.
"""
import os, re, json, glob, subprocess
from . import calls, learn


# ─────────────────────────── intent reconstruction ───────────────────────────
def _clean_stem(stem):
    s = re.sub(r"_?batch_?ids?_?", "_", stem, flags=re.I).strip("_")
    return s or None


def _intent_for(path, repo):
    rel = os.path.relpath(path, repo)
    parts = rel.split(os.sep)
    d = parts[-2] if len(parts) >= 2 and parts[-2] != "data" else (parts[-3] if len(parts) >= 3 else "")
    stem = _clean_stem(os.path.splitext(parts[-1])[0])
    return f"{d}/{stem}" if (d and stem) else (d or stem or "unknown")


def _ids_and_meta(obj):
    """Yield (batch_id, record) over the artifact shapes we've seen: list-of-dict(id=), dict(ids=[…]),
    dict mapping batch_id->{…}, or a plain list of id strings."""
    if isinstance(obj, list):
        for r in obj:
            if isinstance(r, dict) and r.get("id"):
                yield r["id"], r
            elif isinstance(r, str) and (r.startswith("batch_") or r.startswith("msgbatch_")):
                yield r, {}
    elif isinstance(obj, dict):
        if isinstance(obj.get("ids"), list):
            for x in obj["ids"]:
                if isinstance(x, str):
                    yield x, {}
        else:
            for k, v in obj.items():
                if isinstance(k, str) and (k.startswith("batch_") or k.startswith("msgbatch_")):
                    yield k, (v if isinstance(v, dict) else {})


_BID = re.compile(r"(batch_[0-9a-f]{20,}|msgbatch_[0-9A-Za-z]{18,})")
# aggregate/audit dirs are NOT workload intents — only used as a last resort if an id appears nowhere else
_AGG = {"spend_audit", "notes", "archive", "tmp", "temp", "backup", "logs", "old"}


def scan_intent_map(repo, content_scan=True, max_bytes=1_000_000):
    """{batch_id: intent} mined from <repo>. Two passes, most-specific wins:
      1. structured *batch_id*.json artifacts — per-record job_type gives the finest intent.
      2. content scan of small JSON files under data/ — id → its top-level directory (the job folder),
         deprioritizing aggregate/audit dirs. Bounded by file size; zero spend."""
    specific, dirmap = {}, {}
    seen = set()
    for pat in (os.path.join(repo, "**", "*batch_id*.json"), os.path.join(repo, "**", "*batch_ids*.json")):
        for path in glob.glob(pat, recursive=True):
            if "/.git/" in path or path in seen:
                continue
            seen.add(path)
            try:
                obj = json.load(open(path))
            except Exception:
                continue
            base = _intent_for(path, repo)
            for bid, rec in _ids_and_meta(obj):
                jt = rec.get("job_type") if isinstance(rec, dict) else None
                if jt:
                    specific.setdefault(bid, f"{base}:{jt}")
                else:
                    dirmap.setdefault(bid, base)

    if content_scan:
        data_root = os.path.join(repo, "data") if os.path.isdir(os.path.join(repo, "data")) else repo
        from collections import defaultdict
        id_dirs = defaultdict(set)
        for dp, _dn, fn in os.walk(data_root):
            if "/.git/" in dp:
                continue
            for f in fn:
                if not f.endswith(".json"):
                    continue
                p = os.path.join(dp, f)
                try:
                    if os.path.getsize(p) > max_bytes:
                        continue
                    txt = open(p, errors="ignore").read()
                except Exception:
                    continue
                top = os.path.relpath(dp, data_root).split(os.sep)[0]
                if top in _AGG:                      # audit/usage caches carry cost but NOT intent — never a source
                    continue
                for bid in set(_BID.findall(txt)):
                    id_dirs[bid].add(top)
        for bid, ds in id_dirs.items():
            dirmap.setdefault(bid, sorted(ds)[0])

    m = dict(dirmap)
    m.update(specific)   # job_type-specific intent overrides the bare directory
    return m


def reconstruct_intents(repo, apply=False):
    """Match the mined intent map against our run nodes; report coverage; optionally write it on."""
    with learn._lock:
        runs = learn._db().execute("SELECT id, attrs FROM graph_nodes WHERE type='run'").fetchall()
    run_ids = {r[0] for r in runs}
    attrs_by_id = {r[0]: json.loads(r[1] or "{}") for r in runs}

    imap = scan_intent_map(repo)
    matched = {bid: it for bid, it in imap.items() if bid in run_ids}
    print(f"mine-history intents — scanned {repo}")
    print(f"  artifacts mapped {len(imap):,} batch ids; {len(matched):,}/{len(run_ids):,} of our runs matched")
    from collections import Counter
    by_intent = Counter(matched.values())
    for it, n in by_intent.most_common(20):
        print(f"    {n:>4}  {it}")
    if len(by_intent) > 20:
        print(f"    … +{len(by_intent) - 20} more intents")
    if not apply:
        print("  report-only. Re-run with --apply to write intents onto calls + run nodes (no spend).")
        return dict(mapped=len(imap), matched=len(matched), intents=len(by_intent))

    # NOTE: learn + calls use SEPARATE sqlite connections to the SAME db file. Writing through both
    # inside one transaction self-deadlocks ("database is locked"), so commit each phase before the next.
    with learn._lock:
        for bid, intent in matched.items():
            a = attrs_by_id.get(bid, {})
            a["intent"] = intent
            learn._db().execute("UPDATE graph_nodes SET attrs=? WHERE id=?", (json.dumps(a), bid))
        learn._db().commit()
    updated = 0
    with calls._lock:
        for bid, intent in matched.items():
            cid = attrs_by_id.get(bid, {}).get("call")
            if cid:
                calls._db().execute("UPDATE calls SET intent=? WHERE id=? AND (intent IS NULL OR intent='')",
                                    (intent, cid))
            updated += 1
        calls._db().commit()
    print(f"  applied: {updated} runs tagged with reconstructed intents.")
    return dict(mapped=len(imap), matched=len(matched), intents=len(by_intent), applied=updated)


# ─────────────────────────── causal / temporal edges ───────────────────────────
def enrich_graph():
    """Add `preceded` (per-intent chronological run chain) + `derived_from` (insight → cited runs)."""
    with learn._lock:
        runs = learn._db().execute(
            "SELECT id, ts, attrs FROM graph_nodes WHERE type='run' ORDER BY ts").fetchall()
        # idempotent: drop the edges we own and rebuild (intents may have changed since last run)
        learn._db().execute("DELETE FROM graph_edges WHERE rel IN ('preceded','derived_from')")
        learn._db().commit()
        existing = set()

    # group runs by reconstructed intent, link consecutive in time
    from collections import defaultdict
    chains = defaultdict(list)
    for rid, ts, attrs in runs:
        it = json.loads(attrs or "{}").get("intent") or "(none)"
        chains[it].append((ts, rid))
    preceded = 0
    for it, seq in chains.items():
        seq.sort()
        for (ts0, a), (ts1, b) in zip(seq, seq[1:]):
            key = f"{a}|{b}|preceded"
            if key in existing:
                continue
            learn.add_edge(a, b, "preceded", ts=ts1, attrs={"intent": it})
            preceded += 1

    # link each mined insight to the runs whose model/intent it cites
    with learn._lock:
        ins = learn._db().execute("SELECT id, intent FROM insights WHERE source='mined'").fetchall()
    derived = 0
    for iid, it in ins:
        if not it:
            continue
        targets = [rid for rid, ts, attrs in runs if json.loads(attrs or "{}").get("intent") == it]
        for rid in targets:
            key = f"{iid}|{rid}|derived_from"
            if key in existing:
                continue
            learn.add_edge(iid, rid, "derived_from")
            derived += 1
    print(f"enrich-graph — added {preceded} `preceded` + {derived} `derived_from` edges.")
    nodes, edges = learn.graph_stats()
    print("  nodes:", dict(nodes), " edges:", dict(edges))
    return dict(preceded=preceded, derived_from=derived)


# ─────────────────────────── git evolution mining ───────────────────────────
def mine_git(repo, apply=False, max_commits=400):
    """Weak quality signals from git history of the batch-id artifacts. Commit-subject relevance is decided
    AGENTICALLY — conv.classify_evidence cost_lesson (nano, ~free, recorded, caged by caps.meta) — replacing the old
    _SIGNAL keyword regex (fix|bug|wrong…) that decided by topic word and silently dropped real cost commits."""
    if not os.path.isdir(os.path.join(repo, ".git")):
        print(f"  {repo} is not a git repo — skipping git mining.")
        return dict(commits=0, signals=0)
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", f"-{max_commits}", "--name-only", "--pretty=format:%H%x09%cI%x09%s"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        print(f"  git log failed: {e}")
        return dict(commits=0, signals=0)
    from . import conv                                          # AGENTIC commit-subject relevance (cost_lesson)
    rows = [l.split("\t", 2) for l in out.splitlines() if "\t" in l and len(l.split("\t", 2)) == 3]
    ev = conv.classify_evidence([{"id": r[0], "text": r[2]} for r in rows], run=True) if rows else {}
    cost_commit = {r[0] for r in rows if ev.get(r[0], {}).get("cost_lesson")}
    commits, signals = 0, 0
    cur = None
    for line in out.splitlines():
        if "\t" in line and len(line.split("\t", 2)) == 3:
            h, ciso, subj = line.split("\t", 2)
            cur = (h, ciso, subj, h in cost_commit)            # agentic cost-relevance (was _SIGNAL keyword match)
            commits += 1
        elif line.strip().endswith("batch_id.json") or "batch_ids" in line:
            if cur and cur[3]:
                signals += 1
                if apply:
                    nid = learn.add_node("script_version", cur[2][:80],
                                         attrs={"commit": cur[0][:12], "file": line.strip(), "signal": "cost/fix"},
                                         ts=cur[1])
    print(f"mine-git — {commits} commits scanned; {signals} cost/fix-flagged touches of batch-id artifacts"
          + (" (added as script_version nodes)" if apply else " (report-only; --apply to record)"))
    return dict(commits=commits, signals=signals)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard mine-history")
    ap.add_argument("op", choices=["intents", "graph", "git"])
    ap.add_argument("--repo", default=os.getcwd(), help="repo to mine (default: cwd)")
    ap.add_argument("--apply", action="store_true", help="write the result (default: report-only). No spend.")
    a = ap.parse_args(argv)
    if a.op == "intents":
        reconstruct_intents(a.repo, apply=a.apply)
    elif a.op == "graph":
        enrich_graph()
    else:
        mine_git(a.repo, apply=a.apply)
    return 0
