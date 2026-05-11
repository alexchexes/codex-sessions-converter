import json
import math
import re
from pathlib import Path
from typing import Any

from codex_sessions_converter.json_streams import iter_jsonl_objects
from codex_sessions_converter.session_documents import sanitize

SIMPLE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


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
