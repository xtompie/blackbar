# blackbar

A local proxy that strips confidential data out of the traffic Claude Code sends and
puts it back in the reply. The model in the cloud never sees the real values, while on
your machine everything reads normally.

The finding is done by an LLM too - a local one, running on your CPU. Names, companies
and addresses do not follow a pattern, so a regex will not catch them, and shipping the
text to a cloud classifier would just move the leak to another vendor. Nothing leaves
the machine, not even the step that decides what is confidential.

![Values are replaced on the way out and restored on the way back; the real ones never cross the bar](demo/blackbar-demo.gif)

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

**What it cannot do: answer questions about the values it hid.** Ask *"is this email
address valid?"*, *"what date of birth does this ID number encode?"* or *"sort these
people alphabetically"* and the model is looking at `{{sensitive:person:1a33de}}`, not at
the value - so the answer will be useless. Redaction is not selective and cannot be: the
proxy has no way of knowing that this particular value is the subject of the question
rather than incidental to it. When the value itself is the point, run that one session
with `blackbar direct claude`.

## Turning it off

There is no pause switch, on purpose: a global "off" that you can forget about is worse
than no protection at all. Instead you decide per run.

```bash
blackbar direct            # this one run bypasses the proxy (same as `blackbar direct claude`)
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

The daemon appends one line per request to `~/.local/state/blackbar/requests.log`:

```
ts=1785529390.169 id=1 provider=anthropic session=c87238 model=claude-opus-5 stream=0
status=401 masked=2 restored=0 orphans=0 detect_ms=1.4 total_ms=5528.8
kinds=aws_key:1,email:1 layers=regex:2 keys=email:e09c6b,aws_key:8b4c78 cache_read=0
```

That is the whole mechanism - a text file. `tail -f`, `grep`, anything you already use
works on it, blackbar included:

```bash
blackbar watch             # tail -f with the fields laid out for reading
blackbar watch --reveal    # ⚠ also prints the actual values that were replaced
blackbar last -n 5         # the last few lines
blackbar stats --today     # totals per kind and layer
```

The line says what happened, never what was in it: kinds, layers, counters, timings and
vault keys, which are hashes. `--reveal` resolves those keys by asking the running
daemon, because only its memory holds the values - the file never does. It and
`vault show` are the only commands that print real data, and both say so first.

If the log shows nothing while a Claude Code window is running, that window is not going
through the proxy.

## What gets detected

| layer | what it catches |
|---|---|
| `rules` | your own patterns from `~/.config/blackbar/rules.yaml` - client names, project codes |
| `regex` | emails, API keys, tokens, JWTs, IBANs, national IDs, cards, passwords in database URLs |
| `gliner` | names, companies, addresses - a local LLM (NER), multilingual |

Tool results are the main leak source: that is where file contents and command output
end up. Tool definitions and signed `thinking` blocks are left alone.

**Attachments** would normally travel as base64 and be parsed on Anthropic's side, i.e.
leave the machine whole. Instead blackbar opens them locally: PDF, Word, and anything
textual (txt, csv, md, html, JSON, XML, YAML) are turned into text, redacted like any
other text, and sent as text. The model reads the document; it never reads the real
values in it. What it loses is the layout.

Whatever cannot be read - a screenshot, a scanned PDF with no text layer - is refused.
To send such a file anyway, allow its type; it then travels exactly as it is, unredacted,
and both `blackbar status` and `blackbar allow list` say so in yellow:

```bash
blackbar allow list
blackbar allow add image/png     # ⚠ sent as-is, not redacted
blackbar allow remove image/png
```

Everything runs locally. No classifier calls out to the cloud - that would just move the
leak to a different vendor.

```bash
blackbar test "Jan Kowalski, jan@example.com"   # see what would be replaced
blackbar rules add "Acme Ltd" --kind company    # add your own
```

## Updating

```bash
blackbar update
```

Pulls, shows which commits arrived, and restarts the daemon so it runs the new code.

Everything lives in fixed places, so nothing has to be looked up:

```
~/.local/share/blackbar/repo    the clone `update` pulls
~/.local/share/blackbar/venv    the environment it runs from
~/.config/blackbar/             config.toml, rules.yaml
~/.local/state/blackbar/        requests.log, daemon.log
```

The install is editable, so a pull is already enough for code; `update` reinstalls on top
of it anyway, which is what picks up new dependencies.

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
- The model cannot reason about a value it never saw - see the warning above.
- A model can mangle a placeholder in its reply - asking it to translate the text is the
  easy way to trigger this. The real value never left the machine, so the point still
  stands, but the reply comes back with `{{sensitive:...}}` in it. Those are counted as
  **orphans** in `blackbar stats`.
- Only `/v1/messages` and `/v1/messages/count_tokens` are redacted. Any other POST to the
  API is refused with `blackbar_unhandled_endpoint` rather than forwarded unredacted.
- A screenshot cannot be read, so it cannot be redacted. Requests carrying one are
  refused unless the type is listed in `attachments.allow`.

## License

MIT
