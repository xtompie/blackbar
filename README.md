# blackbar

A local proxy that strips confidential data out of the traffic Claude Code sends and
puts it back in the reply. The model in the cloud never sees the real values, while on
your machine everything reads normally.

```
Claude Code ──► blackbar (127.0.0.1:8555) ──► api.anthropic.com
     ▲                │   emails, names, keys → {{sensitive:...}}
     └────────────────┘   {{sensitive:...}} → originals
```

## Install

Paste this into Claude Code, in any directory:

```
Install blackbar for me: https://github.com/xtompie/blackbar
Clone the repo, read INSTALL.md and follow the installation step by step.
```

Claude picks the package manager, the Python version and the model variant that fit
your machine, downloads the model with visible progress, and finishes by showing you a
working redaction. It will also ask how you want the daemon managed - see *Modes*.

Installation deliberately does not go through PyPI: an instruction written for an agent
survives ecosystem churn better than a frozen `pip install`, and you have Claude at hand
anyway.

## Quick start

```bash
blackbar claude              # claude through the proxy (arguments passed verbatim)
blackbar watch               # live view of what gets replaced
blackbar test "Jan Kowalski, jan@example.com"
```

## Modes

| mode | what it does | what it touches |
|---|---|---|
| **manual** *(default)* | only `blackbar claude` goes through the proxy, the daemon starts lazily | nothing outside `~/.config/blackbar/` |
| **service** | same, but the daemon runs under launchd/systemd - survives reboots and crashes | a service file |
| **attached** | every `claude` goes through the proxy, nothing to remember | also `~/.claude/settings.json` |

**Attached mode requires service mode.** Without the launcher nobody checks whether the
daemon is alive, so a dead daemon means `claude` will not start at all. That is
intentional: the alternative would be silently sending data out unredacted.

Switching: `blackbar service install`, `blackbar attach`, `blackbar detach`,
`blackbar mode`.

## Turning it off

```bash
blackbar pause      # traffic flows unredacted, open sessions stay alive
blackbar resume
blackbar stop       # drops connections; in attached mode it stops every claude window
```

## What gets scanned

| layer | what it catches |
|---|---|
| `rules` | your own patterns from `~/.config/blackbar/rules.yaml` - client names, project codes |
| `regex` | emails, API keys, tokens, JWTs, IBANs, national IDs, cards, passwords in database URLs |
| `gliner` | names, companies, addresses - a local NER model, multilingual |

In a request the scanned parts are `system`, message content and **tool results** - the
last one being the main leak source, since that is where file contents and command
output end up. Tool definitions and signed `thinking` blocks are left untouched.

Everything happens locally. No classifier calls out to the cloud - that would just move
the leak to a different vendor.

## Statistics

```bash
blackbar status     # mode, model, active sessions, traffic
blackbar last -n 5  # what went out in recent requests (kinds and keys, never values)
blackbar stats      # detections per kind and layer, orphans, cache, latency
```

The metric that matters most is **orphans**: placeholders that could not be restored
because the model mangled them. Every orphan may have landed in a file on disk.

Second is **traffic per session** - three windows open and traffic from two of them
means one is bypassing the proxy.

The event log stores kinds, layers and counters. Never content: an audit log must not
become a new place to leak from.

## Limitations

- Restarting the daemon wipes the vault, so open sessions lose restoration for their
  earlier history. `blackbar vault clear` does the same.
- Remote Control in Claude Code does not work behind a proxy (since v2.1.196 it is
  gated to `api.anthropic.com`).
- Redaction changes the prompt, so the first request after a rules change misses the
  prompt cache.

## License

MIT
