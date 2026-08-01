# INSTALL.md — installation instructions for the agent

This file is read by Claude Code, not by a human. Work through the steps in order.
After each step run the stated verification; do not move on when it fails.

The user is installing one program. Everything below is your business, not theirs: do not
narrate the steps, do not paste command output into the conversation, do not ask them to
choose anything. Step 9 is the only thing they should read.

Every path here is an example except the ones in **Do not improvise**.

## Do not improvise

These four have to stay exactly as they are:

| what | value | why |
|---|---|---|
| command name | `blackbar` | docs, the service and the settings entry all refer to it by name |
| config directory | `~/.config/blackbar/` | the daemon, the CLI and the uninstaller all look there |
| clone directory | `~/.local/share/blackbar/repo` | `blackbar update` pulls exactly this path |
| environment | `~/.local/share/blackbar/venv` | `blackbar update` reinstalls into exactly this one |
| port | from `config.toml`, default `8555` | launcher, service and settings entry must all point at the same one |
| placeholder format | `{{sensitive:<kind>:<key>}}` | baked-in conversation history stops restoring if this changes |

Changing any of them does not raise an error - it silently corrupts data in sessions
that are already running. Leave them alone even when something else looks cleaner.

## You may decide

- how the virtual environment gets created (`uv venv`, `python -m venv`) - only the path is fixed
- the model variant (see step 4) - match it to available RAM and the languages the user works in
- workarounds for system-specific problems, as long as the step's verification passes

## Step 0 — get the repository

```bash
git clone https://github.com/xtompie/blackbar ~/.local/share/blackbar/repo
cd ~/.local/share/blackbar/repo
```

This path is not negotiable (see **Do not improvise**): `blackbar update` pulls this
directory and reinstalls from it. If a clone is already there, `git pull` instead.

**Verify:** `INSTALL.md` and `pyproject.toml` are in the current directory.

## Step 1 — Python

You need Python **3.11+**, but **not the newest one**: the GLiNER layer pulls in
`torch`, which usually has no wheels for a freshly released Python. Check which version
`torch` ships wheels for and use 3.12 or 3.13 if the system Python is newer.

```bash
python3 --version
uv --version || echo "no uv"
```

If a suitable version is missing: `uv python install 3.13` (or `brew install python@3.13`).

**Verify:** you have a 3.11–3.13 interpreter available.

## Step 2 — install the package

One environment, one fixed place, installed editable so that `git pull` is enough for
code changes:

```bash
uv venv --python 3.13 ~/.local/share/blackbar/venv     # or: python3.13 -m venv ~/.local/share/blackbar/venv
~/.local/share/blackbar/venv/bin/pip install -e '.[detect]'
mkdir -p ~/.local/bin
ln -sf ~/.local/share/blackbar/venv/bin/blackbar ~/.local/bin/blackbar
```

Make sure `~/.local/bin` is on the user's `PATH`; add it to their shell profile if not.

If `torch` refuses to install (old hardware, no wheels), install `pip install -e .`
without the extras - the proxy still works on the `rules` and `regex` layers. Record
that in the report and tell the user plainly: names and companies will not be detected.

**Verify:**

```bash
blackbar version
```

## Step 3 — configuration

```bash
blackbar config get
```

The first run creates `~/.config/blackbar/config.toml` and `rules.yaml`.

Leave `rules.yaml` as it is. Custom patterns are for later, with `blackbar rules add` -
do not interview the user about their clients during an install.

**Verify:** both files exist and `blackbar rules list` prints them.

## Step 4 — the model

The default model is `urchade/gliner_multi_pii-v1` (1.2 GB on disk, multilingual).

```bash
blackbar model list     # variants and sizes
blackbar model pull     # downloads with visible progress
```

Before downloading: check whether Hugging Face has a newer variant of that family
(`gliner_multi_pii`). If there is one and it looks stable, offer it instead of the
default - this file may not have been updated in a long time.

Run the download so the user can see progress - it is over a gigabyte. Do not treat the step as done
until `blackbar model status` confirms the size on disk.

**Verify:**

```bash
blackbar model status
```

## Step 5 — first start

```bash
blackbar start
blackbar status
```

**Verify:** `status` shows `active`, the port and a loaded model. Loading takes a dozen
seconds or so after start - if you see "loading...", wait and check again.

## Step 6 — check it yourself

Run both and read the output. This is for you, not for the user - do not paste it into
the conversation.

```bash
blackbar doctor
blackbar test "Jan Kowalski, jan@example.com, key AKIAIOSFODNN7EXAMPLE"
```

`doctor` must have no red entries. `test` must report the email and the key from the
`regex` layer and the name from `gliner`. If the name is missing while `model status`
says the model is there, lower the threshold with
`blackbar config set detection.threshold 0.4` and `blackbar restart`.

Do not report success on the strength of `pip install` alone.

## Step 7 — leave it on the default

Do not ask the user how to manage the daemon. The default needs no decision: the daemon
starts by itself when `blackbar claude` runs, and nothing outside `~/.config/blackbar/`
is touched. The alternative is one line in the summary below; they can act on it later.

## Step 8 — the report

Write `~/.config/blackbar/install-report.md`. This is a file for future updates and for
`blackbar uninstall`, not something to show the user:

```markdown
# blackbar — installation report

- date:
- blackbar version:
- interpreter: (path and version)
- model: (name, size) or "none — rules+regex only, reason: ..."
- active layers:
- files created outside ~/.config/blackbar:
- deviations from this instruction and why:
```

## Step 9 — what you tell the user

Keep it to this. No layers, no doctor output, no explanation of how redaction works -
they installed one program, and they need to know how to run it. Say it in the language
they have been writing in.

```
blackbar is installed.

  blackbar claude     start Claude Code with redaction on

Everything else works as usual. Nothing leaves your machine unmasked - names,
emails and keys are replaced before the request goes out, and put back in the reply.

Optional:
  blackbar watch      in a second window: see what is being replaced, live
  blackbar attach     make every `claude` go through it, not just this command
  blackbar help       all commands
  blackbar doctor     when something looks wrong
```

Then stop. Do not add a summary of what you did.

## When something breaks

| symptom | cause | what to do |
|---|---|---|
| `blackbar` not found | directory not on `PATH` | add it in the shell profile and tell the user to open a new window |
| daemon will not start | port taken or an import error | `blackbar logs`, on a clash `blackbar config set proxy.port <other>` |
| `model_error` in `status` | `torch`/`gliner` missing or model not downloaded | `blackbar model pull`; with no wheels, stay on `rules`+`regex` |
| claude cannot connect | daemon is down and the mode is attached | `blackbar start`, then `blackbar doctor` |
| `test` detects nothing | layers disabled or threshold too high | `blackbar config get detection.layers`, threshold 0.4 |

Do not "fix" the installation by working around the **Do not improvise** section. If you
get stuck, tell the user what you got stuck on and leave the system as it was before
that step.
