import os

from spendguard.store.configuration.provider_key_env_registry import all_provider_key_envs


def lane_plan_env(keep=()):
    """Child env for a subscription-LANE subprocess: os.environ with EVERY provider's METERED api-key env var
    REMOVED, except names in `keep` (a lane that authenticates with a plan-specific token, not a plan login).

    This is the hard, STRUCTURAL guarantee behind "$0 billed on a lane, no double-usage": a lane subprocess that
    carries no metered key cannot make a metered API call at all — and, above all, a NON-Claude lane can never
    inherit ANTHROPIC_API_KEY and silently spend Claude tokens for work that was meant to ride another plan. (keys.env
    is loaded into os.environ at import, so WITHOUT this scrub a codex/gemini subprocess would inherit every key.)

    Key names come from the provider REGISTRY (so a newly registered provider is covered with no edit here) plus the
    auth-token / base-url variants that re-authenticate or relocate the same billing. No prices, no behaviour — just
    the set of env vars that authorize metered spend."""
    keep = set(keep or ())
    # Both metered API keys AND flat-fee PLAN tokens: a lane carries only the credential(s) it owns (via keep), so a
    # codex/gemini lane holds no zai coding token either. ZAI_CODING_API_KEY is a plan token, not metered, but a lane
    # that does not own it has no business carrying it.
    metered = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "OPENAI_API_KEY", "OPENAI_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY",
               "ZAI_API_KEY", "ZAI_CODING_API_KEY"}
    metered |= all_provider_key_envs()
    metered = {m for m in metered if m} - keep
    return {k: v for k, v in os.environ.items() if k not in metered}


CLI_TEST_OFFLINE_ENVIRONMENT_KEY = "SPENDGUARD_TEST_CLI_OFFLINE"
CLI_TEST_OFFLINE_ENABLED_VALUE = "1"


class IsolatedCliLaunchError(BaseException):
    """Escape fail-soft lane handlers when a test attempts a real CLI launch."""


def refuse_isolated_cli_launch():
    if os.environ.get(CLI_TEST_OFFLINE_ENVIRONMENT_KEY) == CLI_TEST_OFFLINE_ENABLED_VALUE:
        raise IsolatedCliLaunchError(
            f"{CLI_TEST_OFFLINE_ENVIRONMENT_KEY} forbids a real subscription CLI launch; supply a lane/usage fixture")


def lane_exec_env(cli_path, keep=()):
    """Plan-safe child env for a resolved subscription CLI, with its sibling runtime directory first on PATH.

    Node-installed CLIs are commonly shebang scripts whose ``node`` executable lives beside the resolved CLI
    (not on launchd's minimal PATH). Deriving the directory from the resolver's absolute result keeps both probes
    and real calls runnable without encoding any version-manager layout. ``lane_plan_env`` remains the credential
    boundary; this helper changes only PATH and never makes a missing CLI look present.
    """
    refuse_isolated_cli_launch()
    env = lane_plan_env(keep=keep)
    if cli_path:
        cli_dir = os.path.dirname(os.path.abspath(cli_path))
        env["PATH"] = cli_dir + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    return env
