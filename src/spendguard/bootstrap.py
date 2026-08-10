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
    # GUARDED LIKE STEPS 1-5, THE IMPORT INCLUDED. Every earlier step prints "skipped (…)" and carries on;
    # this one did not, so a single failing reasoning step aborted bootstrap after the free corpus was
    # already built — and the run that did the most work was the one most likely to lose its own summary.
    # Each sub-step is guarded SEPARATELY: three of four succeeding beats one raising and hiding the rest,
    # and a step that was skipped must be visible as skipped rather than simply absent.
    try:
        from . import review, advisor, conv
    except Exception as e:
        review = advisor = conv = None
        print(f"  reasoning steps unavailable ({type(e).__name__}: {str(e)[:100]})")
    for _label, _fn in () if review is None else (("review — approach-quality", lambda: review.review(run=run)),
                        ("mine — insight synthesis", lambda: advisor.mine(run=run)),
                        ("mine-conv synth — playbook from chat", lambda: conv.synth(transcripts, run=run)),
                        ("reconstruct — output-quality judge (isolated judging is weak without ground truth)",
                         lambda: advisor.reconstruct(run=run))):
        print(f"\n[{_label}]")
        try:
            _fn()
        except Exception as e:
            print(f"  skipped ({type(e).__name__}: {str(e)[:120]})")

    print("\n" + "=" * 60)
    if run:
        print("bootstrap complete — corpus + insights ready. Try: `spendguard advise` / `spendguard optimize --intent <X>`.")
    else:
        print("bootstrap (estimate) complete — free corpus built. The caged reasoning steps above were NOT run.")
        from . import ui
        ui.estimate_only(action="execute the caged reasoning steps (total ≈ sum of the estimates above)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard bootstrap")
    ap.add_argument("--repo", help="repo to mine for intents (default: cwd)")
    ap.add_argument("--transcripts", help="conversation transcript dir/file (default: ~/.claude/projects)")
    ap.add_argument("--cap", type=int, default=50, help="call_io samples per (intent, model)")
    ap.add_argument("--run", action="store_true", help="execute the paid reasoning steps (default: estimate only)")
    a = ap.parse_args(argv)
    return bootstrap(repo=a.repo, transcripts=a.transcripts, run=a.run, cap=a.cap)
