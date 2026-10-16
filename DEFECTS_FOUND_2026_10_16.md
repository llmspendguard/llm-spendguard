# spendguard defects found 2026-10-16

Found while measuring large-input LLM latency for the caremap pipeline document.
All three reproduced on this machine against the running editable install.
**Not committed; not filed as issues.** Recorded here so they are not lost.

---

## 1. `adapters.call(model=None, metered_only=True)` raises an unhelpful `TypeError`

**Reproduced:**

```
EXC: TypeError | argument of type 'NoneType' is not a container or iterable
deepest frame: adapters.py:51 -> if ":" in model:
```

`provider_for(model)` assumes `model` is a string and does `":" in model` as its
first act. When a caller passes `model=None` — which is the natural way to say "you
choose" — it dies inside provider resolution with a message that names neither the
argument nor the caller's mistake.

**Why it matters beyond ergonomics.** A caller who wants spendguard to pick is
supposed to use `reasoning='best-value'` with an `intent`. `model=None` looks like
the same request and is not, and the resulting `TypeError` gives no hint of the
correct form. Worse, a caller who wraps the call in a broad `except Exception` —
which is exactly the pattern the refusal-containment doctrine warns about — records
this as a failed measurement rather than as a programming error.

**The fix this wants:** a named refusal at the entry point. `metered_only=True` with
no model is a contradiction the API should state (`a metered call needs an explicit
model; to let spendguard choose, pass reasoning='best-value' with an intent`), not a
container-protocol error three frames down.

---

## 2. `governed=True` stalls with no applied model and zero completions

Calling with `governed=True` routed into a batch/admission path that, after roughly
**7 minutes**, had returned **no applied model and zero completions**. No refusal, no
timeout, no progress line — the call simply did not come back.

This is the failure mode the deliberate-refusal doctrine exists to prevent in the
other direction: a path that neither completes nor refuses is indistinguishable from
a hang, and a caller cannot tell whether to wait, retry, or stop. **A governed call
that cannot be admitted should refuse with the admission reason**, not wait.

Lower confidence than defect 1 — this was observed once, under a specific set of
arguments, and was not reduced to a minimal repro. **Needs a bounded repro before it
is filed.**

---

## 3. `spendguard doctor` reports a stale editable install (confirmed, self-reported)

`doctor`'s own output, verbatim:

> `version : 🔴 0.12.21 (running code) ≠ 0.12.20 (this venv's install metadata) —
> STALE EDITABLE INSTALL. Its dependency set is frozen at 0.12.20, so a dependency a
> newer version added is NOT installed here and a lazy import can fail at runtime
> (the 'reviewer unavailable' / ImportError class). Refresh WITH deps:
> `pip install -e .` in THIS venv (NOT --no-deps).`

This one is already detected and well explained by the tool; it is listed only
because it is **currently true and unaddressed**, and because the failure class it
predicts — a lazy import failing at runtime — would be easy to misattribute to
whatever call happened to trigger it.

---

## 4. Also surfaced by `doctor`, and not previously noted here: a ledger integrity class

`doctor` flags several `cost>max-token-price` rows, all `gemini-3-flash` /
`gemini-3-flash-preview` / `gemini-3-flash`, where billed cost exceeds the maximum
the price table could assign to the recorded tokens (up to **1.65×** the ceiling, on
n=1; **1.31×** on n=16). Its own reading: *"the ledger's own cost and token columns
disagree: tokens UNDER-RECORDED, or a STALE price for this model."*

**This had a direct consequence for today's measurement**, which is why it is here.
The large-input latency figure was initially taken as the median of all five recorded
calls above 500,000 input tokens — one of which is `gemini-3.8-flash`. Because that
vendor's token counts carry a standing under-recording question, and because the call
was separately the slowest in the set at the smallest input, the figure was narrowed
to the four same-model `claude-opus-4-8` calls. **The integrity flag changed a
reported number**, from 115.79 s to 98.69 s, which is the strongest argument for
fixing it: it is not cosmetic, it propagates into measurements.
