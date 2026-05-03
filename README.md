# Codex sessions converter

Convert Codex session rollout JSONL files into more readable Markdown or YAML.

It turns session rollup files that can be found in a Codex home directory such as
`~/.codex/sessions/YYYY/MM/DD/rollout-<...>.jsonl` into a more readable YAML, or a dialogue-oriented Markdown file:

```md
# User:

<...>

---

# Codex:

<...>
```

## Install

Install the latest version from GitHub:

```bash
pipx install git+https://github.com/alexchexes/codex-sessions-converter.git
```

Or install from a local checkout:

```bash
pipx install .
```

If `pipx` is not installed yet, install it first:

```bash
python -m pip install --user pipx
python -m pipx ensurepath
```

Or run it from a checkout without installing:

```bash
PYTHONPATH=src python -m codex_sessions_converter --help
```

In PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m codex_sessions_converter --help
```

## Usage

The installed short command is `codex-sessions`. The longer
`codex-sessions-converter` command is also available for existing scripts and
explicitness.

Convert to YAML:

```bash
codex-sessions sessions/YYYY/MM/DD/rollout.jsonl rollout.yaml
```

Use YAML explicitly:

```bash
codex-sessions --yaml sessions/YYYY/MM/DD/rollout.jsonl
```

Convert to Markdown:

```bash
codex-sessions sessions/YYYY/MM/DD/rollout.jsonl rollout.md
```

Use Markdown explicitly when no `.md` output path is supplied:

```bash
codex-sessions --md sessions/YYYY/MM/DD/rollout.jsonl
```

The longer `--format md` form is also supported.

Markdown output truncates base64 data images by default so large screenshots do
not fill the `.md` file with inline image payloads. The placeholder includes
the original rollout path/line and a short base64 prefix so the image can still
be found in the source JSONL. To write those images as real files next to the
Markdown and link them from the document, use:

```bash
codex-sessions --md --md-images extract sessions/YYYY/MM/DD/rollout.jsonl
```

Extracted images are written to a sibling `<markdown-stem>_assets/` directory.
Use `--md-images inline` only when you want to keep base64 image data inline in
the Markdown; inline images include a hidden comment pointing back to truncation
or extraction mode for future cleanup.

When no output path is supplied, the converter writes under the Codex home
directory, not the current directory. Codex home defaults to `CODEX_HOME` or
`~/.codex`, so a session rollout normally writes to a path like
`~/.codex/tmp/sessions/YYYY/MM/DD/rollout-<...>.yaml`.

Convert by session ID:

```bash
codex-sessions 019dd5ce-19e1-78c3-9313-325228ddd983
```

Write an ID conversion to the current directory:

```bash
codex-sessions 019dd5ce-19e1-78c3-9313-325228ddd983 ./
```

Use a specific Codex home directory for session ID lookup and default output:

```bash
codex-sessions --codex-home ~/.codex 019dd5ce-19e1-78c3-9313-325228ddd983
```

List Codex sessions from `CODEX_HOME` or `~/.codex` and cross-check
`session_index.jsonl` against actual session files:

```bash
codex-sessions list
```

Example output:

```text
2026-02-22 13:48 - 2026-02-22 13:50 (UTC+00:00) - 019c8599-6845-7772-9c64-5f0ee47c73f1 - Add scope for type casting types
019c8599-6845-7772-9c64-5f0ee47c73f1 - Add scope for type casting types - NO ROLLOUT FILE
YYYY/MM/DD/rollout-....jsonl - NO ENTRY IN session_index.jsonl
```

Use a specific Codex home directory:

```bash
codex-sessions list --codex-home ~/.codex
```

Search all Codex sessions:

```bash
codex-sessions find -i "dadata-sdk"
```

By default, `find` searches visible user and Codex messages only. Use
`--metadata` to also search compact session metadata such as cwd and repository
URL, `--tools` to also search concise tool call previews such as shell commands,
or `--all` to include both.

`grep` is an alias for `find`:

```bash
codex-sessions grep -i "dadata-sdk"
```

Use regex mode with `-r`, `--regex`, or the grep-style `-E` alias:

```bash
codex-sessions find -i -r "dadata-[a-z]+"
```

Adjust the maximum width of each matching line:

```bash
codex-sessions find --line-width 220 "dadata-sdk"
```

By default, `find` shows up to 5 matching lines per session. Use `-m` or
`--max-lines-per-session` to change the limit, or pass `0` to show all matching
lines.

Matches are highlighted with terminal colors by default when stdout is a
terminal, including Git Bash/MSYS terminals on Windows. Use `--color always` or
`--color never` to override auto-detection.

## Codex Skill

This repo also includes a Codex skill that helps future Codex sessions inspect
previous conversations without loading large raw session files directly.

Install or update the skill from a local checkout:

```bash
mkdir -p ~/.codex/skills
cp -r skills/read-codex-session ~/.codex/skills/
```

In PowerShell:

```powershell
New-Item -ItemType Directory -Force $env:USERPROFILE\.codex\skills
Copy-Item -Recurse -Force .\skills\read-codex-session $env:USERPROFILE\.codex\skills\
```

After restarting Codex, ask for `$read-codex-session` or ask Codex to recover
context from an earlier conversation.

## Markdown Detail

`--md-include` controls broad optional sections:

```bash
# Visible user/Codex messages, reasoning, and progress messages.
codex-sessions --md-include dialogue input.jsonl output.md

# Default: dialogue plus concise tool call previews.
codex-sessions --md-include default input.jsonl output.md

# Add metadata tables such as turn_context and token_count.
codex-sessions --md-include metadata input.jsonl output.md

# Metadata plus raw unhandled records.
codex-sessions --md-include full input.jsonl output.md
```

`--md-tools` controls tool call/output detail:

```bash
# Tool names and call IDs only.
codex-sessions --md-tools names input.jsonl output.md

# Useful previews for known tool calls; unknown tools fall back to names.
codex-sessions --md-tools smart input.jsonl output.md

# Tool names plus truncated arguments and outputs.
codex-sessions --md-tools preview input.jsonl output.md

# Tune preview length.
codex-sessions --md-tools preview --md-tool-preview-chars 1200 input.jsonl output.md

# Hide tools entirely.
codex-sessions --md-tools none input.jsonl output.md
```

The default `--md-tools auto` follows `--md-include`: presets that include tools
render smart tool call previews, and presets without tools omit them. Explicit
`--md-tools` values override that behavior. Smart mode keeps tool outputs to
names and call IDs.

## Notes

- Encrypted reasoning payloads are redacted by default as `...` and rendered
  compactly in Markdown.
- Markdown metadata tables escape pipe characters and replace embedded newlines
  with `<br>`.
- The converter uses Rich for colored search output.

## License

MIT

## Development

Create a local virtual environment and install development tools:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

In PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Run the test suite:

```bash
python -m unittest discover -s tests
```

Run formatting, linting, and type checks:

```bash
python -m ruff format .
python -m ruff check .
python -m mypy
```
