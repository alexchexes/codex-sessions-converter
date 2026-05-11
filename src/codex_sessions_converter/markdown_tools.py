import json
from collections.abc import Iterable
from typing import Any

from codex_sessions_converter.markdown_formatting import (
    fenced_block,
    parse_json_maybe,
    render_json_block_content,
)
from codex_sessions_converter.markdown_images import MarkdownImageHandler

DEFAULT_TOOL_PREVIEW_CHARS = 700


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


def render_preview_block(
    label: str,
    value: Any,
    max_chars: int,
    image_handler: MarkdownImageHandler | None = None,
) -> list[str]:
    body, language = parse_json_maybe(value, image_handler)
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
        append_fenced_preview(lines, "Command preview:", args.get("command"), preview_chars)
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
    payload: dict[str, Any],
    mode: str,
    preview_chars: int,
    image_handler: MarkdownImageHandler | None = None,
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
            lines.extend(
                render_preview_block("Arguments preview:", arguments, preview_chars, image_handler)
            )
        else:
            body, language = parse_json_maybe(arguments, image_handler)
            lines.extend(["", "Arguments:", fenced_block(body, language)])
    else:
        remaining = {
            key: value
            for key, value in payload.items()
            if key not in {"type", "name", "namespace", "call_id", "status", "execution"}
        }
        if remaining:
            if mode == "preview":
                lines.extend(
                    render_preview_block(
                        "Payload preview:", remaining, preview_chars, image_handler
                    )
                )
            else:
                if image_handler:
                    remaining = image_handler.transform_value(remaining)
                lines.extend(["", fenced_block(render_json_block_content(remaining), "json")])
    return "\n".join(lines), full_name


def render_tool_output(
    payload: dict[str, Any],
    mode: str,
    preview_chars: int,
    tool_names_by_call_id: dict[str, str],
    image_handler: MarkdownImageHandler | None = None,
) -> str:
    call_type = payload.get("type", "tool_output")
    call_id = payload.get("call_id")
    tool_name = tool_names_by_call_id.get(call_id, call_type) if call_id else call_type
    lines = [f"**Tool output:** `{tool_name}`"]
    append_tool_identity(lines, payload)

    if mode in {"names", "smart"}:
        return "\n".join(lines)

    if "output" in payload:
        body, language = parse_json_maybe(payload["output"], image_handler)
    else:
        remaining = {
            key: value
            for key, value in payload.items()
            if key not in {"type", "call_id", "status", "execution"}
        }
        if image_handler:
            remaining = image_handler.transform_value(remaining)
        body = render_json_block_content(remaining)
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
