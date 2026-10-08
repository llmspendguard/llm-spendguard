#!/usr/bin/env python3
"""Attribute Claude Code token burn over a WINDOW across every axis that can explain a drain:
repo, conversation, model, agent-kind (main turn vs spawned subagent), and token class.

Why this exists: `overage_estimate_by_day.py` answers "which DAY went over" but not "what was
BURNING it". When the weekly plan cap is hit, the question is which conversation/repo/subagent
is consuming the quota, and that needs the per-session axes the day-level view collapses.

Two correctness rules this script inherits from `spendguard.claudecode`:
  * DEDUP BY message.id across ALL files. Resume/branch/compaction REPLAYS earlier assistant
    messages into new transcript files; counting per-file inflates burn ~2.4x.
  * Price ONLY via spendguard.pricing (never a literal $/token), with cache_read passed as the
    discounted class so Claude Code's dominant cache re-reads are not billed at full input rate.

Caveat this script REPORTS rather than hides: pricing treats every cache WRITE at the base input
rate, but a 1h-TTL ephemeral write bills above that. The 1h write tokens are surfaced as their own
line so the understatement is visible instead of silent. Pure parse + pricing, $0 — no LLM calls.
"""
import os
import sys
import glob
import json
import time
import argparse
import datetime
from collections import defaultdict

import spendguard.pricing as pricing
from spendguard import config
from spendguard.claudecode import _sidebar_titles

DEFAULT_PROJECTS_DIR = "~/.claude/projects"
DEFAULT_WINDOW_DAYS = 2
DEFAULT_TOP_N = 15
PROJECT_FALLBACK = "claude-code"
UNTITLED = "(untitled)"
WEEKLY_LIMIT_MARKER = "weekly limit"
TITLE_MAX_CHARS = 58


def _projects_dir(cli_value):
    raw = cli_value or os.environ.get("SPENDGUARD_CC_DIR") or DEFAULT_PROJECTS_DIR
    return os.path.expanduser(raw)


def _window_start(days):
    """Inclusive first calendar day of the window, in the transcript's own UTC date space."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=days - 1)).isoformat()


def _turn_cost(model, usage):
    """(cost, in_tok, out_tok, cache_read, cache_write) for ONE assistant message.

    in_tok/out_tok/cache_* are kept UN-lumped so the token-class view can show that cache re-reads
    dominate. Cost mirrors spendguard.claudecode._row_cost so totals reconcile with the dashboard.
    """
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    try:
        cost = pricing.realtime_cost(model, inp + write + read, out, read) or 0.0
    except Exception:
        cost = 0.0
    return cost, inp, out, read, write


def _class_costs(model, inp, out, read, write):
    """Split ONE turn's cost into the token classes that can explain a drain.

    realtime_cost is linear in each class, so pricing each class alone and summing reproduces the
    turn total; `reconcile_class_split` asserts that rather than trusting it. The point of the split
    is that Claude Code's cost is dominated by re-reading context, not by what the model writes —
    a distinction the single est-$ figure hides completely.
    """
    def priced(in_tok, out_tok, cached):
        try:
            return pricing.realtime_cost(model, in_tok, out_tok, cached) or 0.0
        except Exception:
            return 0.0
    return {
        "cache_read (context re-read)": priced(read, 0, read),
        "cache_write (context load)": priced(write, 0, 0),
        "input (new prompt)": priced(inp, 0, 0),
        "output (what Claude wrote)": priced(0, out, 0),
    }


def _cache_write_1h(usage):
    """Cache-write tokens on the 1h TTL — the slice priced above base input rate (see module docstring)."""
    return int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)


def _agent_kind(rec):
    """Main conversation turn vs a turn inside a spawned subagent. Subagents bill the same plan,
    so a fan-out of them is a drain the conversation-level view alone would not localize."""
    if rec.get("isSidechain"):
        return "subagent:" + str(rec.get("agentName") or "unnamed")
    return "main"


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


class Bucket:
    """One row of any breakdown. Separate counters, never a single mixed total."""

    __slots__ = ("cost", "in_tok", "out_tok", "read_tok", "write_tok", "write_1h", "turns")

    def __init__(self):
        self.cost = 0.0
        self.in_tok = self.out_tok = self.read_tok = self.write_tok = self.write_1h = 0
        self.turns = 0

    def add(self, cost, inp, out, read, write, write_1h):
        self.cost += cost
        self.in_tok += inp
        self.out_tok += out
        self.read_tok += read
        self.write_tok += write
        self.write_1h += write_1h
        self.turns += 1


def scan(projects_dir, since_day):
    """Walk every transcript once, attributing each NEW assistant response to all axes."""
    axes = {name: defaultdict(Bucket) for name in
            ("repo", "conversation", "model", "agent_kind", "repo_day", "cwd", "entrypoint")}
    titles, conv_repo, conv_last = {}, {}, {}
    seen_ids = set()
    totals = Bucket()
    class_cost = defaultdict(float)
    limit_hits = defaultdict(int)
    files = glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)
    skipped_files = 0

    for path in files:
        try:
            handle = open(path, errors="ignore")
        except OSError:
            skipped_files += 1
            continue
        with handle:
            for line in handle:
                if '"usage"' not in line and WEEKLY_LIMIT_MARKER not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                day = str(rec.get("timestamp") or "")[:10]
                if not day or day < since_day:
                    continue
                msg = rec.get("message") or {}
                if not isinstance(msg, dict):
                    continue

                if WEEKLY_LIMIT_MARKER in _text_of(msg.get("content")):
                    limit_hits[day] += 1

                usage, model = msg.get("usage"), msg.get("model")
                if not usage or not model:
                    continue
                mid = msg.get("id")
                if mid:
                    if mid in seen_ids:          # replayed by resume/branch/compaction
                        continue
                    seen_ids.add(mid)

                cost, inp, out, read, write = _turn_cost(model, usage)
                write_1h = _cache_write_1h(usage)
                repo = config.project_of_cwd(rec.get("cwd"), PROJECT_FALLBACK)
                sid = rec.get("sessionId") or os.path.basename(path)

                for klass, amount in _class_costs(model, inp, out, read, write).items():
                    class_cost[klass] += amount

                row = (cost, inp, out, read, write, write_1h)
                totals.add(*row)
                axes["repo"][repo].add(*row)
                axes["conversation"][sid].add(*row)
                axes["model"][model].add(*row)
                axes["agent_kind"][_agent_kind(rec)].add(*row)
                axes["repo_day"][(day, repo)].add(*row)
                axes["cwd"][str(rec.get("cwd") or "(no cwd)")].add(*row)
                # entrypoint separates a human session from an automated caller (hook, cron, SDK),
                # which the repo axis cannot: both land in whatever cwd the caller happened to use.
                axes["entrypoint"][f"{rec.get('entrypoint') or '(none)'} "
                                   f"/ {rec.get('userType') or '?'}"].add(*row)

                conv_repo[sid] = repo
                ts = str(rec.get("timestamp") or "")
                if ts > conv_last.get(sid, ""):
                    conv_last[sid] = ts
                if rec.get("customTitle"):
                    titles[sid] = str(rec["customTitle"])

    # Sidebar titles are the human-facing names; a transcript-local customTitle wins when present.
    merged_titles = dict(_sidebar_titles() or {})
    merged_titles.update(titles)

    return {"axes": axes, "titles": merged_titles, "conv_repo": conv_repo, "conv_last": conv_last,
            "totals": totals, "class_cost": dict(class_cost), "limit_hits": limit_hits,
            "files": len(files), "skipped_files": skipped_files, "responses": len(seen_ids)}


def reconcile_class_split(class_cost, total_cost, tolerance=0.01):
    """The class split must reproduce the headline total. A silent drift here would mean the
    'what is draining it' answer and the 'how much' answer disagree — so it is checked, not assumed.
    Returns (ok, split_sum, relative_error)."""
    split = sum(class_cost.values())
    if total_cost <= 0:
        return split == 0, split, 0.0
    err = abs(split - total_cost) / total_cost
    return err <= tolerance, split, err


def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def _fmt_tok(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def _print_table(title, rows, total_cost, label_width=46):
    print(f"\n{title}")
    print(f"  {'':<{label_width}} {'est $':>10} {'share':>7} {'turns':>7} "
          f"{'cache-rd':>9} {'out':>8}")
    for label, b in rows:
        text = label if len(label) <= label_width else label[:label_width - 1] + "…"
        print(f"  {text:<{label_width}} {b.cost:>10,.2f} {_pct(b.cost, total_cost):>6.1f}% "
              f"{b.turns:>7,} {_fmt_tok(b.read_tok):>9} {_fmt_tok(b.out_tok):>8}")


def _top(bucket_map, n):
    return sorted(bucket_map.items(), key=lambda kv: kv[1].cost, reverse=True)[:n]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                    help=f"window length in days, inclusive of today (default {DEFAULT_WINDOW_DAYS})")
    ap.add_argument("--projects-dir", default=None,
                    help=f"Claude Code transcript root (default $SPENDGUARD_CC_DIR or {DEFAULT_PROJECTS_DIR})")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                    help=f"rows per breakdown (default {DEFAULT_TOP_N})")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of tables")
    args = ap.parse_args(argv)

    root = _projects_dir(args.projects_dir)
    if not os.path.isdir(root):
        sys.exit(f"transcript root not found: {root}")
    since = _window_start(args.days)

    started = time.time()
    res = scan(root, since)
    axes, totals = res["axes"], res["totals"]

    if args.json:
        payload = {
            "window_start": since, "days": args.days, "root": root,
            "totals": {"est_usd": round(totals.cost, 2), "turns": totals.turns,
                       "in_tok": totals.in_tok, "out_tok": totals.out_tok,
                       "cache_read_tok": totals.read_tok, "cache_write_tok": totals.write_tok,
                       "cache_write_1h_tok": totals.write_1h},
            "weekly_limit_hits_by_day": dict(sorted(res["limit_hits"].items())),
        }
        for axis in ("repo", "model", "agent_kind"):
            payload[axis] = [{"key": k, "est_usd": round(b.cost, 2), "turns": b.turns}
                             for k, b in _top(axes[axis], args.top)]
        payload["conversation"] = [
            {"session": k, "title": res["titles"].get(k, UNTITLED),
             "repo": res["conv_repo"].get(k), "last_seen": res["conv_last"].get(k),
             "est_usd": round(b.cost, 2), "turns": b.turns}
            for k, b in _top(axes["conversation"], args.top)]
        print(json.dumps(payload, indent=2))
        return

    print(f"Claude Code burn — {since} → today ({args.days}d), root={root}")
    print(f"scanned {res['files']:,} transcripts ({res['skipped_files']} unreadable), "
          f"{res['responses']:,} deduped assistant responses in {time.time() - started:.1f}s")
    print(f"\nTOTAL est-value ${totals.cost:,.2f}  over {totals.turns:,} turns")
    print(f"  tokens: in {_fmt_tok(totals.in_tok)} · out {_fmt_tok(totals.out_tok)} · "
          f"cache-read {_fmt_tok(totals.read_tok)} · cache-write {_fmt_tok(totals.write_tok)} "
          f"(1h-TTL slice {_fmt_tok(totals.write_1h)}, priced at base → est is a FLOOR)")
    if res["limit_hits"]:
        hits = ", ".join(f"{d}:{n}" for d, n in sorted(res["limit_hits"].items()))
        print(f"  weekly-limit messages seen: {hits}")

    ok, split_sum, err = reconcile_class_split(res["class_cost"], totals.cost)
    print("\nWHAT THE MONEY WENT ON (token class)")
    for klass, amount in sorted(res["class_cost"].items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {klass:<34} {amount:>10,.2f} {_pct(amount, totals.cost):>6.1f}%")
    if ok:
        print(f"  {'reconciles with total':<34} {split_sum:>10,.2f}  (drift {err * 100:.3f}%)")
    else:
        print(f"  !! CLASS SPLIT DOES NOT RECONCILE: split {split_sum:,.2f} vs "
              f"total {totals.cost:,.2f} (drift {err * 100:.2f}%) — treat split as unverified")

    _print_table("BY REPO", _top(axes["repo"], args.top), totals.cost)
    _print_table("BY MODEL", _top(axes["model"], args.top), totals.cost)
    _print_table("BY AGENT KIND (main turn vs spawned subagent)",
                 _top(axes["agent_kind"], args.top), totals.cost)

    conv_rows = []
    for sid, b in _top(axes["conversation"], args.top):
        title = res["titles"].get(sid) or UNTITLED
        label = f"[{res['conv_repo'].get(sid, '?')}] {title}"
        conv_rows.append((label[:TITLE_MAX_CHARS], b))
    _print_table("BY CONVERSATION", conv_rows, totals.cost, label_width=TITLE_MAX_CHARS)

    _print_table("BY WORKING DIRECTORY (disambiguates the repo fallback bucket)",
                 _top(axes["cwd"], args.top), totals.cost, label_width=58)
    _print_table("BY ENTRYPOINT / USER TYPE (human session vs automated caller)",
                 _top(axes["entrypoint"], args.top), totals.cost, label_width=58)

    print("\nBY DAY × REPO (top movers)")
    for (day, repo), b in _top(axes["repo_day"], args.top):
        print(f"  {day}  {repo:<34} {b.cost:>10,.2f}  {b.turns:>6,} turns")


if __name__ == "__main__":
    main()
