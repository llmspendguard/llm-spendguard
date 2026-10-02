# spendguard serve — a gated cross-LLM ask + embed surface for non-Python callers (e.g. the Vercel SaaS server).
#
# The gate patches the OpenAI/Anthropic SDKs at `import spendguard`, so a plain install ENFORCES; `spendguard serve`
# calls spendguard.require() and FAILS CLOSED if this interpreter somehow isn't gated. Provider keys + the Bearer
# token arrive as runtime env (set them as Fly secrets — never baked into the image); the SQLite ledger persists on a
# mounted volume at $SPENDGUARD_HOME. A non-loopback bind (0.0.0.0) REFUSES to start without SPENDGUARD_SERVE_TOKEN —
# that guard is the whole point, so the token is required, not optional.
FROM python:3.12-slim

# [all] brings the OpenAI + Anthropic SDKs the gate patches. >=0.12.5 is the first release with POST /embed.
RUN pip install --no-cache-dir "llm-spendguard[all]>=0.12.5"

ENV SPENDGUARD_HOME=/data/.spendguard \
    SPENDGUARD_SERVE_PORT=8787

EXPOSE 8787

# exec form via sh so $SPENDGUARD_HOME / $SPENDGUARD_SERVE_TOKEN expand; `exec` keeps spendguard as PID 1 (clean signals).
CMD ["sh", "-c", "mkdir -p \"$SPENDGUARD_HOME\" && exec spendguard serve --host 0.0.0.0 --port \"${SPENDGUARD_SERVE_PORT:-8787}\""]
