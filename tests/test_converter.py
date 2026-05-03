import base64
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_sessions_converter.converter import (  # noqa: E402
    MarkdownOptions,
    cli_prog_from_argv0,
    console_color_options,
    convert_jsonl_to_markdown,
    convert_jsonl_to_yaml_stream,
    default_output_path,
    encode_for_output,
    format_local_timestamp,
    list_session_lines,
    local_timezone_offset_label,
    main,
    parse_markdown_include,
    parse_timestamp,
    render_reasoning,
    resolve_markdown_tool_mode,
    resolve_output_path,
    search_cache_path,
)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


class ConverterTests(unittest.TestCase):
    def test_short_cli_entry_point_is_configured(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('codex-sessions = "codex_sessions_converter.converter:main"', pyproject)
        self.assertIn(
            'codex-sessions-converter = "codex_sessions_converter.converter:main"',
            pyproject,
        )

    def test_cli_prog_prefers_short_name(self) -> None:
        self.assertEqual(cli_prog_from_argv0("codex-sessions.exe"), "codex-sessions")
        self.assertEqual(
            cli_prog_from_argv0("codex-sessions-converter.exe"),
            "codex-sessions-converter",
        )
        self.assertEqual(cli_prog_from_argv0("converter.py"), "codex-sessions")

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

    def test_markdown_truncates_data_images_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.md"
            encoded_image = base64.b64encode(b"fake png bytes" * 10).decode("ascii")
            expected_prefix = f"{encoded_image[:24]}..."
            image_url = f"data:image/png;base64,{encoded_image}"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "see this"},
                                {"type": "input_text", "text": "<image>"},
                                {"type": "input_image", "image_url": image_url},
                                {"type": "input_text", "text": "</image>"},
                            ],
                        },
                    }
                ],
            )

            count = convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="none",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertEqual(count, 1)
            self.assertIn("see this", output)
            self.assertIn("[input image: image/png data URL;", output)
            self.assertIn("base64 chars truncated", output)
            self.assertIn(f"source `{input_path}:1`", output)
            self.assertIn(f"base64 prefix `{expected_prefix}`", output)
            self.assertNotIn(encoded_image, output)
            self.assertNotIn("<image>", output)
            self.assertNotIn("</image>", output)

    def test_markdown_extracts_data_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.md"
            image_bytes = b"fake png bytes"
            encoded_image = base64.b64encode(image_bytes).decode("ascii")
            image_url = f"data:image/png;base64,{encoded_image}"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "<image>"},
                                {"type": "input_image", "image_url": image_url},
                                {"type": "input_text", "text": "</image>"},
                            ],
                        },
                    }
                ],
            )

            count = convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="none",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                    image_mode="extract",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            image_files = list((Path(tmpdir) / "rollout_assets").glob("image-*.png"))
            self.assertEqual(count, 1)
            self.assertEqual(len(image_files), 1)
            self.assertEqual(image_files[0].read_bytes(), image_bytes)
            self.assertIn("![input image](rollout_assets/image-", output)
            self.assertNotIn(encoded_image, output)
            self.assertNotIn("<image>", output)
            self.assertNotIn("</image>", output)

    def test_markdown_inline_data_images_adds_hidden_extraction_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.md"
            encoded_image = base64.b64encode(b"fake png bytes" * 10).decode("ascii")
            image_url = f"data:image/png;base64,{encoded_image}"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_image", "image_url": image_url},
                            ],
                        },
                    }
                ],
            )

            convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="none",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                    image_mode="inline",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("[//]: # (Inline image;", output)
            self.assertIn("--md-images truncate", output)
            self.assertIn("--md-images extract", output)
            self.assertNotIn("To keep the Markdown small", output)
            self.assertNotIn("&#45;&#45;", output)
            self.assertIn(f"Source: {input_path}:1.", output)
            self.assertIn(f"![input image]({image_url})", output)

    def test_markdown_keeps_literal_image_tags_without_image_item(self) -> None:
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
                            "content": [
                                {"type": "input_text", "text": "<image>"},
                            ],
                        },
                    }
                ],
            )

            convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="none",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("<image>", output)

    def test_markdown_full_raw_truncates_data_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.md"
            encoded_image = base64.b64encode(b"fake png bytes" * 10).decode("ascii")
            expected_prefix = f"{encoded_image[:24]}..."
            image_url = f"data:image/png;base64,{encoded_image}"
            write_jsonl(
                input_path,
                [
                    {
                        "timestamp": "2026-04-26T00:00:00Z",
                        "type": "unknown",
                        "payload": {"image_url": image_url},
                    }
                ],
            )

            count = convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="none",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=True,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertEqual(count, 1)
            self.assertIn("data:image/png;base64,image/png data URL;", output)
            self.assertIn("rollout.jsonl:1", output)
            self.assertIn(f"base64 prefix `{expected_prefix}`", output)
            self.assertNotIn(encoded_image, output)

    def test_markdown_smart_mode_falls_back_to_names_for_unknown_tool_shape(self) -> None:
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
                            "type": "function_call",
                            "name": "future_tool",
                            "arguments": '{"text":"do not render this"}',
                            "call_id": "call_1",
                        },
                    }
                ],
            )

            convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="smart",
                    tool_preview_chars=40,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("**Tool call:** `future_tool`", output)
            self.assertIn("Call ID: `call_1`", output)
            self.assertNotIn("do not render this", output)

    def test_markdown_smart_mode_previews_apply_patch_input(self) -> None:
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
                            "type": "custom_tool_call",
                            "name": "apply_patch",
                            "input": "*** Begin Patch\n*** Update File: x\n+hello\n*** End Patch",
                            "call_id": "call_1",
                        },
                    }
                ],
            )

            convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="smart",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("**Tool call:** `apply_patch`", output)
            self.assertIn("Patch preview:", output)
            self.assertIn("*** Begin Patch", output)

    def test_markdown_smart_mode_previews_legacy_mcp_tool_names(self) -> None:
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
                            "type": "function_call",
                            "name": "mcp__playwright__browser_navigate",
                            "arguments": '{"url":"http://localhost:3000/"}',
                            "call_id": "call_1",
                        },
                    }
                ],
            )

            convert_jsonl_to_markdown(
                input_path,
                output_path,
                MarkdownOptions(
                    tool_mode="smart",
                    tool_preview_chars=80,
                    include_metadata=False,
                    include_raw=False,
                    redaction="...",
                ),
            )

            output = output_path.read_text(encoding="utf-8")
            self.assertIn("**Tool call:** `mcp__playwright__browser_navigate`", output)
            self.assertIn("Url: `http://localhost:3000/`", output)

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
        self.assertEqual(resolve_markdown_tool_mode({"tools"}, "auto"), "smart")
        self.assertEqual(resolve_markdown_tool_mode(set(), "auto"), "none")
        self.assertEqual(resolve_markdown_tool_mode(set(), "names"), "names")

    def test_encrypted_reasoning_renders_as_single_line(self) -> None:
        self.assertEqual(
            render_reasoning({"type": "reasoning", "encrypted_content": "secret"}, "..."),
            "**Reasoning (encrypted_content) ...**",
        )

    def test_include_modifiers(self) -> None:
        self.assertEqual(parse_markdown_include("default,-tools"), set())
        self.assertEqual(parse_markdown_include("dialogue,+metadata"), {"metadata"})

    def test_default_output_path_goes_under_codex_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            input_path = codex_home / "sessions" / "2026" / "04" / "30" / "rollout.jsonl"

            output_path = default_output_path(input_path, codex_home, "yaml")

            self.assertEqual(
                output_path,
                codex_home / "tmp" / "sessions" / "2026" / "04" / "30" / "rollout.yaml",
            )

    def test_directory_output_uses_default_output_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            input_path = Path(tmpdir) / "rollout.jsonl"
            codex_home = Path(tmpdir) / ".codex"

            output_path = resolve_output_path(output_dir, input_path, codex_home, "yaml", "abc")

            self.assertEqual(output_path, output_dir / "abc.yaml")

    def test_missing_input_exits_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_input = Path(tmpdir) / "missing.jsonl"
            output_path = Path(tmpdir) / "missing.yaml"

            with self.assertRaises(SystemExit) as raised:
                main([str(missing_input), str(output_path)])

            self.assertEqual(str(raised.exception), f"Input file not found: {missing_input}")
            self.assertFalse(output_path.exists())

    def test_session_id_input_converts_default_output_under_codex_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "019dd5ce-19e1-78c3-9313-325228ddd983"
            input_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(input_path, [{"type": "session_meta", "payload": {"id": session_id}}])

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main([session_id, "--codex-home", str(codex_home)])

            output_path = (
                codex_home / "tmp" / "sessions" / "2026" / "04" / "30" / f"{session_id}.yaml"
            )
            self.assertEqual(result, 0)
            self.assertTrue(output_path.exists())
            self.assertIn(str(output_path), buffer.getvalue())

    def test_session_id_input_can_write_to_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            output_dir = Path(tmpdir) / "out"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            output_dir.mkdir()
            session_id = "019dd5ce-19e1-78c3-9313-325228ddd983"
            input_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(input_path, [{"type": "session_meta", "payload": {"id": session_id}}])

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main([session_id, str(output_dir), "--codex-home", str(codex_home)])

            output_path = output_dir / f"{session_id}.yaml"
            self.assertEqual(result, 0)
            self.assertTrue(output_path.exists())
            self.assertIn(str(output_path), buffer.getvalue())

    def test_md_flag_converts_to_markdown_without_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "019dd5ce-19e1-78c3-9313-325228ddd983"
            input_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                input_path,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "hello",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell_command",
                            "arguments": '{"command":"echo hello"}',
                            "call_id": "call_1",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_1",
                            "output": "very long output",
                        },
                    },
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["--md", session_id, "--codex-home", str(codex_home)])

            output_path = (
                codex_home / "tmp" / "sessions" / "2026" / "04" / "30" / f"{session_id}.md"
            )
            self.assertEqual(result, 0)
            self.assertTrue(output_path.exists())
            output = output_path.read_text(encoding="utf-8")
            self.assertIn("# User:", output)
            self.assertIn("**Tool call:** `shell_command`", output)
            self.assertIn("Command preview:", output)
            self.assertIn("echo hello", output)
            self.assertIn("**Tool output:** `shell_command`", output)
            self.assertNotIn("very long output", output)
            self.assertIn(str(output_path), buffer.getvalue())

    def test_yaml_flag_converts_to_yaml_without_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / ".codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "019dd5ce-19e1-78c3-9313-325228ddd983"
            input_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(input_path, [{"type": "session_meta", "payload": {"id": session_id}}])

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["--yaml", session_id, "--codex-home", str(codex_home)])

            output_path = (
                codex_home / "tmp" / "sessions" / "2026" / "04" / "30" / f"{session_id}.yaml"
            )
            self.assertEqual(result, 0)
            self.assertTrue(output_path.exists())
            self.assertIn("session_meta", output_path.read_text(encoding="utf-8"))
            self.assertIn(str(output_path), buffer.getvalue())

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
                    {
                        "id": matched_id,
                        "thread_name": "Add scope for type casting types",
                        "updated_at": "2026-03-06T13:24:38.0294272Z",
                    },
                    {"id": missing_file_id, "thread_name": "Missing rollout"},
                ],
            )
            matched_path = sessions_day / f"rollout-2026-04-30T18-20-39-{matched_id}.jsonl"
            write_jsonl(
                matched_path,
                [
                    {
                        "timestamp": "2026-02-22T13:48:23.714Z",
                        "type": "session_meta",
                        "payload": {"id": matched_id},
                    },
                    {
                        "timestamp": "2026-02-22T13:50:54.380Z",
                        "type": "event_msg",
                        "payload": {"type": "turn_aborted"},
                    },
                ],
            )
            orphan_path = sessions_day / f"rollout-2026-04-30T18-21-40-{orphan_id}.jsonl"
            orphan_path.write_text("", encoding="utf-8")

            lines = list_session_lines(codex_home)
            started_at = parse_timestamp("2026-02-22T13:48:23.714Z")
            ended_at = parse_timestamp("2026-02-22T13:50:54.380Z")

            self.assertEqual(
                lines,
                [
                    (
                        f"{format_local_timestamp(started_at)} - "
                        f"{format_local_timestamp(ended_at)} "
                        f"({local_timezone_offset_label(ended_at)}) - "
                        f"{matched_id} - "
                        "Add scope for type casting types"
                    ),
                    f"{missing_file_id} - Missing rollout - NO ROLLOUT FILE",
                    f"2026/04/30/{orphan_path.name} - NO ENTRY IN session_index.jsonl",
                ],
            )

    def test_list_sessions_infers_title_for_unindexed_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "12121212-1212-1212-1212-121212121212"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Please add export support. Some extra detail follows.",
                        },
                    }
                ],
            )

            lines = list_session_lines(codex_home)
            started_at = parse_timestamp("2026-04-30T18:20:39Z")

            self.assertEqual(
                lines,
                [
                    (
                        f"{format_local_timestamp(started_at)} - "
                        f"{format_local_timestamp(started_at)} "
                        f"({local_timezone_offset_label(started_at)}) - "
                        f"{session_id} - Please add export support. - "
                        "NO ENTRY IN session_index.jsonl"
                    )
                ],
            )

    def test_list_sessions_skips_injected_context_when_inferring_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "45454545-4545-4545-4545-454545454545"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "<environment_context>\n<cwd>D:\\repos</cwd>",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Please sync this session to the Mac.",
                        },
                    },
                ],
            )

            lines = list_session_lines(codex_home)

            self.assertEqual(len(lines), 1)
            self.assertIn("Please sync this session to the Mac.", lines[0])
            self.assertNotIn("environment_context", lines[0])

    def test_list_sessions_reuses_cached_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "23232323-2323-2323-2323-232323232323"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Cached list title",
                        },
                    }
                ],
            )

            first_lines = list_session_lines(codex_home)
            self.assertTrue(search_cache_path(codex_home).exists())

            with patch(
                "codex_sessions_converter.converter.iter_jsonl_objects",
                side_effect=AssertionError("list should reuse cached session metadata"),
            ):
                second_lines = list_session_lines(codex_home)

            self.assertEqual(second_lines, first_lines)
            self.assertIn("Cached list title", second_lines[0])

    def test_list_sessions_reads_session_id_from_metadata_when_filename_has_no_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "33333333-3333-3333-3333-333333333333"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [
                    {
                        "id": session_id,
                        "thread_name": "Metadata id",
                        "updated_at": "2026-04-30T19:01:00Z",
                    }
                ],
            )
            session_path = sessions_day / "rollout.jsonl"
            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:00Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T19:15:30Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    },
                ],
            )

            lines = list_session_lines(codex_home)
            started_at = parse_timestamp("2026-04-30T18:20:00Z")
            ended_at = parse_timestamp("2026-04-30T19:15:30Z")

            self.assertEqual(
                lines,
                [
                    (
                        f"{format_local_timestamp(started_at)} - "
                        f"{format_local_timestamp(ended_at)} "
                        f"({local_timezone_offset_label(ended_at)}) - "
                        f"{session_id} - Metadata id"
                    )
                ],
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

    def test_find_searches_deserialized_text_and_groups_by_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "77777777-7777-7777-7777-777777777777"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Dadata integration"}],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T18:21:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": 'Install "dadata-sdk"\nThen run it',
                        },
                    },
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["find", "-i", "dadata-sdk", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn(f"{session_id} - Dadata integration", output)
            self.assertIn('Install "dadata-sdk"', output)
            self.assertNotIn("Then run it", output)
            self.assertNotIn("\\n", output)
            self.assertNotIn('\\"', output)

    def test_find_infers_title_for_unindexed_rollout_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "34343434-3434-3434-3434-343434343434"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Hand off this session to a Mac.",
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["find", "Mac", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn(f"{session_id} - Hand off this session to a Mac.", output)
            self.assertIn("NO ENTRY IN session_index.jsonl", output)

    def test_find_searches_visible_messages_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Repo investigation"}],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "cwd": r"d:\repos\copy-as-markdown",
                            "base_instructions": {
                                "text": "Large raw instructions mentioning copy-as-markdown"
                            },
                            "git": {
                                "branch": "main",
                                "repository_url": (
                                    "https://github.com/yorkxin/copy-as-markdown.git"
                                ),
                            },
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "Discuss copy-as-markdown behavior",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:22:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell_command",
                            "arguments": (
                                '{"command":"Get-Content package.json",'
                                '"workdir":"d:\\\\repos\\\\copy-as-markdown"}'
                            ),
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:22:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "copy-as-markdown should not be searched in outputs",
                        },
                    },
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["find", "copy-as-markdown", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("Codex: Discuss copy-as-markdown behavior", output)
            self.assertNotIn("Session metadata:", output)
            self.assertNotIn(r"cwd: d:\repos\copy-as-markdown", output)
            self.assertNotIn(
                "repository_url: https://github.com/yorkxin/copy-as-markdown.git", output
            )
            self.assertNotIn("base_instructions", output)
            self.assertNotIn("Large raw instructions", output)
            self.assertNotIn("should not be searched in outputs", output)
            self.assertNotIn("Tool call", output)

    def test_find_metadata_and_tools_are_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
            session_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "cwd": r"d:\repos\copy-as-markdown",
                            "git": {
                                "repository_url": (
                                    "https://github.com/yorkxin/copy-as-markdown.git"
                                )
                            },
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell_command",
                            "arguments": (
                                '{"command":"rg copy-as-markdown",'
                                '"workdir":"d:\\\\repos\\\\copy-as-markdown"}'
                            ),
                        },
                    },
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                default_result = main(["find", "copy-as-markdown", "--codex-home", str(codex_home)])
            self.assertEqual(default_result, 1)
            self.assertEqual(buffer.getvalue(), "")

            buffer = StringIO()
            with redirect_stdout(buffer):
                metadata_result = main(
                    ["find", "--metadata", "copy-as-markdown", "--codex-home", str(codex_home)]
                )
            self.assertEqual(metadata_result, 0)
            self.assertIn("Session metadata:", buffer.getvalue())

            buffer = StringIO()
            with redirect_stdout(buffer):
                tools_result = main(
                    ["find", "--tools", "copy-as-markdown", "--codex-home", str(codex_home)]
                )
            self.assertEqual(tools_result, 0)
            self.assertIn("Tool call: shell_command", buffer.getvalue())

    def test_find_regex_is_case_insensitive_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "88888888-8888-8888-8888-888888888888"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T18:20:50Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "Need DADATA-SDK setup",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell_command",
                            "arguments": '{"command":"npm install DADATA-SDK"}',
                        },
                    },
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "grep",
                        "-i",
                        "-r",
                        "--line-width",
                        "80",
                        "dadata-[a-z]+",
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("DADATA-SDK", output)
            self.assertIn("NO ENTRY IN session_index.jsonl", output)

    def test_find_truncates_long_matching_lines_with_multiple_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
            long_line = f"{'a' * 80} copy-as-markdown {'b' * 80} copy-as-markdown {'c' * 80}"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": long_line,
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "find",
                        "--line-width",
                        "90",
                        "copy-as-markdown",
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            output = buffer.getvalue()
            matching_lines = [line for line in output.splitlines() if "copy-as-markdown" in line]
            self.assertEqual(result, 0)
            self.assertEqual(len(matching_lines), 1)
            self.assertLessEqual(len(matching_lines[0]), 92)
            self.assertIn("...", matching_lines[0])
            self.assertEqual(matching_lines[0].count("copy-as-markdown"), 2)

    def test_find_uses_available_width_for_single_match_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
            long_line = f"{'a' * 100} copy-as-markdown {'b' * 100}"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": long_line,
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "find",
                        "--line-width",
                        "120",
                        "copy-as-markdown",
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            output = buffer.getvalue()
            matching_lines = [line for line in output.splitlines() if "copy-as-markdown" in line]
            self.assertEqual(result, 0)
            self.assertEqual(len(matching_lines), 1)
            self.assertGreaterEqual(len(matching_lines[0]), 115)
            self.assertLessEqual(len(matching_lines[0]), 122)

    def test_find_summarizes_extra_matches_on_one_long_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
            long_line = (
                "first useful context before copy-as-markdown "
                + " filler text " * 8
                + "second useful context before copy-as-markdown "
                + " filler text " * 8
                + "third copy-as-markdown fourth copy-as-markdown fifth copy-as-markdown"
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": long_line,
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "find",
                        "--line-width",
                        "120",
                        "copy-as-markdown",
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            output = buffer.getvalue()
            matching_lines = [line for line in output.splitlines() if "more on line" in line]
            self.assertEqual(result, 0)
            self.assertEqual(len(matching_lines), 1)
            self.assertLessEqual(len(matching_lines[0]), 122)
            self.assertEqual(matching_lines[0].count("copy-as-markdown"), 1)
            self.assertIn("(+4 more on line)", matching_lines[0])

    def test_find_color_always_highlights_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "99999999-9999-9999-9999-999999999999"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "dadata-sdk",
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "find",
                        "--color",
                        "always",
                        "dadata-sdk",
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("\x1b[", output)
            self.assertIn("\x1b[1;91m", output)
            self.assertIn("dadata-sdk", output)

    def test_color_auto_forces_terminal_for_git_bash_pipe(self) -> None:
        git_bash_env = {"TERM": "xterm-256color", "MSYSTEM": "MINGW64"}
        with patch("codex_sessions_converter.converter.is_windows_pipe_stream", return_value=True):
            self.assertEqual(
                console_color_options("auto", StringIO(), git_bash_env),
                (True, False),
            )

    def test_color_auto_does_not_force_for_git_bash_disk_redirect(self) -> None:
        git_bash_env = {"TERM": "xterm-256color", "MSYSTEM": "MINGW64"}
        with patch("codex_sessions_converter.converter.is_windows_pipe_stream", return_value=False):
            self.assertEqual(
                console_color_options("auto", StringIO(), git_bash_env),
                (None, False),
            )

    def test_color_auto_honors_standard_color_environment_flags(self) -> None:
        self.assertEqual(
            console_color_options("auto", StringIO(), {"NO_COLOR": "1"}),
            (None, True),
        )
        self.assertEqual(
            console_color_options("auto", StringIO(), {"CLICOLOR": "0"}),
            (None, True),
        )
        self.assertEqual(
            console_color_options("auto", StringIO(), {"FORCE_COLOR": "1"}),
            (True, False),
        )

    def test_find_returns_one_when_no_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            codex_home.joinpath("sessions").mkdir()

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["find", "missing", "--codex-home", str(codex_home)])

            self.assertEqual(result, 1)
            self.assertEqual(buffer.getvalue(), "")

    def test_find_reuses_cached_search_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "cached needle",
                        },
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                first_result = main(["find", "needle", "--codex-home", str(codex_home)])
            self.assertEqual(first_result, 0)
            self.assertTrue(search_cache_path(codex_home).exists())

            with patch(
                "codex_sessions_converter.converter.iter_jsonl_objects",
                side_effect=AssertionError("cache should avoid reparsing rollout JSONL"),
            ):
                buffer = StringIO()
                with redirect_stdout(buffer):
                    second_result = main(["find", "needle", "--codex-home", str(codex_home)])

            self.assertEqual(second_result, 0)
            self.assertIn("cached needle", buffer.getvalue())

    def test_find_invalidates_cache_when_rollout_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
            session_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "old needle",
                        },
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                first_result = main(["find", "needle", "--codex-home", str(codex_home)])
            self.assertEqual(first_result, 0)

            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "new replacement text",
                        },
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                old_result = main(["find", "needle", "--codex-home", str(codex_home)])
            buffer = StringIO()
            with redirect_stdout(buffer):
                new_result = main(["find", "replacement", "--codex-home", str(codex_home)])

            self.assertEqual(old_result, 1)
            self.assertEqual(new_result, 0)
            self.assertIn("new replacement text", buffer.getvalue())

    def test_find_no_cache_does_not_write_search_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "abababab-abab-abab-abab-abababababab"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "uncached needle",
                        },
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(["find", "--no-cache", "needle", "--codex-home", str(codex_home)])

            self.assertEqual(result, 0)
            self.assertFalse(search_cache_path(codex_home).exists())

    def test_find_limits_matching_lines_per_session_with_omission_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            records: list[dict[str, Any]] = [
                {
                    "timestamp": "2026-04-30T18:20:39Z",
                    "type": "session_meta",
                    "payload": {"id": session_id},
                }
            ]
            for index in range(3):
                records.append(
                    {
                        "timestamp": "2026-04-30T18:21:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": f"needle context {index}",
                        },
                    }
                )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                records,
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "find",
                        "needle",
                        "--max-lines-per-session",
                        "2",
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("needle context 0", output)
            self.assertIn("needle context 1", output)
            self.assertNotIn("needle context 2", output)
            self.assertIn("+1 more occurrences", output)

    def test_encode_for_output_escapes_characters_unsupported_by_encoding(self) -> None:
        self.assertEqual(encode_for_output("Thread ✓", "cp1252"), r"Thread \u2713")
        self.assertEqual(encode_for_output("Thread ✓", "utf-8"), "Thread ✓")


if __name__ == "__main__":
    unittest.main()
