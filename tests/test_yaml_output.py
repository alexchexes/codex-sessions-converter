import json
import tempfile
import unittest
from pathlib import Path

from codex_sessions_converter.yaml_output import (
    block_style_lines,
    convert_jsonl_to_yaml_stream,
    dump_yaml_lines,
    render_key,
    render_scalar,
)


class YamlOutputTests(unittest.TestCase):
    def test_render_key_quotes_non_simple_keys(self) -> None:
        self.assertEqual(render_key("simple_key"), "simple_key")
        self.assertEqual(render_key("not simple"), '"not simple"')

    def test_render_scalar_outputs_yaml_like_literals(self) -> None:
        self.assertEqual(render_scalar(None), "null")
        self.assertEqual(render_scalar(True), "true")
        self.assertEqual(render_scalar(False), "false")
        self.assertEqual(render_scalar(42), "42")
        self.assertEqual(render_scalar("hello"), '"hello"')

    def test_block_style_lines_selects_header_for_trailing_newlines(self) -> None:
        self.assertEqual(block_style_lines("a\nb"), ("|-", ["a", "b"]))
        self.assertEqual(block_style_lines("a\n"), ("|", ["a"]))
        self.assertEqual(block_style_lines("a\n\n"), ("|+", ["a", ""]))

    def test_dump_yaml_lines_handles_nested_values_and_multiline_strings(self) -> None:
        self.assertEqual(
            dump_yaml_lines({"message": "hello\nworld", "items": [1, {"a b": None}]}),
            [
                "message: |-",
                "  hello",
                "  world",
                "items:",
                "  - 1",
                "  -",
                '    "a b": null',
            ],
        )

    def test_convert_jsonl_to_yaml_stream_redacts_encrypted_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "rollout.jsonl"
            output_path = Path(tmpdir) / "rollout.yaml"
            input_path.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"encrypted_content": "secret"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            count = convert_jsonl_to_yaml_stream(input_path, output_path, "...")

            self.assertEqual(count, 1)
            output = output_path.read_text(encoding="utf-8")
            self.assertIn('encrypted_content: "..."', output)
            self.assertNotIn("secret", output)


if __name__ == "__main__":
    unittest.main()
