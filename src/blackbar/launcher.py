"""Running a client through the proxy.

`exec` instead of a subprocess: the client process replaces ours, so the TTY, signals
and exit code behave exactly like a manual run and no middleman is left in the process
tree.
"""

from __future__ import annotations

import os
import sys

from .config import Config
from . import daemon

# The environment variable each client reads as its API base URL.
ENV_VAR = {
    "anthropic": "ANTHROPIC_BASE_URL",
    "openai": "OPENAI_BASE_URL",
}

# Default executable per provider.
BINARY = {
    "anthropic": "claude",
    "openai": "codex",
}


def launch(config: Config, provider: str, args: list[str], *, quiet: bool = False) -> int:
    binary = BINARY.get(provider, provider)
    env_var = ENV_VAR.get(provider)
    if env_var is None:
        print(f"blackbar: unknown provider '{provider}'", file=sys.stderr)
        return 2

    if not daemon.is_running(config):
        if not config.autostart:
            print(
                "blackbar: daemon is not answering. Run `blackbar start` "
                "or enable launcher.autostart in the config.",
                file=sys.stderr,
            )
            return 1
        if not quiet:
            print("blackbar: starting the daemon...", file=sys.stderr)
        if not daemon.start_background(config):
            print(
                f"blackbar: could not start the daemon, see {config.log_path}",
                file=sys.stderr,
            )
            return 1

    url = config.provider_url(provider)
    env = dict(os.environ)
    env[env_var] = url
    env["BLACKBAR_ACTIVE"] = "1"

    if not quiet:
        print(f"\033[90m▮ blackbar → {url}\033[0m", file=sys.stderr)

    try:
        os.execvpe(binary, [binary, *args], env)
    except FileNotFoundError:
        print(f"blackbar: '{binary}' not found in PATH", file=sys.stderr)
        return 127
    return 0


def launch_direct(binary: str, args: list[str]) -> int:
    """Escape hatch: run a client with the proxy bypassed."""
    env = dict(os.environ)
    for name in ENV_VAR.values():
        env.pop(name, None)
    env.pop("BLACKBAR_ACTIVE", None)
    try:
        os.execvpe(binary, [binary, *args], env)
    except FileNotFoundError:
        print(f"blackbar: '{binary}' not found in PATH", file=sys.stderr)
        return 127
    return 0

