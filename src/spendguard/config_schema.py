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
    dict(section="caps", key="compute.daily", store="config.json:caps.compute.daily", env="GATE_COMPUTE_DAILY", default=None,
         kind="float|null", secret=False, desc="DAILY remote-compute (vast.ai GPU) sub-cap ($) — alert/soft. null = off."),
    dict(section="caps", key="compute.monthly", store="config.json:caps.compute.monthly", env="GATE_COMPUTE_MONTHLY", default=None,
         kind="float|null", secret=False, desc="MONTHLY remote-compute sub-cap ($) — alert/soft. null = off."),
    dict(section="caps", key="meta", store="config.json:caps.meta", env="GATE_META_BUDGET", default=2.0,
         kind="float", secret=False,
         desc="Daily $ cap for spendguard's OWN advisor LLM use (intent spendguard:*) — separate from workload caps."),

    # ── gate enforcement: the estimate → test → run rail for big batches ──
    dict(section="gate", key="enforce", store="config.json:gate.enforce", env="SPENDGUARD_ENFORCE", default="warn",
         kind="enum:off,warn,block", secret=False,
         desc="Test-first enforcement for batches over the size threshold (the estimate → test → run sequence): "
              "off = no requirement; warn = log a 'would-block' when a batch runs without a fresh estimate+test "
              "(default); block = hard-require a fresh estimate → test before the batch runs."),

    # ── learning advisor (Layer 2 — its own LLM use, caged by caps.meta + intent spendguard:*) ──
    dict(section="advisor", key="model", store="config.json:advisor.model", env="SPENDGUARD_ADVISOR_MODEL",
         default="claude-opus-4-8", kind="string", secret=False,
         desc="Model for the advisor's REASONING (insight synthesis + `optimize`). Realtime; must exist in pricing.py."),
    dict(section="advisor", key="judge_model", store="config.json:advisor.judge_model", env="SPENDGUARD_ADVISOR_JUDGE_MODEL",
         default="claude-haiku-4-5", kind="string", secret=False,
         desc="Model for BULK quality reconstruction/judging. Batch API; must exist in pricing.py."),

    # ── budget backend ──
    dict(section="budget", key="backend", store="config.json:budget.backend", env=None, default="memory",
         kind="enum:memory,sqlite", secret=False,
         desc="memory = per-process real-time cap; sqlite = cross-process daily/monthly caps (a shared ledger)."),
    dict(section="budget", key="db_path", store="config.json:budget.db_path", env=None, default="<home>/spend.db",
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
         kind="float", secret=False, desc="Max characters of prompt/output snippet to store."),

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
    dict(section="keys", key="ZAI_API_KEY", store="env", env="ZAI_API_KEY", default=None, kind="string", secret=True, desc="z.ai / Zhipu (GLM models, e.g. glm-5.2) — OpenAI-compatible."),

    # ── remote-compute key (metered into the same ledger; goes in keys.env like the LLM keys) ──
    dict(section="keys", key="VAST_API_KEY", store="env", env="VAST_API_KEY", default=None, kind="string", secret=True, desc="Vast.ai remote GPU compute — meters vast.ai spend into the same ledger."),
]


def sections():
    out = {}
    for s in SETTINGS:
        out.setdefault(s["section"], []).append(s)
    return out
