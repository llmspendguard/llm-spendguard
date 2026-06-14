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
    dict(section="caps", key="daily", store="config.json:caps.daily", env=None, default=None,
         kind="float|null", secret=False,
         desc="Cross-process DAILY spend cap ($). null = off. Requires budget.backend=sqlite."),
    dict(section="caps", key="monthly", store="config.json:caps.monthly", env=None, default=None,
         kind="float|null", secret=False,
         desc="Cross-process MONTHLY spend cap ($). null = off. Requires budget.backend=sqlite."),

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

    # ── pricing ──
    dict(section="pricing", key="prices_override", store="env", env="SPENDGUARD_PRICES", default=None,
         kind="path|null", secret=False, desc="Path to a custom prices.json/.yaml override (highest precedence)."),

    # ── provider API keys (for `compare` and pricing those providers' calls) ──
    dict(section="keys", key="OPENAI_API_KEY", store="env", env="OPENAI_API_KEY", default=None, kind="string", secret=True, desc="OpenAI."),
    dict(section="keys", key="ANTHROPIC_API_KEY", store="env", env="ANTHROPIC_API_KEY", default=None, kind="string", secret=True, desc="Anthropic."),
    dict(section="keys", key="GEMINI_API_KEY", store="env", env="GEMINI_API_KEY", default=None, kind="string", secret=True, desc="Gemini (compare)."),
    dict(section="keys", key="DEEPSEEK_API_KEY", store="env", env="DEEPSEEK_API_KEY", default=None, kind="string", secret=True, desc="DeepSeek (compare)."),
    dict(section="keys", key="DASHSCOPE_API_KEY", store="env", env="DASHSCOPE_API_KEY", default=None, kind="string", secret=True, desc="Qwen / Alibaba Model Studio (compare)."),
]


def sections():
    out = {}
    for s in SETTINGS:
        out.setdefault(s["section"], []).append(s)
    return out
