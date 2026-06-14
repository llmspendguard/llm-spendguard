"""spendguard bootstrap — the COLD-START process: look at ALL of history and build a ready corpus.

One command that mines every source you already have, so the advisor isn't starting blind:
  ledgers      backfill billed batches → cost corpus + run graph                       (free)
  intents      reconstruct each batch's intent from repo artifacts                     (free)
  graph        causal/temporal edges (preceded / derived_from)                         (free)
  call I/O      recover real prompt+output samples from the providers                  (free)
  conversation  index session transcripts → decision events + comments_on edges        (free)
then ESTIMATES the paid, caged (caps.meta) reasoning steps and — only with --run — executes them:
  review (approach-quality) · mine (insights) · mine-conv synth (playbook) · reconstruct (output judge).

Free steps always run (idempotent). Paid steps are estimate-first and meta-capped. This is the answer to
cold start: history → corpus → insights, repeatably.
"""
import argparse
from . import config


def _hdr(step, what):
    print(f"\n── {step} ── {what}")


def bootstrap(repo=None, transcripts=None, run=False, cap=50):
    import os
    repo = repo or os.getcwd()
    print(f"spendguard bootstrap — repo={repo}  (free recovery first; paid reasoning is estimate-first + meta-capped)")

    # ── free recovery ──
    _hdr("1/6 ledgers", "backfill billed batches → cost corpus + run graph")
    try:
        from . import backfill
        n, total = backfill.backfill()
        print(f"  +{n} batch rows (${total:,.2f} historical)")
    except Exception as e:
        print(f"  skipped ({e})")

    _hdr("2/6 intents", "reconstruct each batch's intent from repo artifacts")
    try:
        from . import history
        history.reconstruct_intents(repo, apply=True)
    except Exception as e:
        print(f"  skipped ({e})")

    _hdr("3/6 graph", "causal/temporal edges")
    try:
        from . import history
        history.enrich_graph()
    except Exception as e:
        print(f"  skipped ({e})")

    _hdr("4/6 call I/O", "recover real prompt+output samples from providers (free)")
    try:
        from . import callio
        r = callio.fetch_history(cap=cap)
        print(f"  +{r['added']} samples · {r['batches_fetched']} batches · {r['errors']} unrecoverable")
    except Exception as e:
        print(f"  skipped ({e})")

    _hdr("5/6 conversation", "index transcripts → decision events + comments_on edges (free)")
    try:
        from . import conv
        conv.index_cmd(transcripts, apply=True)
    except Exception as e:
        print(f"  skipped ({e})")

    # ── paid, caged reasoning (estimate-first) ──
    _hdr("6/6 reasoning", f"caged by caps.meta (${config.meta_cap():.0f}/day) — {'RUNNING' if run else 'ESTIMATE-ONLY'}")
    from . import review, advisor, conv
    print("\n[review — approach-quality]")
    review.review(run=run)
    print("\n[mine — insight synthesis]")
    advisor.mine(run=run)
    print("\n[mine-conv synth — playbook from chat]")
    conv.synth(transcripts, run=run)
    print("\n[reconstruct — output-quality judge (note: isolated judging is weak without ground truth)]")
    advisor.reconstruct(run=run)

    print("\n" + "=" * 60)
    if run:
        print("bootstrap complete — corpus + insights ready. Try: `spendguard advise` / `spendguard optimize --intent <X>`.")
    else:
        print("bootstrap (estimate) complete — free corpus built. Re-run with --run to execute the caged "
              "reasoning steps above (total projected cost is the sum of their estimates, meta-capped).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard bootstrap")
    ap.add_argument("--repo", help="repo to mine for intents (default: cwd)")
    ap.add_argument("--transcripts", help="conversation transcript dir/file (default: ~/.claude/projects)")
    ap.add_argument("--cap", type=int, default=50, help="call_io samples per (intent, model)")
    ap.add_argument("--run", action="store_true", help="execute the paid reasoning steps (default: estimate only)")
    a = ap.parse_args(argv)
    return bootstrap(repo=a.repo, transcripts=a.transcripts, run=a.run, cap=a.cap)
