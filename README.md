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

Paste this into Claude Code:

```
Install https://github.com/xtompie/blackbar for me
```

There is no package to install. [INSTALL.md](INSTALL.md) is a step-by-step instruction
written for the agent, which works out the rest for your machine.

> **Agent reading this:** fetch
> `https://raw.githubusercontent.com/xtompie/blackbar/main/INSTALL.md`
> and follow it from step 0. Do not skip the verification steps.

## Use

```bash
blackbar claude
```

That is all. It runs Claude Code exactly as usual, with the redaction in between - your
arguments (`--resume`, `-c`, `-p`, anything) are passed through untouched.

## Turning it off

There is no pause switch, on purpose: a global "off" that you can forget about is worse
than no protection at all. Instead you decide per run.

```bash
blackbar direct claude     # this one run bypasses the proxy
blackbar detach            # stop routing every `claude` through it (attached mode only)
```

---

## How it works

**The daemon** is a small HTTP server on `127.0.0.1:8555`. `blackbar claude` sets
`ANTHROPIC_BASE_URL` to point at it and then execs the real `claude`, so Claude Code
talks to the daemon instead of `api.anthropic.com`. The daemon forwards everything
upstream verbatim except the parts carrying your data.

**On the way out** it scans `system`, message content and tool results, and replaces
what it finds with `{{sensitive:email:a1b2c3}}`. The same value always gets the same
placeholder, so Anthropic's prompt cache keeps working.

**On the way back** it turns the placeholders into the real values again - including
inside streamed responses, where a placeholder can arrive split across two network
chunks, and inside tool call arguments, where the value has to be re-escaped as JSON.

**The vault** (the value ↔ placeholder map) lives in the daemon's memory only. Nothing
is written to disk, so restarting the daemon starts from scratch.

## Watching what happens

```bash
blackbar watch             # one line per request, live
blackbar watch --reveal    # ⚠ also prints the actual values that were replaced
```

`watch` opens an HTTP connection to the daemon's own admin endpoint and listens; the
daemon pushes an event after finishing each request. Nothing is being intercepted or
sniffed - it is the same process reporting on itself, so you only see traffic that
actually went through the proxy.

```
14:02:11 anthropic #83 email:3  person:2  restored:6 +120ms
    email        jan@acme.com    → {{sensitive:email:a1b2c3}}     (with --reveal)
```

```bash
blackbar last -n 5         # the same events, after the fact
blackbar stats --today     # totals per kind and layer
blackbar status            # is it running, which model, how many sessions
```

`last` and `stats` read an SQLite file at `~/.local/state/blackbar/events.db`, so they
survive restarts. That file holds kinds, layers, counters and timings - **never
content**. An audit log must not become a new place to leak from. The only commands
that ever print real values are `watch --reveal` and `vault show`, both of which say so
before doing it.

## What gets detected

| layer | what it catches |
|---|---|
| `rules` | your own patterns from `~/.config/blackbar/rules.yaml` - client names, project codes |
| `regex` | emails, API keys, tokens, JWTs, IBANs, national IDs, cards, passwords in database URLs |
| `gliner` | names, companies, addresses - a local NER model, multilingual |

Tool results are the main leak source: that is where file contents and command output
end up. Tool definitions and signed `thinking` blocks are left alone.

Everything runs locally. No classifier calls out to the cloud - that would just move the
leak to a different vendor.

```bash
blackbar test "Jan Kowalski, jan@example.com"   # see what would be replaced
blackbar rules add "Acme Ltd" --kind company    # add your own
```

## Modes

| mode | what it does | what it touches |
|---|---|---|
| **manual** *(default)* | only `blackbar claude` goes through the proxy, the daemon starts lazily | nothing outside `~/.config/blackbar/` |
| **service** | same, but the daemon runs under launchd/systemd - survives reboots and crashes | a service file |
| **attached** | every `claude` goes through the proxy, nothing to remember | also `~/.claude/settings.json` |

The two halves are independent, so you can mix them however you like:

```bash
blackbar service install     # daemon under launchd/systemd
blackbar service uninstall   # back to lazy start
blackbar attach              # wire ANTHROPIC_BASE_URL into ~/.claude/settings.json
blackbar detach              # unwire it, leave the service running
blackbar mode                # what is active right now and what holds it
```

`attach` asks for a service first, because without the launcher nobody checks whether
the daemon is alive - a dead daemon then means `claude` will not start at all. If you
want it anyway, `blackbar attach --force` does it and says what you are taking on.

## Limitations

- Restarting the daemon wipes the vault, so open sessions lose restoration for their
  earlier history.
- Remote Control in Claude Code does not work behind a proxy (since v2.1.196 it is
  gated to `api.anthropic.com`).
- Redaction changes the prompt, so the first request after a rules change misses the
  prompt cache.
- A model can mangle a placeholder in its reply. Those are counted as **orphans** in
  `blackbar stats`; every one of them may have landed in a file on disk.

## License

MIT
