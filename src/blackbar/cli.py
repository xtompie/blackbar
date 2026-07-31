"""blackbar CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer

from . import __version__, attach as attach_mod, config as config_mod, daemon, launcher, service

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local redaction proxy for Claude Code.",
)

DIM = "\033[90m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
OFF = "\033[0m"

# Launcher commands hand their arguments to the client verbatim, so they must not
# swallow --help or any unknown option.
PASSTHROUGH = {"ignore_unknown_options": True, "allow_extra_args": True, "help_option_names": []}


def _config() -> config_mod.Config:
    return config_mod.ensure_files()


def _die(message: str, code: int = 1) -> None:
    print(f"{RED}blackbar:{OFF} {message}", file=sys.stderr)
    raise typer.Exit(code)


def _require_daemon(config) -> None:
    if not daemon.is_running(config):
        _die("daemon is not answering (run `blackbar start`)")


# --- launchers ----------------------------------------------------------------

@app.command(context_settings=PASSTHROUGH)
def claude(ctx: typer.Context) -> None:
    """Run claude through the proxy (arguments are passed verbatim)."""
    raise typer.Exit(launcher.launch(_config(), "anthropic", ctx.args))


@app.command(context_settings=PASSTHROUGH)
def codex(ctx: typer.Context) -> None:
    """Run codex through the proxy."""
    raise typer.Exit(launcher.launch(_config(), "openai", ctx.args))


@app.command(context_settings=PASSTHROUGH)
def direct(ctx: typer.Context) -> None:
    """Run a client with the proxy bypassed: blackbar direct claude [args]"""
    if not ctx.args:
        _die("name the program, e.g. `blackbar direct claude`")
    raise typer.Exit(launcher.launch_direct(ctx.args[0], ctx.args[1:]))


# --- lifecycle ----------------------------------------------------------------

@app.command()
def start(foreground: bool = typer.Option(False, "--foreground", "-f", help="stay attached to the terminal")) -> None:
    """Start the daemon."""
    config = _config()
    if foreground:
        daemon.run_foreground(config)
        return
    if daemon.is_running(config):
        print(f"{DIM}daemon already running on {config.base_url}{OFF}")
        return
    if daemon.start_background(config):
        print(f"{GREEN}▮{OFF} daemon running on {config.base_url}")
    else:
        _die(f"did not come up in time, see {config.log_path}")


@app.command()
def stop(force: bool = typer.Option(False, "--force", help="do not ask about active sessions")) -> None:
    """Stop the daemon (drops connections of open sessions)."""
    config = _config()
    if not force and daemon.is_running(config):
        try:
            status_data = daemon.admin_get(config, "status")
            sessions = status_data.get("sessions_last_hour") or []
            if attach_mod.is_attached(config):
                print(f"{YELLOW}Warning:{OFF} attached mode - once stopped, no `claude` will start.")
            if sessions:
                print(f"{YELLOW}Warning:{OFF} {len(sessions)} session(s) in the last hour. "
                      f"To turn redaction off without dropping them use `blackbar pause`.")
            if not typer.confirm("Stop anyway?", default=False):
                raise typer.Exit(1)
        except typer.Exit:
            raise
        except Exception:
            pass
    print("stopped" if daemon.stop(config) else "was not running")


@app.command()
def restart() -> None:
    """Restart the daemon (note: the vault starts empty)."""
    config = _config()
    daemon.stop(config)
    if daemon.start_background(config):
        print(f"{GREEN}▮{OFF} daemon restarted, vault empty (old placeholders will not resolve)")
    else:
        _die("restart failed")


@app.command()
def pause() -> None:
    """Pass traffic through WITHOUT redaction - sessions stay alive."""
    config = _config()
    _require_daemon(config)
    daemon.admin_post(config, "pause")
    print(f"{YELLOW}▮ PAUSED{OFF} - traffic reaches the API unredacted")


@app.command()
def resume() -> None:
    """Resume redaction."""
    config = _config()
    _require_daemon(config)
    daemon.admin_post(config, "resume")
    print(f"{GREEN}▮{OFF} redaction active")


@app.command()
def status() -> None:
    """Daemon state and actual traffic."""
    config = _config()
    if not daemon.is_running(config):
        print(f"{RED}▮{OFF} daemon not running   {DIM}blackbar start{OFF}")
        raise typer.Exit(1)
    data = daemon.admin_get(config, "status")
    mode_name, mode_detail = _mode(config)

    state = f"{YELLOW}PAUSED{OFF}" if data["paused"] else f"{GREEN}active{OFF}"
    print(f"{BOLD}blackbar {data['version']}{OFF}  {state}  {config.base_url}")
    print(f"  mode        {mode_name} {DIM}{mode_detail}{OFF}")
    print(f"  uptime      {_duration(data['uptime_s'])}")
    print(f"  layers      {', '.join(data['layers'])}")

    if data.get("model_error"):
        print(f"  model       {RED}{data['model']} - {data['model_error']}{OFF}")
    else:
        loaded = "loaded" if data["model_loaded"] else "loading..."
        print(f"  model       {data['model']} {DIM}({loaded}){OFF}")

    rules_line = f"{data['rules_count']} custom rule(s)"
    if data.get("rules_error"):
        rules_line += f" {RED}({data['rules_error']}){OFF}"
    print(f"  rules       {rules_line}")

    vault = data.get("vault") or {}
    print(f"  vault       {sum(vault.values())} value(s) {DIM}{_kinds(vault)}{OFF}")
    print(f"  requests    {data['requests']} since start")

    sessions = data.get("sessions_last_hour") or []
    if sessions:
        print(f"  sessions/1h {len(sessions)}")
        for entry in sessions[:5]:
            age = _duration(time.time() - entry["last_ts"])
            print(f"    {DIM}{entry['session']}  {entry['requests']} req, last {age} ago{OFF}")


@app.command()
def mode() -> None:
    """Which hook-up mode is active and what holds it."""
    config = _config()
    name, detail = _mode(config)
    print(f"{BOLD}{name}{OFF} {DIM}{detail}{OFF}")
    print(f"  system service     {service.status()}")
    print(f"  settings entry     {attach_mod.current() or '-'}")
    print(f"  daemon             {'running' if daemon.is_running(config) else 'not running'}")


# --- observation --------------------------------------------------------------

@app.command()
def watch() -> None:
    """Live view of the traffic."""
    import httpx

    config = _config()
    _require_daemon(config)
    print(f"{DIM}watching {config.base_url} - Ctrl+C to stop{OFF}")
    try:
        with httpx.stream("GET", f"{config.base_url}/_admin/watch", timeout=None) as response:
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                print(_format_event(json.loads(line[5:].strip())))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        _die(str(exc))


@app.command()
def last(n: int = typer.Option(5, "-n", help="how many recent requests")) -> None:
    """Details of recent requests (kinds and keys, never values)."""
    config = _config()
    _require_daemon(config)
    for entry in reversed(daemon.admin_get(config, "last", n=n)["requests"]):
        stamp = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
        flags = []
        if entry["paused"]:
            flags.append(f"{YELLOW}paused{OFF}")
        if entry["orphans"]:
            flags.append(f"{RED}orphans:{entry['orphans']}{OFF}")
        print(
            f"{stamp} {DIM}#{entry['id']}{OFF} {entry['provider']} "
            f"{DIM}{entry['model']}{OFF} session:{entry['session']} "
            f"masked:{entry['masked']} restored:{entry['restored']} "
            f"{DIM}+{entry['detect_ms']:.0f}ms detect / {entry['total_ms']:.0f}ms total{OFF} "
            + " ".join(flags)
        )
        if entry["kinds"]:
            print(f"    {DIM}{_kinds(entry['kinds'])}{OFF}")


@app.command()
def stats(
    today: bool = typer.Option(False, "--today"),
    week: bool = typer.Option(False, "--week"),
) -> None:
    """Detection counters, orphans, cache, latency."""
    config = _config()
    _require_daemon(config)
    since = None
    if today:
        since = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    elif week:
        since = time.time() - 7 * 86400
    data = daemon.admin_get(config, "stats", **({"since": since} if since else {}))
    totals = data["totals"]

    requests = totals.get("requests") or 0
    print(f"{BOLD}requests{OFF}      {requests}   sessions: {totals.get('sessions') or 0}")
    print(f"{BOLD}masked{OFF}        {totals.get('masked') or 0}")
    restored = totals.get("restored") or 0
    orphans = totals.get("orphans") or 0
    orphan_mark = f"{RED}{orphans}{OFF}" if orphans else f"{GREEN}0{OFF}"
    print(f"{BOLD}restored{OFF}      {restored}   orphans: {orphan_mark}")
    if requests:
        print(f"{BOLD}latency{OFF}       +{(totals.get('detect_ms') or 0):.0f} ms of detection per request")
    cache_read = totals.get("cache_read") or 0
    input_tokens = totals.get("input_tokens") or 0
    if cache_read or input_tokens:
        share = 100 * cache_read / max(cache_read + input_tokens, 1)
        print(f"{BOLD}cache{OFF}         {cache_read} tokens read from cache ({share:.0f}%)")
    if data["kinds"]:
        print(f"{BOLD}kinds{OFF}         {_kinds(data['kinds'])}")
    if data["layers"]:
        print(f"{BOLD}layers{OFF}        {_kinds(data['layers'])}")
    if orphans:
        print(f"\n{RED}Orphans are placeholders that could not be restored - "
              f"they may have ended up in files on disk.{OFF}")


@app.command()
def logs(follow: bool = typer.Option(False, "-f", "--follow")) -> None:
    """Daemon log."""
    config = _config()
    if not config.log_path.exists():
        _die(f"no {config.log_path}")
    if follow:
        subprocess.run(["tail", "-f", str(config.log_path)])
    else:
        subprocess.run(["tail", "-n", "50", str(config.log_path)])


# --- detection ----------------------------------------------------------------

@app.command()
def test(
    text: str = typer.Argument(None, help="text to check"),
    file: Path = typer.Option(None, "--file", help="file to check"),
) -> None:
    """Detection dry run: what would be replaced, and by which layer."""
    config = _config()
    if file:
        text = file.read_text(encoding="utf-8")
    if not text:
        _die("pass some text or --file")

    if daemon.is_running(config):
        import httpx

        response = httpx.post(f"{config.base_url}/_admin/test", json={"text": text}, timeout=120)
        response.raise_for_status()
        data = response.json()
    else:
        print(f"{DIM}daemon not running - computing locally (the model may take a moment to load){OFF}")
        data = _test_local(config, text)

    if not data["spans"]:
        print(f"{DIM}nothing detected{OFF}")
        return
    for span in data["spans"]:
        print(f"  {span['layer']:<6} {span['kind']:<14} {BOLD}{span['text']}{OFF} {DIM}→ {span['placeholder']}{OFF}")
    print(f"\n{DIM}--- redacted text ---{OFF}\n{data['masked']}")


def _test_local(config, text: str) -> dict:
    from .detect import Redactor
    from .detect.base import apply_spans
    from .detect.gliner_layer import GlinerDetector
    from .detect.regexes import RegexDetector
    from .detect.rules import RulesDetector
    from .vault import Vault

    vault = Vault()
    gliner = GlinerDetector(config.model, config.threshold) if "gliner" in config.layers else None
    redactor = Redactor(vault, RulesDetector(config.rules_path), RegexDetector(), gliner)
    spans = redactor.detect_sync(text)
    masked = apply_spans(text, spans, lambda s: vault.mask(s.kind, s.text))
    return {
        "masked": masked,
        "spans": [
            {"kind": s.kind, "layer": s.layer, "text": s.text, "placeholder": vault.mask(s.kind, s.text)}
            for s in spans
        ],
    }


rules_app = typer.Typer(help="Custom rules (rules.yaml).", no_args_is_help=True)
app.add_typer(rules_app, name="rules")


@rules_app.command("list")
def rules_list() -> None:
    """Print the rules file."""
    config = _config()
    print(config.rules_path.read_text(encoding="utf-8"))


@rules_app.command("edit")
def rules_edit() -> None:
    """Open the rules in $EDITOR and reload them."""
    config = _config()
    subprocess.run([os.environ.get("EDITOR", "vi"), str(config.rules_path)])
    rules_reload()


@rules_app.command("add")
def rules_add(
    value: str = typer.Argument(..., help="value to mask"),
    kind: str = typer.Option("custom", "--kind", help="placeholder kind"),
) -> None:
    """Append a literal rule."""
    config = _config()
    content = config.rules_path.read_text(encoding="utf-8")
    entry = f'\n  - kind: {kind}\n    values:\n      - "{value}"\n'
    if "\nterms:" in content:
        head, _, tail = content.partition("\nterms:")
        content = head + "\nterms:" + entry + tail
    else:
        content += f"\nterms:{entry}"
    config.rules_path.write_text(content, encoding="utf-8")
    print(f"added {kind}: {value}")
    rules_reload()


@rules_app.command("reload")
def rules_reload() -> None:
    """Reload the rules in a running daemon."""
    config = _config()
    if not daemon.is_running(config):
        print(f"{DIM}daemon not running - rules will load at start{OFF}")
        return
    data = daemon.admin_post(config, "rules/reload")
    if data.get("error"):
        _die(data["error"])
    print(f"loaded {data['count']} rule(s)")


# --- model --------------------------------------------------------------------

model_app = typer.Typer(help="GLiNER model.", no_args_is_help=True)
app.add_typer(model_app, name="model")

KNOWN_MODELS = [
    ("urchade/gliner_multi_pii-v1", "~500 MB", "default, multilingual, PII entity types"),
    ("urchade/gliner_small-v2.1", "~150 MB", "for low RAM; weaker outside English"),
    ("urchade/gliner_large-v2.1", "~1.5 GB", "best quality, slower"),
]


@model_app.command("list")
def model_list() -> None:
    """Known models to choose from."""
    for name, size, note in KNOWN_MODELS:
        print(f"  {BOLD}{name}{OFF}  {size}\n    {DIM}{note}{OFF}")
    print(f"\n{DIM}to change: blackbar config set detection.model <name>{OFF}")


@model_app.command("pull")
def model_pull(name: str = typer.Argument(None, help="model name (defaults to the configured one)")) -> None:
    """Download the model (shows progress)."""
    config = _config()
    name = name or config.model
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        _die("huggingface_hub is missing - install the project dependencies")
    print(f"downloading {name}...")
    path = snapshot_download(name)
    print(f"{GREEN}▮{OFF} done: {path}")


@model_app.command("status")
def model_status() -> None:
    """Whether the model is downloaded and loaded."""
    config = _config()
    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(config.model, local_files_only=True)
        size = sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
        print(f"{GREEN}▮{OFF} {config.model} downloaded ({size / 1e6:.0f} MB)\n  {DIM}{path}{OFF}")
    except Exception:
        print(f"{YELLOW}▮{OFF} {config.model} not downloaded   {DIM}blackbar model pull{OFF}")
    if daemon.is_running(config):
        data = daemon.admin_get(config, "status")
        print(f"  in daemon: {'loaded' if data['model_loaded'] else 'not loaded'}"
              + (f" {RED}{data['model_error']}{OFF}" if data.get("model_error") else ""))


# --- vault --------------------------------------------------------------------

vault_app = typer.Typer(help="Value ↔ placeholder map.", no_args_is_help=True)
app.add_typer(vault_app, name="vault")


@vault_app.command("status")
def vault_status() -> None:
    """Counters per kind, without values."""
    config = _config()
    _require_daemon(config)
    counts = daemon.admin_get(config, "vault")["counts"]
    if not counts:
        print(f"{DIM}vault empty{OFF}")
        return
    for kind, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {kind:<16} {count}")


@vault_app.command("show")
def vault_show(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """⚠ Print the original values."""
    import httpx

    config = _config()
    _require_daemon(config)
    if not yes:
        print(f"{YELLOW}⚠ This is the only command that prints real data to the screen.{OFF}")
        if not typer.confirm("Continue?", default=False):
            raise typer.Exit(1)
    response = httpx.get(f"{config.base_url}/_admin/vault", params={"reveal": "1"}, timeout=10)
    for entry in response.json()["entries"]:
        print(f"  {entry['kind']:<14} {DIM}{{{{sensitive:{entry['kind']}:{entry['key']}}}}}{OFF}  {entry['value']}")


@vault_app.command("clear")
def vault_clear(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """⚠ Wipe the map - invalidates prompt cache and restore for old placeholders."""
    config = _config()
    _require_daemon(config)
    if not yes:
        print(f"{YELLOW}⚠ Open sessions lose restoration for their earlier history "
              f"and the prompt cache is gone.{OFF}")
        if not typer.confirm("Continue?", default=False):
            raise typer.Exit(1)
    daemon.admin_post(config, "vault/clear")
    print("vault cleared")


# --- service and attach -------------------------------------------------------

service_app = typer.Typer(help="System service (launchd/systemd).", no_args_is_help=True)
app.add_typer(service_app, name="service")


@service_app.command("install")
def service_install() -> None:
    """Install the service - the daemon survives reboots and crashes."""
    config = _config()
    try:
        path = service.install(config)
    except Exception as exc:
        _die(str(exc))
    print(f"{GREEN}▮{OFF} service installed: {path}")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Remove the service."""
    config = _config()
    if attach_mod.is_attached(config):
        print(f"{YELLOW}⚠ Attached mode is on - without the service, claude stops starting "
              f"after every daemon crash. Consider `blackbar detach` first.{OFF}")
        if not typer.confirm("Remove the service anyway?", default=False):
            raise typer.Exit(1)
    print("removed" if service.uninstall(config) else "was not installed")


@service_app.command("status")
def service_status() -> None:
    """Service state."""
    print(service.status())


@app.command()
def attach(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Hook the proxy up for good: every `claude` goes through blackbar."""
    config = _config()
    if not service.installed():
        _die("run `blackbar service install` first - without the service a dead daemon "
             "means claude does not start at all")
    if attach_mod.is_attached(config):
        print(f"{DIM}already attached: {attach_mod.current()}{OFF}")
        return
    print(f"{BOLD}{attach_mod.SETTINGS_PATH}{OFF} after the change:")
    print(f"{DIM}{attach_mod.preview(config)}{OFF}")
    if not yes and not typer.confirm("Write it?", default=False):
        raise typer.Exit(1)
    backup = attach_mod.attach(config)
    print(f"{GREEN}▮{OFF} attached - new claude windows go through the proxy")
    if backup:
        print(f"{DIM}backup: {backup}{OFF}")


@app.command()
def detach() -> None:
    """Undo the global hook-up."""
    config = _config()
    if attach_mod.detach(config):
        print("detached - `claude` connects directly again")
    else:
        print(f"{DIM}was not attached{OFF}")


# --- configuration and diagnostics --------------------------------------------

config_app = typer.Typer(help="Configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("get")
def config_get(key: str = typer.Argument(None)) -> None:
    """Show the configuration."""
    config = _config()
    if key is None:
        print(config.path.read_text(encoding="utf-8"))
        return
    values = {
        "proxy.host": config.host,
        "proxy.port": config.port,
        "launcher.autostart": config.autostart,
        "detection.model": config.model,
        "detection.threshold": config.threshold,
        "detection.layers": config.layers,
    }
    if key not in values:
        _die(f"unknown key: {key}")
    print(values[key])


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a value (needs a daemon restart)."""
    _config()
    try:
        print(config_mod.set_value(key, value))
    except ValueError as exc:
        _die(str(exc))
    print(f"{DIM}takes effect after `blackbar restart`{OFF}")


@config_app.command("edit")
def config_edit() -> None:
    """Open the config in $EDITOR."""
    config = _config()
    subprocess.run([os.environ.get("EDITOR", "vi"), str(config.path)])


@app.command()
def doctor() -> None:
    """Check proxy health: port, model, upstream, rules, mode consistency."""
    config = _config()
    problems = 0

    def check(label: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        nonlocal problems
        if ok:
            mark = f"{GREEN}▮{OFF}"
        elif warn:
            mark = f"{YELLOW}▮{OFF}"
        else:
            mark = f"{RED}▮{OFF}"
            problems += 1
        print(f" {mark} {label}{('  ' + DIM + detail + OFF) if detail else ''}")

    check("config", config.path.exists(), str(config.path))
    check("rules", config.rules_path.exists(), str(config.rules_path))

    running = daemon.is_running(config)
    check("daemon", running, config.base_url if running else "not answering", warn=not running)

    if running:
        data = daemon.admin_get(config, "status")
        check("model", data["model_loaded"], data.get("model_error") or data["model"],
              warn=not data["model_loaded"] and not data.get("model_error"))
        check("rules loaded", not data.get("rules_error"), data.get("rules_error") or f"{data['rules_count']}")
        if data["paused"]:
            check("redaction", False, "PAUSED - traffic goes out unredacted", warn=True)

    import httpx

    for name, provider in config.providers.items():
        if not provider.upstream:
            continue
        try:
            response = httpx.get(f"{provider.upstream}/v1/models", timeout=5)
            reachable = response.status_code < 500
        except httpx.HTTPError:
            reachable = False
        check(f"upstream {name}", reachable, provider.upstream, warn=not reachable)

    attached = attach_mod.is_attached(config)
    installed = service.installed()
    if attached and not installed:
        check("mode consistency", False,
              "attached without a system service - claude will not start after a daemon crash")
    else:
        check("mode consistency", True, _mode(config)[0])

    print()
    print(f"{GREEN}no problems{OFF}" if problems == 0 else f"{RED}problems: {problems}{OFF}")
    raise typer.Exit(1 if problems else 0)


@app.command()
def version() -> None:
    """Version."""
    print(f"blackbar {__version__}")


@app.command()
def uninstall(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Undo the installation: hook-up, service, state data."""
    config = _config()
    print("To remove:")
    print(f"  settings.json entry   {'yes' if attach_mod.is_attached(config) else 'no'}")
    print(f"  system service        {service.status()}")
    print(f"  state data            {config_mod.state_dir()}")
    print(f"  configuration         {config_mod.config_dir()} {DIM}(kept){OFF}")
    if not yes and not typer.confirm("Continue?", default=False):
        raise typer.Exit(1)
    attach_mod.detach(config)
    service.uninstall(config)
    daemon.stop(config)
    for path in (config.pid_path, config.db_path, config.log_path):
        path.unlink(missing_ok=True)
    print("done - the config directory is left in place (remove it by hand if you want)")


# --- helpers ------------------------------------------------------------------

def _mode(config) -> tuple[str, str]:
    attached = attach_mod.is_attached(config)
    installed = service.installed()
    if attached and installed:
        return "attached", "every `claude` through the proxy, daemon under a service"
    if attached:
        return "attached (inconsistent)", "no service - a daemon crash stops claude"
    if installed:
        return "service", "daemon under a service, proxy only for `blackbar claude`"
    return "manual", "proxy only for `blackbar claude`"


def _kinds(counts: dict) -> str:
    return "  ".join(f"{kind}:{count}" for kind, count in sorted(counts.items(), key=lambda i: -i[1]))


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"


def _format_event(event: dict) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(event["ts"]))
    kinds = _kinds(event.get("kinds") or {})
    orphans = event.get("orphans") or 0
    tail = f" {RED}orphans:{orphans}{OFF}" if orphans else ""
    if event.get("paused"):
        tail += f" {YELLOW}paused{OFF}"
    return (
        f"{stamp} {event['provider']} {DIM}#{event['id']}{OFF} "
        f"{kinds or DIM + 'none' + OFF} "
        f"restored:{event['restored']} {DIM}+{event['detect_ms']:.0f}ms{OFF}{tail}"
    )


def main() -> None:
    app()
