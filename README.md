# codex-sessions-converter

Convert Codex session rollout JSONL files into readable Markdown or YAML.

The main use case is turning files from a Codex home directory such as
`sessions/2026/04/26/rollout-....jsonl` into a dialogue-oriented Markdown file:

```md
# User:

...

---

# Codex:

...
```

## Install

From a local checkout:

```bash
pip install .
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

Convert to YAML:

```bash
codex-sessions-converter sessions/2026/04/26/rollout.jsonl rollout.yaml
```

Convert to Markdown:

```bash
codex-sessions-converter sessions/2026/04/26/rollout.jsonl rollout.md
```

Use Markdown explicitly when no `.md` output path is supplied:

```bash
codex-sessions-converter --format md sessions/2026/04/26/rollout.jsonl
```

## Markdown Detail

`--md-include` controls broad optional sections:

```bash
# Visible user/Codex messages, reasoning, and progress messages.
codex-sessions-converter --md-include dialogue input.jsonl output.md

# Default: dialogue plus full tool calls and outputs.
codex-sessions-converter --md-include default input.jsonl output.md

# Add metadata tables such as turn_context and token_count.
codex-sessions-converter --md-include metadata input.jsonl output.md

# Metadata plus raw unhandled records.
codex-sessions-converter --md-include full input.jsonl output.md
```

`--md-tools` controls tool call/output detail:

```bash
# Tool names and call IDs only.
codex-sessions-converter --md-tools names input.jsonl output.md

# Tool names plus truncated arguments and outputs.
codex-sessions-converter --md-tools preview input.jsonl output.md

# Tune preview length.
codex-sessions-converter --md-tools preview --md-tool-preview-chars 1200 input.jsonl output.md

# Hide tools entirely.
codex-sessions-converter --md-tools none input.jsonl output.md
```

The default `--md-tools auto` follows `--md-include`: presets that include tools
render full tool details, and presets without tools omit them. Explicit
`--md-tools` values override that behavior.

## Notes

- Encrypted reasoning payloads are redacted by default as `...`.
- Markdown metadata tables escape pipe characters and replace embedded newlines
  with `<br>`.
- The converter uses only the Python standard library.

## Development

Run the test suite:

```bash
python -m unittest discover -s tests
```
