import base64
import json
import os
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
    cli_prog_from_argv0,
    default_output_path,
    format_local_timestamp,
    list_session_lines,
    local_timezone_offset_label,
    main,
    parse_markdown_include,
    resolve_markdown_tool_mode,
    resolve_output_path,
)
from codex_sessions_converter.markdown_output import (  # noqa: E402
    MarkdownOptions,
    convert_jsonl_to_markdown,
    render_reasoning,
)
from codex_sessions_converter.search_cache import search_cache_path  # noqa: E402
from codex_sessions_converter.search_output import (  # noqa: E402
    console_color_options,
    encode_for_output,
)
from codex_sessions_converter.timestamps import parse_timestamp  # noqa: E402
from codex_sessions_converter.yaml_output import convert_jsonl_to_yaml_stream  # noqa: E402


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


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

    def test_list_sessions_infers_title_from_request_inside_ide_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "89898989-8989-8989-8989-898989898989"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": (
                                "# Context from my IDE setup:\n\n"
                                "## Active file: converter.py\n\n"
                                "## My request for Codex:\n"
                                "Please repair the index title."
                            ),
                        },
                    }
                ],
            )

            lines = list_session_lines(codex_home)

            self.assertEqual(len(lines), 1)
            self.assertIn("Please repair the index title.", lines[0])
            self.assertNotIn("Context from my IDE setup", lines[0])

    def test_list_sessions_infers_title_from_event_message_ide_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "bcbcbcbc-bcbc-bcbc-bcbc-bcbcbcbcbcbc"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                "# Context from my IDE setup:\n\n"
                                "## Active file: converter.py\n\n"
                                "## My request for Codex:\n"
                                "Please title this event message."
                            ),
                        },
                    }
                ],
            )

            lines = list_session_lines(codex_home)

            self.assertEqual(len(lines), 1)
            self.assertIn("Please title this event message.", lines[0])
            self.assertNotIn("Context from my IDE setup", lines[0])

    def test_list_sessions_prefers_thread_name_updated_title_from_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Rollout title wins",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "This fallback title should not be used.",
                        },
                    },
                ],
            )

            lines = list_session_lines(codex_home)

            self.assertEqual(len(lines), 1)
            self.assertIn("Rollout title wins", lines[0])
            self.assertNotIn("This fallback title", lines[0])

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
                "codex_sessions_converter.session_documents.iter_jsonl_objects",
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

    def test_repair_index_dry_run_reports_missing_entries_without_modifying_index(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            indexed_id = "56565656-5656-5656-5656-565656565656"
            missing_id = "67676767-6767-6767-6767-676767676767"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(
                index_path,
                [{"id": indexed_id, "thread_name": "Already indexed"}],
            )
            original_index = index_path.read_text(encoding="utf-8")
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{indexed_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Already indexed message",
                        },
                    }
                ],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-21-39-{missing_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Repair the missing index entry. More details.",
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["repair-index", "--dry-run", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertEqual(index_path.read_text(encoding="utf-8"), original_index)
            self.assertIn("Missing session_index.jsonl entries: 1", output)
            self.assertIn("Would add:", output)
            self.assertIn(f"{missing_id} - Repair the missing index entry.", output)
            self.assertIn("2026/04/30/rollout-2026-04-30T18-21-39-", output)
            self.assertIn("updated_at: 2026-04-30T18:21:39+00:00", output)
            self.assertIn("State cache reset required after repair.", output)
            self.assertNotIn(indexed_id, output)

    def test_repair_index_prefers_thread_name_updated_title_from_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            missing_id = "57575757-5757-5757-5757-575757575757"
            write_jsonl(codex_home / "session_index.jsonl", [])
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-21-39-{missing_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": missing_id,
                            "thread_name": "Repair uses rollout title",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:22:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "This fallback title should not be used.",
                        },
                    },
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["repair-index", "--dry-run", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn(f"{missing_id} - Repair uses rollout title", output)
            self.assertNotIn("This fallback title", output)

    def test_repair_index_dry_run_reports_no_missing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "78787878-7878-7878-7878-787878787878"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Already indexed"}],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Already indexed message",
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["repair-index", "--dry-run", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("Missing session_index.jsonl entries: 0", output)
            self.assertIn("No missing session_index.jsonl entries found.", output)
            self.assertNotIn("State cache reset required", output)

    def test_repair_index_adds_entries_and_resets_state_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            missing_id = "89898989-8989-8989-8989-898989898989"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(index_path, [{"id": "11111111-1111-1111-1111-111111111111"}])
            original_index = index_path.read_text(encoding="utf-8")
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-21-39-{missing_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Repair the real index entry.",
                        },
                    }
                ],
            )
            state_db = codex_home / "state_5.sqlite"
            state_wal = codex_home / "state_5.sqlite-wal"
            logs_db = codex_home / "logs_2.sqlite"
            state_db.write_text("state", encoding="utf-8")
            state_wal.write_text("wal", encoding="utf-8")
            logs_db.write_text("logs", encoding="utf-8")

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["repair-index", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            index_records = [
                json.loads(line)
                for line in index_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertEqual(result, 0)
            self.assertIn("Added session_index.jsonl entries: 1", output)
            self.assertIn("Session index backup:", output)
            self.assertIn("State cache backups:", output)
            self.assertEqual(index_records[-1]["id"], missing_id)
            self.assertEqual(index_records[-1]["thread_name"], "Repair the real index entry.")
            self.assertEqual(index_records[-1]["updated_at"], "2026-04-30T18:21:39Z")
            self.assertFalse(state_db.exists())
            self.assertFalse(state_wal.exists())
            self.assertTrue(logs_db.exists())
            backup_dirs = sorted((codex_home / "backups" / "codex-sessions").iterdir())
            self.assertEqual(len(backup_dirs), 1)
            backup_dir = backup_dirs[0]
            self.assertEqual(
                (backup_dir / "session_index.jsonl").read_text(encoding="utf-8"),
                original_index,
            )
            self.assertEqual((backup_dir / "state_5.sqlite").read_text(encoding="utf-8"), "state")
            self.assertEqual(
                (backup_dir / "state_5.sqlite-wal").read_text(encoding="utf-8"),
                "wal",
            )
            self.assertEqual(list(codex_home.glob("state_5.sqlite.backup-*")), [])
            self.assertEqual(list(codex_home.glob("session_index.jsonl.backup-*")), [])

    def test_repair_index_rolls_back_index_when_state_reset_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            missing_id = "90909090-9090-9090-9090-909090909090"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(index_path, [{"id": "11111111-1111-1111-1111-111111111111"}])
            original_index = index_path.read_text(encoding="utf-8")
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-21-39-{missing_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Rollback the failed repair.",
                        },
                    }
                ],
            )

            with patch(
                "codex_sessions_converter.converter.reset_codex_state_cache",
                side_effect=OSError("locked"),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["repair-index", "--codex-home", str(codex_home)])

            self.assertIn("Rolled back session_index.jsonl", str(raised.exception))
            self.assertEqual(index_path.read_text(encoding="utf-8"), original_index)
            self.assertEqual(list(codex_home.glob("session_index.jsonl.backup-*")), [])
            self.assertEqual(
                list(codex_home.glob("backups/codex-sessions/*/session_index.jsonl")),
                [],
            )

    def test_rename_updates_index_and_resets_state_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            session_id = "12121212-1212-1212-1212-121212121212"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(
                index_path,
                [
                    {
                        "id": session_id,
                        "thread_name": "Old title",
                        "updated_at": "2026-04-30T18:21:39Z",
                        "extra": "preserved",
                    }
                ],
            )
            original_index = index_path.read_text(encoding="utf-8")
            state_db = codex_home / "state_5.sqlite"
            state_db.write_text("state", encoding="utf-8")
            previous_backup = codex_home / "state_5.sqlite.backup-20260504-112050-16164"
            previous_backup.write_text("previous backup", encoding="utf-8")

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "rename",
                        "--codex-home",
                        str(codex_home),
                        session_id,
                        "New",
                        "title",
                    ]
                )

            output = buffer.getvalue()
            index_records = [
                json.loads(line)
                for line in index_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertEqual(result, 0)
            self.assertIn(f"Renamed session: {session_id}", output)
            self.assertIn("From: Old title", output)
            self.assertIn("To: New title", output)
            self.assertIn("Session index backup:", output)
            self.assertIn("State cache backups:", output)
            self.assertEqual(index_records[0]["thread_name"], "New title")
            self.assertEqual(index_records[0]["updated_at"], "2026-04-30T18:21:39Z")
            self.assertEqual(index_records[0]["extra"], "preserved")
            self.assertFalse(state_db.exists())
            backup_dirs = sorted((codex_home / "backups" / "codex-sessions").iterdir())
            self.assertEqual(len(backup_dirs), 1)
            backup_dir = backup_dirs[0]
            self.assertEqual(
                (backup_dir / "session_index.jsonl").read_text(encoding="utf-8"),
                original_index,
            )
            self.assertEqual((backup_dir / "state_5.sqlite").read_text(encoding="utf-8"), "state")
            self.assertEqual(list(codex_home.glob("session_index.jsonl.backup-*")), [])
            self.assertTrue(previous_backup.exists())
            self.assertEqual(previous_backup.read_text(encoding="utf-8"), "previous backup")
            self.assertEqual(
                list(codex_home.glob("state_5.sqlite.backup-*.backup-*")),
                [],
            )

    def test_rename_updates_rollout_when_index_title_is_already_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "15151515-1515-1515-1515-151515151515"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(index_path, [{"id": session_id, "thread_name": "New title"}])
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Old rollout title",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Session body",
                        },
                    },
                ],
            )
            original_rollout = rollout_path.read_text(encoding="utf-8")
            state_db = codex_home / "state_5.sqlite"
            state_db.write_text("state", encoding="utf-8")

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["rename", "--codex-home", str(codex_home), session_id, "New title"])

            output = buffer.getvalue()
            rollout_records = [
                json.loads(line)
                for line in rollout_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            backup_dirs = sorted((codex_home / "backups" / "codex-sessions").iterdir())
            self.assertEqual(result, 0)
            self.assertIn("Session index title was already set.", output)
            self.assertIn("Rollout title from: Old rollout title", output)
            self.assertEqual(
                rollout_records[0]["payload"]["thread_name"],
                "New title",
            )
            self.assertEqual(len(backup_dirs), 1)
            backup_dir = backup_dirs[0]
            self.assertFalse((backup_dir / "session_index.jsonl").exists())
            self.assertEqual(
                (backup_dir / rollout_path.name).read_text(encoding="utf-8"),
                original_rollout,
            )
            self.assertEqual((backup_dir / "state_5.sqlite").read_text(encoding="utf-8"), "state")

    def test_rename_inserts_rollout_title_event_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "16161616-1616-1616-1616-161616161616"
            write_jsonl(
                codex_home / "session_index.jsonl", [{"id": session_id, "thread_name": "Old"}]
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Session body",
                        },
                    },
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(["rename", "--codex-home", str(codex_home), session_id, "New"])

            rollout_records = [
                json.loads(line)
                for line in rollout_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertEqual(result, 0)
            self.assertEqual(rollout_records[0]["type"], "session_meta")
            self.assertEqual(rollout_records[1]["type"], "event_msg")
            self.assertEqual(rollout_records[1]["timestamp"], "2026-04-30T18:20:39Z")
            self.assertEqual(rollout_records[1]["payload"]["type"], "thread_name_updated")
            self.assertEqual(rollout_records[1]["payload"]["thread_name"], "New")
            self.assertEqual(rollout_records[-1]["timestamp"], "2026-04-30T18:21:39Z")

    def test_rename_accepts_exact_existing_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            session_id = "13131313-1313-1313-1313-131313131313"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(
                index_path,
                [{"id": session_id, "thread_name": "Old exact title"}],
            )

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "rename",
                        "--codex-home",
                        str(codex_home),
                        "Old exact title",
                        "New exact title",
                    ]
                )

            index_records = [
                json.loads(line)
                for line in index_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            self.assertEqual(result, 0)
            self.assertEqual(index_records[0]["thread_name"], "New exact title")

    def test_rename_rolls_back_index_when_state_reset_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            session_id = "14141414-1414-1414-1414-141414141414"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(index_path, [{"id": session_id, "thread_name": "Old title"}])
            original_index = index_path.read_text(encoding="utf-8")

            with patch(
                "codex_sessions_converter.converter.reset_codex_state_cache",
                side_effect=OSError("locked"),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(
                        [
                            "rename",
                            "--codex-home",
                            str(codex_home),
                            session_id,
                            "New title",
                        ]
                    )

            self.assertIn("Rolled back Codex session files", str(raised.exception))
            self.assertEqual(index_path.read_text(encoding="utf-8"), original_index)
            self.assertEqual(list(codex_home.glob("session_index.jsonl.backup-*")), [])
            self.assertEqual(
                list(codex_home.glob("backups/codex-sessions/*/session_index.jsonl")),
                [],
            )

    def test_import_dry_run_reports_plan_without_modifying_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            source_dir = root / "incoming"
            source_dir.mkdir()
            session_id = "17171717-1717-1717-1717-171717171717"
            source_path = source_dir / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Import dry run title",
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    ["import", "--dry-run", "--codex-home", str(codex_home), str(source_path)]
                )

            output = buffer.getvalue()
            target_path = codex_home / "sessions" / "2026" / "04" / "30" / source_path.name
            self.assertEqual(result, 0)
            self.assertIn(f"Session: {session_id} - Import dry run title", output)
            self.assertIn(f"Target rollout: {target_path}", output)
            self.assertIn("Index action: add session_index.jsonl entry", output)
            self.assertIn("Rollout action: copy unchanged", output)
            self.assertIn("Source fingerprint:", output)
            self.assertFalse(target_path.exists())
            self.assertFalse((codex_home / "session_index.jsonl").exists())

    def test_import_bare_rollout_adds_index_copies_rollout_and_resets_state_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            codex_home.mkdir()
            source_dir = root / "incoming"
            source_dir.mkdir()
            session_id = "18181818-1818-1818-1818-181818181818"
            source_path = source_dir / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            records = [
                {
                    "timestamp": "2026-04-30T18:20:39Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_name_updated",
                        "thread_id": session_id,
                        "thread_name": "Imported title",
                    },
                },
                {
                    "timestamp": "2026-04-30T18:21:39Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": "Imported body",
                    },
                },
            ]
            write_jsonl(source_path, records)
            state_db = codex_home / "state_5.sqlite"
            state_db.write_text("state", encoding="utf-8")

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["import", "--codex-home", str(codex_home), str(source_path)])

            output = buffer.getvalue()
            target_path = codex_home / "sessions" / "2026" / "04" / "30" / source_path.name
            index_records = read_jsonl(codex_home / "session_index.jsonl")
            backup_dirs = sorted((codex_home / "backups" / "codex-sessions").iterdir())
            self.assertEqual(result, 0)
            self.assertIn(f"Imported session: {session_id} - Imported title", output)
            self.assertIn("Index action: add session_index.jsonl entry", output)
            self.assertIn("State cache backups:", output)
            self.assertEqual(
                target_path.read_text(encoding="utf-8"), source_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                index_records,
                [
                    {
                        "id": session_id,
                        "thread_name": "Imported title",
                        "updated_at": "2026-04-30T18:21:39Z",
                    }
                ],
            )
            self.assertFalse(state_db.exists())
            self.assertEqual(len(backup_dirs), 1)
            self.assertEqual(
                (backup_dirs[0] / "state_5.sqlite").read_text(encoding="utf-8"), "state"
            )

    def test_import_inserts_rollout_title_event_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            source_dir = root / "incoming"
            source_dir.mkdir()
            session_id = "19191919-1919-1919-1919-191919191919"
            source_path = source_dir / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Infer this imported title. More body.",
                        },
                    },
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(["import", "--codex-home", str(codex_home), str(source_path)])

            target_path = codex_home / "sessions" / "2026" / "04" / "30" / source_path.name
            rollout_records = read_jsonl(target_path)
            index_records = read_jsonl(codex_home / "session_index.jsonl")
            self.assertEqual(result, 0)
            self.assertEqual(rollout_records[0]["type"], "session_meta")
            self.assertEqual(rollout_records[1]["type"], "event_msg")
            self.assertEqual(rollout_records[1]["timestamp"], "2026-04-30T18:20:39Z")
            self.assertEqual(
                rollout_records[1]["payload"]["thread_name"], "Infer this imported title."
            )
            self.assertEqual(index_records[0]["thread_name"], "Infer this imported title.")

    def test_import_refuses_identical_existing_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            source_dir = root / "incoming"
            source_dir.mkdir()
            session_id = "20202020-2020-2020-2020-202020202020"
            filename = f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            source_path = source_dir / filename
            target_path = codex_home / "sessions" / "2026" / "04" / "30" / filename
            target_path.parent.mkdir(parents=True)
            records = [
                {
                    "timestamp": "2026-04-30T18:20:39Z",
                    "type": "session_meta",
                    "payload": {"id": session_id},
                }
            ]
            write_jsonl(source_path, records)
            write_jsonl(target_path, records)

            with self.assertRaises(SystemExit) as raised:
                main(["import", "--codex-home", str(codex_home), str(source_path)])

            self.assertIn("already imported with identical rollout file", str(raised.exception))
            self.assertIn("sha256", str(raised.exception))

    def test_import_refuses_different_existing_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            source_dir = root / "incoming"
            source_dir.mkdir()
            session_id = "21212121-2121-2121-2121-212121212121"
            filename = f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            source_path = source_dir / filename
            target_path = codex_home / "sessions" / "2026" / "04" / "30" / filename
            target_path.parent.mkdir(parents=True)
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    }
                ],
            )
            write_jsonl(
                target_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": "different"},
                    },
                ],
            )

            with self.assertRaises(SystemExit) as raised:
                main(["import", "--codex-home", str(codex_home), str(source_path)])

            message = str(raised.exception)
            self.assertIn("already imported, but rollout file differs", message)
            self.assertIn("Existing:", message)
            self.assertIn("import:", message)
            self.assertIn("sha256", message)

    def test_import_existing_index_without_rollout_uses_existing_index_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            codex_home.mkdir()
            source_dir = root / "incoming"
            source_dir.mkdir()
            session_id = "22222222-2222-2222-2222-222222222222"
            source_path = source_dir / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Existing index title"}],
            )
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Source rollout title",
                        },
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(["import", "--codex-home", str(codex_home), str(source_path)])

            target_path = codex_home / "sessions" / "2026" / "04" / "30" / source_path.name
            rollout_records = read_jsonl(target_path)
            index_records = read_jsonl(codex_home / "session_index.jsonl")
            self.assertEqual(result, 0)
            self.assertEqual(rollout_records[0]["payload"]["thread_name"], "Existing index title")
            self.assertEqual(
                index_records, [{"id": session_id, "thread_name": "Existing index title"}]
            )

    def test_import_rolls_back_index_and_target_rollout_when_state_reset_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            codex_home.mkdir()
            source_dir = root / "incoming"
            source_dir.mkdir()
            session_id = "28282828-2828-2828-2828-282828282828"
            index_path = codex_home / "session_index.jsonl"
            write_jsonl(index_path, [{"id": "11111111-1111-1111-1111-111111111111"}])
            original_index = index_path.read_text(encoding="utf-8")
            source_path = source_dir / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Rollback import title",
                        },
                    }
                ],
            )
            target_path = codex_home / "sessions" / "2026" / "04" / "30" / source_path.name

            with patch(
                "codex_sessions_converter.converter.reset_codex_state_cache",
                side_effect=OSError("locked"),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main(["import", "--codex-home", str(codex_home), str(source_path)])

            self.assertIn("Rolled back imported Codex session files", str(raised.exception))
            self.assertEqual(index_path.read_text(encoding="utf-8"), original_index)
            self.assertFalse(target_path.exists())
            self.assertEqual(
                list(codex_home.glob("backups/codex-sessions/*/session_index.jsonl")),
                [],
            )

    def test_import_reports_missing_input_without_writing_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex"
            missing_path = Path(tmpdir) / "missing.jsonl"

            with self.assertRaises(SystemExit) as raised:
                main(["import", "--codex-home", str(codex_home), str(missing_path)])

            self.assertIn("Input file not found", str(raised.exception))
            self.assertFalse((codex_home / "session_index.jsonl").exists())

    def test_import_reports_directory_input_without_writing_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            source_dir = root / "incoming"
            source_dir.mkdir()

            with self.assertRaises(SystemExit) as raised:
                main(["import", "--codex-home", str(codex_home), str(source_dir)])

            self.assertIn("Input path is not a file", str(raised.exception))
            self.assertFalse((codex_home / "session_index.jsonl").exists())

    def test_import_reports_rollout_without_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            source_path = root / "rollout-2026-04-30T18-20-39.jsonl"
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "No ID here",
                        },
                    }
                ],
            )

            with self.assertRaises(SystemExit) as raised:
                main(["import", "--codex-home", str(codex_home), str(source_path)])

            self.assertIn("Cannot infer session id from rollout", str(raised.exception))
            self.assertFalse((codex_home / "session_index.jsonl").exists())

    def test_import_non_rollout_filename_generates_codex_rollout_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            source_path = root / "session.jsonl"
            session_id = "29292929-2929-2929-2929-292929292929"
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T12:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T12:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Import with generated name.",
                        },
                    },
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(["import", "--codex-home", str(codex_home), str(source_path)])

            imported_files = list((codex_home / "sessions").rglob("*.jsonl"))
            self.assertEqual(result, 0)
            self.assertEqual(len(imported_files), 1)
            self.assertNotEqual(imported_files[0].name, source_path.name)
            self.assertTrue(imported_files[0].name.startswith("rollout-2026-04-30T"))
            self.assertTrue(imported_files[0].name.endswith(f"-{session_id}.jsonl"))

    def test_import_name_updates_existing_index_without_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            codex_home.mkdir()
            source_path = root / "incoming.jsonl"
            session_id = "30303030-3030-3030-3030-303030303030"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Old title", "extra": "preserved"}],
            )
            write_jsonl(
                source_path,
                [
                    {
                        "timestamp": "2026-04-30T12:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    },
                    {
                        "timestamp": "2026-04-30T12:21:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Source title",
                        },
                    },
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "import",
                        "--codex-home",
                        str(codex_home),
                        "--name",
                        "Explicit import title",
                        str(source_path),
                    ]
                )

            index_records = read_jsonl(codex_home / "session_index.jsonl")
            imported_files = list((codex_home / "sessions").rglob("*.jsonl"))
            rollout_records = read_jsonl(imported_files[0])
            self.assertEqual(result, 0)
            self.assertEqual(index_records[0]["thread_name"], "Explicit import title")
            self.assertEqual(index_records[0]["extra"], "preserved")
            self.assertEqual(rollout_records[1]["payload"]["thread_name"], "Explicit import title")

    def test_export_by_id_writes_readable_file_with_index_title_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            output_dir = root / "exports"
            session_id = "23232323-2323-2323-2323-232323232323"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Index title for export"}],
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Old rollout title",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Export body",
                        },
                    },
                ],
            )
            original_rollout = rollout_path.read_text(encoding="utf-8")

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "export",
                        "--codex-home",
                        str(codex_home),
                        session_id,
                        str(output_dir),
                    ]
                )

            output = buffer.getvalue()
            exported_path = output_dir / f"2026-04-30--Index-title-for-export--{session_id}.jsonl"
            exported_records = read_jsonl(exported_path)
            source_records = read_jsonl(rollout_path)
            self.assertEqual(result, 0)
            self.assertIn(f"Exported session: {session_id} - Index title for export", output)
            self.assertTrue(exported_path.exists())
            self.assertEqual(
                exported_records[0]["payload"]["thread_name"], "Index title for export"
            )
            self.assertEqual(source_records[0]["payload"]["thread_name"], "Old rollout title")
            self.assertEqual(rollout_path.read_text(encoding="utf-8"), original_rollout)

    def test_export_by_exact_title_to_explicit_file_copies_unchanged_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            output_path = root / "session.jsonl"
            session_id = "24242424-2424-2424-2424-242424242424"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Exact export title"}],
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Exact export title",
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "export",
                        "--codex-home",
                        str(codex_home),
                        "Exact export title",
                        str(output_path),
                    ]
                )

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("Rollout action: copy unchanged", output)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"), rollout_path.read_text(encoding="utf-8")
            )

    def test_export_dry_run_reports_plan_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            output_path = root / "session.jsonl"
            session_id = "25252525-2525-2525-2525-252525252525"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Dry export title"}],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(
                    [
                        "export",
                        "--dry-run",
                        "--codex-home",
                        str(codex_home),
                        session_id,
                        str(output_path),
                    ]
                )

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn(f"Session: {session_id} - Dry export title", output)
            self.assertIn(f"Output rollout: {output_path}", output)
            self.assertIn("Rollout action: copy with rollout title event update", output)
            self.assertFalse(output_path.exists())

    def test_export_refuses_existing_output_unless_force_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            output_path = root / "session.jsonl"
            output_path.write_text("existing", encoding="utf-8")
            session_id = "26262626-2626-2626-2626-262626262626"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Force export title"}],
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Force export title",
                        },
                    }
                ],
            )

            with self.assertRaises(SystemExit) as raised:
                main(["export", "--codex-home", str(codex_home), session_id, str(output_path)])

            self.assertIn("Output file already exists", str(raised.exception))

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "export",
                        "--force",
                        "--codex-home",
                        str(codex_home),
                        session_id,
                        str(output_path),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"), rollout_path.read_text(encoding="utf-8")
            )

    def test_export_by_id_without_index_uses_rollout_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            output_dir = root / "exports"
            session_id = "27272727-2727-2727-2727-272727272727"
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Rollout only title",
                        },
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(
                    [
                        "export",
                        "--codex-home",
                        str(codex_home),
                        session_id,
                        str(output_dir),
                    ]
                )

            exported_path = output_dir / f"2026-04-30--Rollout-only-title--{session_id}.jsonl"
            self.assertEqual(result, 0)
            self.assertEqual(
                exported_path.read_text(encoding="utf-8"),
                rollout_path.read_text(encoding="utf-8"),
            )

    def test_export_without_output_writes_readable_file_to_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            cwd = root / "cwd"
            cwd.mkdir()
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "31313131-3131-3131-3131-313131313131"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Default export output"}],
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Default export output",
                        },
                    }
                ],
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                with redirect_stdout(StringIO()):
                    result = main(["export", "--codex-home", str(codex_home), session_id])
            finally:
                os.chdir(previous_cwd)

            exported_path = cwd / f"2026-04-30--Default-export-output--{session_id}.jsonl"
            self.assertEqual(result, 0)
            self.assertEqual(
                exported_path.read_text(encoding="utf-8"), rollout_path.read_text(encoding="utf-8")
            )

    def test_export_to_non_existing_directory_path_creates_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            output_dir = root / "new-exports"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "32323232-3232-3232-3232-323232323232"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Create export directory"}],
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Create export directory",
                        },
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(
                    ["export", "--codex-home", str(codex_home), session_id, str(output_dir)]
                )

            exported_path = output_dir / f"2026-04-30--Create-export-directory--{session_id}.jsonl"
            self.assertEqual(result, 0)
            self.assertEqual(
                exported_path.read_text(encoding="utf-8"), rollout_path.read_text(encoding="utf-8")
            )

    def test_export_refuses_output_path_equal_to_source_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "33333333-3333-3333-3333-333333333333"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Source output refusal"}],
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Source output refusal",
                        },
                    }
                ],
            )

            with self.assertRaises(SystemExit) as raised:
                main(["export", "--codex-home", str(codex_home), session_id, str(rollout_path)])

            self.assertIn("Export output path is the source rollout file", str(raised.exception))

    def test_export_refuses_multiple_rollout_files_for_same_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            first_day = codex_home / "sessions" / "2026" / "04" / "30"
            second_day = codex_home / "sessions" / "2026" / "05" / "01"
            first_day.mkdir(parents=True)
            second_day.mkdir(parents=True)
            session_id = "34343434-3434-3434-3434-343434343434"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Duplicate rollout export"}],
            )
            for index, day in enumerate((first_day, second_day), start=1):
                write_jsonl(
                    day / f"rollout-2026-04-{29 + index:02d}T18-20-39-{session_id}.jsonl",
                    [
                        {
                            "timestamp": "2026-04-30T18:20:39Z",
                            "type": "session_meta",
                            "payload": {"id": session_id},
                        }
                    ],
                )

            with self.assertRaises(SystemExit) as raised:
                main(["export", "--codex-home", str(codex_home), session_id, str(root / "out")])

            self.assertIn("Multiple Codex session files found", str(raised.exception))

    def test_export_refuses_duplicate_exact_title_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex"
            codex_home.mkdir()
            (codex_home / "sessions").mkdir()
            write_jsonl(
                codex_home / "session_index.jsonl",
                [
                    {
                        "id": "35353535-3535-3535-3535-353535353535",
                        "thread_name": "Duplicate exact title",
                    },
                    {
                        "id": "36363636-3636-3636-3636-363636363636",
                        "thread_name": "Duplicate exact title",
                    },
                ],
            )

            with self.assertRaises(SystemExit) as raised:
                main(["export", "--codex-home", str(codex_home), "Duplicate exact title"])

            self.assertIn(
                "Multiple session_index.jsonl entries matched title", str(raised.exception)
            )

    def test_export_readable_filename_sanitizes_and_truncates_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / "codex"
            output_dir = root / "exports"
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            session_id = "37373737-3737-3737-3737-373737373737"
            long_title = "Need: punctuation / spaces? " + ("word " * 30)
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": long_title}],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "session_meta",
                        "payload": {"id": session_id},
                    }
                ],
            )

            with redirect_stdout(StringIO()):
                result = main(
                    ["export", "--codex-home", str(codex_home), session_id, str(output_dir)]
                )

            exported_files = list(output_dir.glob("*.jsonl"))
            self.assertEqual(result, 0)
            self.assertEqual(len(exported_files), 1)
            self.assertTrue(
                exported_files[0].name.startswith("2026-04-30--Need-punctuation-spaces-word")
            )
            self.assertTrue(exported_files[0].name.endswith(f"--{session_id}.jsonl"))
            title_part = (
                exported_files[0]
                .name.removeprefix("2026-04-30--")
                .removesuffix(f"--{session_id}.jsonl")
            )
            self.assertLessEqual(len(title_part), 80)

    def test_export_then_import_round_trips_title_and_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_home = root / "source"
            target_home = root / "target"
            export_dir = root / "exports"
            sessions_day = source_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)
            target_home.mkdir()
            session_id = "38383838-3838-3838-3838-383838383838"
            write_jsonl(
                source_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Round trip index title"}],
            )
            rollout_path = sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            write_jsonl(
                rollout_path,
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Old rollout title",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Round-trip body",
                        },
                    },
                ],
            )
            state_db = target_home / "state_5.sqlite"
            state_db.write_text("state", encoding="utf-8")

            with redirect_stdout(StringIO()):
                export_result = main(
                    ["export", "--codex-home", str(source_home), session_id, str(export_dir)]
                )
            exported_path = export_dir / f"2026-04-30--Round-trip-index-title--{session_id}.jsonl"
            with redirect_stdout(StringIO()):
                import_result = main(
                    ["import", "--codex-home", str(target_home), str(exported_path)]
                )

            target_rollouts = list((target_home / "sessions").rglob("*.jsonl"))
            target_records = read_jsonl(target_rollouts[0])
            index_records = read_jsonl(target_home / "session_index.jsonl")
            self.assertEqual(export_result, 0)
            self.assertEqual(import_result, 0)
            self.assertEqual(len(target_rollouts), 1)
            self.assertEqual(index_records[0]["thread_name"], "Round trip index title")
            self.assertEqual(target_records[0]["payload"]["thread_name"], "Round trip index title")
            self.assertEqual(target_records[1]["payload"]["content"], "Round-trip body")
            self.assertFalse(state_db.exists())
            state_backups = tuple(
                (target_home / "backups" / "codex-sessions").glob("*/state_5.sqlite")
            )
            self.assertEqual(len(state_backups), 1)
            self.assertEqual(state_backups[0].read_text(encoding="utf-8"), "state")

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

    def test_find_searches_indexed_session_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "39393939-3939-3939-3939-393939393939"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Explore user input options"}],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Discuss a different topic.",
                        },
                    }
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["find", "Explore user input", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn(f"{session_id} - Explore user input options", output)
            self.assertNotIn("User:", output)

    def test_find_searches_unindexed_rollout_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "40404040-4040-4040-4040-404040404040"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_name_updated",
                            "thread_id": session_id,
                            "thread_name": "Explore user input options",
                        },
                    },
                    {
                        "timestamp": "2026-04-30T18:21:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Discuss a different topic.",
                        },
                    },
                ],
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["find", "Explore user input", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn(f"{session_id} - Explore user input options", output)
            self.assertIn("NO ENTRY IN session_index.jsonl", output)
            self.assertNotIn("User:", output)

    def test_find_color_always_highlights_title_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "41414141-4141-4141-4141-414141414141"
            write_jsonl(
                codex_home / "session_index.jsonl",
                [{"id": session_id, "thread_name": "Explore user input options"}],
            )
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "Discuss a different topic.",
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
                        "Explore user input",
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("\x1b[1;91m", output)
            self.assertIn("Explore user input", output)

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
        with patch(
            "codex_sessions_converter.search_output.is_windows_pipe_stream",
            return_value=True,
        ):
            self.assertEqual(
                console_color_options("auto", StringIO(), git_bash_env),
                (True, False),
            )

    def test_color_auto_does_not_force_for_git_bash_disk_redirect(self) -> None:
        git_bash_env = {"TERM": "xterm-256color", "MSYSTEM": "MINGW64"}
        with patch(
            "codex_sessions_converter.search_output.is_windows_pipe_stream",
            return_value=False,
        ):
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
                "codex_sessions_converter.session_documents.iter_jsonl_objects",
                side_effect=AssertionError("cache should avoid reparsing rollout JSONL"),
            ):
                buffer = StringIO()
                with redirect_stdout(buffer):
                    second_result = main(["find", "needle", "--codex-home", str(codex_home)])

            self.assertEqual(second_result, 0)
            self.assertIn("cached needle", buffer.getvalue())

    def test_find_ignores_stale_search_cache_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_day = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_day.mkdir(parents=True)

            session_id = "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd"
            write_jsonl(
                sessions_day / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl",
                [
                    {
                        "timestamp": "2026-04-30T18:20:39Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "fresh needle",
                        },
                    }
                ],
            )
            cache_path = search_cache_path(codex_home)
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            "stale": {
                                "path": str((sessions_day / "missing.jsonl").resolve()),
                                "visible_lines": ["Codex: stale needle"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            buffer = StringIO()
            with redirect_stdout(buffer):
                result = main(["find", "needle", "--codex-home", str(codex_home)])

            output = buffer.getvalue()
            self.assertEqual(result, 0)
            self.assertIn("fresh needle", output)
            self.assertNotIn("stale needle", output)

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
