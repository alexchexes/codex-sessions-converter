from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from codex_sessions_converter.json_streams import iter_jsonl_objects
from codex_sessions_converter.markdown_formatting import (
    fenced_block,
    render_json_block_content,
    render_markdown_table,
)
from codex_sessions_converter.markdown_images import MarkdownImageHandler
from codex_sessions_converter.markdown_tools import render_tool_call, render_tool_output
from codex_sessions_converter.message_content import content_to_text, is_injected_user_context
from codex_sessions_converter.session_documents import sanitize


@dataclass(frozen=True)
class MarkdownOptions:
    tool_mode: str
    tool_preview_chars: int
    include_metadata: bool
    include_raw: bool
    redaction: str
    image_mode: str = "truncate"


def render_reasoning(
    payload: dict[str, Any],
    redaction: str,
    image_handler: MarkdownImageHandler | None = None,
) -> str:
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
                text = (
                    content_to_text(item.get("content"), image_handler)
                    if isinstance(item, dict)
                    else str(item)
                )
                if text:
                    lines.append(f"- {text}")
        else:
            lines.append(str(summary))

    content = payload.get("content")
    if content:
        lines.extend(["", content_to_text(content, image_handler)])
    elif payload.get("encrypted_content") is not None:
        lines.extend(["", f"`encrypted_content`: {redaction}"])

    return "\n".join(lines)


def metadata_title(record: dict[str, Any]) -> str:
    record_type = record.get("type", "metadata")
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("type"):
        return f"Metadata: `{record_type}.{payload['type']}`"
    return f"Metadata: `{record_type}`"


def render_metadata(
    record: dict[str, Any],
    image_handler: MarkdownImageHandler | None = None,
) -> str:
    rendered_record = image_handler.transform_value(record) if image_handler else record
    return "\n".join(
        [
            f"Timestamp: `{record.get('timestamp', '')}`",
            "",
            render_markdown_table(rendered_record),
        ]
    )


def render_raw_record(
    line_number: int,
    record: dict[str, Any],
    image_handler: MarkdownImageHandler | None = None,
) -> str:
    rendered_record = image_handler.transform_value(record) if image_handler else record
    return "\n".join(
        [
            f"Line: `{line_number}`",
            "",
            fenced_block(render_json_block_content(rendered_record), "json"),
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
    image_handler = MarkdownImageHandler(options.image_mode, output_path, input_path)

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
            image_handler.set_source_line(line_number)
            record = sanitize(raw_record, options.redaction)
            record_type = record.get("type")
            payload = record.get("payload")
            handled = False

            if record_type == "response_item" and isinstance(payload, dict):
                payload_type = payload.get("type")
                if payload_type == "message":
                    role = payload.get("role")
                    text = content_to_text(payload.get("content"), image_handler)
                    if role == "assistant":
                        write_dialogue(dst, "Codex", text)
                        handled = True
                    elif role == "user" and not is_injected_user_context(text):
                        write_dialogue(dst, "User", text)
                        handled = True
                elif payload_type == "reasoning":
                    write_section(
                        dst,
                        "Codex",
                        render_reasoning(payload, options.redaction, image_handler),
                    )
                    handled = True
                elif payload_type in {"function_call", "tool_search_call", "custom_tool_call"}:
                    if options.tool_mode != "none":
                        rendered_tool_call, tool_name = render_tool_call(
                            payload,
                            options.tool_mode,
                            options.tool_preview_chars,
                            image_handler,
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
                                image_handler,
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
                write_section(
                    dst,
                    metadata_title(record),
                    render_metadata(record, image_handler),
                )
                handled = True

            if not handled and options.include_raw:
                write_section(
                    dst,
                    "Raw",
                    render_raw_record(line_number, record, image_handler),
                )

    return count
