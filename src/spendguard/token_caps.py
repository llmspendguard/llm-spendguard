"""Find every hardcoded output-token cap in the tree, and make each one answer for itself.

WHY THIS FILE EXISTS. A low `max_tokens` has been the single most persistent defect in this codebase —
found, fixed, and back again over more than a week. Each fix was real and each left a different hole:

  1. `call_complete` was built as a SIBLING of the normal call path. One of nine scripts adopted it.
  2. The controls moved INSIDE `adapters.call`, which was right — and `max_tokens=512` stayed on as the
     default, so every caller who named no number got a cap nobody chose.
  3. `call()` lost its default, and `_call_guarded` and `_call_once` kept theirs.

The pattern is not carelessness about any one number. It is that NOTHING FAILED when the number came back.
A cap does not announce itself: you are billed for tokens GENERATED, so a low cap saves nothing, and what
it does instead is cut the answer off. A cut-off JSON body does not raise — it fails to match, and the
caller reads "no findings" where the truth was "no answer". That is why it kept being rediscovered by
accident instead of caught: the failure looks exactly like success.

So this module is not another fix. It is the thing that FAILS when the fix regresses.

TWO HALVES, EACH DOING WHAT IT IS GOOD AT.

  * FINDING every cap is mechanical and must be COMPLETE. It walks the AST for the token-cap keywords, at
    call sites and in signature defaults alike. Structure, not meaning: the parse tree either has an int
    literal bound to that keyword or it does not. No regex, and nothing to miss.

  * DECIDING whether a given cap is HARMLESS is a judgement, and it goes to a model. `max_tokens=16` on a
    connectivity probe that throws its output away is fine; `max_tokens=500` on a call whose reply is
    parsed as JSON is the bug that started all this — and the two are indistinguishable from the number.
    They are told apart by what the surrounding code DOES with the reply, which is meaning. Two reasonable
    people can disagree about a given site, which is the test for a judgement.

THE VERDICTS ARE RECORDED, AND ABSENCE OF A VERDICT IS A FAILURE. The judging pass writes to a ledger in
the repo; the test reads the ledger and never calls a model, so CI stays offline and deterministic. A cap
that is new, or whose value changed, has no verdict — and an unjudged cap FAILS the test rather than
passing quietly. That is the whole point: to reintroduce this defect you would have to add a cap AND get a
model to certify it harmless AND commit the certificate. It cannot happen by forgetting.
"""
import ast
import json
import os
import pathlib

# The keyword every provider dialect uses to bound generated output. Named here so a new dialect is one
# edit rather than a new blind spot.
CAP_KWARGS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "maxOutputTokens")

SKIP_DIRS = {".venv", ".venv.nosync", "__pycache__", "build", "dist", ".git", ".mypy_cache", ".pytest_cache"}

LEDGER_NAME = "token_cap_verdicts.json"

# What the judge may answer. The prompt defines this enum, so reading it back later is reading a recorded
# answer, not re-deciding anything.
VERDICT_HARMLESS = "harmless_probe"      # output is discarded or a fixed token; truncation cannot mislead
VERDICT_CONTENT = "content_call"         # output is USED — a cap here can truncate a real answer
VERDICTS = (VERDICT_HARMLESS, VERDICT_CONTENT)


def _iter_py(root):
    for p in sorted(pathlib.Path(root).rglob("*.py")):
        if not any(part in SKIP_DIRS for part in p.parts):
            yield p


def _enclosing(tree):
    """{lineno: qualified symbol} so a site's identity survives edits above it. Keying a verdict to a line
    number would expire every verdict whenever anyone added an import."""
    out = {}
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{child.name}" if prefix else child.name
                for ln in range(child.lineno, (getattr(child, "end_lineno", child.lineno) or child.lineno) + 1):
                    out[ln] = qual
                walk(child, qual)
            else:
                walk(child, prefix)
    walk(tree, "")
    return out


def sites(root):
    """Every hardcoded output-token cap under `root`, as dicts. Complete by construction: an int literal
    bound to a cap keyword is either in the parse tree or it is not."""
    found = []
    root = pathlib.Path(root)
    for p in _iter_py(root):
        try:
            src = p.read_text(errors="ignore")
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        lines = src.splitlines()
        encl = _enclosing(tree)
        rel = str(p.relative_to(root))

        def add(lineno, kwarg, value, kind):
            found.append({
                "file": rel,
                "symbol": encl.get(lineno, "<module>"),
                "kwarg": kwarg,
                "value": value,
                "kind": kind,
                "line": lineno,
                "code": "\n".join(lines[max(0, lineno - 3):lineno + 2]).strip(),
            })

        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                for kwd in n.keywords:
                    if kwd.arg in CAP_KWARGS and isinstance(kwd.value, ast.Constant) \
                            and isinstance(kwd.value.value, int) and not isinstance(kwd.value.value, bool):
                        add(n.lineno, kwd.arg, kwd.value.value, "call-site")
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                pairs = []
                if a.defaults:
                    pairs += list(zip(a.args[-len(a.defaults):], a.defaults))
                pairs += list(zip(a.kwonlyargs, a.kw_defaults))
                for arg, d in pairs:
                    if arg.arg in CAP_KWARGS and isinstance(d, ast.Constant) \
                            and isinstance(d.value, int) and not isinstance(d.value, bool):
                        add(n.lineno, arg.arg, d.value, "signature-default")
    return found


def key(site):
    """Identity of a cap for the ledger: WHERE it is and WHAT the number is — not which line it sits on.

    The value is part of the key on purpose. Changing 16 to 4000, or 4000 to 16, is a new decision and must
    be judged again; a verdict that survived an edit to the number would certify a cap nobody looked at."""
    return f"{site['file']}::{site['symbol']}::{site['kwarg']}={site['value']}"


JUDGE_PROMPT = """You are deciding whether ONE hardcoded output-token cap in a Python codebase is dangerous.

Background: a `max_tokens` (or equivalent) cap does NOT control cost — billing is per token GENERATED, so a
low cap saves nothing. What a low cap does is CUT THE REPLY OFF. When the caller then parses that reply as
JSON, the truncated body does not raise: it fails to match, and the caller reads "no results" where the
truth was "no answer". That silent-wrong-answer is the failure you are looking for.

Here is the call site:

  file:   {file}
  symbol: {symbol}
  cap:    {kwarg}={value}   ({kind})

  code:
{code}

Answer ONE question: is the model's OUTPUT from this call USED as content by the surrounding code?

  - "{harmless}" — the output is discarded, or only its existence/shape matters. A connectivity ping, a
    cache-behaviour probe, a latency check, a test fixture. Truncating it cannot mislead anyone, because
    nobody reads it. Choose this ONLY if you can see that the reply is thrown away or is a single fixed token.
  - "{content}" — the output is parsed, stored, displayed, returned, scored, or otherwise consumed. A cap
    here can silently truncate a real answer. If the code parses the reply (json.loads, a regex over it,
    reading .text into a value), it is this. When you cannot tell, answer "{content}" — an unjustified cap
    on a content call is exactly the defect, and guessing "harmless" is how it survived for a week.

Reply with JSON only: {{"verdict": "{harmless}" or "{content}", "why": "<one sentence naming what the code does with the reply>"}}
"""


def judge(site, model=None):
    """Ask a model whether this one cap is harmless. Returns the recorded verdict dict.

    Agentic because it is a judgement: the number alone cannot tell a discarded probe from a truncated
    answer, only what the surrounding code does with the reply can, and that is meaning."""
    from . import adapters, config, output_contract
    model = model or config.advisor_model()
    prompt = JUDGE_PROMPT.format(harmless=VERDICT_HARMLESS, content=VERDICT_CONTENT, **site)
    r = adapters.call(model, prompt, sig="spendguard:token-cap-judge",
                      schema={"type": "object", "required": ["verdict", "why"]})
    if r.get("error") or not r.get("text"):
        raise RuntimeError(f"judge failed for {key(site)}: {r.get('error') or 'no text (truncated?)'}")
    obj, _salvaged = output_contract._as_obj(r["text"])
    v = obj.get("verdict")
    if v not in VERDICTS:
        raise RuntimeError(f"judge returned {v!r}, not one of {VERDICTS}")
    return {"verdict": v, "why": obj.get("why", ""), "model": model,
            "value": site["value"], "file": site["file"], "symbol": site["symbol"], "kwarg": site["kwarg"]}


def ledger_path(repo_root):
    return pathlib.Path(repo_root) / "tests" / LEDGER_NAME


def load_ledger(repo_root):
    p = ledger_path(repo_root)
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def compare_to_ledger(repo_root, scan_dir=None):
    """Compare the caps that EXIST against the caps that have been JUDGED.

    Returns {unjudged, content_caps, harmless, stale}. The test turns a non-empty `unjudged` or
    `content_caps` into a failure. `stale` is verdicts whose site is gone — informational, not a failure,
    since deleting a capped call is the outcome we want."""
    repo_root = pathlib.Path(repo_root)
    scan = pathlib.Path(scan_dir) if scan_dir else repo_root / "src"
    present = {key(s): s for s in sites(scan)}
    led = load_ledger(repo_root)
    unjudged = [s for k, s in present.items() if k not in led]
    content = [{**present[k], **led[k]} for k in present if k in led and led[k].get("verdict") == VERDICT_CONTENT]
    harmless = [k for k in present if k in led and led[k].get("verdict") == VERDICT_HARMLESS]
    stale = [k for k in led if k not in present]
    return {"unjudged": unjudged, "content_caps": content, "harmless": harmless,
            "stale": stale, "total": len(present)}


def cmd(argv=None):
    """`spendguard token-caps [--judge] [--scan DIR]` — list caps, or judge the unjudged ones and record."""
    import sys
    argv = list(sys.argv[2:] if argv is None else argv)
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    scan = None
    if "--scan" in argv:
        scan = argv[argv.index("--scan") + 1]
    res = compare_to_ledger(repo_root, scan)
    print(f"{res['total']} hardcoded token cap(s) under {scan or 'src/'} · "
          f"{len(res['harmless'])} judged harmless · {len(res['content_caps'])} judged CONTENT · "
          f"{len(res['unjudged'])} UNJUDGED")
    for c in res["content_caps"]:
        print(f"  CONTENT CAP  {c['file']}:{c['symbol']} {c['kwarg']}={c['value']} — {c.get('why', '')}")
    for s in res["unjudged"]:
        print(f"  unjudged     {s['file']}:{s['symbol']} {s['kwarg']}={s['value']} ({s['kind']})")
    if "--judge" not in argv:
        if res["unjudged"]:
            print("\nrun with --judge to have a model rule on the unjudged ones (they FAIL the test until then)")
        return 1 if (res["unjudged"] or res["content_caps"]) else 0

    led = load_ledger(repo_root)
    for s in res["unjudged"]:
        v = judge(s)
        led[key(s)] = v
        print(f"  judged {key(s)} -> {v['verdict']}: {v['why']}")
    p = ledger_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Through config.update_json, not write_text: this repo's rule is that every whole-file JSON write goes
    # through the atomic, backing-up writer, and its own guard test caught this module breaking it. The
    # ledger is a certificate — replacing it with a truncated or half-written file would silently un-certify
    # caps that were judged. Sorted so it diffs readably in review.
    from . import config
    config.update_json(str(p), lambda _d: dict(sorted(led.items())))
    print(f"\nwrote {len(led)} verdict(s) -> {p}")
    after = compare_to_ledger(repo_root, scan)
    return 1 if (after["unjudged"] or after["content_caps"]) else 0
