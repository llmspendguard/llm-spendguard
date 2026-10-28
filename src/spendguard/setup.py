"""`spendguard config` and `spendguard init` — both generated from config_schema.SETTINGS,
so they always match the code. `config` shows resolved values + where each came from; `init`
runs the interview and writes ~/.spendguard/config.json (+ email.json), leaving API keys to env.
"""
import os, json
from . import config, config_schema


# THE MARK THAT SAYS WE WROTE THIS FILE. Ownership used to be tested with `"spendguard" in contents`,
# which any file merely MENTIONING spendguard satisfies — including a user's own sitecustomize with a
# comment about it. --uninstall then deleted a hook we never created, and the install path treated it as
# ours to overwrite. A distinctive marker we emit ourselves is the only honest test of authorship, and
# deleting someone else's interpreter hook is not something they can recover from here.
_SENTINEL = "spendguard-gate-hook/v1"
_PTH_SENTINEL = "spendguard-gate-hook/v2"
_PTH_NAME = "spendguard_gate_hook.pth"
_FILEPROVIDER_XATTR_PREFIX = "com.apple.fileprovider."
_GATED_SDKS = ("openai", "anthropic", "httpx")
_STARTUP_GATE_PROBE_HOME_PREFIX = "spendguard-startup-probe-"
_SPENDGUARD_HOME_ENVIRONMENT_VARIABLE = "SPENDGUARD_HOME"
_EDITABLE_METADATA_PROBE = (
    "import importlib.metadata as m,json;"
    "print(int(any((d.metadata.get('Name') or '').lower().replace('_','-')=='llm-spendguard' "
    "and (lambda u: bool(u and json.loads(u).get('dir_info',{}).get('editable')))"
    "(d.read_text('direct_url.json')) for d in m.distributions())))")
_EDITABLE_INSTALL_OPTIONS = ("--no-cache-dir",)
_WHEEL_UPGRADE_OPTION = "-U"
_WHEEL_NO_INDEX_OPTION = "--no-index"
_WHEEL_FIND_LINKS_OPTION = "--find-links"
_SPENDGUARD_DISTRIBUTION_NAME = "llm-spendguard"
_PEP517_EDITABLE_INSTALL_OPTION = "--use-pep517"
_NO_BUILD_ISOLATION_OPTION = "--no-build-isolation"
_PIP_NO_INDEX_ENVIRONMENT_VARIABLE = "PIP_NO_INDEX"
_PIP_FALSE_ENVIRONMENT_VALUES = frozenset(("", "0", "false", "no", "off"))
_SETUPTOOLS_DISTRIBUTION_NAME = "setuptools"
_SETUPTOOLS_VERSION_PROBE = (
    "import importlib.metadata as m;"
    "import setuptools;"
    "print(m.version('setuptools'))"
)


def purge_shadowing_in_tree_egg_info(repo_root):
    """Remove only stale package metadata contained by this checkout's source root."""
    import shutil
    from pathlib import Path
    source_root = (Path(repo_root) / "src").resolve()
    removed = []
    for candidate in source_root.glob("*.egg-info"):
        resolved = candidate.resolve()
        if not resolved.is_relative_to(source_root):
            continue
        shutil.rmtree(resolved)
        removed.append(str(resolved))
    return removed


def _declared_setuptools_build_requirement(repo_root):
    """Return this checkout's setuptools build requirement from its pyproject SSOT."""
    import tomllib
    from pathlib import Path
    try:
        from packaging.requirements import Requirement
    except ModuleNotFoundError:
        from pip._vendor.packaging.requirements import Requirement

    build_configuration = tomllib.loads((Path(repo_root) / "pyproject.toml").read_text())
    build_requirements = build_configuration["build-system"]["requires"]
    for requirement_text in build_requirements:
        parsed_requirement = Requirement(requirement_text)
        if parsed_requirement.name.lower() == _SETUPTOOLS_DISTRIBUTION_NAME:
            return parsed_requirement
    raise RuntimeError("pyproject.toml build-system.requires does not declare setuptools")


def _consumer_has_satisfying_setuptools(python_executable, setuptools_requirement, timeout):
    """Mechanically verify that the target interpreter can import the declared setuptools version."""
    import subprocess
    probe_result = subprocess.run(
        [str(python_executable), "-c", _SETUPTOOLS_VERSION_PROBE],
        capture_output=True, text=True, timeout=timeout)
    return (probe_result.returncode == 0
            and probe_result.stdout.strip() in setuptools_requirement.specifier)


def _pip_index_is_disabled(environment):
    configured_value = environment.get(_PIP_NO_INDEX_ENVIRONMENT_VARIABLE)
    return (configured_value is not None
            and configured_value.strip().lower() not in _PIP_FALSE_ENVIRONMENT_VALUES)


def _consumer_editable_install_command(python_executable, repo_root, timeout, environment=None):
    """Build the pip argv, reusing a satisfactory target backend or retaining online isolation."""
    setuptools_requirement = _declared_setuptools_build_requirement(repo_root)
    backend_is_importable = _consumer_has_satisfying_setuptools(
        python_executable, setuptools_requirement, timeout)
    if not backend_is_importable and _pip_index_is_disabled(os.environ if environment is None else environment):
        raise RuntimeError(
            "editable build backend unavailable offline: "
            f"{setuptools_requirement} not importable in {python_executable} and no index")
    isolation_options = (_NO_BUILD_ISOLATION_OPTION,) if backend_is_importable else ()
    return [str(python_executable), "-m", "pip", "install", "-e", str(repo_root),
            _PEP517_EDITABLE_INSTALL_OPTION, *isolation_options, *_EDITABLE_INSTALL_OPTIONS]


def refresh_consumer_editable_package(python_executable, repo_root, timeout):
    """Install this checkout into a consumer interpreter; shared by install-hook and fleet promotion."""
    import subprocess
    install_command = _consumer_editable_install_command(python_executable, repo_root, timeout)
    result = subprocess.run(install_command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "pip install failed").strip())
    metadata_probe = subprocess.run([str(python_executable), "-c", _EDITABLE_METADATA_PROBE],
                                    capture_output=True, text=True, timeout=timeout)
    if metadata_probe.returncode == 0 and metadata_probe.stdout.strip() == "1":
        purge_shadowing_in_tree_egg_info(repo_root)


def consumer_wheel_install_command(python_executable, wheel_cache, version):
    """Build the pinned, offline consumer install command."""
    requirement = f"{_SPENDGUARD_DISTRIBUTION_NAME}=={version}"
    return [str(python_executable), "-m", "pip", "install", _WHEEL_UPGRADE_OPTION,
            _WHEEL_NO_INDEX_OPTION, _WHEEL_FIND_LINKS_OPTION, str(wheel_cache), requirement]


def refresh_consumer_wheel_package(python_executable, wheel_cache, version, timeout):
    """Install one cached wheel without consulting an index."""
    import subprocess
    command = consumer_wheel_install_command(python_executable, wheel_cache, version)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "pip wheel install failed").strip())

_FAILCLOSED_FINDER_SOURCE = '''class _SpendguardGateFailclosedFinder:
    _spendguard_gate_failclosed = True

    def __init__(self, gated_modules, gate_failure_reason):
        self._spendguard_gated_modules = gated_modules
        self._spendguard_gate_failure_reason = gate_failure_reason

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == module_name or fullname.startswith(module_name + ".")
               for module_name in self._spendguard_gated_modules):
            raise ImportError(
                f"[spendguard-gate-hook] gate could not arm "
                f"({self._spendguard_gate_failure_reason}); refusing to import "
                f"'{fullname}' UNGATED. Fix the spendguard checkout, or set "
                "GATE_DISABLE=1 to bypass.")
        return None

if not any(getattr(finder, "_spendguard_gate_failclosed", False) for finder in sys.meta_path):
    sys.meta_path.insert(0, _SpendguardGateFailclosedFinder(%(gated_modules)r, str(_e)))
'''


def _resolved_gated_sdk_modules(gated_modules):
    if gated_modules is None:
        from .gate import gated_sdk_modules
        return tuple(gated_sdk_modules())
    return tuple(gated_modules)


def _baked_failclosed_finder_source(gated_modules):
    return _FAILCLOSED_FINDER_SOURCE % {
        "gated_modules": _resolved_gated_sdk_modules(gated_modules),
    }


def _sitecustomize_hook_source(inject_src=None, gated_modules=None):
    injected_source_line = ""
    if inject_src is not None:
        injected_source_line = f"        sys.path.insert(0, {inject_src!r})\n"
    finder_source = "".join(
        f"            {line}\n" if line else "\n"
        for line in _baked_failclosed_finder_source(gated_modules).splitlines()
    )
    return f'''# Auto-installs the spendguard cost gate for every process in this venv (the `spendguard` package).
# spendguard-gate-hook/v1 — this marker is how `install-hook --uninstall` knows the file is ours.
# Kill switches checked HERE first, before the import, so disabling works even if the package is broken:
#   GATE_DISABLE=1 (per-run)  OR  `spendguard off` (persistent flag).  Nuclear option: delete this file.
import os, sys
from pathlib import Path
_home = os.environ.get("SPENDGUARD_HOME") or str(Path.home() / ".spendguard")
if os.environ.get("GATE_DISABLE") != "1" and not os.path.exists(os.path.join(_home, "disabled")):
    try:
{injected_source_line}        import spendguard
        spendguard.install()
    except Exception as _e:  # never let the gate break a process
        sys.stderr.write(f"[sitecustomize] spendguard not installed: {{_e}}\\n")
        try:
{finder_source}        except Exception:
            pass
'''


_HOOK = _sitecustomize_hook_source()

# A .pth executable line is the shadow-proof fallback when an earlier stdlib/site entry owns the
# ``sitecustomize`` import. Keep the kill switches identical to _HOOK. The checkout source directory
# is supplied by the same _pkg_src() derivation used by the editable install, because sitecustomize may
# discard the editable .pth path after site.addpackage processes it.
def _gate_hook_for_checkout_src(checkout_src, gated_modules=None):
    finder_source = "".join(
        f"            {line}\n" if line else "\n"
        for line in _baked_failclosed_finder_source(gated_modules).splitlines()
    )
    hook_source = f'''import sys
from pathlib import Path
_src = {checkout_src!r}
_home = os.environ.get("SPENDGUARD_HOME") or str(Path.home() / ".spendguard")
if os.environ.get("GATE_DISABLE") != "1" and not os.path.exists(os.path.join(_home, "disabled")):
    try:
        if os.path.isdir(_src) and _src not in sys.path:
            sys.path.insert(0, _src)
        import spendguard
        spendguard.install()
    except Exception as _e:
        sys.stderr.write(f"[spendguard-gate-hook] spendguard not installed: {{_e}}\\n")
        try:
{finder_source}        except Exception:
            pass
# {_SENTINEL}
'''
    return f"import os;exec({hook_source!r})  # {_PTH_SENTINEL}\n"

# A portable rule for any AI coding assistant (Claude Code, Cursor, etc.). `install-rule` drops it into a
# CLAUDE.md so EVERY conversation in that project automatically wires spendguard into whatever it builds.
_RULE_BEGIN = "<!-- spendguard:rule:begin -->"
_RULE_END = "<!-- spendguard:rule:end -->"
_RULE = _RULE_BEGIN + """
## LLM Cost Governance — MANDATORY (llm-spendguard)

Any code in this project that calls an LLM or embeddings API (OpenAI **or** Anthropic) MUST go through
**llm-spendguard**. This is a hard rule that OVERRIDES default behavior — never write or run ungated LLM code.

When you WRITE or RUN code that calls an LLM:
1. **Run it under a gated interpreter.** Use a venv whose `sitecustomize.py` loads the gate (or a python whose
   user-site has the spendguard `usercustomize`). Never bare/system `python3` that isn't gated. Verify with
   **`spendguard doctor`** — it must print `ENFORCING HERE: YES`.
2. **Fail closed.** Put `import spendguard; spendguard.require()` at the top of the script. It raises if the
   gate isn't actually enforcing in that interpreter, so a bypass can't run silently.
3. **Never hardcode prices.** Get $/token only from `spendguard.pricing` (or the repo's `pricing.py`).
4. **Estimate before you spend.** For any paid batch, do a SEPARATE zero-spend estimate run (count + $ est),
   confirm, then submit. Never cancel/kill a running job as cost control — completed requests still bill.
5. Prefer the **Batch API** for non-interactive work; keep a per-job cost estimate + approval for large batches.
6. **Surface the receipt.** After substantive LLM/spend work in a turn, show the contextual spend receipt —
   `spendguard receipt` (scoped to THIS repo + its proportional plan share; `--all` expands to every repo). In the
   desktop/web app there is no auto status line, so this is how the running tally stays visible each turn; in a
   terminal the status line does it automatically.
7. **ALWAYS split billed $ from est-value — NEVER sum them (hard rule, for spendguard AND for me in chat).** A single
   summed cost number causes huge confusion. Whenever cost is shown, display the REAL dollars broken into named
   components, then the est-value as a SEPARATE axis after a `::`:
   `Total $X = $A API + $S Subscription (plan name) + $R Remote (provider)  ::  Est sub value $V`
   where **REAL $ = API (per-token billing) + Subscription (flat plan fee: Anthropic Max, OpenAI Pro, …) + Remote
   compute (Vast.ai, …)** — the actual money out the door — and **Est sub value** = the value of subscription-covered
   usage (Claude Code / claude.ai / Codex plan usage), which is NOT billed and is never added into the real total.
   The subscription is BOTH a real cost (its flat fee) AND the source of the est-value (what it delivered) — show both.
   Example: `$3,376 = $2,189 API + $200 Subscription (Anthropic Max) + $987 Remote (Vast.ai)  ::  Est sub value $21K`.
   Per org, the Subscription line is that org's proportional share (by plan-usage). Never collapse to one mixed total.
8. **Prefer lane-routed comprehension over Claude sub-agents (biggest cost lever when the plan is capped).** For
   batchable comprehension / doc-mining / gap-analysis, run a spendguard-gated script that fans the work across your
   $0 subscription lanes — `spendguard comprehend <globs> --intent <job>` (or `spendguard ask`, or a script calling
   `adapters.call(..., reasoning="best-value", intent=...)`) — NOT the coding agent's own sub-agents. A sub-agent runs
   ONLY on the Claude/Anthropic plan: it bills the Max plan (hitting weekly overage) and cannot use the codex / gemini
   / zai plans, whereas a lane-routed script does the same work at $0 on another subscription. Matters most when the
   Claude plan is near its weekly cap (check `spendguard lanes --usage`): spendguard's router already sheds eligible
   tagged intents off an exhausted lane onto the others, but sub-agents never reach that routing.
   **The boundary:** BATCHABLE COMPREHENSION — classify / extract / summarize / gap-find across many files, each an
   INDEPENDENT one-shot prompt — lane-routes; INTERACTIVE code-editing, multi-step tool use, or worktrees do NOT
   (they are not one-shot and can't ride a lane), so a Claude subagent is the right tool THERE. Don't force lane
   routing onto work that needs a real agent. **Auto-route the batchable kind:** set `advisor.default_reasoning`
   to `best-value` (or `SPENDGUARD_DEFAULT_REASONING=best-value`) so a LABELLED `adapters.call(intent=…)` that
   didn't pin a model picks the cheapest (model, effort) whose MEASURED quality held — no per-call opt-in.
   **When the Anthropic plan is CAPPED / on paid overage, move even AGENTIC subwork off it — delegate to another
   provider's own CLI, never a Claude Task subagent.** The Claude Code agent and every Task/Agent subagent it spawns
   run ONLY on the Anthropic plan and NO flag redirects them; the only routes onto another provider are (i) the
   lane-routed one-shot comprehension above, or (ii) handing a SELF-CONTAINED sub-task to another provider's plan. The
   ergonomic front-door is **`spendguard delegate "<task>"`** — it classifies the task on a $0 lane (one-shot vs
   agentic vs needs-Claude), routes an agentic one to that provider's agent CLI ONLY when its lane is READY (else it
   names the ready alternatives, never fail-open into the capped Claude call), and prints a SPLIT receipt
   (real-$ / est-value / saved-vs-overage); `--estimate` dry-runs it, `--yes` executes. Under the hood it rides the
   same provider CLIs you can also call directly — `codex exec "<task>"` (OpenAI plan), `agy` (gemini), `kimi`, `zai`
   — collecting the result so the heavy tokens bill THAT subscription instead of the capped Anthropic plan. While capped, also set
   `SPENDGUARD_BULK_LANES=codex,gemini,zai-coding` so bulk work skips the Anthropic lane entirely; `spendguard lanes
   --usage` shows which lanes have room and whether the plan is on overage.

Setup (one-time): `spendguard install-hook --venv <venv>` (or `--user --python <interp>` for system python),
then `spendguard doctor`. Surface the tally: `spendguard install-receipts` (terminal status line) and this rule
(desktop/web). Kill switch: `GATE_DISABLE=1` or `spendguard off`.
""" + _RULE_END + "\n"


# The assistant-rule files install-rule maintains, one per tool convention: Claude Code reads CLAUDE.md, Codex
# and the emerging cross-tool standard read AGENTS.md, Cursor reads .cursorrules. All are markdown/plaintext, so
# the SAME marked block is valid in each — one rule, every assistant in the project.
_RULE_TARGETS = ("CLAUDE.md", "AGENTS.md", ".cursorrules")


def _content_outside_block(text):
    """The file's content with any managed spendguard block (between the BEGIN/END markers) removed — i.e. the
    USER's own content. Used to assert install-rule changes ONLY its own block and never the user's rules."""
    if _RULE_BEGIN in text and _RULE_END in text:
        pre, rest = text.split(_RULE_BEGIN, 1)
        _, post = rest.split(_RULE_END, 1)
        return pre + post
    return text


def _backup_rule_file(path):
    """Copy an existing rule file into ~/.spendguard/rule_backups/ (keyed by name + timestamp) BEFORE it is
    rewritten, so any accidental loss is RECOVERABLE — the trash+count durability pattern for a text file that
    may hold the user's own hand-written rules. Returns the backup path, or None when there was nothing to save."""
    import shutil
    import time as _t
    if not path.exists():
        return None
    bdir = config.HOME / "rule_backups"
    bdir.mkdir(parents=True, exist_ok=True)
    b = bdir / f"{path.name}.{int(_t.time())}.bak"
    shutil.copy2(path, b)
    return b


def _write_rule_block(path):
    """Idempotently write the marked rule block into ONE file: replace an existing block in place, else append
    below whatever is already there, else create. Returns (action, backup_path).

    NON-DESTRUCTIVE, THREE WAYS. (1) The BEGIN/END markers bound the only region ever rewritten; everything
    outside them is the user's own content, carried verbatim. (2) Before any overwrite the prior file is backed
    up under ~/.spendguard/rule_backups (recoverable). (3) The invariant is ASSERTED before the write: if the
    content outside the managed block would change at all, refuse LOUDLY rather than risk dropping a user's rules."""
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text() if path.exists() else ""
    if _RULE_BEGIN in old and _RULE_END in old:                       # replace the existing block in place
        pre, rest = old.split(_RULE_BEGIN, 1)
        _, post = rest.split(_RULE_END, 1)
        new = pre + _RULE.rstrip("\n") + post
        action = "updated"
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + _RULE
        action = "appended to" if old.strip() else "created"
    if _content_outside_block(new).strip() != _content_outside_block(old).strip():
        raise RuntimeError(f"install_rule: refusing to write {path} — it would change content OUTSIDE the "
                           f"managed spendguard block (the user's own rules). Not writing; this is a bug.")
    backup = _backup_rule_file(path) if new != old else None          # only back up when we are actually changing it
    path.write_text(new)
    return action, backup


def install_rule(target=None, glob_=False):
    """Write the spendguard usage rule into the AI-assistant rule files so EVERY assistant conversation in that
     project auto-wires spendguard into whatever it builds — CLAUDE.md (Claude Code), AGENTS.md (Codex + the
     cross-tool standard), and .cursorrules (Cursor). `--project <dir>` (default: cwd) writes all three; `--global`
     (~/.claude) writes the one file with a standard global home, CLAUDE.md. Idempotent: replaces the marked block
     if present, else appends (never clobbering the user's own content; the prior file is backed up first). Re-run
     after `spendguard` upgrades."""
    from pathlib import Path
    if glob_:
        # Only CLAUDE.md has a standard GLOBAL home (~/.claude); AGENTS.md / .cursorrules are per-project.
        targets = [Path.home() / ".claude" / "CLAUDE.md"]
    else:
        base = Path(target or ".").expanduser().resolve()
        targets = [base / name for name in _RULE_TARGETS]
    for path in targets:
        action, backup = _write_rule_block(path)
        print(f"  ✓ {action} {path}" + (f"  (backed up prior → {backup})" if backup else ""))
    print("  every AI-assistant conversation in this project will now be told to route LLM code through spendguard "
          "(and to prefer $0 lanes over Claude sub-agents for batchable comprehension).")
    return 0


def cmd_install_rule(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard install-rule")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--global", dest="glob_", action="store_true", help="write to ~/.claude/CLAUDE.md (all projects)")
    g.add_argument("--project", help="project dir whose CLAUDE.md to write (default: current dir)")
    a = ap.parse_args(argv)
    return install_rule(a.project, glob_=a.glob_)


def _probe(interp):
    """(version, has_sdks, enforcing|None) for an interpreter. enforcing=None if it can't even run.
    'enforcing' means the gate is ACTUALLY patched onto the OpenAI SDK in that interpreter's startup."""
    import subprocess
    try:
        ver = subprocess.run([interp, "--version"], capture_output=True, text=True, timeout=10
                             ).stdout.strip().split()[-1]
    except Exception:
        return (None, False, None)
    # "has" = the SDK actually IMPORTS (find_spec lies on arch-mismatched installs, e.g. intel pydantic on arm64).
    chk = ("has=False; enf=None\n"
           "try:\n"
           " import openai; has=True\n"
           " from openai.resources import files as of; enf=bool(getattr(of.Files.create,'_spend_gated',False))\n"
           "except Exception: pass\n"
           "if enf is None:\n"
           " try:\n"
           "  import anthropic; has=True\n"
           "  from anthropic.resources.messages import batches as ab; enf=bool(getattr(ab.Batches.create,'_spend_gated',False))\n"
           " except Exception: pass\n"
           "print(int(has),int(bool(enf)))")
    try:
        out = subprocess.run([interp, "-c", chk], capture_output=True, text=True, timeout=20).stdout.strip().split()
        has, enf = bool(int(out[0])), bool(int(out[1]))
    except Exception:
        has, enf = False, False
    return (ver, has, enf)


_DISTRIBUTION_SCAN_DIRECTORY_BUDGET_DEFAULT = 65536


def discover_distribution_interpreters(roots=None, settings=None, extra=None):
    """Union configured on-disk venvs, host installs and registrations by real identity.

    Filesystem structure defines interpreter identity here; no judgement about source meaning or
    LLM use removes a consumer. Resource or discovery failures are explicit attestation gaps.
    """
    import sys
    from pathlib import Path
    from . import consumer_audit, fleet

    resolved_settings = config.consumer_audit_settings() if settings is None else settings
    exemptions = tuple(Path(value).expanduser().absolute()
                       for value in resolved_settings.get("consumer_audit_exempt", ()))
    candidates, gaps = {}, []

    def retain_distribution_interpreter(python, venv=None, kind="framework-python"):
        executable = Path(python).expanduser().absolute().resolve()
        venv_path = Path(venv).expanduser().absolute().resolve() if venv else None
        if venv_path is None and (executable.parent.parent /
                                 consumer_audit.CONSUMER_AUDIT_VENV_CONFIGURATION_NAME).is_file():
            venv_path = executable.parent.parent
        if venv_path is not None:
            executable_paths = (*consumer_audit.CONSUMER_AUDIT_POSIX_EXECUTABLE_PATHS,
                                consumer_audit.CONSUMER_AUDIT_WINDOWS_EXECUTABLE_PATH)
            executable = next((venv_path.joinpath(*parts) for parts in executable_paths
                               if venv_path.joinpath(*parts).is_file()), executable)
            kind = "venv"
        if consumer_audit._consumer_path_is_exempt(None, venv_path or executable, exemptions):
            return
        if not executable.is_file():
            gaps.append({"surface": str(venv_path or executable), "reason": "interpreter missing"})
            return
        identity = str(executable)
        candidates.setdefault(identity, {"python": identity,
                                         "venv": str(venv_path) if venv_path else None, "kind": kind})

    retain_distribution_interpreter(sys.executable,
                                    sys.prefix if sys.prefix != sys.base_prefix else None)
    for discovery_name, discover in (("registered interpreters", fleet.registered_interpreters),
                                    ("host installations", fleet.discover_host_spendguard_installations)):
        try:
            for row in discover():
                retain_distribution_interpreter(row["python_executable"], row.get("venv_root"),
                                                "venv" if row.get("venv_root") else "framework-python")
        except Exception as exc:
            gaps.append({"surface": discovery_name, "reason": f"discovery failed: {exc}"})
    scan_roots = list(resolved_settings.get("workspace_roots", ()) if roots is None else roots)
    scan_roots.extend(resolved_settings.get("workspace_extra_venv_dirs", ()))
    for value in extra or ():
        candidate = Path(value).expanduser().absolute()
        if candidate.is_file():
            retain_distribution_interpreter(candidate)
        else:
            scan_roots.append(candidate)
    directory_budget = int(resolved_settings.get(
        "distribution_scan_directory_budget", config._cfg_get(
            "distribute", "scan_directory_budget", _DISTRIBUTION_SCAN_DIRECTORY_BUDGET_DEFAULT)))
    visited_directory_realpaths = set()
    for root in sorted({Path(value).expanduser().absolute() for value in scan_roots}):
        if consumer_audit._consumer_path_is_exempt(None, root, exemptions):
            continue
        if not root.is_dir():
            gaps.append({"surface": str(root), "reason": "discovery root missing"})
            continue
        visited_directories = 0

        def record_distribution_walk_failure(error, scan_root=root):
            gaps.append({"surface": str(error.filename or scan_root), "reason": f"enumeration failed: {error}"})

        for directory, names, files in os.walk(root, onerror=record_distribution_walk_failure):
            current = Path(directory)
            try:
                current_realpath = current.resolve(strict=True)
            except OSError as error:
                record_distribution_walk_failure(error)
                names[:] = []
                continue
            if current_realpath in visited_directory_realpaths:
                names[:] = []
                continue
            visited_directory_realpaths.add(current_realpath)
            visited_directories += 1
            if visited_directories > directory_budget:
                gaps.append({"surface": str(root), "reason": "enumeration directory budget exhausted"})
                break
            names[:] = sorted(name for name in names if not consumer_audit._consumer_path_is_exempt(
                None, current / name, exemptions))
            linked_directory_names = []
            for name in tuple(names):
                linked_directory = current / name
                if not linked_directory.is_symlink():
                    continue
                linked_directory_names.append(name)
                try:
                    resolved_directory = linked_directory.resolve(strict=True)
                except OSError as error:
                    gaps.append({"surface": str(linked_directory),
                                 "reason": f"symlink resolution failed: {error}"})
                    continue
                configuration = resolved_directory / consumer_audit.CONSUMER_AUDIT_VENV_CONFIGURATION_NAME
                if not configuration.is_file():
                    continue
                executable_paths = (*consumer_audit.CONSUMER_AUDIT_POSIX_EXECUTABLE_PATHS,
                                    consumer_audit.CONSUMER_AUDIT_WINDOWS_EXECUTABLE_PATH)
                executable = next((resolved_directory.joinpath(*parts) for parts in executable_paths
                                   if resolved_directory.joinpath(*parts).is_file()), None)
                if executable is None:
                    gaps.append({"surface": str(resolved_directory), "reason": "venv interpreter missing"})
                else:
                    retain_distribution_interpreter(executable, resolved_directory, "venv")
            names[:] = [name for name in names if name not in linked_directory_names]
            if consumer_audit.CONSUMER_AUDIT_VENV_CONFIGURATION_NAME in files:
                executable_paths = (*consumer_audit.CONSUMER_AUDIT_POSIX_EXECUTABLE_PATHS,
                                    consumer_audit.CONSUMER_AUDIT_WINDOWS_EXECUTABLE_PATH)
                executable = next((current.joinpath(*parts) for parts in executable_paths
                                   if current.joinpath(*parts).is_file()), None)
                if executable is None:
                    gaps.append({"surface": str(current), "reason": "venv interpreter missing"})
                else:
                    retain_distribution_interpreter(executable, current, "venv")
    return {"interpreters": sorted(candidates.values(), key=lambda row: row["python"]), "gaps": gaps}


def coverage(extra=None):
    """Show, across EVERY python on this machine (you use 3.11/3.14/…), which can make LLM calls and which
     are actually GATED. The gate is per-interpreter, so this is how you confirm nothing is silently ungated.
     `spendguard gate-coverage [interp_or_venv ...]` (distinct from `coverage`, which reports per-VENV realtime capture)."""
    discovery = discover_distribution_interpreters(extra=extra)
    rows = []
    failed_probes = []
    for interpreter in discovery["interpreters"]:
        c = interpreter["python"]
        ver, has, enf = _probe(c)
        if ver is None:
            failed_probes.append(c)
            continue
        rows.append((c, ver, has, enf))
    print("spendguard coverage — the gate is PER-INTERPRETER; each python/venv must be gated on its own.\n")
    print(f"  {'interpreter':<52}{'ver':<9}{'LLM SDKs':<10}{'GATED'}")
    gap = list(failed_probes)
    for discovery_gap in discovery["gaps"]:
        print(f"  discovery gap: {discovery_gap['surface']}: {discovery_gap['reason']}")
    for c, ver, has, enf in rows:
        mark = ("🟢 yes" if enf else "🔴 NO") if has else "— n/a"
        print(f"  {c[:51]:<52}{ver:<9}{('yes' if has else 'no'):<10}{mark}")
        if has and not enf:
            gap.append(c)
    print()
    if gap:
        print("  ⚠️ these CAN call LLMs but are NOT gated — gate each:")
        for c in gap:
            if "/.venv/" in c or c.endswith("/.venv/bin/python"):
                print(f"     spendguard install-hook --venv {c.rsplit('/bin/python',1)[0]}")
            else:
                print(f"     spendguard install-hook --user --python {c}")
        print("  (or rely on `import spendguard; spendguard.require()` at the top of the script — fail-closed in ANY interpreter.)")
    else:
        print("  ✓ every interpreter that has the LLM SDKs is gated.")
    return 2 if gap or discovery["gaps"] else 0


def cmd_coverage(argv=None):
    return coverage(list(argv or []))


def cmd_gate_discovered_host(argv=None):
    """Offer or apply user-site gating for every discovered installation missing from the fleet registry."""
    from . import fleet
    apply = "--apply" in (argv or [])
    audit = fleet.reconcile_registered_host_installations()
    failures = []
    for escape in audit["escapes"]:
        interpreter = escape["python_executable"]
        if not apply:
            print(f"registration escape: {interpreter}; fix: {escape['remediation']}")
            continue
        try:
            install_hook(user=True, python=interpreter)
        except Exception as exc:
            failures.append({"python_executable": interpreter, "error": str(exc)})
    if failures:
        for failure in failures:
            print(f"gate-host failed {failure['python_executable']}: {failure['error']}")
        return 1
    return 0 if apply or audit["complete"] else 2


def _interpreter_purelib(target, timeout=10):
    """Ask *target* for the directory it actually imports pure Python packages from; never infer its version."""
    import subprocess
    try:
        r = subprocess.run([target, "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
                           capture_output=True, text=True, check=True, timeout=timeout)
        path = r.stdout.strip()
        return path or None
    except Exception:
        return None


def gate_pth_hidden_diagnosis(target, timeout=10):
    """Describe a macOS-hidden installed gate hook, or return None."""
    import stat
    import sys
    from pathlib import Path

    # This is fixed-format filesystem flag/xattr parsing, not a semantic judgement.
    if sys.platform != "darwin" or not hasattr(stat, "UF_HIDDEN"):
        return None
    purelib = _interpreter_purelib(target, timeout)
    if not purelib:
        return None
    gate_pth_path = Path(purelib) / _PTH_NAME
    try:
        gate_pth_stat = gate_pth_path.stat()
    except OSError:
        return None
    if not hasattr(gate_pth_stat, "st_flags") or not gate_pth_stat.st_flags & stat.UF_HIDDEN:
        return None

    under_fileprovider = False
    try:
        under_fileprovider = any(
            attribute.startswith(_FILEPROVIDER_XATTR_PREFIX)
            for attribute in os.listxattr(gate_pth_path.parent)
        )
    except (AttributeError, OSError):
        pass
    return {
        "hidden": True,
        "path": gate_pth_path,
        "under_fileprovider": under_fileprovider,
    }


def _startup_gate_active(target, timeout=10):
    """True only when startup itself loaded spendguard's existing public install path (no probe-side import)."""
    import subprocess
    import tempfile
    code = ("import sys;"
            "m=sys.modules.get('spendguard');"
            "print(int(m is not None and callable(getattr(m,'install',None)) "
            "and 'spendguard.gate' in sys.modules))")
    try:
        with tempfile.TemporaryDirectory(prefix=_STARTUP_GATE_PROBE_HOME_PREFIX) as probe_home:
            probe_environment = os.environ.copy()
            probe_environment[_SPENDGUARD_HOME_ENVIRONMENT_VARIABLE] = probe_home
            r = subprocess.run([target, "-c", code], capture_output=True, text=True,
                               check=True, timeout=timeout, env=probe_environment)
        return r.stdout.strip() == "1"
    except Exception:
        return False


def _sdk_gate_states(target, timeout=10):
    """Return each gated SDK's target-interpreter state from a fixed JSON machine response."""
    import subprocess
    code = (
        "import importlib, importlib.util, json\n"
        f"sdk_names = {_GATED_SDKS!r}\n"
        "sdk_gate_states = {}\n"
        "for sdk_name in sdk_names:\n"
        "    if importlib.util.find_spec(sdk_name) is None:\n"
        "        sdk_gate_states[sdk_name] = 'absent'\n"
        "        continue\n"
        "    sdk_patched = False\n"
        "    try:\n"
        "        if sdk_name == sdk_names[0]:\n"
        "            m = importlib.import_module('openai.resources.files')\n"
        "            sdk_patched = bool(getattr(m.Files.create, '_spend_gated', False))\n"
        "        elif sdk_name == sdk_names[1]:\n"
        "            m = importlib.import_module('anthropic.resources.messages.batches')\n"
        "            sdk_patched = bool(getattr(m.Batches.create, '_spend_gated', False))\n"
        "        else:\n"
        "            m = importlib.import_module(sdk_name)\n"
        "            sdk_patched = all(getattr(fn, '_spend_gated', False) for fn in\n"
        "                              (m.Client.send, m.AsyncClient.send))\n"
        "    except Exception:\n"
        "        sdk_patched = False\n"
        "    sdk_gate_states[sdk_name] = 'patched' if sdk_patched else 'present_ungated'\n"
        "print(json.dumps(sdk_gate_states, sort_keys=True, separators=(',', ':')))\n"
    )
    result = subprocess.run([target, "-c", code], capture_output=True, text=True,
                            check=True, timeout=timeout)
    import json
    sdk_gate_states = json.loads(result.stdout)
    valid_states = {"patched", "present_ungated", "absent"}
    if (set(sdk_gate_states) != set(_GATED_SDKS)
            or any(value not in valid_states for value in sdk_gate_states.values())):
        raise ValueError("target returned an invalid SDK gate-state response")
    return sdk_gate_states


def _render_sdk_gate_states(sdk_gate_states):
    labels = {"patched": "ENFORCING", "present_ungated": "NOT gated", "absent": "SDK not installed"}
    return " · ".join(f"{name}: {labels[sdk_gate_states[name]]}" for name in _GATED_SDKS)


def _resolved_sitecustomize(target, timeout=10):
    import subprocess
    try:
        r = subprocess.run([target, "-c", "import sitecustomize,inspect;print(inspect.getfile(sitecustomize))"],
                           capture_output=True, text=True, check=True, timeout=timeout)
        return r.stdout.strip() or None
    except Exception:
        return None


def _pkg_src():
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2] / "src")


_PTH_HOOK = _gate_hook_for_checkout_src(_pkg_src())


def install_hook(venv=None, uninstall=False, install_pkg=True, user=False, python=None):
    """Gate every process in another interpreter:
      --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).
      --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —
                               NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).
    Closes the system-python bypass. `--uninstall` removes the hook."""
    import subprocess
    from pathlib import Path
    cross = user or python                          # user/python mode = path-injected usercustomize (no pip)
    if cross:
        target = python or __import__("sys").executable
        try:
            sp = subprocess.run([target, "-c", "import site,os;os.makedirs(site.getusersitepackages(),exist_ok=True);"
                                 "print(site.getusersitepackages())"], capture_output=True, text=True, check=True).stdout.strip()
        except Exception as e:
            print(f"  ✗ couldn't resolve {target}'s user site: {e}"); return 1
        hook = os.path.join(sp, "usercustomize.py")
    else:
        venv = os.path.abspath(os.path.expanduser(venv))
        target = os.path.join(venv, "bin", "python")
        if not os.path.exists(target):
            print(f"  ✗ not a venv (no {target}). Create one: python -m venv {venv}"); return 1
        sp = _interpreter_purelib(target)
        if not sp:
            print(f"  ✗ couldn't resolve {target}'s active purelib; refusing to guess from lib/python*/site-packages.")
            return 1
        hook = os.path.join(sp, "sitecustomize.py")
    pth_hook = Path(sp) / _PTH_NAME

    # OWNERSHIP IS A SENTINEL WE WROTE, NOT THE WORD "spendguard" APPEARING SOMEWHERE. A user's own
    # sitecustomize that merely mentions spendguard in a comment satisfied the substring test — so
    # --uninstall DELETED a file we never created, and the install path treated it as ours to overwrite.
    # Deleting someone else's interpreter hook is not recoverable from here. The handles are also closed
    # deterministically rather than left to the GC.
    def _reads(path):
        try:
            with open(path, errors="ignore") as fh:
                return fh.read()
        except Exception:
            return ""

    if uninstall:
        refused = False
        for owned_hook in (hook, pth_hook):
            if os.path.exists(owned_hook) and any(
                    marker in _reads(owned_hook) for marker in (_SENTINEL, _PTH_SENTINEL)):
                os.remove(owned_hook); print(f"  ✓ removed gate hook: {owned_hook}")
            elif os.path.exists(owned_hook):
                print(f"  ✗ {owned_hook} exists but carries no spendguard marker — NOT removing someone else's "
                      "hook. Delete it by hand if you are sure it is ours.")
                refused = True
            else:
                print(f"  (no spendguard hook at {owned_hook})")
        return 1 if refused else 0
    if os.path.exists(hook) and not any(
            marker in _reads(hook) for marker in (_SENTINEL, _PTH_SENTINEL)):
        print(f"  ✗ {hook} exists and isn't ours — not overwriting. Merge the spendguard.install() snippet manually.")
        return 1

    if cross:                                       # path-injected — no pip (PEP668-safe)
        body = _sitecustomize_hook_source(inject_src=_pkg_src())
        open(hook, "w").write(body)
    else:
        pkg_root = str(Path(__file__).resolve().parents[2])
        if install_pkg:
            print(f"  pip install -e {pkg_root}  →  {venv}")
            try:
                refresh_consumer_editable_package(target, pkg_root, timeout=None)
            except RuntimeError as exc:
                print("  ✗ pip install failed:\n" + str(exc)); return 1
        open(hook, "w").write(_sitecustomize_hook_source())

    print(f"  ✓ hook → {hook}")
    active = _startup_gate_active(target)
    if not active and not cross:
        resolved = _resolved_sitecustomize(target)
        if resolved and os.path.realpath(resolved) != os.path.realpath(hook):
            if os.path.exists(pth_hook) and not any(
                    marker in _reads(pth_hook) for marker in (_SENTINEL, _PTH_SENTINEL)):
                print(f"  ✗ startup gate INACTIVE: {resolved} shadows {hook}, and {pth_hook} is not ours; "
                      "refusing to overwrite it.")
                return 1
            pth_hook.write_text(_PTH_HOOK)
            print(f"  ! {resolved} shadows {hook}; installed shadow-proof fallback → {pth_hook}")
            active = _startup_gate_active(target)
    if not active:
        resolved = _resolved_sitecustomize(target)
        why = (f"sitecustomize resolves to {resolved}, not {hook}" if resolved and
               os.path.realpath(resolved) != os.path.realpath(hook)
               else f"the startup hook at {hook} did not load spendguard.install()")
        print(f"  ✗ startup gate INACTIVE: {why}. Fix the interpreter's site configuration or conflicting "
              "sitecustomize, then rerun install-hook.")
        return 1
    try:
        sdk_gate_states = _sdk_gate_states(target)
    except Exception as e:
        print(f"  ✗ couldn't verify SDK gate state in {target}: {e}")
        return 1
    print(f"  verify ({target}): {_render_sdk_gate_states(sdk_gate_states)}")
    if "present_ungated" in sdk_gate_states.values():
        print("  ✗ gate hook fired, but a present LLM SDK is NOT gated; refusing to claim this interpreter "
              "is enforcing. Run `spendguard doctor` and repair the SDK gate before use.")
        return 1
    # ensure setup is actually USABLE, not just installed: keys must resolve in this interpreter (the repo-move
    # break was silent because nothing checked this). Reconcile/report are dead without them.
    kk = subprocess.run([target, "-c", "from spendguard import config as c;"
                         "print('  keys: openai='+('ok' if c.api_key('OPENAI_API_KEY') else 'MISSING')+"
                         "', anthropic='+('ok' if c.api_key('ANTHROPIC_API_KEY') else 'MISSING'))"],
                        capture_output=True, text=True)
    print(kk.stdout.strip() or ("  keys: (check failed) " + kk.stderr.strip()[-120:]))
    if "patched" in sdk_gate_states.values():
        print("  that interpreter is now gated (kill switch: GATE_DISABLE=1 or `spendguard off`).")
    else:
        print(f"  gate hook wired; no LLM SDK ({'/'.join(_GATED_SDKS)}) present here yet, so there is nothing "
              "to patch — `spendguard doctor` will show NOT ENFORCING until an SDK is installed, at which "
              "point this hook gates it.")
    print(f"  next: `spendguard doctor` — verifies keys + SaaS push readiness for this repo. Add keys to "
          f"{config.KEYS_ENV} (cwd-independent) if MISSING; add a per-repo .spendguard.json to push this repo to the server.")
    return 0


def install_skills(dest=None):
    """Deploy the repo's skills/ as Claude slash-commands (copy into ~/.claude/skills/). They then work
    as /<name> in Claude Code (CLI + the VS Code extension). `spendguard install-skills`."""
    import shutil
    from pathlib import Path
    dest = Path(dest or (Path.home() / ".claude" / "skills"))
    src = Path(__file__).resolve().parents[2] / "skills"
    if not src.is_dir():
        print(f"  no skills/ dir at {src}"); return 1
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for d in sorted(src.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            tgt = dest / d.name
            tgt.mkdir(exist_ok=True)
            shutil.copy2(d / "SKILL.md", tgt / "SKILL.md")
            copied.append(d.name)
    print(f"  ✓ installed {len(copied)} skill(s) → {dest}: {', '.join('/' + c for c in copied)}")
    print("  use them as slash-commands in Claude Code (CLI or the VS Code extension). `spend` is the quick status.")
    return 0


def cmd_install_skills(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard install-skills")
    ap.add_argument("--dest", help="skills dir (default: ~/.claude/skills)")
    a = ap.parse_args(argv)
    return install_skills(a.dest)


def cmd_install_hook(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard install-hook")
    ap.add_argument("--venv", help="path to the target virtualenv (e.g. ../slide-recon/.venv)")
    ap.add_argument("--user", action="store_true", help="gate the per-USER site of the target python "
                    "(covers `python3 …` from anywhere for that interpreter — the system-python bypass)")
    ap.add_argument("--python", help="target interpreter for --user (default: current python). "
                    "Use the system python you want to gate, e.g. /opt/homebrew/bin/python3 — "
                    "writes a path-injecting usercustomize, NO pip, so it works on PEP668-managed pythons")
    ap.add_argument("--uninstall", action="store_true", help="remove the gate hook")
    ap.add_argument("--no-pkg", action="store_true", help="skip pip install (package already present)")
    a = ap.parse_args(argv)
    if not a.venv and not a.user and not a.python:
        ap.error("give --venv <path>, or --user [--python <interp>]")
    return install_hook(a.venv, uninstall=a.uninstall, install_pkg=not a.no_pkg, user=a.user, python=a.python)


def _resolve_setting(s):
    """(value, source) for one setting. env always wins; then the file; then the default."""
    env = s.get("env")
    if env and os.environ.get(env) not in (None, ""):
        return os.environ[env], f"env:{env}"
    store = s["store"]
    if store == "env":
        return s["default"], "default"
    if store.startswith("config.json:"):
        remainder = store[len("config.json:"):]
        if "." in remainder:
            sec, key = remainder.split(".", 1)
            v = (config._cfg().get(sec) or {}).get(key)
        else:
            v = config._cfg().get(remainder)
        return (v, "config.json") if v is not None else (s["default"], "default")
    if store.startswith("email.json:"):
        v = config.email_config().get(store[len("email.json:"):])
        return (v, "email.json") if v is not None else (s["default"], "default")
    if store.startswith("saas.json:"):
        v = config.saas_config().get(store[len("saas.json:"):])
        return (v, "saas.json") if v not in (None, "") else (s["default"], "default")
    return s["default"], "default"


def _coerce(ans, kind):
    if ans.lower() in ("null", "none"):
        return None
    if kind.startswith("float"):
        return float(ans)
    if kind == "bool":
        return ans.lower() in ("1", "true", "yes", "y")
    if kind.startswith("enum:"):
        opts = kind[5:].split("|")[0].split(",")
        if ans not in opts:
            print(f"  (warning: '{ans}' not in {opts})")
    if kind.startswith("json"):
        import json as _json
        return _json.loads(ans)          # structured knobs (e.g. subscription.plans) — invalid JSON raises, loudly
    return ans


def _write_store(s, value):
    """Persist one setting to the file its schema `store` names. Returns (path, note) or raises ValueError for a
    store this command can't own (secrets live in keys.env / the real env, never in config.json)."""
    import json
    store = s["store"]
    if store.startswith("config.json:"):
        remainder = store[len("config.json:"):]
        if (store == "config.json:consumer_audit_exempt" and value is not None
                and (not isinstance(value, list) or any(not isinstance(item, str) for item in value))):
            raise ValueError("consumers.audit_exempt must be a JSON list of absolute path strings")
        p = config.CONFIG_JSON
        try:
            cfg = json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            raise ValueError(f"{p} is not valid JSON — fix or remove it first")
        if "." in remainder:
            sec, key = remainder.split(".", 1)
            cfg.setdefault(sec, {})
            if value is None:
                cfg[sec].pop(key, None)                 # explicit null = unset → fall back to the default
            else:
                cfg[sec][key] = value
        elif value is None:
            cfg.pop(remainder, None)
        else:
            cfg[remainder] = value
        p.parent.mkdir(parents=True, exist_ok=True)
        config.update_json(p, lambda _d: cfg,
                           required=True)               # p is CONFIG_JSON (merged settings): a corrupt file RAISES rather
        #                                                 than silently declining and dropping every other setting
        config._cfg._cache = None                       # drop the process cache so a read-back shows the new value
        return p, None
    if store in ("env", "(env only)"):
        raise ValueError(f"{s['key']} is read from the environment"
                         + (f" (${s['env']})" if s.get("env") else "")
                         + (" — put secrets in " + str(config.KEYS_ENV) if s["secret"] else ""))
    raise ValueError(f"{s['key']} lives in {store} — edit that file (or use the command that owns it)")


def cmd_config(argv=None):
    argv = list(argv or [])
    # `config set <dotted.key> <value>` — the documented way to set caps. It was a NO-OP for four releases
    # (argv was never read): the docs-site quickstart's "set caps" step printed a confident config table and
    # changed nothing, on a tool whose whole job is caps. Driven by the SETTINGS registry, so every knob is
    # settable and validated the same way `init` does it.
    if argv and argv[0] == "set":
        if len(argv) < 3:
            print("usage: spendguard config set <section.key> <value>   (value 'null' unsets → default)")
            print("  e.g. spendguard config set caps.per_batch 30 · caps.llm.daily 50 · gate.autotune apply")
            return 2
        dotted, raw = argv[1], " ".join(argv[2:])
        by_dotted = {f"{s['section']}.{s['key']}": s for s in config_schema.SETTINGS}
        s = by_dotted.get(dotted)
        if not s:
            import difflib
            near = difflib.get_close_matches(dotted, by_dotted, n=3, cutoff=0.5)
            print(f"unknown setting {dotted!r}" + (f" — did you mean: {', '.join(near)}?" if near else ""))
            print("  `spendguard config` lists every setting with its current value + source.")
            return 2
        try:
            value = _coerce(raw, s["kind"])
            path, _ = _write_store(s, value)
        except ValueError as e:
            print(f"cannot set {dotted}: {e}")
            return 2
        except Exception as e:
            print(f"cannot set {dotted}: {e}")
            return 1
        shown = "***set***" if s["secret"] else ("(unset → default)" if value is None else value)
        print(f"{dotted} = {shown}   → {path}")
        now, src = _resolve_setting(s)
        if src.startswith("env:"):                      # a live env var still wins — say so, don't pretend
            print(f"  ⚠ an environment variable ({src[4:]}) overrides this at runtime; current effective value: {now}")
        return 0
    if argv and argv[0] not in ("show", "list"):
        print(f"unknown config subcommand {argv[0]!r} — `config` (show all) or `config set <section.key> <value>`")
        return 2
    print(f"spendguard config  (home: {config.HOME})\n")
    for sec, items in config_schema.sections().items():
        print(f"[{sec}]")
        for s in items:
            v, src = _resolve_setting(s)
            if s["secret"] and v:
                disp = "***set***"
            elif v in (None, ""):
                disp = "(unset)"
            else:
                disp = v
            print(f"  {s['key']:<20} {str(disp):<28} {src}")
    print(f"\nfiles: {config.CONFIG_JSON} (config) · {config.KEYS_ENV} (secrets) · {config.saas_path()} · {config.HOME / 'email.json'}")
    print("change one: `spendguard config set <section.key> <value>`  (e.g. config set caps.per_batch 30; 'null' unsets)")
    print("Secrets (LLM / compute / org keys) live in keys.env or the environment — never in the config files.")
    return 0


def _parse_caps_json(text):
    """Tolerantly pull a {llm,compute,total} JSON object of USD monthly caps from the model's reply."""
    import re
    m = re.search(r"\{[^{}]*\}", text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    out = {}
    for k in ("llm", "compute", "total"):
        v = d.get(k)
        if isinstance(v, (int, float)) and v > 0:
            out[k] = float(v)
    return out or None


def _chat_caps():
    """Conversational cap setup. Uses YOUR own key for ONE small realtime call, caged under caps.meta (intent
    spendguard:init — separate budget, excluded from the corpus), estimate-first. NEVER the server. Returns
    {llm,compute,total} monthly USD, or None to fall back to the deterministic prompts."""
    from . import gate, adapters, calls
    gate.install()   # ensure the gate is enforcing IN THIS PROCESS so the call is metered under caps.meta
    model = config.advisor_model()
    try:
        ans = input('  Describe your monthly budgets (+ anything about projects), e.g.\n'
                    '  "$2k/mo for LLMs and $800 for GPUs"\n  > ').strip()
    except EOFError:
        return None
    if not ans:
        return None
    sys = ("Extract the user's MONTHLY spend caps as STRICT JSON, USD numbers only, no prose, no code fence. "
           "Keys: llm (LLM/API monthly cap), compute (GPU/remote-compute monthly cap), total (overall ceiling; "
           "if unstated use llm+compute). Omit any key you cannot infer. "
           'Example: {"llm":2000,"compute":800,"total":2800}')
    print(f'\n  (this uses YOUR {model} key for one small call, caged under caps.meta ${config.meta_cap():.2f}/day, est ~$0.001 — never the server)')
    try:
        with calls.context(intent="spendguard:init"):
            r = adapters.call(model, ans, sig="spendguard:init", system=sys)
    except Exception as e:
        print(f"  (conversational setup unavailable: {e} — falling back to prompts)")
        return None
    if r.get("error"):
        print(f"  (conversational setup unavailable: {r['error']} — falling back to prompts)")
        return None
    caps = _parse_caps_json(r.get("text", ""))
    if caps and r.get("cost"):
        print(f"  parsed your budgets (caged cost ${r['cost']:.4f}).")
    return caps


def _scaffold_keys_env():
    """Write ~/.spendguard/keys.env with commented placeholders for every secret key (LLM providers, vast.ai,
    the team/org roll-up key) — ONLY if it doesn't already exist, so real keys are never clobbered. chmod 600.
    Returns (path, created?). The file is loaded into the environment on `import spendguard` (config.load_key_files),
    so both spendguard and the user's own openai/anthropic clients pick the keys up. A real env var always wins."""
    import os
    p = config.KEYS_ENV
    if p.exists():
        return p, False
    key_names = [s["env"] for s in config_schema.SETTINGS if s["section"] == "keys" and s.get("env")]
    llm = [k for k in key_names if k != "VAST_API_KEY"]
    lines = [
        "# spendguard secrets — fill the ones you use (blank = that provider/feature is off).",
        "# Loaded into the environment on `import spendguard`; a REAL env var always wins. Keep private (chmod 600).",
        "",
        "# ── LLM provider keys ──",
        *[f"{k}=" for k in llm],
        "",
        "# ── Remote compute ──",
        "VAST_API_KEY=",
        "",
        "# ── Team/org roll-up key from llmspendguard.com (dashboard → keys) ──",
        "SPENDGUARD_SAAS_KEY=",
    ]
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        return p, True
    except Exception:
        return p, False


def _reraise_if_governance_stop(e):
    """Re-raise a GOVERNANCE stop — a spend refusal / dispatch deadline (`gate.is_deliberate_stop`) or a ledger lock
    (`ledger.LockedError`) — so a best-effort setup step never downgrades it to 'skipped'. The same 'stop or locked'
    concept `provider_tokens._stop_or_locked` uses, delegated to the canonical predicates rather than re-listed. Any
    other exception is left for the caller's broad handler to report."""
    try:
        from . import gate, ledger
        stop = gate.is_deliberate_stop(e) or isinstance(e, ledger.LockedError)
    except Exception:
        stop = False
    if stop:
        raise e


def _prompt_yn(prompt, default=True, assume=False, eof=None):
    """A [Y/n] prompt. `assume` (the zero-prompt path) returns `default` with NO input; a closed stdin (EOFError —
    a piped `init`, or CI) returns `eof` when given else `default`; an empty line keeps `default`. One place so every
    step's prompt behaves identically."""
    if assume:
        return default
    try:
        ans = input(f"  {prompt} {'[Y/n]' if default else '[y/N]'}\n  > ").strip().lower()
    except EOFError:
        return default if eof is None else eof
    if ans == "":
        return default
    return ans in ("y", "yes", "1", "true")


def _autowire_lanes():
    """Auto-detect installed + logged-in $0 subscription lanes and, when at least one is READY and advisor.executor
    is still the metered default, flip it to 'pool' so realtime meta/best-value calls prefer the plans (billed $0)
    before the metered API. Reuses the detection primitive (`lanes.lanes_status`) and the validated writer
    (`tier_config._write_advisor_cfg`) — no new mechanism. NEVER clobbers an executor the user or the
    SPENDGUARD_ADVISOR_EXECUTOR env already chose. Detection is host state only ($0, no model calls), so it is safe
    under --quick / on CI (no ready lane ⇒ no change). advisor.lane_models is intentionally NOT auto-seeded: without
    the plan's real model a priced guess would UNDERCOUNT the lane's value (a wrong number pointing the wrong way) —
    the user declares it deliberately with `spendguard lanes set-model`, which validates the model is priced."""
    try:
        from . import lanes as _lanes, tier_config, config as _cfg
        st = _lanes.lanes_status()
    except Exception as e:
        _reraise_if_governance_stop(e)
        print(f"\n  (subscription-lane auto-detect unavailable: {e})")
        return None
    ready = [ln for ln in st["lanes"] if ln.get("auth") == "ok"]
    print("\nSubscription lanes ($0 plan-covered execution for realtime meta prompts + best-value calls):")
    env_exec = os.environ.get("SPENDGUARD_ADVISOR_EXECUTOR")
    stored_exec = _cfg._cfg_get("advisor", "executor", None)
    if ready:
        names = ", ".join(ln["lane"] for ln in ready)
        if env_exec:
            print(f"  detected ready: {names} — but $SPENDGUARD_ADVISOR_EXECUTOR={env_exec} pins the executor; leaving it.")
        elif stored_exec and stored_exec != "api":
            print(f"  detected ready: {names} — advisor.executor already {stored_exec!r}; leaving it.")
        else:
            try:
                tier_config._write_advisor_cfg("executor", "pool")
                print(f"  ✓ detected ready: {names}")
                print("  ✓ set advisor.executor = 'pool' — realtime calls now try these $0 lanes first "
                      "(revert anytime: `spendguard config set advisor.executor api`).")
            except Exception as e:
                _reraise_if_governance_stop(e)             # a lock / refusal from the writer must surface, not 'skip'
                print(f"  (could not set advisor.executor: {e})")
    # the standard per-lane state block (now reflects the executor just set) + the definitive probe hint
    try:
        summary = _lanes.lane_summary_lines()
    except Exception as e:
        _reraise_if_governance_stop(e)
        summary = []
    if summary:
        for line in summary:
            print("  " + line)
        print("  Verify end-to-end: `spendguard lanes --probe` (one tiny plan-billed prompt per lane, $0).")
    elif not ready:
        hint = next((ln["activate"] for ln in st["lanes"] if ln.get("activate")), None)
        print("  none active yet — realtime runs on the metered API. Activate a plan lane to route at $0"
              + (f":\n    {hint}" if hint else " (`spendguard lanes` shows each lane's step)."))
        print("  then re-run `spendguard init` (or `spendguard config set advisor.executor pool`).")
    # re-read so the RETURNED status reflects any executor we just wrote (the closing card must not show the pre-write
    # value) — cheap: lanes_status reads host state + the cached auth artifacts, no model calls.
    try:
        return _lanes.lanes_status()
    except Exception:
        return st


def _run_integration_installers(quick=False, do_all=False):
    """One guided flow that folds the five modular installers together — gate THIS interpreter, register the MCP
    tools, install the in-chat receipt, drop the global assistant rule, install the slash-commands. Each step is
    idempotent + reversible, prints its own result, and is a per-step [Y/n]. Reuses the existing installers (no
    reimplementation): `install_hook`, `mcp_server.register_client`, `receipt.install_cli`, `install_rule`,
    `install_skills`. A governance stop from any step propagates (never downgraded to 'skipped').

    Flag semantics (backward-compatible): plain `--quick` stays CONFIG-ONLY and returns here untouched; `--all`
    opts into the wiring (per-step prompts); `--all --quick` runs every step at its default with NO prompts (the
    one-command full setup); plain interactive asks a single gateway question first, then per-step. Returns the list
    of step labels that were wired (for the closing setup card), or None when the flow was skipped entirely."""
    import sys
    if quick and not do_all:
        return None                                        # preserve the original --quick: config defaults only
    if not do_all:                                         # plain interactive: one gateway question gates the flow
        if not _prompt_yn("Wire spendguard into your dev tools now — gate this Python, MCP tools, in-chat receipt, "
                          "global assistant rule, slash-commands?", default=True, eof=False):
            print("  skipped — wire any later: `spendguard install-hook`, `spendguard mcp --register`, "
                  "`spendguard install-receipts`, `spendguard install-rule --global`, `spendguard install-skills` "
                  "(or `spendguard init --all` for all of them).")
            return None
    print("\nWiring spendguard into your tools (each step optional, idempotent, reversible):")
    wired = []                                             # step labels that actually ran → the closing card
    # 1) gate THIS interpreter so every LLM script it runs is spend-checked
    if _prompt_yn("Gate this Python so LLM scripts here are always spend-checked (recommended)?",
                  default=True, assume=quick):
        try:
            if sys.prefix != sys.base_prefix:              # inside a venv where spendguard already imports → no pip
                install_hook(venv=sys.prefix, install_pkg=False)
            else:                                          # base/system python: PEP668-safe path-injecting usercustomize
                install_hook(user=True, python=sys.executable)
            wired.append("gate")
        except Exception as e:
            _reraise_if_governance_stop(e)
            print(f"  (gate step skipped: {e})")
    # 2) MCP tools in Claude Code
    if _prompt_yn("Register the spendguard MCP tools (model-advisor + spend) in Claude Code?",
                  default=True, assume=quick):
        try:
            from . import mcp_server
            mcp_server.register_client()
            wired.append("MCP")
        except Exception as e:
            _reraise_if_governance_stop(e)
            print(f"  (MCP step skipped: {e})")
    # 3) always-on in-chat spend receipt
    if _prompt_yn("Install the always-on in-chat spend receipt (Claude Code)?", default=True, assume=quick):
        try:
            from . import receipt
            receipt.install_cli([])
            wired.append("receipt")
        except Exception as e:
            _reraise_if_governance_stop(e)
            print(f"  (receipt step skipped: {e})")
    # 4) global assistant rule — every project's assistant then routes LLM code through spendguard
    if _prompt_yn("Add the spendguard rule to your global assistant instructions (~/.claude/CLAUDE.md)?",
                  default=True, assume=quick):
        try:
            install_rule(glob_=True)
            wired.append("rule")
        except Exception as e:
            _reraise_if_governance_stop(e)
            print(f"  (rule step skipped: {e})")
    # 5) slash-commands
    if _prompt_yn("Install spendguard slash-commands (/spend, /spendguard-learn, …) for Claude Code?",
                  default=True, assume=quick):
        try:
            install_skills()
            wired.append("skills")
        except Exception as e:
            _reraise_if_governance_stop(e)
            print(f"  (skills step skipped: {e})")
    print("\n  ✓ tool wiring complete — `spendguard doctor` confirms the gate is ENFORCING here.")
    return wired


def _preflight_keys():
    """Report, for EVERY declared provider/compute key (config_schema SETTINGS section=='keys' — the SSOT, not a
    hand-picked openai/anthropic pair), whether it RESOLVES in this interpreter, and for each one NOT set print where
    to GET it (config_schema.KEY_HELP_URLS) so a first-timer isn't left guessing. This is the silent gap that broke
    reconcile/report after a repo move (a cwd-relative .env lost the keys). Data-driven: a new provider key added to
    SETTINGS appears here automatically. Returns (resolved, missing) counts for a caller's summary card."""
    print(f"  key pre-flight — reconcile/report + metered fallback are blind without these (put the ones you use in "
          f"{config.KEYS_ENV}, which is cwd-independent):")
    resolved = missing = 0
    for s in config_schema.SETTINGS:
        # section=='keys' also carries the key_profile SELECTOR (not a credential); only s['secret'] rows are actual
        # provider/compute keys to resolve — filtering on that keeps a non-secret selector out of the pre-flight.
        if s["section"] != "keys" or not s.get("secret") or not s.get("env"):
            continue
        env = s["env"]
        try:
            k = config.api_key(env)
        except Exception:
            k = None
        if k:
            resolved += 1
            print(f"    🟢 {env:<20} resolved")
        else:
            missing += 1
            url = config_schema.KEY_HELP_URLS.get(env)
            print(f"    🔴 {env:<20} not set" + (f"  → get one: {url}" if url else ""))
    print(f"  ({resolved} resolved · {missing} not set — set only the providers you use; blank = that provider is off.)")
    return resolved, missing


def _setup_summary_card(keys_counts, lanes_st, wired):
    """The closing 'you're set up' card: the few facts that answer 'is spendguard actually working HERE', assembled
    from state cmd_init ALREADY computed (no re-probe) — the gate's own enforcing check (gate._any_patched), the key
    pre-flight counts, the ready $0 lanes from lanes_status, and what the installer flow wired — then the ONE command
    that proves the path end-to-end. The proof is OFFERED, never auto-run: a probe bills plan tokens, and setup must
    not spend on the user's behalf."""
    from . import gate as _gate
    try:
        enforcing = _gate._any_patched()
    except Exception:
        enforcing = None
    resolved, missing = keys_counts if keys_counts else (0, 0)
    ready = [ln["lane"] for ln in (lanes_st or {}).get("lanes", []) if ln.get("auth") == "ok"]
    executor = (lanes_st or {}).get("executor", "api")
    bar = "─" * 64
    print("\n" + bar)
    print("spendguard is set up — status here:")
    if enforcing:
        print("  gate       🟢 ENFORCING in this interpreter")
    else:
        print("  gate       🟡 not enforcing in THIS shell — put `import spendguard; spendguard.require()` at the top "
              "of a script (fails closed), or `spendguard install-hook` to gate the interpreter")
    print(f"  keys       {resolved} resolved"
          + (f" · {missing} not set (get-a-key links above)" if missing else "  (all declared keys set)"))
    if ready:
        print(f"  $0 lanes   🟢 {', '.join(ready)}   (advisor.executor = {executor})")
    else:
        print(f"  $0 lanes   none active — realtime runs metered (advisor.executor = {executor})")
    if wired:
        print(f"  wired      {', '.join(wired)}")
    print(bar)
    if ready:
        print("Prove the $0-lane path end-to-end →  spendguard lanes --probe   (one tiny plan-billed prompt per lane, $0)")
    elif resolved:
        print("Prove it end-to-end →  spendguard doctor   (verifies the gate + keys + ledger for this repo)")
    else:
        print("Add a key (links above), then →  spendguard doctor   (verifies everything is wired)")


def cmd_init(argv=None):
    argv = list(argv or [])
    quick = bool({"--quick", "--yes", "-y"} & set(argv))   # fast path: write defaults, zero prompts (CI/onboarding)
    do_all = "--all" in argv                                # also WIRE the tools (gate/MCP/receipt/rule/skills)
    _mode = "  (--all --quick: full setup, no prompts)" if (quick and do_all) else \
            "  (--quick: writing defaults, no prompts)" if quick else \
            "  (--all: also wiring your tools)" if do_all else ""
    print("spendguard setup" + _mode + "\n")
    print("  spendguard runs FULLY STANDALONE — a local spend gate on this machine, no account needed.")
    print("  Optionally connect to a team/org dashboard (llmspendguard.com) to roll spend up across your team.")
    print("  This also auto-detects your $0 subscription lanes; add `--all` to wire the gate, MCP tools, in-chat "
          "receipt, assistant rule + slash-commands in one go (`--all --quick` = zero prompts).\n")
    connect = "--connect" in argv
    if not connect and "--local" not in argv and not quick:
        try:
            connect = input("  Connect to a team/org now? (needs an org key from your admin; or use `spendguard saas link` later) [y/N]\n  > ").strip().lower() in ("y", "yes")
        except EOFError:
            connect = False
    cfgjson = dict(config._cfg())
    ep = config.HOME / "email.json"
    sp = config.saas_path()
    email, saas = {}, {}
    if ep.exists():
        try:
            email = json.loads(ep.read_text())
        except Exception:
            pass
    if sp.exists():
        try:
            saas = json.loads(sp.read_text())
        except Exception:
            pass
    # Two ways to set caps: --chat (one caged LLM call parses your plain-English budgets) or the default
    # deterministic prompts. --chat falls back to prompts if no key / the call fails.
    chat = "--chat" in (argv or [])
    chat_set = False
    if chat:
        print("\n  Conversational setup (--chat):")
        got = _chat_caps()
        if got:
            caps = cfgjson.setdefault("caps", {})
            for k in ("llm", "compute", "total"):
                if got.get(k):
                    caps.setdefault(k, {})["monthly"] = got[k]
            print("  → set " + ", ".join(f"caps.{k}.monthly=${int(v)}" for k, v in got.items()))
            chat_set = True
        else:
            print("  (falling back to prompts)")
    if not quick:
        print("\n  Enter keeps the current/default; 'null' clears.\n")
    for s in config_schema.SETTINGS:
        if quick:
            continue  # --quick: no prompts — every setting keeps its current/default value
        if chat_set and s["section"] != "saas":
            continue  # --chat set the caps; keep defaults for the rest (tune later via `spendguard config set`)
        if s["section"] == "keys" or s["store"] == "env":
            continue  # env-only (API keys, home, prices override) — instructed below, not written
        if not connect and s["section"] == "saas":
            continue  # local-only: skip all team/org connection prompts
        cur, _src = _resolve_setting(s)
        try:
            ans = input(f"{s['section']}.{s['key']}  [{cur}]  — {s['desc']}\n  > ").strip()
        except EOFError:
            ans = ""
        if ans == "":
            continue
        try:
            val = _coerce(ans, s["kind"])
        except ValueError:
            print(f"  (couldn't parse '{ans}' as {s['kind']}; skipped)")
            continue
        if s["store"].startswith("config.json:"):
            sec, key = s["store"][len("config.json:"):].split(".", 1)
            cfgjson.setdefault(sec, {})[key] = val
        elif s["store"].startswith("email.json:"):
            email[s["store"][len("email.json:"):]] = val
        elif s["store"].startswith("saas.json:"):
            saas[s["store"][len("saas.json:"):]] = val
    # THROUGH THE ONE SAFE WRITER. `cfgjson` is built from the settings this command is changing, so
    # writing it whole DROPPED every key the command did not touch. Merged into the existing file instead,
    # atomically, with the prior version kept.
    def _merge(cur):
        for sec, kv in cfgjson.items():
            cur.setdefault(sec, {}).update(kv if isinstance(kv, dict) else {})
        return cur
    config.save_config(_merge, reason="setup")
    if email:
        config.update_json(ep, lambda _d: email, quarantine_unparseable=True)   # init writes the user's identity wholesale:
    if saas:                                                                     # recover a corrupt prior file so the new
        config.update_json(sp, lambda _d: saas, quarantine_unparseable=True)     # values persist, never a silent decline reported as "wrote"
    print(f"\nwrote {config.CONFIG_JSON}" + (f" and {ep}" if email else "") + (f" and {sp}" if saas else ""))
    # Contributor identity is a MUST (it's the billable/rollup user). Materialize + show the resolved id now so it's
    # never blank/unattributed; an email here also becomes the alert target.
    try:
        from . import saas as _saas
        ident = _saas.contributor()
        if config.is_email(ident):
            print(f"Contributor: {ident}  (email — per-user roll-up, billing, AND alerts)")
        else:
            print(f"Contributor: {ident}  (auto anonymous id — attribution works; set an email via `spendguard init` for alerts)")
    except Exception:
        pass
    if connect:
        print("\nTeam/org: put your org key in saas.json (saas.api_key) if you haven't, then run `spendguard saas link` "
              "to approve in the browser + set your verified email.")
    else:
        print("\nRunning LOCAL-ONLY (no account). Connect a team anytime: `spendguard init --connect`, or "
              "`spendguard saas link` once you have an org key.")
    kp, created = _scaffold_keys_env()
    keys = ", ".join(s["env"] for s in config_schema.SETTINGS if s["section"] == "keys")
    print(f"\nSecrets → {kp}" + ("  (scaffolded with placeholders — fill the ones you use)" if created
                                 else "  (already present)"))
    print(f"  holds: {keys}, SPENDGUARD_SAAS_KEY — loaded into the env on `import spendguard`; a real env var wins.")
    # Pre-flight: do the keys actually RESOLVE here? This is exactly the silent gap that broke reconcile/report
    # after a repo move (cwd-relative .env lost the keys). Now data-driven over EVERY declared key, with a get-a-key
    # link for anything not set — same check as `spendguard doctor`, surfaced at setup.
    _keys = _preflight_keys()
    if (cfgjson.get("budget") or {}).get("backend") == "sqlite":
        print(f"SQLite budget ledger will be created at {config.db_path()} on first charge.")
    # Subscription lanes: DETECT the ready ones and auto-route realtime calls through them at $0 (executor='pool'),
    # then show each lane's state + exactly what activates the inactive ones. Supersedes the old print-only block —
    # at call time a dead lane degrades silently to the metered API, so setup is where the routing must be wired.
    _lanes_st = _autowire_lanes()
    # Fold the modular installers into this one command (gate / MCP / receipt / rule / skills). Plain `--quick` stays
    # config-only; `--all` (optionally with --quick) wires the tools; plain interactive asks a single gateway first.
    _wired = _run_integration_installers(quick=quick, do_all=do_all)
    # Cold-start the cost advisor from your OWN history (so day-one recommendations aren't empty).
    print("\nSeed the advisor: `spendguard bootstrap` mines your past provider batches (free retrieval) into a "
          "starter cost+quality corpus — the paid reasoning step is caged by caps.meta + estimate-first (opt-in --run).")
    print("  In Claude Code, the /spendguard-learn skill runs this for you.")
    # Close on the 'you're set up' card + the one command that proves it (assembled from the state above, no re-probe).
    _setup_summary_card(_keys, _lanes_st, _wired)
    return 0
