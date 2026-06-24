"""Realign the taxonomy to the real org → team → project structure (ventures under ensight, not flattened as
orgs/projects under Healiom/engineering). Edits ~/.spendguard/config.json `chat.taxonomy` (the local canonical;
also push to the server with setTaxonomy so the dashboard matches). Idempotent + prints before/after. NO spend —
this only restructures the menu the classifier picks from; the re-classify (paid, caged) comes after.

Changes:
  • orgs: drop llm-spendguard / manga2anime / omega / cothynx as ORGS; add `ensight`. keep Healiom, Personal.
  • teams: add ensight ventures llm-spendguard[launched] / manga2anime[forming] / goedel-warden[incubating,
    housed_in lmm/goedel-warden]; add Healiom team `lmm`; lowercase any 'Ensight' → 'ensight'.
  • projects: re-home the venture projects to ensight + their team; rename omega→goedel-warden; drop cothynx (empty).
"""
import json
import sys
from pathlib import Path

CFG = Path.home() / ".spendguard" / "config.json"
VENTURE_TEAM = {"llm-spendguard": "llm-spendguard", "llmseg": "llm-spendguard",
                "manga2anime": "manga2anime", "omega": "goedel-warden"}     # old project/org → ensight team
RENAME_PROJECT = {"omega": "goedel-warden"}
DROP_ORGS = {"llm-spendguard", "manga2anime", "omega", "cothynx"}
ENSIGHT_TEAMS = [
    {"name": "llm-spendguard", "org": "ensight", "stage": "launched",
     "hints": "spendguard cost gate, receipts, reconcile, the SaaS dashboard (llmspendguard.com), pricing"},
    {"name": "manga2anime", "org": "ensight", "stage": "forming",
     "hints": "anime pipeline, SAM3/3D segmentation, captioning, dataset, vast.ai training, dynamics model"},
    {"name": "goedel-warden", "org": "ensight", "stage": "incubating", "housed_in": "lmm/goedel-warden",
     "hints": "asset/knowledge governance, catalog as a control layer, CodeAlphaGraph, inventory/trust/gate, warden"},
]
HEALIOM_LMM = {"name": "lmm", "org": "Healiom",
               "hints": "LMM port, DDX engine, medical taxonomy, concept model, SNOMED/UMLS/ICD, prevalence ladder"}
# an `other` catch-all per org — work that's forming/early and doesn't fit a defined team yet (graduates later)
OTHER_TEAMS = [{"name": "other", "org": o, "hints": "work that does not fit a specific team yet — forming, early, "
                "exploratory, or cross-cutting; use this rather than forcing a wrong team or leaving it blank"}
               for o in ("Healiom", "ensight", "Personal")]


def realign(t):
    orgs = [o for o in (t.get("orgs") or []) if o not in DROP_ORGS]
    if "ensight" not in orgs:
        orgs.append("ensight")
    t["orgs"] = orgs

    teams = []
    seen = set()
    for tm in (t.get("teams") or []):
        org = "ensight" if (tm.get("org") or "").lower() == "ensight" else tm.get("org")
        key = (tm.get("name"), org)
        if key in seen:
            continue
        seen.add(key)
        teams.append({**tm, "org": org})
    for extra in ([HEALIOM_LMM] + ENSIGHT_TEAMS + OTHER_TEAMS):
        if (extra["name"], extra["org"]) not in seen:
            teams.append(extra); seen.add((extra["name"], extra["org"]))
    t["teams"] = teams

    projects = []
    for p in (t.get("projects") or []):
        org = (p.get("org") or "")
        if org in DROP_ORGS and org not in VENTURE_TEAM and p.get("project", p.get("name")) not in VENTURE_TEAM:
            # org is a venture but neither name maps → drop only the truly-empty cothynx
            if org == "cothynx":
                continue
        name = p.get("name") or p.get("project") or ""
        if org == "cothynx" or name == "cothynx" or name == "cothynx-platform":
            continue                                        # empty — drop
        team = VENTURE_TEAM.get(org) or VENTURE_TEAM.get(name)
        if team:                                            # a venture project → re-home under ensight
            p = {**p, "org": "ensight", "team": team, "name": RENAME_PROJECT.get(name, name)}
        projects.append(p)
    t["projects"] = projects
    return t


def main():
    cfg = json.loads(CFG.read_text())
    taxo = (cfg.get("chat") or {}).get("taxonomy")
    if not isinstance(taxo, dict):
        print("no chat.taxonomy dict to realign"); return 1
    before = {"orgs": list(taxo.get("orgs") or []),
              "teams": [(x.get("name"), x.get("org")) for x in (taxo.get("teams") or [])]}
    taxo = realign(taxo)
    cfg.setdefault("chat", {})["taxonomy"] = taxo
    if "--apply" in sys.argv:
        CFG.write_text(json.dumps(cfg, indent=1))
        print("APPLIED to", CFG)
    else:
        print("DRY-RUN (pass --apply to write). Result:")
    print("  orgs:", taxo["orgs"])
    print("  teams:")
    for x in taxo["teams"]:
        tag = f"  [stage:{x['stage']}{(' · '+x['housed_in']) if x.get('housed_in') else ''}]" if x.get("stage") else ""
        print(f"     {x.get('name'):<18} {x.get('org')}{tag}")
    print(f"  projects: {len(taxo['projects'])} (ventures re-homed under ensight; omega→goedel-warden; cothynx dropped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
