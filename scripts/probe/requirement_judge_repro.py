"""Ground-truth reproduce for the requirement_judge two-tier screen labeling NOTHING on soft/holistic tasks.

Instruments adapters.call to print, for each judge tier (requirement-extract / -screen / -adjudicate): the model,
the actual INPUT size the call received (Bug #1: is the screen prompt assembled WHOLE, or ~10 tokens?), whether a
schema was passed and the max_tokens budget (Bug #2), the finish_reason / truncated / out_tok, and a preview of the
RAW reply text + parsed json (Bug #3: why the screen verdict does or doesn't parse, and whether it escalates).

Gated; ONE synthetic soft-criteria item; ~$0 on subscription lanes (screen=Haiku lane, extract=sol lane), a few
cents at most if a tier rides metered. Run: ./.venv.nosync/bin/python scripts/probe/requirement_judge_repro.py
"""
import spendguard
spendguard.require()

import json  # noqa: E402
from spendguard import requirement_judge, adapters  # noqa: E402

# A SOFT / holistic task — the kind the report says silently labels nothing (no single mechanical right answer;
# "a good digest" is a judgement). Synthetic + self-contained; contains no secrets.
PROMPT = (
    "You are given a support-chat transcript. Write a crisp 2-sentence DIGEST that captures the customer's core "
    "problem and how it was resolved. Be faithful to the transcript; do not invent details.\n\n"
    "TRANSCRIPT:\n"
    "Customer: I reset my password an hour ago and now I can't log in at all — it just spins.\n"
    "Agent: Thanks — I see a stale session token on your account from before the reset. Let me clear it.\n"
    "Agent: Done. Please hard-refresh and try again.\n"
    "Customer: That worked, I'm in now. Thank you!\n"
)
OUTPUT = (
    "The customer was locked out after a password reset because a stale pre-reset session token was blocking "
    "login. The agent cleared the token and, after a hard refresh, the customer regained access."
)

_orig_call = adapters.call
_seen = []


def _traced_call(model, prompt, **kw):
    r = _orig_call(model, prompt, **kw)
    sig = str(kw.get("sig") or "")
    if "requirement" in sig:
        txt = r.get("text")
        _seen.append(sig)
        print(f"\n=== adapters.call  sig={sig}  model={model} ===")
        print(f"  INPUT chars={len(prompt or '')}  (~{len(prompt or '') // 4} tok est)   "
              f"schema={'YES' if kw.get('schema') else 'no'}   max_tokens={kw.get('max_tokens')}")
        print(f"  reported in_tok={r.get('in_tok')}  out_tok={r.get('out_tok')}   "
              f"finish_reason={r.get('finish_reason')}  truncated={r.get('truncated')}")
        print(f"  error={r.get('error')!r}")
        print(f"  text_len={len(txt) if txt else 0}   text_head={(txt or '')[:400]!r}")
        print(f"  parsed_present={isinstance(r.get('parsed'), (dict, list))}   executor={r.get('executor')}")
    return r


adapters.call = _traced_call

print("-- reproducing judge_requirements on a soft-criteria item --")
v = requirement_judge.judge_requirements(PROMPT, OUTPUT)
print("\n=== FINAL VERDICT ===")
print(json.dumps(v, indent=2, default=str))
print(f"\ntiers hit (in order): {_seen}")
print(f"good={v.get('good')!r}  tier={v.get('tier')!r}  labeled={'YES' if v.get('good') is not None else 'NO'}")
