import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_sessions_converter.converter import (  # noqa: E402
    MarkdownOptions,
    convert_jsonl_to_markdown,
    convert_jsonl_to_yaml_stream,
    parse_markdown_include,
    resolve_markdown_tool_mode,
)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


class ConverterTests(unittest.TestCase):
    def test_yaml_conversion_redacts_encrypted_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.yaml"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "reasoning",
                            "encrypted_content": "secret",
                        },
                    }
                ],
            )

            count = convert_jsonl_to_yaml_stream(input_path, output_path, "...")

            self.assertEqual(count, 1)
            output = output_path.read_text(encoding="utf-8")
            self.assertIn('encrypted_content: "..."', output)
            self.assertNotIn("secret", output)

    def test_markdown_names_mode_omits_tool_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.md"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hello"}],
                        },
                    },
                    {
                        "timestamp": "2026-04-26T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell_command",
                            "arguments": '{"command":"echo hello"}',
                            "call_id": "call_1",
                        },
                    },
                    {
                        "timestamp": "2026-04-26T00:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_1",
                            "output": "very long output",
                        },
                    },
                ],
            )

            count = convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="names",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertEqual(count, 3)
            self.assertIn("**Tool call:** `shell_command`", output)
            self.assertIn("**Tool output:** `shell_command`", output)
            self.assertNotIn("echo hello", output)
            self.assertNotIn("very long output", output)

    def test_markdown_preview_mode_truncates_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.md"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "tool_search_output",
                            "call_id": "call_1",
                            "status": "completed",
                            "tools": [{"name": "example", "description": "x" * 80}],
                        },
                    }
                ],
            )

            convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="preview",
                    tool_preview_chars=40,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("Output preview:", output)
            self.assertIn("truncated", output)

    def test_markdown_metadata_table_escapes_pipes_and_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.md"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:00Z",
                        "type": "turn_context",
                        "payload": {"note": "a|b\nc"},
                    }
                ],
            )

            convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="none",
                    tool_preview_chars=80,
                    include_metadata=True,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("a\\|b<br>c", output)

    def test_tool_mode_auto_follows_include_preset(self) -> None:
        self.assertEqual(resolve_markdown_tool_mode({"tools"}, "auto"), "full")
        self.assertEqual(resolve_markdown_tool_mode(set(), "auto"), "none")
        self.assertEqual(resolve_markdown_tool_mode(set(), "names"), "names")

    def test_include_modifiers(self) -> None:
        self.assertEqual(parse_markdown_include("default,-tools"), set())
        self.assertEqual(parse_markdown_include("dialogue,+metadata"), {"metadata"})


if __name__ == "__main__":
    unittest.main()
