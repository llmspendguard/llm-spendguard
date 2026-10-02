# Hosting `spendguard serve` — the gated ask + embed surface

The Vercel SaaS server (`llm-spendguard-server`) routes its LLM calls here so they're **estimate-first + capped +
ledgered**, and the provider keys live **here** (as secrets), not in Vercel. This directory deploys `spendguard serve`
to **Fly.io**; the `Dockerfile` is portable, so Railway / Render / a VPS work the same way (only the platform config
differs).

- `POST /embed` — gated embeddings (`text-embedding-3-small` by default), the path the server's `embed.ts` calls.
- `POST /ask` — gated chat, the path the server's `llm.ts` calls (`vendors=["anthropic:<model>"], mode="first"`).
- `GET /health` — `{ok, version}` (requires the Bearer token, like every route except the network-bind guard).

Requires **llm-spendguard ≥ 0.12.5** (first release with `/embed`); the Dockerfile pins it.

## One-time provision (you run these — needs your Fly account + `flyctl`)

From this directory:

```bash
# 1. Create the app (don't deploy yet) — pick a name + region.
fly launch --no-deploy --copy-config --name <your-app>

# 2. Persistent volume for the SQLite ledger (same region as the app).
fly volumes create spendguard_data --size 1 --region <region>

# 3. Secrets: a fresh serve token + the SSOT keys (read straight from your keys.env — no hand-typing).
TOKEN="$(openssl rand -hex 32)"; echo "SPENDGUARD_SERVE_TOKEN=$TOKEN   # <- save this; it goes in Vercel"
OPENAI_KEY="$(python3 -c "import os;[print(l.split('=',1)[1].strip().strip(chr(34)).strip(chr(39)),end='') for l in open(os.path.expanduser('~/.spendguard/keys.env')) if l.strip().startswith('OPENAI_API_KEY=')]")"
ANTHROPIC_KEY="$(python3 -c "import os;[print(l.split('=',1)[1].strip().strip(chr(34)).strip(chr(39)),end='') for l in open(os.path.expanduser('~/.spendguard/keys.env')) if l.strip().startswith('ANTHROPIC_API_KEY=')]")"
fly secrets set SPENDGUARD_SERVE_TOKEN="$TOKEN" OPENAI_API_KEY="$OPENAI_KEY" ANTHROPIC_API_KEY="$ANTHROPIC_KEY"

# 4. Deploy.
fly deploy
```

Your surface is now `https://<your-app>.fly.dev`. Keep the `TOKEN` — the server needs it.

## Wire the server to it

In the Vercel `llm-spendguard-server` project (after PR C lands), set:
- `SPENDGUARD_URL=https://<your-app>.fly.dev`
- `SPENDGUARD_SERVE_TOKEN=<the TOKEN above>`

and **remove `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` from Vercel** — the keys now live only on this host. (If the server
can't reach this surface, it degrades to FTS / sources-only; it never calls a provider ungated.)

## Verify

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://<your-app>.fly.dev/health
curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"texts":["hello world"]}' https://<your-app>.fly.dev/embed      # -> {"vectors":[[...]],"model":"text-embedding-3-small",...}
```

## Keep the ledger unified

The container records spend to its own SQLite on the volume. To roll it into the org dashboard (llmspendguard.com),
run `spendguard saas sync` from the host on a schedule — e.g. a Fly **scheduled machine** (cron) that execs
`spendguard saas sync` daily. Until that's wired, the host's spend is captured locally + on the unified OpenAI/Anthropic
account (so provider-truth reconcile still sees it); the scheduled push just makes it appear per-call in the org roll-up.

## Cost / ops notes

- `fly.toml` suspends to zero when idle (cheapest; a brief cold start on the first request after idle). For a hot search
  path, set `min_machines_running = 1` (always warm). The ledger volume is unaffected either way.
- Rotate the token: `fly secrets set SPENDGUARD_SERVE_TOKEN="$(openssl rand -hex 32)"` here, then update Vercel.
- Logs: `fly logs`. The server never logs request bodies (a prompt could ride the path/query), so logs are safe to share.
