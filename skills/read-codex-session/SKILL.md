---
name: read-codex-session
description: Read or recover context from previous Codex conversations or from the current conversation before context compaction. Use when asked to summarize, inspect, remember, or hand off context from an earlier or current Codex dialogue.
---

# Read Codex Session

Use this skill to inspect Codex session history without loading raw rollout JSONL into context.

## Workflow

1. Resolve the target session:
   - If the user gives a rollout path, use it directly.
   - If the user gives a session id, rollout id fragment, or thread name, resolve it under `$CODEX_HOME/sessions` or `~/.codex/sessions`.
   - If the user says "latest" or does not specify a session, use the newest `rollout-*.jsonl`.

2. Prepare compact Markdown with the bundled helper:

```bash
python ~/.codex/skills/read-codex-session/scripts/prepare_session_markdown.py <target>
```

On Windows PowerShell:

```powershell
python $env:USERPROFILE\.codex\skills\read-codex-session\scripts\prepare_session_markdown.py <target>
```

The helper writes Markdown under `$CODEX_HOME/tmp/sessions/...` and prints the path.

3. Read the generated Markdown, not the raw JSONL. Prefer targeted reads with `rg`, `Select-String`, `Get-Content -TotalCount`, or equivalent before loading a large file.

4. Summarize only the session facts needed for the user request. Mention when tool outputs were omitted or previewed.

## Detail Levels

Default helper output uses `--md-tools auto`: visible dialogue plus smart tool
previews.

Use a higher-detail pass only when needed:

```bash
python ~/.codex/skills/read-codex-session/scripts/prepare_session_markdown.py <target> --md-tools names
python ~/.codex/skills/read-codex-session/scripts/prepare_session_markdown.py <target> --md-tools smart
python ~/.codex/skills/read-codex-session/scripts/prepare_session_markdown.py <target> --md-tools preview --preview-chars 1200
python ~/.codex/skills/read-codex-session/scripts/prepare_session_markdown.py <target> --md-tools full
python ~/.codex/skills/read-codex-session/scripts/prepare_session_markdown.py <target> --md-include metadata
```

Use `--md-include metadata` when turn context, token counts, cwd, model, or rate-limit information matters.

Base64 data images are truncated by default. Use `--md-images extract` when
image content matters and the Markdown should link to real image files. Use
`--md-images inline` only when the renderer must receive self-contained
Markdown; inline image notes point back to `--md-images truncate` and
`--md-images extract` for cleanup.

Use `--format yaml` or the converter CLI directly only when the user asks for raw structured inspection.

## Search

When the user asks to find a previous conversation by topic or phrase, prefer
the converter search before raw `rg` over JSONL:

```bash
codex-sessions find -i "search phrase"
codex-sessions find -i -r "regex|pattern"
codex-sessions find --tools "shell command"
codex-sessions find --metadata "repository-or-cwd"
```

`find` searches deserialized visible messages by default, highlights matches,
and groups results by session. Use raw `rg` only for narrow file-format checks or
when searching fields not covered by `find`.

## Manual Fallback

If the helper is unavailable, run `codex-sessions` directly:

```bash
codex-sessions --md-tools smart <rollout.jsonl> <output.md>
codex-sessions --md-tools preview --md-tool-preview-chars 1200 <rollout.jsonl> <output.md>
codex-sessions --md-images extract <rollout.jsonl> <output.md>
```

Avoid opening raw JSONL except for narrow targeted searches such as finding a missing record type.
