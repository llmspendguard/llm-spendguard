"""Align the client-side connection configs to the agreed org → team model (after the server folded the venture
ORGS into ensight as TEAMS and re-pointed their keys to ensight). Without this, a venture repo's `.spendguard.json`
still says org=manga2anime while its api_key now resolves to ensight on the server — inconsistent.

Fixes (idempotent, dry-by-default — pass --apply):
  • each venture repo `.spendguard.json`: org (manga2anime|llm-spendguard|omega|cothynx) → ensight, team → the
    venture name (goedel-warden for omega), contributor → the org's identity. project is left as-is.
  • the Healiom lmm repo: team → lmm (org already Healiom).
  • global ~/.spendguard/saas.json `identities`: collapse the stale per-venture entries to the real cross-org map
    {Healiom: ash@healiom.com, ensight: ash@ensight.ai, Personal: ash@ensight.ai}.

Detection is by the CURRENT org value (not hardcoded paths beyond the known repo roots), so re-running is a no-op.
"""
import json
import sys
from pathlib import Path

# venture org name → (ensight team name). omega is the old name for goedel-warden; cothynx was a dropped exploration.
VENTURE_TEAM = {"manga2anime": "manga2anime", "llm-spendguard": "llm-spendguard",
                "llmseg": "llm-spendguard", "omega": "goedel-warden", "cothynx": "other"}
IDENTITIES = {"Healiom": "ash@healiom.com", "ensight": "ash@ensight.ai", "Personal": "ash@ensight.ai"}
# known connection configs on this machine (the only ones that exist); each is fixed only if it needs it
REPO_CONFIGS = [
    Path("/Users/ashdamle/Documents/animepipe/manga2anime/.spendguard.json"),
    Path("/Users/ashdamle/Documents/claude/llm-spendguard/.spendguard.json"),
    Path("/Users/ashdamle/Documents/claude/lmm/.spendguard.json"),
]
SAAS = Path.home() / ".spendguard" / "saas.json"


def fix_repo(cfg):
    """Return (changed, before, after) for a single connection dict — pure."""
    org = (cfg.get("org") or "").strip()
    before = {"org": org, "team": cfg.get("team"), "contributor": cfg.get("contributor")}
    if org in VENTURE_TEAM:                                  # a venture org → ensight + its team
        cfg["org"] = "ensight"
        cfg["team"] = VENTURE_TEAM[org]
        cfg["contributor"] = IDENTITIES["ensight"]
    elif org == "Healiom" and not (cfg.get("team") or "").strip():
        cfg["team"] = "lmm"                                  # the only Healiom repo here is lmm
    after = {"org": cfg.get("org"), "team": cfg.get("team"), "contributor": cfg.get("contributor")}
    return before != after, before, after


def main():
    apply = "--apply" in sys.argv
    print("DRY-RUN (pass --apply to write)\n" if not apply else "APPLYING\n")
    for path in REPO_CONFIGS:
        if not path.exists():
            print(f"  {path}: (missing, skip)"); continue
        cfg = json.loads(path.read_text())
        changed, before, after = fix_repo(cfg)
        tag = "CHANGED" if changed else "ok"
        print(f"  {path.parent.name}/.spendguard.json [{tag}]")
        if changed:
            print(f"     org {before['org']!r}→{after['org']!r}  team {before['team']!r}→{after['team']!r}  "
                  f"contributor {before['contributor']!r}→{after['contributor']!r}")
            if apply:
                path.write_text(json.dumps(cfg, indent=1))
    # global identities map
    if SAAS.exists():
        s = json.loads(SAAS.read_text())
        cur = s.get("identities")
        if cur != IDENTITIES:
            print(f"\n  saas.json identities [CHANGED]\n     {cur}\n     → {IDENTITIES}")
            if apply:
                s["identities"] = IDENTITIES
                SAAS.write_text(json.dumps(s, indent=1))
        else:
            print("\n  saas.json identities [ok]")
    print("\n" + ("✓ applied" if apply else "dry-run only"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
