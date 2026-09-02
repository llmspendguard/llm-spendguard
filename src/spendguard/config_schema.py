"""The declarative registry of EVERY spendguard setting — the single source of truth that
drives `spendguard config`, `spendguard init`, SETUP.md, and validation.

Add a knob here (with its store, default, options, and whether it's secret) and it automatically
appears in the setup interview, the config dump, and the docs. This is what lets a human — or Claude
reading the repo — enumerate exactly what's configurable, what the valid options are, and drive setup.

`store` says where the value lives:
  env                      -> environment variable only (e.g. provider API keys)
  config.json:<dotpath>    -> ~/.spendguard/config.json (operational, non-secret)
  email.json:<key>         -> ~/.spendguard/email.json (email config; may be secret)
Environment variable (`env`) always overrides the file when set.
"""

SETTINGS = [
    # ── core ──
    dict(section="core", key="home", store="env", env="SPENDGUARD_HOME", default="~/.spendguard",
         kind="path", secret=False,
         desc="Directory for logs, kill-switch flag, price cache, spend db, and config files."),

    # ── caps (per-call / cumulative) ──
    dict(section="caps", key="per_batch", store="config.json:caps.per_batch", env="GATE_CAP", default=75,
         kind="float", secret=False,
         desc="Hard-stop any single batch whose projected cost exceeds this many dollars."),
    dict(section="caps", key="realtime", store="config.json:caps.realtime", env="GATE_RT_BUDGET", default=50,
         kind="float", secret=False,
         desc="Cumulative real-time spend cap ($) before the gate refuses further calls."),
    # Resource-class caps: a TOTAL ceiling + per-class sub-caps (LLM vs remote-compute), each daily & monthly.
    # null = off. Require budget.backend=sqlite. LLM caps are hard (gate-enforced); compute caps are alert/soft
    # (vast.ai launches don't hit the gate — see resources.compute_exceeded). Legacy flat caps.daily/caps.monthly
    # are still honored as the TOTAL ceiling.
    dict(section="caps", key="total.daily", store="config.json:caps.total.daily", env="GATE_TOTAL_DAILY", default=None,
         kind="float|null", secret=False, desc="DAILY total spend ceiling ($), LLM + remote-compute. null = off."),
    dict(section="caps", key="total.monthly", store="config.json:caps.total.monthly", env="GATE_TOTAL_MONTHLY", default=None,
         kind="float|null", secret=False, desc="MONTHLY total spend ceiling ($), LLM + remote-compute. null = off."),
    dict(section="caps", key="llm.daily", store="config.json:caps.llm.daily", env="GATE_LLM_DAILY", default=None,
         kind="float|null", secret=False, desc="DAILY LLM (OpenAI+Anthropic) sub-cap ($) — HARD, gate-enforced. null = off."),
    dict(section="caps", key="llm.monthly", store="config.json:caps.llm.monthly", env="GATE_LLM_MONTHLY", default=None,
         kind="float|null", secret=False, desc="MONTHLY LLM sub-cap ($) — HARD, gate-enforced. null = off."),
    dict(section="caps", key="external.daily", store="config.json:caps.external.daily", env="GATE_EXTERNAL_DAILY", default=None,
         kind="float|null", secret=False, desc="DAILY non-token EXTERNAL (MCP/tool + external-API) sub-cap ($) — HARD "
         "for a wrapper that calls spendguard.gate_external() before the paid call. null = off (opt-in)."),
    dict(section="caps", key="on_breach", store="config.json:caps.on_breach", env="SPENDGUARD_ON_BREACH", default="refuse",
         kind="str", secret=False, desc="On a cap breach: 'refuse' (default, fail-closed — the gate hard-stops) or "
         "'downgrade' (opt-in — the refusal names the cheapest idle $0 lane to route to instead)."),
    dict(section="caps", key="compute.daily", store="config.json:caps.compute.daily", env="GATE_COMPUTE_DAILY", default=None,
         kind="float|null", secret=False, desc="DAILY remote-compute (vast.ai GPU) sub-cap ($) — alert/soft. null = off."),
    dict(section="caps", key="compute.monthly", store="config.json:caps.compute.monthly", env="GATE_COMPUTE_MONTHLY", default=None,
         kind="float|null", secret=False, desc="MONTHLY remote-compute sub-cap ($) — alert/soft. null = off."),
    dict(section="caps", key="meta", store="config.json:caps.meta", env="GATE_META_BUDGET", default=2.0,
         kind="float", secret=False,
         desc="Daily $ cap for spendguard's OWN advisor LLM use (intent spendguard:*) — separate from workload caps."),

    # ── gate enforcement: the estimate → test → run rail for big batches ──
    dict(section="gate", key="autotune", store="config.json:gate.autotune", env="SPENDGUARD_AUTOTUNE", default="suggest",
         kind="enum:off,suggest,apply", secret=False,
         desc="Learned max_tokens at call time: suggest prints the measured delta (p99×1.5 vs your cap) once per "
              "call-class; apply SHRINKS a wasteful cap to the measured bound — never raises or adds one, vetoed "
              "by <30 observations or ANY truncation history (one truncation permanently backs the class off), "
              "per-call opt-out kw autotune=False. Every application is logged."),
    dict(section="gate", key="http_capture", store="(env only)", env="SPENDGUARD_HTTP_CAPTURE", default="on",
         kind="enum:on,off", secret=False,
         desc="Raw-HTTP visibility net: parse usage from httpx/requests calls made straight at provider hosts "
              "(no SDK) into the realtime ledger; unparseable provider responses log a loud unmetered event. "
              "Capture-only (never blocks); SDK calls are suppressed (no double count)."),
    dict(section="gate", key="enforce", store="config.json:gate.enforce", env="SPENDGUARD_ENFORCE", default="warn",
         kind="enum:off,warn,block", secret=False,
         desc="Lifecycle enforcement for batches over the size threshold (the estimate → test → eval → run sequence): "
              "off = no requirement; warn = log a 'would-block' when a batch runs without a fresh estimate+test+eval "
              "(default); block = hard-require the fresh estimate → test → eval before the batch runs."),
    dict(section="gate", key="require_eval", store="config.json:gate.require_eval", env="SPENDGUARD_REQUIRE_EVAL",
         default=True, kind="bool", secret=False,
         desc="Does a gated scale run also require a fresh PASSING eval (a STATED bar + an AGENTIC verdict on the test "
              "sample), on top of estimate+test? Default on — the eval is the lifecycle's quality checkpoint. Set "
              "false to fall back to the estimate+test-only gate while a repo adopts evals."),
    dict(section="gate", key="eval_model", store="config.json:gate.eval_model", env="SPENDGUARD_EVAL_MODEL",
         default=None, kind="str|null", secret=False,
         desc="Model for the eval JUDGE (bulkgate.eval_job) — a cheap-but-capable class is right (Haiku / a mini "
              "model). Unset → falls back to config.advisor_judge_model. Config, never hardcoded."),

    # ── subscription (flat plan fees — a REAL cost line, and the denominator the est-value axis sits against) ──
    dict(section="subscription", key="plan_usd", store="config.json:subscription.plan_usd", env="SPENDGUARD_PLAN_USD",
         default=None, kind="float|null", secret=False,
         desc="Your total MONTHLY subscription fee (Claude Max / ChatGPT Pro / seats). Until set, the receipt "
              "shows an ASSUMED default marked with * — an assumed number is never presented as an actual charge. "
              "Set this and the * disappears. (Per-plan breakdown: `subscription.plans` = [{name, usd}, …].)"),
    dict(section="subscription", key="plans", store="config.json:subscription.plans", default=None,
         kind="json|null", secret=False,
         desc="Optional per-plan breakdown [{\"name\":\"Claude Max\",\"usd\":200}, …] — sums to the subscription "
              "line and NAMES it on the receipt (the label is built from these, never hardcoded)."),
    dict(section="subscription", key="pace", store="config.json:subscription.pace", default=None,
         kind="json|null", secret=False,
         desc="{lane: {\"policy\": ...}} — per-plan VALUE policy for the pace router. policy=\"maximize\" (default for "
              "any unlisted lane): fill this plan toward 100% of its window; behind-pace boosts it, ahead-pace just "
              "eases off. policy=\"protect\" (or \"conservative\"): once this plan is AHEAD of pace, SHED discretionary "
              "work off it entirely — preserve its remaining allowance for the work only IT can do (e.g. a Claude Max "
              "weekly reserved for interactive coding, so background/fungible calls go to other plans or metered). "
              "A protected plan that is BEHIND pace still absorbs work. General: every user declares which of their "
              "own plans to protect; nothing here is specific to one setup. e.g. {\"claude-code\":{\"policy\":\"protect\"}}. "
              "Optional per-lane \"reserve_frac\" (0..0.99): the router SHEDS the lane once its remaining window "
              "capacity is at/below this — the HARD CAP that makes 'use to ~100%, never over' real. Default 0 (use "
              "fully; shed only at exactly 0%). e.g. {\"gemini\":{\"reserve_frac\":0.05}} stops with 5% held back."),
    dict(section="subscription", key="pace_reserve_frac", store="config.json:subscription.pace_reserve_frac",
         env="SPENDGUARD_PACE_RESERVE_FRAC", default=0.0, kind="float", secret=False,
         desc="GLOBAL fallback for the per-lane subscription.pace[lane].reserve_frac — the fraction of ANY lane's "
              "window to hold back before the router sheds it (the hard cap enforcing 'never over'). 0.0 = use every "
              "plan fully, shedding only at exactly 0% remaining (the plan's own boundary); a positive value (e.g. "
              "0.03) stops a little early to avoid overage from a stale headroom reading on a plan that BILLS past "
              "100% instead of blocking. Clamped to [0, 0.99]."),

    dict(section="subscription", key="claude_code_weekly_cap_usd",
         store="config.json:subscription.claude_code_weekly_cap_usd", env="SPENDGUARD_CC_WEEKLY_CAP_USD",
         default=None, kind="float|null", secret=False,
         desc="The est-VALUE ($, at realtime API rates) your Claude Code plan covers per WEEKLY window before usage "
              "overflows to real pay-as-you-go API $. Used ONLY by `claude-code overflow` to reconstruct which turns "
              "billed for real once the weekly cap was exhausted — the blind spot behind '$0 in the ledger while the "
              "balance dropped'. RECONSTRUCTED estimate, never hardcoded: the plan meters in its own opaque unit, so "
              "this is a proxy you DECLARE and can CALIBRATE against the Admin cost_report (dev-only cross-check). "
              "Unset (default) → nothing is reconstructed as overflow; every turn stays plan_covered (no fabrication)."),
    dict(section="subscription", key="claude_code_week_anchor",
         store="config.json:subscription.claude_code_week_anchor", default=None, kind="string|null", secret=False,
         desc="Optional ISO datetime of any KNOWN weekly-window boundary for the Claude Code plan (e.g. "
              "\"2026-08-25T00:00:00Z\"); windows are then [anchor+7k, anchor+7(k+1)). Pin it if you know when your "
              "weekly limit resets. Unset → the ISO calendar week (Monday 00:00 UTC) is used as the window."),

    # ── pricing freshness (the LiteLLM breadth layer; curated prices.json always wins) ──
    dict(section="pricing", key="refresh_days", store="config.json:pricing.refresh_days", env="SPENDGUARD_PRICES_REFRESH_DAYS",
         default=1, kind="float", secret=False,
         desc="Auto-refresh the LiteLLM price cache when older than this many days, at the top of every `saas sync` "
              "(the installed `spendguard schedule` agent runs sync on a cadence, so prices stay current with no "
              "dedicated price scheduler; an hourly agent still refreshes at most once per this window). Fail-open: "
              "a failed fetch keeps the existing cache + curated prices.json. 0 = never auto-refresh (manual "
              "`spendguard sync-prices` only)."),

    # ── model-catalog freshness (live /models validation; grounds a dispatch against what the provider serves) ──
    dict(section="catalog", key="refresh_hours", store="config.json:catalog.refresh_hours",
         env="SPENDGUARD_CATALOG_REFRESH_HOURS", default=12, kind="float", secret=False,
         desc="Auto-refresh the live model-catalog cache when older than this many hours, at the top of every "
              "`saas sync`. Feeds the pre-dispatch check that catches a stale/rotated model id with the nearest "
              "live ids instead of a mystery provider 404. Fail-open: a failed fetch keeps the existing cache. "
              "0 = never auto-refresh (manual `spendguard sync-catalog` only)."),

    # ── metered prepay balances (per-vendor "how much is available", for load-balancing metered spend) ──
    dict(section="balances", key="refresh_hours", store="config.json:balances.refresh_hours",
         env="SPENDGUARD_BALANCES_REFRESH_HOURS", default=6, kind="float", secret=False,
         desc="Auto-refresh the per-vendor metered balance cache when older than this many hours, at the top of "
              "every `saas sync`. Balances move with usage, so a shorter TTL than the model catalog. Fail-open: a "
              "failed fetch keeps the existing cache. 0 = never auto-refresh (manual `spendguard balances` only)."),

    # ── bulk resilience (the chunk-never-single-shot rule, enforced at the bulk entrypoint) ──
    dict(section="bulk", key="resilience_min_units", store="config.json:bulk.resilience_min_units",
         env="SPENDGUARD_BULK_RESILIENCE_MIN_UNITS", default=1000, kind="int", secret=False,
         desc="Above this many units, bulk_delegate REFUSES a single-shot run that lacks crash-resilience — no "
              "checkpoint (a crash/transient stall would lose the whole run) or chunk_size >= the unit count (not "
              "actually chunked, so one bad unit or a momentary full-lane pass can wedge everything). This is the "
              "durable half of the chunk-never-single-shot rule: it makes the 54k-unit-single-shot mistake "
              "un-submittable, not just discouraged. Pass force=True to override and own the risk. 0 = disable "
              "the gate (never refuse on size)."),

    # ── learning advisor (Layer 2 — its own LLM use, caged by caps.meta + intent spendguard:*) ──
    dict(section="advisor", key="model", store="config.json:advisor.model", env="SPENDGUARD_ADVISOR_MODEL",
         default="claude-opus-4-8", kind="string", secret=False,
         desc="Model for the advisor's REASONING (insight synthesis + `optimize`). Realtime; must exist in pricing.py."),
    dict(section="advisor", key="auto_fresh", store="config.json:advisor.auto_fresh", env="SPENDGUARD_AUTO_FRESH",
         default="weekly", kind="enum:off,weekly,daily", secret=False,
         desc="Auto-refresh Learnings from RECENT activity (caged review of the top intents, caps.meta-bounded) "
              "when the daily report runs. off = manual `spendguard review --run` only."),
    dict(section="advisor", key="judge_model", store="config.json:advisor.judge_model", env="SPENDGUARD_ADVISOR_JUDGE_MODEL",
         default="claude-haiku-4-5", kind="string", secret=False,
         desc="Model for BULK quality reconstruction/judging. Batch API; must exist in pricing.py."),
    dict(section="advisor", key="lane_models", store="config.json:advisor.lane_models", default=None,
         kind="json|null", secret=False,
         desc="{lane: model} — the representative model each subscription plan offers, e.g. "
              "{\"codex\":\"gpt-5.5\",\"gemini\":\"gemini-3.7-flash-high\",\"zai-coding\":\"glm-4.6\"}. The "
              "load-balancer's substitute CANDIDATES (lane_balance.candidate_models); unset → nothing to propose."),
    dict(section="advisor", key="tiers", store="config.json:advisor.tiers", default=None,
         kind="json|null", secret=False,
         desc="{group: [models YOU declare interchangeable for it]} — named ROUTING GROUPS a fungible caller can "
              "request instead of pinning a model, e.g. {\"cheap\":[\"glm-5.3\",\"gpt-5.6-luna\"],\"strong\":"
              "[\"gpt-5.6-sol\",\"claude-opus-4-8\"]}. YOU decide which of your own models belong in a group (the "
              "capability judgement is yours, made once here — the code asserts nothing); route_utility.rank_for_tier "
              "then water-fills that group across whichever $0 lane / cheapest credit is best RIGHT NOW (pace-aware). "
              "A lane serves a group when its advisor.lane_models base is in the group's list. Unset → no group "
              "routing. This is how a 7thsense-style caller asks for 'my cheap group' and gets the best-value one."),
    dict(section="advisor", key="compaction_context_tokens", store="config.json:advisor.compaction_context_tokens",
         env="SPENDGUARD_COMPACTION_CONTEXT_TOKENS", default=100000, kind="int", secret=False,
         desc="Re-read context size (in_tok + cache_read + cache_write, per turn) above which a Claude Code "
              "conversation is a COMPACTION CANDIDATE — every turn re-reads this much context at the cache-read rate, "
              "so a session sustaining it is expensive to keep alive. This THRESHOLD only pre-filters which sessions "
              "`claude-code context` surfaces; whether the retained context is still worth its per-turn cost is an "
              "agentic judgement, never decided by this number. Absolute (the re-read COST is absolute), not a "
              "fraction of any model's window."),
    dict(section="advisor", key="compaction_min_turns", store="config.json:advisor.compaction_min_turns",
         env="SPENDGUARD_COMPACTION_MIN_TURNS", default=5, kind="int", secret=False,
         desc="How many of a conversation's most-recent turns must ALL exceed compaction_context_tokens before it is "
              "flagged — so a single large turn is not mistaken for a sustained large context. Configurable."),
    dict(section="advisor", key="lane_pace_weight", store="config.json:advisor.lane_pace_weight",
         env="SPENDGUARD_LANE_PACE_WEIGHT", default=2.0, kind="float", secret=False,
         desc="How strongly BEHIND-pace plans are preferred in lane ranking. A plan that has spent less than its "
              "elapsed window fraction (lane_economics.pace_by_lane > 0) gets score += pace × this weight, so "
              "discretionary work fills a plan that would otherwise waste its allowance at reset. 0 = ignore pace "
              "(rank on headroom alone); higher = chase 100%-of-window utilisation harder."),
    dict(section="advisor", key="lane_balance_margin", store="config.json:advisor.lane_balance_margin",
         env="SPENDGUARD_LANE_BALANCE_MARGIN", default=0.5, kind="float", secret=False,
         desc="Load-balance sensitivity: proactively route an intent to an idle substitute plan when that plan sits "
              "more than this many x-of-fee BELOW the primary's utilisation. Higher = shed only big imbalances; "
              "lower = spread more aggressively. The margin that stops thrashing once plans are even."),
    dict(section="advisor", key="lane_idle_ratio", store="config.json:advisor.lane_idle_ratio", default=0.5,
         kind="float", secret=False,
         desc="Below this x-of-fee est-value a plan reads as IDLE in `spendguard lanes --balance` (display only)."),
    dict(section="advisor", key="lane_hot_ratio", store="config.json:advisor.lane_hot_ratio", default=1.5,
         kind="float", secret=False,
         desc="Above this x-of-fee est-value a plan reads as HOT in `spendguard lanes --balance` (display only)."),
    dict(section="advisor", key="executor", store="config.json:advisor.executor", env="SPENDGUARD_ADVISOR_EXECUTOR",
         default="api", kind="enum:api,claude-code,codex,zai-coding,pool", secret=False,
         desc="Where spendguard's OWN meta prompts run: api = metered API under caps.meta (default); "
              "claude-code = one-shot headless `claude -p` on the Anthropic plan (anthropic-model prompts only); "
              "codex = headless `codex exec` on the ChatGPT plan (openai-model prompts only); "
              "zai-coding = z.ai GLM Coding Plan over its Anthropic-compatible endpoint (zai-model prompts only, "
              "key ZAI_CODING_API_KEY); pool = ALL lanes, "
              "each serving its own provider — a prompt never runs on a different provider's plan than the model "
              "the advisor chose ($0 billed; value on the est-value axis; the provider key env var is stripped "
              "from the child so a plan call can never silently become metered). Any lane failure cools that "
              "lane (advisor.pool_cooldown_s) and falls back to the API path."),
    dict(section="advisor", key="lane_bandit", store="config.json:advisor.lane_bandit", env="SPENDGUARD_LANE_BANDIT",
         default=False, kind="enum:true,false", secret=False,
         desc="Enable the LEARNED cross-lane router (a decaying bandit): `delegate` picks the lane it has learned "
              "wins for an intent (equal-start → bake-off-judge → exploit), and the MAIN path sheds allow-listed "
              "intents (advisor.bandit_intents) to the learned lane. Relearns as models change. Default off. See "
              "`spendguard lanes --learn` / `--bakeoff` / `--estimate`."),
    dict(section="advisor", key="bandit_intents", store="config.json:advisor.bandit_intents", default=None,
         kind="json|null", secret=False,
         desc="Allowlist of intents the MAIN-path bandit may redirect to another lane — ONLY name intents SAFE to "
              "run on a different model (never work that needs a specific model, e.g. a gpt-5-mini batch). "
              "Empty/unset ⇒ main-path routing stays inert (delegate still learns). e.g. [\"classify\",\"summarize\"]. "
              "Used only when advisor.bandit_mode=allowlist."),
    dict(section="advisor", key="bandit_mode", store="config.json:advisor.bandit_mode", default="allowlist",
         kind="enum:allowlist,optout", secret=False,
         desc="Main-path shedding posture. allowlist (default, conservative): the bandit redirects ONLY intents in "
              "advisor.bandit_intents. optout: it redirects EVERY intent except META and advisor.bandit_denylist — the "
              "'use the idle plans by default' setting that drives non-Claude lane usage up. Either way the arms "
              "exclude the primary lane and quality stays bake-off-learned; deny an intent that needs the primary model."),
    dict(section="advisor", key="bandit_denylist", store="config.json:advisor.bandit_denylist", default=None,
         kind="json|null", secret=False,
         desc="In optout mode, intents the bandit must NEVER redirect off the primary (claude) lane — work that "
              "genuinely needs that model. Ignored in allowlist mode. e.g. [\"vision:extract\",\"legal:review\"]."),
    dict(section="advisor", key="bandit_judge_model", store="config.json:advisor.bandit_judge_model", default=None,
         kind="string", secret=False,
         desc="Cheap model that judges bake-offs (which lane's output is better). Unset → advisor.judge_model "
              "(haiku). ~$0.004 per bake-off; bake-offs run only while an intent is cold or at rate ε."),
    dict(section="advisor", key="queue_lease_s", store="config.json:advisor.queue_lease_s", default=300.0,
         kind="float", secret=False,
         desc="Durable lane QUEUE (lane_queue): how long a drainer's lease on a task holds before it is reclaimed "
              "as a crashed worker (returned to pending, or failed if attempts are exhausted). `spendguard lanes "
              "--enqueue` adds work at any utilization; `--drain` runs it onto idle lanes at $0."),
    dict(section="advisor", key="queue_batch", store="config.json:advisor.queue_batch", default=0,
         kind="int", secret=False,
         desc="Tasks a drain round leases at once. 0 ⇒ the dispatch global-concurrency default (never a second "
              "hardcoded number). The batch fans across lanes via bulk_delegate (governor-bounded)."),
    dict(section="advisor", key="queue_max_attempts", store="config.json:advisor.queue_max_attempts", default=3,
         kind="int", secret=False,
         desc="How many times a queued task is retried (on lane failure or a crashed lease) before it is marked "
              "failed — so a permanently-bad task never loops forever."),
    dict(section="advisor", key="queue_idle_rounds", store="config.json:advisor.queue_idle_rounds", default=2,
         kind="int", secret=False,
         desc="A foreground `--drain` stops after this many consecutive EMPTY leases (the backlog is drained). "
              "`--drain --forever` (a daemon) ignores this and waits for new work instead."),
    dict(section="advisor", key="queue_idle_sleep", store="config.json:advisor.queue_idle_sleep", default=2.0,
         kind="float", secret=False,
         desc="Seconds a drainer waits between empty leases and between load-ceiling re-checks."),
    dict(section="advisor", key="queue_load_ceiling", store="config.json:advisor.queue_load_ceiling", default=0.0,
         kind="float", secret=False,
         desc="LOCAL-machine guard: pause leasing while the 1-min load average exceeds this (subprocess lanes on a "
              "thrashing box only make it worse). 0 ⇒ off. The lane-saturation case is handled separately by the "
              "dispatch governor; this is the CPU-saturation case."),
    dict(section="advisor", key="queue_retain_days", store="config.json:advisor.queue_retain_days", default=7.0,
         kind="float", secret=False,
         desc="Terminal queue rows (done/failed) older than this are archived to lane_queue_archive.jsonl (a "
              "reviewable log) and removed from the live queue, so it never accumulates forever. Recent terminal "
              "rows stay for `spendguard lanes --queue`. `--drain` purges each cycle; `--purge` runs it on demand."),
    dict(section="callio", key="snip_chars", store="config.json:callio.snip_chars", env="SPENDGUARD_CALLIO_SNIP",
         default=800, kind="int", secret=False,
         desc="Chars kept per recovered prompt / output in the call_io corpus. 800 is sized for the caged JUDGE "
              "(enough to rate an answer) and is the storage-bounded default. It is NOT enough to REPLAY a call: "
              "a prompt cut at 800 chars is a different task, so a replay built on truncated rows measures "
              "something other than the work it names. Raise it and re-run `spendguard callio fetch` for a "
              "full-fidelity refill — the provider input/output files are downloadable, so it costs no tokens."),
    dict(section="advisor", key="pool_cooldown_s", store="config.json:advisor.pool_cooldown_s", env="SPENDGUARD_POOL_COOLDOWN_S",
         default=900, kind="float", secret=False,
         desc="After a subscription lane fails (plan window exhausted, CLI missing), skip that lane for this many "
              "seconds (in-process) so bursts of meta prompts go straight to the API instead of hammering a dead lane."),
    dict(section="dispatch", key="lane_concurrency", store="config.json:dispatch.lane_concurrency",
         env="SPENDGUARD_DISPATCH_LANE_CONCURRENCY", default=3, kind="int", secret=False,
         desc="Max concurrent calls sharing ONE subscription lane (all anthropic vendors on claude-code, all "
              "openai on codex, all zai on the GLM plan). A lane is a heavy subprocess (CLI cold-start + context "
              "injection); a small pool beats a swarm and avoids the plan's own concurrency throttle. The dispatch "
              "governor (dispatch.py) queues overflow instead of firing it; a 4-vendor panel touches no limit."),
    dict(section="dispatch", key="vendor_concurrency", store="config.json:dispatch.vendor_concurrency",
         env="SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY", default=8, kind="int", secret=False,
         desc="Max concurrent metered calls to ONE vendor, so a large cross-LLM fan-out queues instead of "
              "429-storming the provider. Per-vendor requests/minute pacing is also available and OFF by default "
              "— set env SPENDGUARD_DISPATCH_RPM_<VENDOR> (e.g. _MOONSHOT=60) to enable it for a vendor."),
    dict(section="dispatch", key="global_concurrency", store="config.json:dispatch.global_concurrency",
         env="SPENDGUARD_DISPATCH_GLOBAL_CONCURRENCY", default=24, kind="int", secret=False,
         desc="Machine-wide ceiling on in-flight LLM calls across ALL vendors/lanes — the last backstop against a "
              "runaway fan-out. SPENDGUARD_DISPATCH_OFF=1 disables the governor entirely (every acquire a no-op). "
              "Lane concurrency is also co-governed ACROSS processes via flock slot-files (two separate runs share "
              "one subscription plan's budget); SPENDGUARD_DISPATCH_XP_OFF=1 disables just that cross-process layer."),
    dict(section="ask", key="default_vendors", store="config.json:ask.default_vendors",
         env="SPENDGUARD_ASK_DEFAULT_VENDORS", default=None, kind="string|null", secret=False,
         desc="Default cross-LLM panel for `spendguard.ask` / `spendguard ask` when the caller names none — a "
              "comma-separated 'vendor:model' list (e.g. anthropic:claude-opus-4-8,openai:gpt-5.5,moonshot:kimi-k3,"
              "zai:glm-5.3). No model ids are hardcoded in code; a deployment sets its own panel here once, then "
              "callers can omit `vendors` and just pick HOW MANY to use with n=. Unset → the caller must pass vendors."),
    dict(section="keys", key="key_profile", store=".spendguard.json:key_profile", env="SPENDGUARD_KEY_PROFILE",
         default=None, kind="string|null", secret=False,
         desc="Per-repo key selection: with key_profile=<name>, provider keys resolve from `<VAR>__<name>` entries "
              "in ~/.spendguard/keys.env (e.g. ANTHROPIC_API_KEY__lmm) instead of the unsuffixed defaults — so one "
              "global keys.env holds every workspace/project-scoped key and each repo picks its own. A REAL "
              "environment variable always wins. Pair with provider-side scoping (OpenAI project keys / Anthropic "
              "workspace keys) to get per-repo billing truth from the provider itself."),
    dict(section="safety", key="keep_backups", store="config.json", env="SPENDGUARD_KEEP_BACKUPS",
         default="3", kind="int", secret=False,
         desc="How many TIMESTAMPED copies of a rewritten JSON file to retain, on top of the Emacs-style "
              "`<file>~` that always holds the immediately-previous version. Every whole-file JSON write in "
              "this package goes through config.update_json, which backs up before replacing — the default "
              "used to be 0 and almost every caller left it there, which is how ~/.spendguard/config.json "
              "went from 9KB of settings to a 26-byte probe value with no copy of it anywhere. Set 0 to keep "
              "only `<file>~`; raise it if you edit settings often and want deeper history."),
    dict(section="calibrate", key="pair_horizon_hours", store="(env only)", env="SPENDGUARD_PAIR_HORIZON_H",
         default="24", kind="int", secret=False,        # "number" was a one-off; every other numeric is int/float
         desc="Learned-estimator pairing window: a logged job prediction (`calibrate.record_estimate`) collects "
              "its captured actuals for this many hours (chain==job_id matches pair regardless). After it closes "
              "with no actuals the prediction is marked expired — visible UNKNOWN, never $0."),

    # ── budget backend ──
    dict(section="budget", key="backend", store="config.json:budget.backend", env=None, default="memory",
         kind="enum:memory,sqlite", secret=False,
         desc="memory = per-process real-time cap; sqlite = cross-process daily/monthly caps (a shared ledger)."),
    # NOT "<home>/spend.db": nothing in this registry or its loader expands "<home>", so the default
    # shipped as a LITERAL path — a consumer taking it at face value would create a directory called
    # "<home>" wherever it happened to be running.
    dict(section="budget", key="db_path", store="config.json:budget.db_path", env=None, default="~/.spendguard/spend.db",
         kind="path", secret=False,
         desc="Location of the SQLite spend ledger (used when backend=sqlite)."),

    # ── observability ──
    dict(section="emit", key="webhook", store="config.json:emit.webhook", env="SPENDGUARD_WEBHOOK", default=None,
         kind="url|null", secret=False,
         desc="POST each gated event as JSON to this URL (Slack, your collector, …). null = off."),
    dict(section="emit", key="otel", store="config.json:emit.otel", env="SPENDGUARD_OTEL", default=False,
         kind="bool", secret=False,
         desc="Emit an OpenTelemetry cost counter per event (needs opentelemetry-sdk)."),

    # ── email (daily report delivery) ──
    dict(section="email", key="provider", store="email.json:provider", env="SPENDGUARD_EMAIL_PROVIDER", default=None,
         kind="enum:resend,smtp|null", secret=False,
         desc="Email backend for the daily report. null = no email (report still prints + delivers in-app)."),
    dict(section="email", key="to", store="email.json:to", env="SPENDGUARD_EMAIL_TO", default=None,
         kind="email", secret=False, desc="Report recipient address."),
    dict(section="email", key="from_", store="email.json:from_", env="SPENDGUARD_EMAIL_FROM", default=None,
         kind="email", secret=False,
         desc="Sender. A verified domain address, or onboarding@resend.dev to self-send (lands in spam)."),
    dict(section="email", key="api_key", store="email.json:api_key", env="SPENDGUARD_RESEND_KEY", default=None,
         kind="string", secret=True, desc="Resend API key (re_…) when provider=resend."),
    dict(section="email", key="host", store="email.json:host", env="SPENDGUARD_SMTP_HOST", default=None,
         kind="string", secret=False, desc="SMTP host (e.g. smtp.gmail.com) when provider=smtp."),
    dict(section="email", key="user", store="email.json:user", env="SPENDGUARD_SMTP_USER", default=None,
         kind="string", secret=False, desc="SMTP username when provider=smtp."),
    dict(section="email", key="password", store="email.json:password", env="SPENDGUARD_SMTP_PASS", default=None,
         kind="string", secret=True, desc="SMTP app password when provider=smtp."),

    # ── call context log (cost + quality corpus) ──
    dict(section="calls", key="enabled", store="config.json:calls.enabled", env="SPENDGUARD_CALLS", default=False,
         kind="bool", secret=False,
         desc="Record per-call context (caller, intent, cost, quality) to the SQLite calls table. Off by default."),
    dict(section="calls", key="store_prompts", store="config.json:calls.store_prompts", env=None, default=False,
         kind="bool", secret=False,
         desc="Also store prompt/output SNIPPETS — enables implicit 'used' detection + optimize. Privacy-sensitive."),
    dict(section="calls", key="snippet_len", store="config.json:calls.snippet_len", env=None, default=200,
         kind="int", secret=False,                     # a CHARACTER COUNT; float invited a fractional cap
         desc="Max characters of prompt/output snippet to store."),

    # ── de-identification (client-side redaction of the text that leaves on the opt-in sync paths) ──
    dict(section="deid", key="engine", store="config.json:deid.engine", env="SPENDGUARD_DEID_ENGINE",
         default="regex", kind="enum:regex,presidio,off", secret=False,
         desc="Redact PII/PHI from the text that leaves this machine (insight abstracts, work summaries, commit "
              "subjects). regex = built-in deterministic floor (email/phone/SSN/credit-card/IP/keys/$, zero deps); "
              "presidio = floor + Microsoft Presidio NER for names/locations/dates (needs `pip install "
              "llm-spendguard[deid]`; falls back to the floor if absent); off = NO redaction (footgun, trusted data only)."),
    dict(section="deid", key="entities", store="config.json:deid.entities", env="SPENDGUARD_DEID_ENTITIES", default=None,
         kind="string|null", secret=False,
         desc="Comma-list restricting which entity types are redacted (e.g. EMAIL,PHONE,SSN,API_KEY). null = all."),

    # ── saas / team roll-up (client seam — points at the FUTURE separate server repo, llmspendguard.com) ──
    # ONE key is the identity: the server resolves user→team→org hierarchy from it. The client holds no ids.
    dict(section="saas", key="enabled", store="saas.json:enabled", env="SPENDGUARD_SAAS", default=False,
         kind="bool", secret=False,
         desc="Sync this machine's ledger/insights to a spendguard server for team/org roll-up. Off until the server exists."),
    dict(section="saas", key="url", store="saas.json:url", env="SPENDGUARD_SAAS_URL", default=None,
         kind="url|null", secret=False,
         desc="Base URL of the spendguard server (e.g. https://llmspendguard.com). The server is a SEPARATE repo."),
    dict(section="saas", key="api_key", store="saas.json:api_key", env="SPENDGUARD_SAAS_KEY", default=None,
         kind="string", secret=True,
         desc="Your spendguard server key (Bearer). The SERVER maps this key to your user/team/org — the client "
              "stores no team_id/org_id. Secret — env or saas.json only."),
    dict(section="saas", key="visibility", store="saas.json:visibility", env="SPENDGUARD_VISIBILITY", default="private",
         kind="enum:private,team,org", secret=False,
         desc="How far this user's SCRUBBED insights/spend roll up. private = nothing leaves. Partner, not supervisor."),
    dict(section="saas", key="sync_interval", store="saas.json:sync_interval", env="SPENDGUARD_SYNC_INTERVAL",
         default="daily", kind="enum:off,hourly,daily,weekly", secret=False,
         desc="How often `saas sync --if-due` (and the daily report) push the roll-up. off = manual only."),
    dict(section="saas", key="contributor", store="saas.json:contributor", env="SPENDGUARD_CONTRIBUTOR", default=None,
         kind="string|null", secret=False,
         desc="Who this install attributes spend to (member_ref) for per-user → team → org roll-up + billing. Use "
              "your ORG EMAIL (recommended — maps you to your SaaS member AND lets the server email you alerts). "
              "Leave blank to fall back to git user.email, then a stable auto-generated anonymous id (usr_…) — "
              "spend is never unattributed, but alerts need a real email."),
    dict(section="saas", key="project", store="saas.json:project", env="SPENDGUARD_PROJECT", default=None,
         kind="string|null", secret=False,
         desc="Project tag for this repo's charges (the WHAT, next to org/team/user). The roll-up push only sends "
              "rows for this project, so one machine's ledger can feed multiple orgs. Defaults to the git repo name."),

    # ── pricing ──
    dict(section="pricing", key="prices_override", store="env", env="SPENDGUARD_PRICES", default=None,
         kind="path|null", secret=False, desc="Path to a custom prices.json/.yaml override (highest precedence)."),

    # ── provider API keys (for `compare` and pricing those providers' calls) ──
    dict(section="keys", key="OPENAI_API_KEY", store="env", env="OPENAI_API_KEY", default=None, kind="string", secret=True, desc="OpenAI."),
    dict(section="keys", key="ANTHROPIC_API_KEY", store="env", env="ANTHROPIC_API_KEY", default=None, kind="string", secret=True, desc="Anthropic."),
    dict(section="keys", key="GEMINI_API_KEY", store="env", env="GEMINI_API_KEY", default=None, kind="string", secret=True, desc="Gemini (compare)."),
    dict(section="keys", key="DEEPSEEK_API_KEY", store="env", env="DEEPSEEK_API_KEY", default=None, kind="string", secret=True, desc="DeepSeek (compare)."),
    dict(section="keys", key="DASHSCOPE_API_KEY", store="env", env="DASHSCOPE_API_KEY", default=None, kind="string", secret=True, desc="Qwen / Alibaba Model Studio (compare)."),
    dict(section="keys", key="ZAI_API_KEY", store="env", env="ZAI_API_KEY", default=None, kind="string", secret=True, desc="z.ai / Zhipu (GLM family: glm-5.x, glm-4.x) — OpenAI-compatible; rates from the synced breadth layer."),
    dict(section="keys", key="MOONSHOT_API_KEY", store="env", env="MOONSHOT_API_KEY", default=None, kind="string", secret=True, desc="Moonshot AI / Kimi (kimi-k2.x, kimi-latest, and future kimi-* ids) — OpenAI-compatible; rates from the synced breadth layer. Mainland-China accounts: register_provider() with api.moonshot.cn."),

    # ── remote-compute keys (metered into the same ledger; go in keys.env like the LLM keys) ──
    dict(section="keys", key="VAST_API_KEY", store="env", env="VAST_API_KEY", default=None, kind="string", secret=True, desc="Vast.ai remote GPU compute — meters vast.ai spend into the same ledger."),
    dict(section="keys", key="RUNPOD_API_KEY", store="env", env="RUNPOD_API_KEY", default=None, kind="string", secret=True,
         desc="RunPod remote GPU compute — meters pod spend (RunPod's own costPerHr via GraphQL myself.pods) into the same ledger."),
    dict(section="keys", key="MODAL_TOKEN_ID", store="env", env="MODAL_TOKEN_ID", default=None, kind="string", secret=True,
         desc="Modal token id (pairs with MODAL_TOKEN_SECRET) — meters Modal's workspace billing report into the same ledger."),
    dict(section="keys", key="MODAL_TOKEN_SECRET", store="env", env="MODAL_TOKEN_SECRET", default=None, kind="string", secret=True,
         desc="Modal token secret (pairs with MODAL_TOKEN_ID)."),
    dict(section="keys", key="LAMBDA_API_KEY", store="env", env="LAMBDA_API_KEY", default=None, kind="string", secret=True,
         desc="Lambda (lambdalabs.com GPU cloud) — lists instances + their provider-priced $/hr into the same ledger."),
]


def sections():
    out = {}
    for s in SETTINGS:
        out.setdefault(s["section"], []).append(s)
    return out
