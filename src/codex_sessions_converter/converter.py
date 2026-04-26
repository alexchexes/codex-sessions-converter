import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


__version__ = "0.1.0"


SIMPLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
MARKDOWN_FEATURES = {"tools", "metadata", "raw"}
MARKDOWN_TOOL_MODES = {"auto", "none", "names", "preview", "full"}
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-sessions-converter",
        description="Convert Codex session rollout JSONL files to YAML or Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Markdown include presets:\n"
            "  dialogue   visible user/Codex messages, reasoning, progress messages\n"
            "  default    dialogue plus tool calls and tool outputs\n"
            "  metadata   default plus metadata tables such as turn_context/token_count\n"
            "  full       metadata plus raw blocks for unhandled records\n\n"
            "Markdown tool detail modes:\n"
            "  auto       full when tools are included by --md-include, otherwise none\n"
            "  none       omit tool call/output sections\n"
            "  names      show only tool names and call IDs\n"
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
        help=(
            "Path to the output file. Defaults to replacing .jsonl with .yaml, "
            "or .md when Markdown output is selected."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("yaml", "md", "markdown"),
        help="Output format. Defaults to Markdown for .md/.markdown output paths, otherwise YAML.",
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
    explicit_format = normalize_output_format(args.format)
    if explicit_format:
        return explicit_format
    if args.output and args.output.suffix.lower() in {".md", ".markdown"}:
        return "md"
    return "yaml"


def default_output_path(input_path: Path, output_format: str = "yaml") -> Path:
    suffix = ".md" if output_format == "md" else ".yaml"
    if input_path.suffix.lower() == ".jsonl":
        return input_path.with_suffix(suffix)
    return input_path.with_suffix(input_path.suffix + suffix)


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
        return "full" if "tools" in markdown_features else "none"
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


def render_tool_call(
    payload: dict[str, Any], mode: str, preview_chars: int
) -> tuple[str, str | None]:
    full_name = tool_display_name(payload)
    lines = [f"**Tool call:** `{full_name}`"]
    append_tool_identity(lines, payload)

    if mode == "names":
        return "\n".join(lines), full_name

    arguments = payload.get("arguments")
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

    if mode == "names":
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
        lines.extend(["", "Output preview:", fenced_block(truncate_preview(body, preview_chars), language)])
    else:
        lines.extend(["", fenced_block(body, language)])
    return "\n".join(lines)


def render_reasoning(payload: dict[str, Any], redaction: str) -> str:
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


def write_markdown_section(dst, title: str, body: str) -> None:
    dst.write(f"# {title}:\n\n")
    dst.write(body.rstrip())
    dst.write("\n\n---\n\n")


def convert_jsonl_to_markdown(
    input_path: Path, output_path: Path, options: MarkdownOptions
) -> int:
    count = 0
    seen_dialogue: set[tuple[str, str]] = set()
    tool_names_by_call_id: dict[str, str] = {}

    def write_dialogue(dst, title: str, body: str) -> None:
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

    def write_section(dst, title: str, body: str) -> None:
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
                elif payload_type in {"function_call", "tool_search_call"}:
                    if options.tool_mode != "none":
                        rendered_tool_call, tool_name = render_tool_call(
                            payload, options.tool_mode, options.tool_preview_chars
                        )
                        call_id = payload.get("call_id")
                        if call_id and tool_name:
                            tool_names_by_call_id[call_id] = tool_name
                        write_section(dst, "Codex", rendered_tool_call)
                    handled = True
                elif payload_type in {"function_call_output", "tool_search_output"}:
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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        markdown_features = parse_markdown_include(args.md_include)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.md_tool_preview_chars < 1:
        raise SystemExit("--md-tool-preview-chars must be greater than zero")

    output_format = infer_output_format(args)
    input_path = args.input.resolve()
    output_path = (args.output or default_output_path(input_path, output_format)).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "md":
        tool_mode = resolve_markdown_tool_mode(markdown_features, args.md_tools)
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
        print(f"Wrote {count} Markdown sections to {output_path}")
        return 0

    count = convert_jsonl_to_yaml_stream(
        input_path=input_path,
        output_path=output_path,
        redaction=args.redact_encrypted,
    )
    print(f"Wrote {count} YAML documents to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
