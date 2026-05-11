import unittest
from io import StringIO

from codex_sessions_converter.markdown_output import (
    is_metadata_record,
    metadata_title,
    render_metadata,
    render_raw_record,
    write_markdown_section,
)


class MarkdownOutputTests(unittest.TestCase):
    def test_metadata_title_uses_payload_type_when_present(self) -> None:
        self.assertEqual(
            metadata_title({"type": "event_msg", "payload": {"type": "token_count"}}),
            "Metadata: `event_msg.token_count`",
        )
        self.assertEqual(metadata_title({"type": "session_meta"}), "Metadata: `session_meta`")

    def test_render_metadata_flattens_record_into_markdown_table(self) -> None:
        rendered = render_metadata(
            {
                "timestamp": "2026-04-26T00:00:00Z",
                "type": "session_meta",
                "payload": {"cwd": "D:/repo"},
            }
        )

        self.assertIn("Timestamp: `2026-04-26T00:00:00Z`", rendered)
        self.assertIn("| payload.cwd | D:/repo |", rendered)

    def test_render_raw_record_includes_line_number_and_json_block(self) -> None:
        rendered = render_raw_record(3, {"type": "unknown", "payload": {"ok": True}})

        self.assertIn("Line: `3`", rendered)
        self.assertIn("```json", rendered)
        self.assertIn('"ok": true', rendered)

    def test_is_metadata_record_recognizes_known_metadata_records(self) -> None:
        self.assertTrue(is_metadata_record({"type": "session_meta"}))
        self.assertTrue(is_metadata_record({"type": "turn_context"}))
        self.assertTrue(
            is_metadata_record({"type": "event_msg", "payload": {"type": "token_count"}})
        )
        self.assertFalse(is_metadata_record({"type": "response_item"}))
        self.assertFalse(is_metadata_record({"type": "event_msg", "payload": {"type": "x"}}))

    def test_write_markdown_section_trims_body_end(self) -> None:
        output = StringIO()

        write_markdown_section(output, "Codex", "hello\n\n")

        self.assertEqual(output.getvalue(), "# Codex:\n\nhello\n\n---\n\n")


if __name__ == "__main__":
    unittest.main()
