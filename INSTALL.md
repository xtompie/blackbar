# INSTALL.md — installation instructions for the agent

This file is read by Claude Code, not by a human. Work through the steps in order.
After each step run the stated verification; do not move on when it fails.

Every path here is an example except the ones in **Do not improvise**.

## Do not improvise

These four have to stay exactly as they are:

| what | value | why |
|---|---|---|
| command name | `blackbar` | docs, the service and the settings entry all refer to it by name |
| config directory | `~/.config/blackbar/` | the daemon, the CLI and the uninstaller all look there |
| port | from `config.toml`, default `8555` | launcher, service and settings entry must all point at the same one |
| placeholder format | `{{sensitive:<kind>:<key>}}` | baked-in conversation history stops restoring if this changes |

Changing any of them does not raise an error - it silently corrupts data in sessions
that are already running. Leave them alone even when something else looks cleaner.

## You may decide

- package manager: `uv` (preferred), `pipx`, plain `venv` - install what is missing
- where the environment lives, as long as `blackbar` ends up on the user's `PATH`
- the model variant (see step 4) - match it to available RAM and the languages the user works in
- workarounds for system-specific problems, as long as the step's verification passes

## Step 0 — get the repository

If you are not already inside a clone of it:

```bash
git clone https://github.com/xtompie/blackbar ~/.local/share/blackbar/src
cd ~/.local/share/blackbar/src
```

That location is only a suggestion - anywhere the user prefers is fine, but the clone has
to stay: `blackbar update` pulls it and reinstalls from it. Record the exact path in the
report in step 8, on a line containing the word `repo` - that is where update looks
for it.

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

From the repository directory:

```bash
uv tool install --python 3.13 --with 'gliner,huggingface_hub' .
```

Without `uv`:

```bash
python3.13 -m venv ~/.local/share/blackbar/venv
~/.local/share/blackbar/venv/bin/pip install '.[detect]'
ln -sf ~/.local/share/blackbar/venv/bin/blackbar ~/.local/bin/blackbar
```

If `torch` refuses to install (old hardware, no wheels), install plain `pip install .`
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

Ask the user whether they have recurring things to mask (company domain, client names,
project codes). Put whatever they name into `rules.yaml`, under `terms` or `patterns`.

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

## Step 6 — proof that it works

This is the real end of the installation. Show the user the output of both commands:

```bash
blackbar doctor
blackbar test "Jan Kowalski, jan@example.com, key AKIAIOSFODNN7EXAMPLE"
```

Expected: `doctor` with no red entries, and `test` reporting hits from **three** layers
(`rules` may be empty if the user gave no patterns of their own) - the email and the key
from `regex`, the name from `gliner`.

If the name does not show up while `model status` says the model is there, lower the
threshold: `blackbar config set detection.threshold 0.4` and `blackbar restart`.

An installation without this step is unfinished. Do not report success on the strength
of `pip install` alone.

## Step 7 — daemon management mode

Ask the user exactly this and wait for an answer:

> How should the daemon be managed?
> **1) on demand** — `blackbar claude` brings it up *(default, touches nothing in the system)*
> 2) system service — survives reboots and crashes
> 3) service + global hook-up — every `claude` goes through the proxy *(edits `~/.claude/settings.json`)*

Carrying it out:

- **1** — do nothing, that is the state after step 5.
- **2** — `blackbar service install`
- **3** — `blackbar service install`, then `blackbar attach`

The two halves stay independent afterwards: `blackbar detach` leaves the service
running, `blackbar service uninstall` leaves the settings entry alone, and
`blackbar attach --force` wires things up with no service at all. The user can rearrange
this later without reinstalling anything.

For option 3, say this before doing it: from that point on, a dead daemon means `claude`
will not start at all (by design - the alternative is silently sending data out
unredacted). The escape hatches are `blackbar direct claude` and `blackbar detach`.
`blackbar attach` shows a diff of the file before writing and keeps a backup.

**Verify:**

```bash
blackbar mode
```

## Step 8 — the report

Write `~/.config/blackbar/install-report.md`:

```markdown
# blackbar — installation report

- date:
- blackbar version:
- interpreter: (path and version)
- environment: (uv tool / venv — exact path)
- `blackbar` command: (path on PATH, how it was linked)
- model: (name, size, cache directory) or "none — rules+regex only, reason: ..."
- active layers:
- mode: manual | service | attached
- files created outside ~/.config/blackbar:
- backups: (e.g. ~/.claude/settings.json.blackbar-backup)
- deviations from this instruction and why:
```

A future update and `blackbar uninstall` both read this file - without it they have to
guess the state of the machine. Fill it in honestly, including whatever did not go to
plan.

## Step 9 — first run

Tell the user to open a **new** terminal window and run:

```bash
blackbar claude
```

and to run `blackbar watch` in a second window to see what actually gets replaced.

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
