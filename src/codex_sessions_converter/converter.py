import argparse
import json
import math
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

__version__ = "0.1.0"


SIMPLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
SESSION_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
NO_ROLLOUT_FILE = "NO ROLLOUT FILE"
NO_SESSION_INDEX_ENTRY = "NO ENTRY IN session_index.jsonl"
MARKDOWN_FEATURES = {"tools", "metadata", "raw"}
MARKDOWN_TOOL_MODES = {"auto", "none", "names", "smart", "preview", "full"}
DEFAULT_TOOL_PREVIEW_CHARS = 700
MARKDOWN_PRESETS = {
    "dialogue": set(),
    "minimal": set(),
    "default": {"tools"},
    "tools": {"tools"},
    "metadata": {"tools", "metadata"},
    "full": {"tools", "metadata", "raw"},
}
MARKDOWN_INCLUDE_ALIASES = {
    "all": "all",
    "none": "none",
    "tool": "tools",
    "tools": "tools",
    "tool-call": "tools",
    "tool-calls": "tools",
    "tool_call": "tools",
    "tool_calls": "tools",
    "meta": "metadata",
    "metadata": "metadata",
    "raw": "raw",
    "unhandled": "raw",
}


@dataclass(frozen=True)
class MarkdownOptions:
    tool_mode: str
    tool_preview_chars: int
    include_metadata: bool
    include_raw: bool
    redaction: str


@dataclass(frozen=True)
class SessionIndexEntry:
    session_id: str
    thread_name: str
    updated_at: datetime | None


@dataclass(frozen=True)
class SessionFile:
    path: Path
    relative_path: str
    session_id: str | None
    started_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True)
class ConversionInput:
    path: Path
    output_stem: str | None


class CliError(Exception):
    pass


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def parse_list_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-sessions-converter list",
        description=(
            "List Codex sessions and cross-check session_index.jsonl against rollout JSONL files."
        ),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        help="Path to session_index.jsonl. Defaults to <codex-home>/session_index.jsonl.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Path to Codex sessions directory. Defaults to <codex-home>/sessions.",
    )
    return parser.parse_args(argv)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-sessions-converter",
        description="Convert Codex session rollout JSONL files to YAML or Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            "  list       list sessions and cross-check session_index.jsonl with rollout files\n\n"
            "Markdown include presets:\n"
            "  dialogue   visible user/Codex messages, reasoning, progress messages\n"
            "  default    dialogue plus tool calls and tool outputs\n"
            "  metadata   default plus metadata tables such as turn_context/token_count\n"
            "  full       metadata plus raw blocks for unhandled records\n\n"
            "Markdown tool detail modes:\n"
            "  auto       smart when tools are included by --md-include, otherwise none\n"
            "  none       omit tool call/output sections\n"
            "  names      show only tool names and call IDs\n"
            "  smart      show useful previews for known tool calls, otherwise names\n"
            "  preview    show names plus truncated arguments/outputs\n"
            "  full       show full arguments/outputs\n\n"
            "The --md-include value can also use modifiers, for example:\n"
            "  default,-tools\n"
            "  dialogue,+metadata\n"
            "  full,-raw\n\n"
            "Explicit --md-tools values override the tools setting from --md-include.\n"
        ),
    )
    parser.add_argument("input", type=Path, help="Path to the source JSONL file.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help=("Path to the output file or directory. Defaults under <codex-home>/tmp."),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help=(
            "Codex home directory for session ID lookup and default output. "
            "Defaults to CODEX_HOME or ~/.codex."
        ),
    )
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument(
        "--format",
        choices=("yaml", "md", "markdown"),
        help="Output format. Defaults to Markdown for .md/.markdown output paths, otherwise YAML.",
    )
    format_group.add_argument(
        "--md",
        action="store_true",
        help="Write Markdown output without specifying an .md output path.",
    )
    format_group.add_argument(
        "--yaml",
        action="store_true",
        help="Write YAML output explicitly.",
    )
    parser.add_argument(
        "--md-include",
        default="default",
        metavar="SPEC",
        help="Markdown preset/modifiers controlling optional content. Default: default.",
    )
    parser.add_argument(
        "--md-tools",
        choices=tuple(sorted(MARKDOWN_TOOL_MODES)),
        default="auto",
        help="Markdown tool detail mode. Default: auto.",
    )
    parser.add_argument(
        "--md-tool-preview-chars",
        type=int,
        default=DEFAULT_TOOL_PREVIEW_CHARS,
        metavar="N",
        help=(
            "Maximum characters per tool argument/output preview when "
            "--md-tools=preview. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--redact-encrypted",
        default="...",
        help="Replacement text for any encrypted_content field.",
    )
    return parser.parse_args(argv)


def normalize_output_format(output_format: str | None) -> str | None:
    if output_format == "markdown":
        return "md"
    return output_format


def infer_output_format(args: argparse.Namespace) -> str:
    if args.md:
        return "md"
    if args.yaml:
        return "yaml"
    explicit_format = normalize_output_format(args.format)
    if explicit_format:
        return explicit_format
    if args.output and args.output.suffix.lower() in {".md", ".markdown"}:
        return "md"
    return "yaml"


def output_filename(input_path: Path, output_format: str = "yaml", stem: str | None = None) -> str:
    suffix = ".md" if output_format == "md" else ".yaml"
    if stem:
        return f"{stem}{suffix}"
    if input_path.suffix.lower() == ".jsonl":
        return input_path.with_suffix(suffix).name
    return input_path.with_suffix(input_path.suffix + suffix).name


def default_output_path(
    input_path: Path,
    codex_home: Path,
    output_format: str = "yaml",
    stem: str | None = None,
) -> Path:
    output_name = output_filename(input_path, output_format, stem)
    try:
        relative_input = input_path.resolve().relative_to(codex_home.resolve())
    except ValueError:
        return codex_home / "tmp" / output_name
    return (codex_home / "tmp" / relative_input).with_name(output_name)


def resolve_output_path(
    output_arg: Path | None,
    input_path: Path,
    codex_home: Path,
    output_format: str,
    stem: str | None = None,
) -> Path:
    if output_arg is None:
        return default_output_path(input_path, codex_home, output_format, stem).resolve()

    expanded_output = output_arg.expanduser()
    if expanded_output.exists() and expanded_output.is_dir():
        return (expanded_output / output_filename(input_path, output_format, stem)).resolve()
    return expanded_output.resolve()


def sanitize(value: Any, redaction: str) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, inner in value.items():
            if key == "encrypted_content":
                sanitized[key] = redaction
            else:
                sanitized[key] = sanitize(inner, redaction)
        return sanitized
    if isinstance(value, list):
        return [sanitize(item, redaction) for item in value]
    return value


def iter_jsonl_objects(input_path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with input_path.open("r", encoding="utf-8") as src:
        for line_number, raw_line in enumerate(src, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON on line {line_number} of {input_path}: {exc}"
                ) from exc
            yield line_number, obj


def iter_concatenated_json_objects(input_path: Path) -> Iterable[tuple[int, Any]]:
    decoder = json.JSONDecoder()
    with input_path.open("r", encoding="utf-8") as src:
        for line_number, raw_line in enumerate(src, start=1):
            remaining = raw_line.strip()
            while remaining:
                try:
                    obj, end = decoder.raw_decode(remaining)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON on line {line_number} of {input_path}: {exc}"
                    ) from exc
                yield line_number, obj
                remaining = remaining[end:].lstrip()


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    fractional = re.search(r"\.(\d+)(?=[+-]\d\d:?\d\d$|$)", text)
    if fractional and len(fractional.group(1)) > 6:
        text = text[: fractional.start(1) + 6] + text[fractional.end(1) :]

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_local_timestamp(value: datetime | None) -> str:
    if value is None:
        return "UNKNOWN"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def local_timezone_offset_label(value: datetime | None) -> str:
    converted = (value or datetime.now(timezone.utc)).astimezone()
    offset = converted.utcoffset()
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    absolute_minutes = abs(total_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def normalize_session_id(session_id: str) -> str:
    return session_id.lower()


def is_session_id(value: str) -> bool:
    return SESSION_ID_RE.fullmatch(value) is not None


def session_id_from_path(path: Path) -> str | None:
    match = SESSION_ID_RE.search(path.stem)
    if not match:
        return None
    return match.group(0)


def session_id_from_metadata(path: Path) -> str | None:
    try:
        for count, (_, record) in enumerate(iter_jsonl_objects(path), start=1):
            payload = record.get("payload")
            if record.get("type") == "session_meta" and isinstance(payload, dict):
                session_id = payload.get("id")
                if isinstance(session_id, str) and session_id:
                    return session_id
            if count >= 20:
                break
    except (OSError, ValueError):
        return None
    return None


def session_file_metadata(
    path: Path, *, include_ended_at: bool = False
) -> tuple[str | None, datetime | None, datetime | None]:
    session_id = session_id_from_path(path)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    try:
        for count, (_, record) in enumerate(iter_jsonl_objects(path), start=1):
            record_timestamp = parse_timestamp(record.get("timestamp"))
            if include_ended_at and record_timestamp is not None:
                ended_at = record_timestamp

            payload = record.get("payload")
            if started_at is None:
                started_at = record_timestamp
                if started_at is None and isinstance(payload, dict):
                    started_at = parse_timestamp(payload.get("timestamp"))
            if (
                session_id is None
                and record.get("type") == "session_meta"
                and isinstance(payload, dict)
            ):
                payload_id = payload.get("id")
                if isinstance(payload_id, str) and payload_id:
                    session_id = payload_id
            if not include_ended_at and session_id is not None and started_at is not None:
                break
            if count >= 20:
                if include_ended_at:
                    continue
                break
    except (OSError, ValueError):
        return session_id, started_at, ended_at
    return session_id, started_at, ended_at


def read_session_index(index_path: Path) -> list[SessionIndexEntry]:
    if not index_path.exists():
        return []

    entries = []
    for _, record in iter_concatenated_json_objects(index_path):
        if not isinstance(record, dict):
            continue
        session_id = record.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        thread_name = record.get("thread_name")
        entries.append(
            SessionIndexEntry(
                session_id=session_id,
                thread_name=thread_name if isinstance(thread_name, str) else "",
                updated_at=parse_timestamp(record.get("updated_at")),
            )
        )
    return entries


def format_session_file_path(path: Path, sessions_dir: Path) -> str:
    try:
        relative_path = path.resolve().relative_to(sessions_dir.resolve())
    except ValueError:
        relative_path = path
    return relative_path.as_posix()


def discover_session_files(
    sessions_dir: Path, *, include_ended_at: bool = False
) -> list[SessionFile]:
    if not sessions_dir.exists():
        return []

    paths = sorted(candidate for candidate in sessions_dir.rglob("*.jsonl") if candidate.is_file())
    session_files = []
    for path in paths:
        session_id, started_at, ended_at = session_file_metadata(
            path, include_ended_at=include_ended_at
        )
        session_files.append(
            SessionFile(
                path=path,
                relative_path=format_session_file_path(path, sessions_dir),
                session_id=session_id,
                started_at=started_at,
                ended_at=ended_at,
            )
        )
    return session_files


def resolve_session_id(session_id: str, codex_home: Path) -> Path:
    sessions_dir = codex_home / "sessions"
    normalized_id = normalize_session_id(session_id)
    matches = [
        session_file.path
        for session_file in discover_session_files(sessions_dir)
        if (
            session_file.session_id
            and normalize_session_id(session_file.session_id) == normalized_id
        )
    ]
    if not matches:
        raise CliError(f"No Codex session found for ID: {session_id}")
    if len(matches) > 1:
        rendered_matches = ", ".join(
            format_session_file_path(path, sessions_dir) for path in matches
        )
        raise CliError(
            f"Multiple Codex session files found for ID {session_id}: {rendered_matches}"
        )
    return matches[0].resolve()


def resolve_conversion_input(raw_input: Path, codex_home: Path) -> ConversionInput:
    input_text = str(raw_input)
    if is_session_id(input_text):
        return ConversionInput(
            path=resolve_session_id(input_text, codex_home),
            output_stem=normalize_session_id(input_text),
        )

    expanded_input = raw_input.expanduser()
    if not expanded_input.exists():
        raise CliError(f"Input file not found: {raw_input}")
    if not expanded_input.is_file():
        raise CliError(f"Input path is not a file: {raw_input}")
    return ConversionInput(path=expanded_input.resolve(), output_stem=None)


def list_session_lines(
    codex_home: Path,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
) -> list[str]:
    index_path = session_index_path or codex_home / "session_index.jsonl"
    resolved_sessions_dir = sessions_dir or codex_home / "sessions"

    index_entries = read_session_index(index_path)
    session_files = discover_session_files(resolved_sessions_dir, include_ended_at=True)
    session_files_by_id: dict[str, list[SessionFile]] = {}
    for session_file in session_files:
        if session_file.session_id:
            normalized_id = normalize_session_id(session_file.session_id)
            session_files_by_id.setdefault(normalized_id, []).append(session_file)

    indexed_ids = {normalize_session_id(entry.session_id) for entry in index_entries}
    lines = []
    for entry in index_entries:
        matching_files = session_files_by_id.get(normalize_session_id(entry.session_id), [])
        if not matching_files:
            lines.append(f"{entry.session_id} - {entry.thread_name} - {NO_ROLLOUT_FILE}")
            continue
        session_file = matching_files[0]
        timezone_label = local_timezone_offset_label(
            session_file.ended_at or session_file.started_at
        )
        lines.append(
            f"{format_local_timestamp(session_file.started_at)} - "
            f"{format_local_timestamp(session_file.ended_at)} ({timezone_label}) - "
            f"{entry.session_id} - "
            f"{entry.thread_name}"
        )

    for session_file in session_files:
        if session_file.session_id and normalize_session_id(session_file.session_id) in indexed_ids:
            continue
        lines.append(f"{session_file.relative_path} - {NO_SESSION_INDEX_ENTRY}")

    return lines


def encode_for_output(text: str, encoding: str | None) -> str:
    if not encoding:
        return text
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def render_key(key: str) -> str:
    if SIMPLE_KEY_RE.match(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def render_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return json.dumps(value)
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False)


def block_style_lines(text: str) -> tuple[str, list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    trailing_newlines = len(normalized) - len(normalized.rstrip("\n"))
    if trailing_newlines == 0:
        header = "|-"
    elif trailing_newlines == 1:
        header = "|"
    else:
        header = "|+"

    lines = normalized.split("\n")
    if normalized.endswith("\n"):
        lines = lines[:-1]
    if not lines:
        lines = [""]
    return header, lines


def is_multiline_string(value: Any) -> bool:
    return isinstance(value, str) and ("\n" in value or "\r" in value)


def dump_yaml_lines(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent

    if isinstance(value, dict):
        if not value:
            return [prefix + "{}"]
        lines = []
        for key, inner in value.items():
            rendered_key = render_key(key)
            if is_multiline_string(inner):
                header, block_lines = block_style_lines(inner)
                lines.append(f"{prefix}{rendered_key}: {header}")
                lines.extend((" " * (indent + 2)) + line for line in block_lines)
            elif isinstance(inner, (dict, list)):
                lines.append(f"{prefix}{rendered_key}:")
                lines.extend(dump_yaml_lines(inner, indent + 2))
            else:
                lines.append(f"{prefix}{rendered_key}: {render_scalar(inner)}")
        return lines

    if isinstance(value, list):
        if not value:
            return [prefix + "[]"]
        lines = []
        for item in value:
            if is_multiline_string(item):
                header, block_lines = block_style_lines(item)
                lines.append(f"{prefix}- {header}")
                lines.extend((" " * (indent + 2)) + line for line in block_lines)
            elif isinstance(item, (dict, list)):
                lines.append(prefix + "-")
                lines.extend(dump_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}- {render_scalar(item)}")
        return lines

    return [prefix + render_scalar(value)]


def convert_jsonl_to_yaml_stream(input_path: Path, output_path: Path, redaction: str) -> int:
    count = 0
    with output_path.open("w", encoding="utf-8", newline="\n") as dst:
        for _, obj in iter_jsonl_objects(input_path):
            sanitized = sanitize(obj, redaction)
            dst.write("---\n")
            dst.write("\n".join(dump_yaml_lines(sanitized)))
            dst.write("\n")
            count += 1
    return count


def parse_markdown_include(spec: str) -> set[str]:
    parts = [part.strip().lower() for part in spec.split(",") if part.strip()]
    if not parts:
        parts = ["default"]

    first = parts[0]
    if first in MARKDOWN_PRESETS:
        features = set(MARKDOWN_PRESETS[first])
        parts = parts[1:]
    else:
        alias = MARKDOWN_INCLUDE_ALIASES.get(first)
        if alias == "all":
            features = set(MARKDOWN_FEATURES)
            parts = parts[1:]
        elif alias == "none":
            features = set()
            parts = parts[1:]
        else:
            features = set(MARKDOWN_PRESETS["default"])

    for raw_part in parts:
        include = True
        part = raw_part
        if part.startswith("+"):
            part = part[1:]
        elif part.startswith("-"):
            include = False
            part = part[1:]

        alias = MARKDOWN_INCLUDE_ALIASES.get(part)
        if alias is None:
            allowed = sorted(set(MARKDOWN_PRESETS) | set(MARKDOWN_INCLUDE_ALIASES))
            raise ValueError(
                f"Unknown --md-include item {raw_part!r}. Allowed values: {', '.join(allowed)}"
            )
        if alias == "all":
            if include:
                features.update(MARKDOWN_FEATURES)
            else:
                features.clear()
            continue
        if alias == "none":
            if include:
                features.clear()
            continue
        if include:
            features.add(alias)
        else:
            features.discard(alias)

    return features


def resolve_markdown_tool_mode(markdown_features: set[str], requested_mode: str) -> str:
    if requested_mode == "auto":
        return "smart" if "tools" in markdown_features else "none"
    return requested_mode


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return render_json_block_content(content)

    parts = []
    for item in content:
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "image_url":
                parts.append(f"[image: {item.get('image_url', '')}]")
            elif item.get("type") == "local_image":
                parts.append(f"[local image: {item.get('path') or item.get('name') or ''}]")
            else:
                parts.append(render_json_block_content(item))
        else:
            parts.append(str(item))
    return "\n\n".join(part for part in parts if part)


def is_injected_user_context(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("# AGENTS.md instructions")


def render_json_block_content(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_markdown_table_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif value is None:
        text = "null"
    elif value is True:
        text = "true"
    elif value is False:
        text = "false"
    else:
        text = str(value)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("|", r"\|").replace("\n", "<br>")


def flatten_table_rows(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        if not value:
            return [(prefix, {})]
        rows = []
        for key, inner in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(flatten_table_rows(inner, child_prefix))
        return rows

    if isinstance(value, list):
        if not value:
            return [(prefix, [])]
        rows = []
        for index, inner in enumerate(value):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            rows.extend(flatten_table_rows(inner, child_prefix))
        return rows

    return [(prefix, value)]


def render_markdown_table(value: Any) -> str:
    rows = flatten_table_rows(value)
    lines = ["| Field | Value |", "| --- | --- |"]
    for key, inner in rows:
        rendered_key = render_markdown_table_value(key)
        rendered_value = render_markdown_table_value(inner)
        lines.append(f"| {rendered_key} | {rendered_value} |")
    return "\n".join(lines)


def fenced_block(content: str, language: str = "") -> str:
    max_backticks = 2
    for match in re.finditer(r"`+", content):
        max_backticks = max(max_backticks, len(match.group(0)))
    fence = "`" * max(3, max_backticks + 1)
    suffix = language if language else ""
    return f"{fence}{suffix}\n{content}\n{fence}"


def parse_json_maybe(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value, "text"
            return render_json_block_content(parsed), "json"
        return value, "text"
    return render_json_block_content(value), "json"


def parse_json_object_maybe(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def normalized_tool_short_name(full_name: str) -> str:
    short_name = full_name.rsplit(".", 1)[-1]
    for prefix in (
        "mcp__playwright__",
        "mcp__codex_apps__",
        "mcp__ask_human_for_context__",
    ):
        if short_name.startswith(prefix):
            return short_name[len(prefix) :]
    return short_name


def tool_display_name(payload: dict[str, Any]) -> str:
    call_type = payload.get("type", "tool_call")
    name = payload.get("name") or call_type
    namespace = payload.get("namespace")
    return f"{namespace}.{name}" if namespace else name


def append_tool_identity(lines: list[str], payload: dict[str, Any]) -> None:
    if payload.get("call_id"):
        lines.append(f"Call ID: `{payload['call_id']}`")
    if payload.get("status"):
        lines.append(f"Status: `{payload['status']}`")
    if payload.get("execution"):
        lines.append(f"Execution: `{payload['execution']}`")


def truncate_preview(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars].rstrip()}\n\n... [truncated, {omitted} characters omitted]"


def render_preview_block(label: str, value: Any, max_chars: int) -> list[str]:
    body, language = parse_json_maybe(value)
    return ["", label, fenced_block(truncate_preview(body, max_chars), language)]


def append_inline_preview(lines: list[str], label: str, value: Any, max_chars: int) -> None:
    if value is None:
        return
    text = str(value)
    if not text:
        return
    lines.append(f"{label}: `{truncate_preview(text, max_chars)}`")


def append_sequence_preview(
    lines: list[str], label: str, value: Any, max_chars: int, max_items: int = 8
) -> None:
    if value is None:
        return
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        append_inline_preview(lines, label, value, max_chars)
        return

    items = list(value)
    rendered_items = []
    for item in items[:max_items]:
        if isinstance(item, (dict, list)):
            rendered_items.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        else:
            rendered_items.append(str(item))
    if not rendered_items:
        return
    if len(items) > max_items:
        rendered_items.append(f"... [{len(items) - max_items} more omitted]")
    append_fenced_preview(lines, label, "\n".join(rendered_items), max_chars)


def append_fenced_preview(
    lines: list[str],
    label: str,
    value: Any,
    max_chars: int,
    language: str = "text",
) -> None:
    if value is None:
        return
    text = str(value)
    if not text:
        return
    lines.extend(["", label, fenced_block(truncate_preview(text, max_chars), language)])


def render_smart_tool_call_preview(
    full_name: str, arguments: Any, preview_chars: int
) -> list[str] | None:
    short_name = normalized_tool_short_name(full_name)
    lines: list[str] = []

    if short_name == "apply_patch":
        append_fenced_preview(lines, "Patch preview:", arguments, preview_chars, "diff")
        return lines or None

    args = parse_json_object_maybe(arguments)
    if args is None:
        return None

    if short_name == "shell_command":
        append_fenced_preview(lines, "Preview:", args.get("command"), preview_chars)
        append_inline_preview(lines, "Workdir", args.get("workdir"), preview_chars)
        append_inline_preview(lines, "Timeout ms", args.get("timeout_ms"), preview_chars)
        return lines or None

    if short_name in {"browser_run_code", "browser_evaluate"}:
        label = "Code preview:" if short_name == "browser_run_code" else "Function preview:"
        append_fenced_preview(
            lines,
            label,
            args.get("code") or args.get("function"),
            preview_chars,
            "javascript",
        )
        append_inline_preview(lines, "Target", args.get("target"), preview_chars)
        append_inline_preview(lines, "Filename", args.get("filename"), preview_chars)
        return lines or None

    if short_name == "update_plan":
        explanation = args.get("explanation")
        append_inline_preview(lines, "Explanation", explanation, preview_chars)
        plan = args.get("plan")
        if isinstance(plan, list):
            preview_lines = []
            for item in plan[:8]:
                if not isinstance(item, dict):
                    continue
                status = item.get("status", "unknown")
                step = item.get("step", "")
                preview_lines.append(f"- {status}: {step}")
            if len(plan) > 8:
                preview_lines.append(f"... [{len(plan) - 8} more steps omitted]")
            append_fenced_preview(lines, "Plan preview:", "\n".join(preview_lines), preview_chars)
        return lines or None

    if full_name == "tool_search_call":
        append_inline_preview(lines, "Query", args.get("query"), preview_chars)
        append_inline_preview(lines, "Limit", args.get("limit"), preview_chars)
        return lines or None

    if short_name in {
        "request_user_input",
        "asking_user_missing_context",
    }:
        questions = args.get("questions")
        if isinstance(questions, list) and questions:
            first_question = questions[0]
            if isinstance(first_question, dict):
                append_inline_preview(
                    lines, "Question", first_question.get("question"), preview_chars
                )
        append_inline_preview(lines, "Question", args.get("question"), preview_chars)
        append_inline_preview(lines, "Context", args.get("context"), preview_chars)
        return lines or None

    if short_name in {"spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent"}:
        append_inline_preview(lines, "Target", args.get("target"), preview_chars)
        append_inline_preview(lines, "ID", args.get("id"), preview_chars)
        append_inline_preview(lines, "Agent type", args.get("agent_type"), preview_chars)
        append_sequence_preview(lines, "Targets preview:", args.get("targets"), preview_chars)
        append_fenced_preview(lines, "Message preview:", args.get("message"), preview_chars)
        append_inline_preview(lines, "Timeout ms", args.get("timeout_ms"), preview_chars)
        return lines or None

    if short_name == "parallel":
        tool_uses = args.get("tool_uses")
        if isinstance(tool_uses, list):
            preview_lines = []
            for tool_use in tool_uses[:8]:
                if not isinstance(tool_use, dict):
                    continue
                name = tool_use.get("recipient_name", "unknown")
                parameters = tool_use.get("parameters")
                if isinstance(parameters, dict):
                    keys = ", ".join(parameters.keys())
                    preview_lines.append(f"- {name} ({keys})" if keys else f"- {name}")
                else:
                    preview_lines.append(f"- {name}")
            if len(tool_uses) > 8:
                preview_lines.append(f"... [{len(tool_uses) - 8} more tool uses omitted]")
            append_fenced_preview(
                lines, "Tool uses preview:", "\n".join(preview_lines), preview_chars
            )
        return lines or None

    if short_name in {
        "browser_click",
        "browser_close",
        "browser_console_messages",
        "browser_drag",
        "browser_drop",
        "browser_file_upload",
        "browser_fill_form",
        "browser_handle_dialog",
        "browser_hover",
        "browser_navigate",
        "browser_navigate_back",
        "browser_network_request",
        "browser_network_requests",
        "browser_press_key",
        "browser_resize",
        "browser_select_option",
        "browser_snapshot",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_type",
        "browser_wait_for",
    }:
        for key in (
            "url",
            "action",
            "target",
            "ref",
            "element",
            "button",
            "key",
            "text",
            "textGone",
            "filename",
            "filter",
            "part",
            "index",
            "time",
            "width",
            "height",
            "fullPage",
            "type",
            "level",
            "all",
            "depth",
            "boxes",
            "accept",
            "promptText",
            "submit",
            "slowly",
            "static",
        ):
            append_inline_preview(
                lines, key.replace("_", " ").title(), args.get(key), preview_chars
            )
        append_sequence_preview(lines, "Paths preview:", args.get("paths"), preview_chars)
        append_sequence_preview(lines, "Values preview:", args.get("values"), preview_chars)
        fields = args.get("fields")
        if isinstance(fields, list):
            field_lines = []
            for field in fields[:8]:
                if not isinstance(field, dict):
                    continue
                name = field.get("name", "field")
                field_type = field.get("type")
                target = field.get("target")
                value = field.get("value")
                details = [str(name)]
                if field_type:
                    details.append(f"type={field_type}")
                if target:
                    details.append(f"target={target}")
                if value is not None:
                    details.append(f"value={value}")
                field_lines.append(" - ".join(details))
            if len(fields) > 8:
                field_lines.append(f"... [{len(fields) - 8} more fields omitted]")
            append_fenced_preview(lines, "Fields preview:", "\n".join(field_lines), preview_chars)
        return lines or None

    if short_name == "view_image":
        append_inline_preview(lines, "Path", args.get("path"), preview_chars)
        append_sequence_preview(lines, "Paths preview:", args.get("paths"), preview_chars)
        append_inline_preview(lines, "Detail", args.get("detail"), preview_chars)
        return lines or None

    if short_name in {"github_fetch", "github_search"}:
        append_inline_preview(lines, "URL", args.get("url"), preview_chars)
        append_inline_preview(lines, "Query", args.get("query"), preview_chars)
        append_inline_preview(lines, "Repository name", args.get("repository_name"), preview_chars)
        append_inline_preview(lines, "Top N", args.get("topn"), preview_chars)
        return lines or None

    if short_name in {"tool_suggest"}:
        append_inline_preview(lines, "Tool ID", args.get("tool_id"), preview_chars)
        append_inline_preview(lines, "Tool type", args.get("tool_type"), preview_chars)
        append_inline_preview(lines, "Action type", args.get("action_type"), preview_chars)
        append_inline_preview(lines, "Reason", args.get("suggest_reason"), preview_chars)
        return lines or None

    if short_name in {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}:
        append_inline_preview(lines, "Server", args.get("server"), preview_chars)
        append_inline_preview(lines, "URI", args.get("uri"), preview_chars)
        append_inline_preview(lines, "Cursor", args.get("cursor"), preview_chars)
        return lines or None

    return None


def render_tool_call(
    payload: dict[str, Any], mode: str, preview_chars: int
) -> tuple[str, str | None]:
    full_name = tool_display_name(payload)
    lines = [f"**Tool call:** `{full_name}`"]
    append_tool_identity(lines, payload)

    if mode == "names":
        return "\n".join(lines), full_name

    arguments = payload.get("arguments") if "arguments" in payload else payload.get("input")
    if mode == "smart":
        smart_preview = render_smart_tool_call_preview(full_name, arguments, preview_chars)
        if smart_preview:
            lines.extend(smart_preview)
        return "\n".join(lines), full_name

    if arguments is not None:
        if mode == "preview":
            lines.extend(render_preview_block("Arguments preview:", arguments, preview_chars))
        else:
            body, language = parse_json_maybe(arguments)
            lines.extend(["", "Arguments:", fenced_block(body, language)])
    else:
        remaining = {
            key: value
            for key, value in payload.items()
            if key not in {"type", "name", "namespace", "call_id", "status", "execution"}
        }
        if remaining:
            if mode == "preview":
                lines.extend(render_preview_block("Payload preview:", remaining, preview_chars))
            else:
                lines.extend(["", fenced_block(render_json_block_content(remaining), "json")])
    return "\n".join(lines), full_name


def render_tool_output(
    payload: dict[str, Any],
    mode: str,
    preview_chars: int,
    tool_names_by_call_id: dict[str, str],
) -> str:
    call_type = payload.get("type", "tool_output")
    call_id = payload.get("call_id")
    tool_name = tool_names_by_call_id.get(call_id, call_type) if call_id else call_type
    lines = [f"**Tool output:** `{tool_name}`"]
    append_tool_identity(lines, payload)

    if mode in {"names", "smart"}:
        return "\n".join(lines)

    if "output" in payload:
        body, language = parse_json_maybe(payload["output"])
    else:
        body = render_json_block_content(
            {
                key: value
                for key, value in payload.items()
                if key not in {"type", "call_id", "status", "execution"}
            }
        )
        language = "json"
    if mode == "preview":
        lines.extend(
            [
                "",
                "Output preview:",
                fenced_block(truncate_preview(body, preview_chars), language),
            ]
        )
    else:
        lines.extend(["", fenced_block(body, language)])
    return "\n".join(lines)


def render_reasoning(payload: dict[str, Any], redaction: str) -> str:
    if (
        not payload.get("summary")
        and not payload.get("content")
        and payload.get("encrypted_content") is not None
    ):
        return f"**Reasoning (encrypted_content) {redaction}**"

    lines = ["**Reasoning**"]

    summary = payload.get("summary")
    if summary:
        lines.extend(["", "Summary:"])
        if isinstance(summary, list):
            for item in summary:
                text = content_to_text(item.get("content")) if isinstance(item, dict) else str(item)
                if text:
                    lines.append(f"- {text}")
        else:
            lines.append(str(summary))

    content = payload.get("content")
    if content:
        lines.extend(["", content_to_text(content)])
    elif payload.get("encrypted_content") is not None:
        lines.extend(["", f"`encrypted_content`: {redaction}"])

    return "\n".join(lines)


def metadata_title(record: dict[str, Any]) -> str:
    record_type = record.get("type", "metadata")
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("type"):
        return f"Metadata: `{record_type}.{payload['type']}`"
    return f"Metadata: `{record_type}`"


def render_metadata(record: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Timestamp: `{record.get('timestamp', '')}`",
            "",
            render_markdown_table(record),
        ]
    )


def render_raw_record(line_number: int, record: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Line: `{line_number}`",
            "",
            fenced_block(render_json_block_content(record), "json"),
        ]
    )


def is_metadata_record(record: dict[str, Any]) -> bool:
    record_type = record.get("type")
    if record_type in {"session_meta", "turn_context"}:
        return True
    if record_type == "event_msg":
        payload = record.get("payload")
        if isinstance(payload, dict):
            return payload.get("type") in {
                "mcp_tool_call_end",
                "task_complete",
                "task_started",
                "thread_name_updated",
                "token_count",
            }
    return False


def write_markdown_section(dst: TextIO, title: str, body: str) -> None:
    dst.write(f"# {title}:\n\n")
    dst.write(body.rstrip())
    dst.write("\n\n---\n\n")


def convert_jsonl_to_markdown(input_path: Path, output_path: Path, options: MarkdownOptions) -> int:
    count = 0
    seen_dialogue: set[tuple[str, str]] = set()
    tool_names_by_call_id: dict[str, str] = {}

    def write_dialogue(dst: TextIO, title: str, body: str) -> None:
        nonlocal count
        normalized_body = body.strip()
        if not normalized_body:
            return
        key = (title, normalized_body)
        if key in seen_dialogue:
            return
        seen_dialogue.add(key)
        write_markdown_section(dst, title, normalized_body)
        count += 1

    def write_section(dst: TextIO, title: str, body: str) -> None:
        nonlocal count
        normalized_body = body.strip()
        if not normalized_body:
            return
        write_markdown_section(dst, title, normalized_body)
        count += 1

    with output_path.open("w", encoding="utf-8", newline="\n") as dst:
        for line_number, raw_record in iter_jsonl_objects(input_path):
            record = sanitize(raw_record, options.redaction)
            record_type = record.get("type")
            payload = record.get("payload")
            handled = False

            if record_type == "response_item" and isinstance(payload, dict):
                payload_type = payload.get("type")
                if payload_type == "message":
                    role = payload.get("role")
                    text = content_to_text(payload.get("content"))
                    if role == "assistant":
                        write_dialogue(dst, "Codex", text)
                        handled = True
                    elif role == "user" and not is_injected_user_context(text):
                        write_dialogue(dst, "User", text)
                        handled = True
                elif payload_type == "reasoning":
                    write_section(dst, "Codex", render_reasoning(payload, options.redaction))
                    handled = True
                elif payload_type in {"function_call", "tool_search_call", "custom_tool_call"}:
                    if options.tool_mode != "none":
                        rendered_tool_call, tool_name = render_tool_call(
                            payload, options.tool_mode, options.tool_preview_chars
                        )
                        call_id = payload.get("call_id")
                        if call_id and tool_name:
                            tool_names_by_call_id[call_id] = tool_name
                        write_section(dst, "Codex", rendered_tool_call)
                    handled = True
                elif payload_type in {
                    "function_call_output",
                    "tool_search_output",
                    "custom_tool_call_output",
                }:
                    if options.tool_mode != "none":
                        write_section(
                            dst,
                            "Codex",
                            render_tool_output(
                                payload,
                                options.tool_mode,
                                options.tool_preview_chars,
                                tool_names_by_call_id,
                            ),
                        )
                    handled = True

            elif record_type == "event_msg" and isinstance(payload, dict):
                payload_type = payload.get("type")
                if payload_type == "user_message":
                    write_dialogue(dst, "User", payload.get("message", ""))
                    handled = True
                elif payload_type == "agent_message":
                    write_dialogue(dst, "Codex", payload.get("message", ""))
                    handled = True

            if not handled and options.include_metadata and is_metadata_record(record):
                write_section(dst, metadata_title(record), render_metadata(record))
                handled = True

            if not handled and options.include_raw:
                write_section(dst, "Raw", render_raw_record(line_number, record))

    return count


def run_list_command(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.expanduser().resolve()
    session_index_path = (
        args.session_index.expanduser().resolve()
        if args.session_index
        else codex_home / "session_index.jsonl"
    )
    sessions_dir = (
        args.sessions_dir.expanduser().resolve() if args.sessions_dir else codex_home / "sessions"
    )

    try:
        lines = list_session_lines(
            codex_home=codex_home,
            session_index_path=session_index_path,
            sessions_dir=sessions_dir,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    for line in lines:
        print(encode_for_output(line, sys.stdout.encoding))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv[:1] == ["list"]:
        return run_list_command(parse_list_args(raw_argv[1:]))

    args = parse_args(raw_argv)
    codex_home = args.codex_home.expanduser().resolve()
    try:
        markdown_features = parse_markdown_include(args.md_include)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.md_tool_preview_chars < 1:
        raise SystemExit("--md-tool-preview-chars must be greater than zero")

    output_format = infer_output_format(args)
    try:
        conversion_input = resolve_conversion_input(args.input, codex_home)
    except CliError as exc:
        raise SystemExit(str(exc)) from exc

    input_path = conversion_input.path
    output_path = resolve_output_path(
        args.output,
        input_path,
        codex_home,
        output_format,
        conversion_input.output_stem,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "md":
        tool_mode = resolve_markdown_tool_mode(markdown_features, args.md_tools)
        try:
            count = convert_jsonl_to_markdown(
                input_path=input_path,
                output_path=output_path,
                options=MarkdownOptions(
                    tool_mode=tool_mode,
                    tool_preview_chars=args.md_tool_preview_chars,
                    include_metadata="metadata" in markdown_features,
                    include_raw="raw" in markdown_features,
                    redaction=args.redact_encrypted,
                ),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Wrote {count} Markdown sections to {output_path}")
        return 0

    try:
        count = convert_jsonl_to_yaml_stream(
            input_path=input_path,
            output_path=output_path,
            redaction=args.redact_encrypted,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Wrote {count} YAML documents to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
