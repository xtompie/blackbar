"""blackbar CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer

from . import (
    __version__, attach as attach_mod, config as config_mod, daemon, launcher, service,
    stats as stats_mod,
)

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
    raise typer.Exit(launcher.launch(_config(), ctx.args))


@app.command(context_settings=PASSTHROUGH)
def direct(ctx: typer.Context) -> None:
    """Run claude with the proxy bypassed - nothing is redacted."""
    args = ctx.args
    binary = args[0] if args and not args[0].startswith("-") else launcher.BINARY
    rest = args[1:] if binary is not launcher.BINARY else args
    raise typer.Exit(launcher.launch_direct(binary, rest))


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
    if not daemon.start_background(config):
        _die(f"did not come up in time, see {config.log_path}")
    if not daemon.is_ready(config):
        print(f"{DIM}loading the detection model into the daemon - about 15 seconds{OFF}", flush=True)
        daemon.wait_until_ready(config)
    print(f"{GREEN}▮{OFF} daemon running on {config.base_url}")


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
                print(f"{YELLOW}Warning:{OFF} {len(sessions)} session(s) in the last hour "
                      f"will lose their connection.")
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
        print(f"{GREEN}▮{OFF} daemon restarted, vault empty - sessions keep working, prompt cache does not")
    else:
        _die("restart failed")


@app.command()
def status() -> None:
    """What is running, what it covers, and what it has actually done."""
    config = _config()
    if not daemon.is_running(config):
        print(f"{RED}▮{OFF} daemon not running   {DIM}blackbar start{OFF}")
        raise typer.Exit(1)
    data = daemon.admin_get(config, "status")
    mode_name, mode_detail = _mode(config)

    print(f"{BOLD}blackbar {data['version']}{OFF}  {GREEN}running{OFF}  {config.base_url}")
    print(f"  mode        {mode_name} {DIM}{mode_detail}{OFF}")
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["started_ts"]))
    print(f"  uptime      {_duration(data['uptime_s'])} {DIM}(since {started}){OFF}")

    print(f"\n{BOLD}redaction{OFF}")
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
    print(f"  endpoints   {', '.join(data['endpoints'])} {DIM}(anything else is refused){OFF}")

    print(f"\n{BOLD}attachments{OFF}")
    print(f"  read        {', '.join(data['attachments_read'])}"
          f"  {DIM}→ extracted, redacted, sent as text{OFF}")
    allowed = data.get("attachments_allowed") or []
    if allowed:
        print(f"  {YELLOW}sent as-is  {', '.join(allowed)}  ⚠ not redacted{OFF}")
    else:
        print(f"  sent as-is  {DIM}none - everything unreadable is refused{OFF}")

    print(f"\n{BOLD}traffic{OFF}")
    last_ts = data.get("last_request_ts")
    when = f"{_duration(time.time() - last_ts)} ago" if last_ts else "never"
    print(f"  requests    {data['requests']} since start   {DIM}last: {when}{OFF}")
    print(f"  last hour   {data['requests_last_hour']} requests, "
          f"{data['masked_last_hour']} values masked")
    orphans = data.get("orphans_last_hour") or 0
    if orphans:
        print(f"  {RED}orphans     {orphans} in the last hour - a placeholder came back mangled{OFF}")
    refusals = data.get("refusals_last_hour") or {}
    if refusals:
        print(f"  {YELLOW}refused     {_kinds(refusals)}{OFF}")

    vault = data.get("vault") or {}
    print(f"  vault       {sum(vault.values())} value(s) {DIM}{_kinds(vault)}{OFF}")

    sessions = data.get("sessions_last_hour") or []
    if sessions:
        print(f"  sessions    {len(sessions)} in the last hour")
        for entry in sessions[:5]:
            age = _duration(time.time() - entry["last_ts"])
            print(f"    {DIM}{entry['session']}  {entry['requests']} req, last {age} ago{OFF}")

    print(f"\n{DIM}log: {data['log_path']}{OFF}")


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
def watch(
    reveal: bool = typer.Option(False, "--reveal", help="⚠ also print the replaced values"),
) -> None:
    """Follow the request log, one line per request.

    This is `tail -f` on ~/.local/state/blackbar/requests.log with the fields laid out
    for reading. The plain file works with tail, grep and anything else on its own.
    """
    config = _config()
    path = config.requests_path
    if reveal:
        print(f"{YELLOW}⚠ --reveal prints real data to the screen.{OFF}")
    print(f"{DIM}following {path} - Ctrl+C to stop{OFF}")

    values: dict[str, str] = {}
    try:
        handle = path.open("r", encoding="utf-8", errors="replace") if path.exists() else None
        if handle:
            handle.seek(0, 2)
        while True:
            if handle is None:
                if not path.exists():
                    time.sleep(0.5)
                    continue
                handle = path.open("r", encoding="utf-8", errors="replace")
            line = handle.readline()
            if not line:
                time.sleep(0.2)
                continue
            entry = stats_mod.parse_line(line)
            if not entry:
                continue
            # flush: watch is meant to be piped into grep/tee as well
            print(_format_event(entry), flush=True)
            if reveal and entry["keys"]:
                _print_values(config, entry["keys"], values)
    except KeyboardInterrupt:
        pass


def _print_values(config, keys: list, cache: dict[str, str]) -> None:
    """Resolves vault keys from a log line into the values they replaced.

    The file never holds a value; only the running daemon can answer this.
    """
    import httpx

    if any(key not in cache for _, key in keys):
        if not daemon.is_running(config):
            print(f"    {DIM}(daemon not running - values unavailable){OFF}", flush=True)
            return
        response = httpx.get(f"{config.base_url}/_admin/vault", params={"reveal": "1"}, timeout=10)
        cache.clear()
        cache.update({entry["key"]: entry["value"] for entry in response.json()["entries"]})
    for kind, key in keys:
        value = cache.get(key, "?")
        print(f"    {DIM}{kind:<12}{OFF} {value} {DIM}→ {{{{sensitive:{kind}:{key}}}}}{OFF}", flush=True)


@app.command()
def last(n: int = typer.Option(5, "-n", help="how many recent requests")) -> None:
    """Details of recent requests (kinds and keys, never values)."""
    config = _config()
    # the log has two lines per exchange, so pair them up before taking the last n
    all_entries = stats_mod.exchanges(stats_mod.read_lines(config.requests_path))
    for entry in all_entries[-n:]:
        stamp = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
        if entry.get("refused"):
            print(f"{stamp} {DIM}#{entry['id']}{OFF} {RED}refused: {entry['refused']}{OFF} "
                  f"{DIM}{entry.get('path', '-')}{OFF}")
            continue
        print(
            f"{stamp} {DIM}#{entry['id']} {entry['model']} session:{entry['session']}{OFF}"
        )
        print(f"    {DIM}sent{OFF}  {entry['masked']} masked "
              f"{DIM}({_kinds(entry['kinds']) or 'nothing found'}) "
              f"in {entry['chars']} chars, {entry['detect_ms']:.0f}ms{OFF}")
        back = f"{entry['restored']} restored"
        if entry["orphans"]:
            back += f", {RED}{entry['orphans']} NOT restored{OFF}"
        if entry.get("pending"):
            print(f"    {YELLOW}back{OFF}  still running - no reply yet")
        else:
            print(f"    {DIM}back{OFF}  {back} {DIM}(status {entry['status']}, "
                  f"{entry['total_ms']:.0f}ms total){OFF}")


@app.command()
def stats(
    today: bool = typer.Option(False, "--today"),
    week: bool = typer.Option(False, "--week"),
) -> None:
    """Detection counters, orphans, cache, latency."""
    config = _config()
    since = None
    if today:
        since = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    elif week:
        since = time.time() - 7 * 86400
    data = stats_mod.summary(stats_mod.read_lines(config.requests_path, since=since))
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


@app.command()
def file(
    source: Path = typer.Argument(..., help="file to read"),
    target: Path = typer.Argument(None, help="file to write (default: stdout)"),
) -> None:
    """Replace confidential values in a file with placeholders.

    For text going somewhere other than Claude Code - paste the result anywhere. The
    placeholder keeps an id, so the model can still tell one person from another and
    answer about "{{sensitive:person:a1b2c3}}"; you know who that is.

    Works without the daemon; it just has to load the model first.
    """
    config = _config()
    if not source.exists():
        _die(f"no such file: {source}")
    text = source.read_text(encoding="utf-8")

    masked = kinds = None
    if daemon.is_running(config):
        import httpx

        try:
            response = httpx.post(f"{config.base_url}/_admin/mask", json={"text": text}, timeout=1800)
            response.raise_for_status()
            data = response.json()
            masked, kinds = data["text"], data["kinds"]
        except httpx.HTTPError:
            # An older daemon has no such endpoint; do it here instead.
            masked = None
    if masked is None:
        print(f"{DIM}loading the detection model - about 15 seconds{OFF}", file=sys.stderr, flush=True)
        masked, kinds = _mask_locally(config, text)

    if target:
        target.write_text(masked, encoding="utf-8")
    else:
        print(masked, end="")
    print(f"{DIM}{sum(kinds.values())} value(s) replaced ({_kinds(kinds) or 'none'}){OFF}",
          file=sys.stderr)


def _mask_locally(config, text: str) -> tuple[str, dict]:
    """Same layers as the proxy, without needing it to run."""
    import collections
    import os

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    import warnings

    warnings.filterwarnings("ignore")

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
    return masked, dict(collections.Counter(span.kind for span in spans))


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
    ("urchade/gliner_multi_pii-v1", "1.2 GB", "default, multilingual, PII entity types"),
    ("urchade/gliner_small-v2.1", "~500 MB", "for low RAM; weaker outside English"),
    ("urchade/gliner_large-v2.1", "~2 GB", "best quality, slower"),
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
        print(f"{YELLOW}⚠ The prompt cache is gone. Any placeholder already written to "
              f"a file stays unresolvable.{OFF}")
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
def attach(
    yes: bool = typer.Option(False, "--yes", "-y"),
    force: bool = typer.Option(False, "--force", help="attach without a system service"),
) -> None:
    """Hook the proxy up for good: every `claude` goes through blackbar."""
    config = _config()
    if not service.installed():
        if not force:
            _die("run `blackbar service install` first - without the service a dead daemon "
                 "means claude does not start at all. Use --force to attach anyway.")
        print(f"{YELLOW}⚠ No system service: whenever the daemon is down, claude will not "
              f"start at all. You keep the pieces.{OFF}")
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


allow_app = typer.Typer(help="File types sent through unredacted.", no_args_is_help=True)
app.add_typer(allow_app, name="allow")


@allow_app.command("list")
def allow_list() -> None:
    """Types that are sent as-is, and types that are read and redacted."""
    from .attachments import supported_types

    config = _config()
    print(f"{BOLD}read and redacted{OFF}  {', '.join(supported_types())}")
    if config.allow:
        print(f"{YELLOW}{BOLD}sent as-is{OFF}         {', '.join(config.allow)}  {YELLOW}⚠ not redacted{OFF}")
    else:
        print(f"{BOLD}sent as-is{OFF}         {DIM}none - anything unreadable is refused{OFF}")


@allow_app.command("add")
def allow_add(
    media_type: str = typer.Argument(..., help="e.g. image/png"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """⚠ Let a type through without redaction."""
    from .attachments import reader_for

    config = _config()
    if media_type in config.allow:
        print(f"{DIM}already allowed{OFF}")
        return
    if reader_for(media_type) is not None:
        print(f"{DIM}note: {media_type} is already read and redacted; allowing it means "
              f"sending the raw file instead{OFF}")
    print(f"{YELLOW}⚠ {media_type} will be sent to the API exactly as it is - "
          f"blackbar will not look inside it.{OFF}")
    if not yes and not typer.confirm("Continue?", default=False):
        raise typer.Exit(1)
    _write_allow(config, [*config.allow, media_type])
    print(f"{GREEN}▮{OFF} {media_type} allowed")


@allow_app.command("remove")
def allow_remove(media_type: str = typer.Argument(..., help="e.g. image/png")) -> None:
    """Stop sending a type; it goes back to being refused."""
    config = _config()
    if media_type not in config.allow:
        print(f"{DIM}not in the list{OFF}")
        return
    _write_allow(config, [item for item in config.allow if item != media_type])
    print(f"{media_type} removed - requests carrying it will be refused again")


def _write_allow(config, values: list[str]) -> None:
    import json

    config_mod.set_value("attachments.allow", json.dumps(values))
    if daemon.is_running(config):
        daemon.admin_post(config, "allow/reload")
    else:
        print(f"{DIM}daemon not running - the change applies at start{OFF}")


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
        "upstream.url": config.upstream,
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
    import httpx

    try:
        response = httpx.get(f"{config.upstream}/v1/models", timeout=5)
        reachable = response.status_code < 500
    except httpx.HTTPError:
        reachable = False
    check("upstream", reachable, config.upstream, warn=not reachable)

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


@app.command("help")
def help_command(ctx: typer.Context) -> None:
    """Show this help (same as --help)."""
    parent = ctx.parent
    print(parent.get_help() if parent is not None else ctx.get_help())


@app.command()
def version() -> None:
    """Version."""
    print(f"blackbar {__version__}")


@app.command()
def update(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Pull the repository and reinstall, so the command matches the code."""
    config = _config()
    repo = _find_repo(config)
    if repo is None:
        _die(f"no clone at {config_mod.repo_dir()} - reinstall with INSTALL.md, "
             f"which puts it there")
    print(f"{DIM}repository: {repo}{OFF}")

    before = _git(repo, "rev-parse", "--short", "HEAD")
    pull = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"],
                          capture_output=True, text=True)
    if pull.returncode != 0:
        _die(pull.stderr.strip() or "git pull failed")
    after = _git(repo, "rev-parse", "--short", "HEAD")

    if before == after:
        print("already up to date")
        return
    changes = _git(repo, "log", "--oneline", f"{before}..{after}")
    print(changes or f"{before} → {after}")

    if not yes and not typer.confirm("Reinstall from this?", default=True):
        raise typer.Exit(1)

    editable = _is_editable()
    if editable:
        print(f"{DIM}editable install - the code is already live{OFF}")
    else:
        pip = config_mod.venv_dir() / "bin" / "pip"
        command = ([str(pip), "install", "--quiet", "-e", "."] if pip.exists()
                   else [sys.executable, "-m", "pip", "install", "--quiet", "-e", "."])
        result = subprocess.run(command, cwd=str(repo), capture_output=True, text=True)
        if result.returncode != 0:
            _die(result.stderr.strip() or "reinstall failed")
        print(f"{GREEN}▮{OFF} reinstalled")

    if daemon.is_running(config):
        print(f"{DIM}restarting the daemon so it runs the new code{OFF}")
        daemon.stop(config)
        daemon.start_background(config)
        print(f"{GREEN}▮{OFF} daemon restarted, vault empty - prompt cache starts over")


def _find_repo(config) -> Path | None:
    """The clone lives in one place. Running from a working copy also counts."""
    standard = config_mod.repo_dir()
    if (standard / ".git").exists():
        return standard
    here = Path(__file__).resolve().parents[2]
    return here if (here / ".git").exists() else None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return result.stdout.strip()


def _is_editable() -> bool:
    """An editable install runs straight from the clone, so a pull is already enough."""
    return (Path(__file__).resolve().parents[2] / ".git").exists()


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
    for path in (config.pid_path, config.log_path):
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
    """One line per phase, as it happens: the request when it leaves, the reply when it
    lands. Zeros are left out - except orphans, where anything but zero is the alarm."""
    stamp = time.strftime("%H:%M:%S", time.localtime(event["ts"]))
    tag = f"{stamp} {DIM}#{event['id']}{OFF}"

    if event.get("refused"):
        where = event.get("path", "-")
        return f"{tag} {RED}refused: {event['refused']}{OFF} {DIM}{where}{OFF}"

    if event.get("phase") == "back":
        parts = [tag, f"{DIM}←{OFF}"]
        if event.get("orphans"):
            parts.append(f"{RED}{event['orphans']} NOT restored{OFF}")
        elif event.get("restored"):
            parts.append(f"{event['restored']} restored")
        else:
            parts.append(f"{DIM}nothing to restore{OFF}")
        parts.append(f"{DIM}status {event.get('status')} · {event.get('total_ms', 0):.0f}ms{OFF}")
        return " ".join(parts)

    kinds = _kinds(event.get("kinds") or {})
    parts = [tag, f"{DIM}→{OFF}", kinds or f"{DIM}nothing to mask{OFF}"]
    detect = event.get("detect_ms") or 0
    if detect >= 1:
        parts.append(f"{DIM}{detect:.0f}ms scanning {event.get('chars', 0)} chars{OFF}")
    return " ".join(parts)



def main() -> None:
    app()
