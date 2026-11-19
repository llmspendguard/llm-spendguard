import os
from pathlib import Path


# Well-known user-local CLI install dirs, for hosts where PATH doesn't carry them (launchd/cron daemons run
# with a minimal PATH that misses ~/.local/bin and nvm's versioned bins — the subscription lanes must still
# find the plan CLIs there). Globs allowed; newest executable wins.
_CLI_SEARCH_DIRS = ("~/.claude/local", "~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin",
                    "~/.nvm/versions/node/*/bin", "~/.kimi-code/bin", "~/.grok/bin")


def resolve_cli(name, env_var=None):
    """Absolute path of a host CLI or None: $<env_var> pin → PATH → well-known user-local dirs. An explicit
    env pin that doesn't exist returns None (fail LOUD at the pin, never silently substitute another binary)."""
    import shutil
    import glob as _glob
    pin = os.environ.get(env_var) if env_var else None
    if pin:
        return pin if (Path(pin).exists() and os.access(pin, os.X_OK)) else None
    w = shutil.which(name)
    if w:
        return w
    hits = []
    for d in _CLI_SEARCH_DIRS:
        for p in _glob.glob(str(Path(d).expanduser() / name)):
            if os.access(p, os.X_OK):
                hits.append(Path(p))
    # THE FILE CAN VANISH BETWEEN THE GLOB AND THE stat(). These directories include version-manager
    # trees (nvm and friends) that rewrite themselves as a version is installed or removed, so a hit that
    # existed a microsecond ago may be gone — and an unguarded stat() raised FileNotFoundError out of a
    # function whose whole job is "find the CLI, or return None". A disappeared candidate sorts LAST
    # rather than killing the search: it is one fewer option, not an error.
    def _mtime(c):
        try:
            return c.stat().st_mtime
        except OSError:
            return -1.0
    return str(max(hits, key=_mtime)) if hits else None


def cli_resolver(name, env_var=None):
    """Build a zero-arg resolver for one lane's host CLI via resolve_cli(name, env_var).

    It returns the absolute path or None via the env pin, PATH, then well-known user-local dirs.
    Daemons (launchd/cron) have a minimal PATH that misses nvm/~/.local installs, so PATH alone is
    insufficient. There is deliberately no shutil.which fast path ahead of the pin: that path used
    to ignore an explicit $SPENDGUARD_*_BIN when a same-named binary was on PATH. resolve_cli fails
    loud (returns None) at a missing pin instead of substituting another binary. Keeping this in
    one factory prevents that invariant from drifting between lanes.
    """
    def resolve_bound_cli():
        return resolve_cli(name, env_var)
    return resolve_bound_cli
