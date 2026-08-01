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

# The variable Claude Code reads as its API base URL.
ENV_VAR = "ANTHROPIC_BASE_URL"
BINARY = "claude"


def launch(config: Config, args: list[str], *, quiet: bool = False) -> int:
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

    if not daemon.is_ready(config):
        # A request landing mid-load would just sit there; better to say why.
        if not quiet:
            print("blackbar: daemon is starting up - loading the detection model, about "
                  "15 seconds. Later windows start instantly.", file=sys.stderr)
        daemon.wait_until_ready(config)

    url = config.base_url
    env = dict(os.environ)
    env[ENV_VAR] = url
    env["BLACKBAR_ACTIVE"] = "1"

    if not quiet:
        print(f"\033[90m▮ blackbar → {url}\033[0m", file=sys.stderr)

    try:
        os.execvpe(BINARY, [BINARY, *args], env)
    except FileNotFoundError:
        print(f"blackbar: '{BINARY}' not found in PATH", file=sys.stderr)
        return 127
    return 0


def launch_direct(binary: str, args: list[str]) -> int:
    """Escape hatch: run a client with the proxy bypassed."""
    env = dict(os.environ)
    env.pop(ENV_VAR, None)
    env.pop("BLACKBAR_ACTIVE", None)
    try:
        os.execvpe(binary, [binary, *args], env)
    except FileNotFoundError:
        print(f"blackbar: '{binary}' not found in PATH", file=sys.stderr)
        return 127
    return 0

