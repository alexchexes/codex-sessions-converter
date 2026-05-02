import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_sessions_converter.converter import (  # noqa: E402
    MarkdownOptions,
    convert_jsonl_to_markdown,
    convert_jsonl_to_yaml_stream,
    encode_for_output,
    list_session_lines,
    main,
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

    def test_list_sessions_cross_checks_index_and_session_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            matched_id = "019c8599-6845-7772-9c64-5f0ee47c73f1"
            missing_file_id = "11111111-1111-1111-1111-111111111111"
            orphan_id = "22222222-2222-2222-2222-222222222222"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [
                    {"id": matched_id, "thread_name": "Add scope for type casting types"},
                    {"id": missing_file_id, "thread_name": "Missing rollout"},
                ],
            )
            matched_path = sessions_day / f"rollout-2026-04-30T18-20-39-{matched_id}.jsonl"
            matched_path.write_text("", encoding="utf-8")
            orphan_path = sessions_day / f"rollout-2026-04-30T18-21-40-{orphan_id}.jsonl"
            orphan_path.write_text("", encoding="utf-8")

            lines = list_session_lines(codex_home)

            self.assertEqual(
                lines,
                [
                    (
                        f"{matched_id} - Add scope for type casting types - "
                        f"2026/04/30/{matched_path.name}"
                    ),
                    f"{missing_file_id} - Missing rollout - NO ROLLOUT FILE",
                    f"2026/04/30/{orphan_path.name} - NO ENTRY IN session_index.jsonl",
                ],
            )

    def test_list_sessions_reads_session_id_from_metadata_when_filename_has_no_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "33333333-3333-3333-3333-333333333333"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Metadata id"}],
            )
            session_path = sessions_day / "rollout.jsonl"
            write_jsonl(
                session_path,
                [{"type": "session_meta", "payload": {"id": session_id}}],
            )

            lines = list_session_lines(codex_home)

            self.assertEqual(
                lines,
                [f"{session_id} - Metadata id - 2026/04/30/rollout.jsonl"],
            )

    def test_list_sessions_accepts_concatenated_index_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            codex_home.joinpath("sessions").mkdir()

            first_id = "55555555-5555-5555-5555-555555555555"
            second_id = "66666666-6666-6666-6666-666666666666"
            records = [
                {"id": first_id, "thread_name": "First"},
                {"id": second_id, "thread_name": "Second"},
            ]
            codex_home.joinpath("session_index.jsonl").write_text(
                "".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            lines = list_session_lines(codex_home)

            self.assertEqual(
                lines,
                [
                    f"{first_id} - First - NO ROLLOUT FILE",
                    f"{second_id} - Second - NO ROLLOUT FILE",
                ],
            )

    def test_list_command_prints_session_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "44444444-4444-4444-4444-444444444444"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "CLI list"}],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["list", "--codex-home", str(codex_home)])

            self.assertEqual(result, 0)
            self.assertEqual(
                buffer.getvalue().splitlines(),
                [f"{session_id} - CLI list - NO ROLLOUT FILE"],
            )

    def test_encode_for_output_escapes_characters_unsupported_by_encoding(self) -> None:
        self.assertEqual(encode_for_output("Thread ✓", "cp1252"), r"Thread \u2713")
        self.assertEqual(encode_for_output("Thread ✓", "utf-8"), "Thread ✓")


if __name__ == "__main__":
    unittest.main()
