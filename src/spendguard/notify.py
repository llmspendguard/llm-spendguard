"""Email delivery for spendguard reports — Resend API or SMTP.

Config via ~/.spendguard/email.json (gitignored) or env — never commit secrets.

Resend (recommended; no app-password/Workspace issues):
  {"provider": "resend", "to": "you@x.com", "from_": "reports@your-verified-domain",
   "api_key": "re_..."}
  env: SPENDGUARD_EMAIL_PROVIDER=resend, SPENDGUARD_RESEND_KEY=re_..., SPENDGUARD_EMAIL_FROM, SPENDGUARD_EMAIL_TO

SMTP:
  {"host": "smtp.gmail.com", "port": 587, "user": "you@x.com", "password": "<app pw>",
   "from_": "you@x.com", "to": "you@x.com"}
  env: SPENDGUARD_SMTP_HOST/_PORT/_USER/_PASS, SPENDGUARD_EMAIL_FROM/_TO
"""
import json
import smtplib
import urllib.request
import urllib.error
from email.message import EmailMessage

from . import config


def _recipients(to):
    return [t.strip() for t in str(to).split(",") if t.strip()]


def _send_resend(subject, body, to, cfg):
    key = cfg.get("api_key")
    if not key or key.startswith("re_PASTE"):
        raise RuntimeError("no Resend api_key (set in ~/.spendguard/email.json or SPENDGUARD_RESEND_KEY)")
    frm = cfg.get("from_") or "onboarding@resend.dev"
    payload = json.dumps({"from": frm, "to": _recipients(to), "subject": subject, "text": body}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json",
                 # api.resend.com is behind Cloudflare, which 403s urllib's default UA ("error code: 1010").
                 "User-Agent": "spendguard/0.1 (+https://github.com/llmspendguard/llm-spendguard)"})
    try:
        with urllib.request.urlopen(req, context=config.ssl_context(), timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        if "1010" in detail or "cloudflare" in detail.lower():
            hint = " — Cloudflare blocked the request (User-Agent); upgrade spendguard."
        elif e.code == 403:
            hint = (" — to send to an address other than your Resend signup email, verify a domain at "
                    "resend.com/domains and set from_ to an address on it.")
        else:
            hint = ""
        raise RuntimeError(f"Resend HTTP {e.code}: {detail}{hint}")
    return to


def _send_smtp(subject, body, to, cfg):
    host = cfg.get("host")
    if not host:
        raise RuntimeError("no SMTP host (set SPENDGUARD_SMTP_HOST or ~/.spendguard/email.json)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from_") or cfg.get("user") or to
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(host, int(cfg.get("port", 587)), timeout=30) as s:
        s.starttls(context=config.ssl_context())
        if cfg.get("user") and cfg.get("password"):
            s.login(cfg["user"], cfg["password"])
        s.send_message(msg)
    return to


def is_configured(cfg=None):
    """True if a usable email backend is set up (so report can distinguish 'not set up'
    from 'tried and failed')."""
    cfg = config.email_config() if cfg is None else cfg
    provider = cfg.get("provider") or ("resend" if cfg.get("api_key") else ("smtp" if cfg.get("host") else None))
    if provider == "resend":
        key = str(cfg.get("api_key") or "")
        return bool(key) and not key.startswith("re_PASTE")
    if provider == "smtp":
        return bool(cfg.get("host"))
    return False


def send_email(subject, body, to=None, cfg=None):
    cfg = dict(cfg or config.email_config())
    to = to or cfg.get("to")
    if not to:
        raise RuntimeError("no recipient (set SPENDGUARD_EMAIL_TO, ~/.spendguard/email.json, or --email-to)")
    provider = cfg.get("provider") or ("resend" if cfg.get("api_key") else "smtp")
    if provider == "resend":
        return _send_resend(subject, body, to, cfg)
    return _send_smtp(subject, body, to, cfg)
