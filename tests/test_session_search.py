import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from codex_sessions_converter.search import SearchOptions
from codex_sessions_converter.session_documents import SearchDocument
from codex_sessions_converter.session_search import (
    build_search_document,
    render_search_line_groups,
    render_search_text,
    render_tool_call_search_lines,
    search_document_lines,
    search_sessions,
)


def search_options(**overrides: Any) -> SearchOptions:
    values: dict[str, Any] = {
        "pattern": "needle",
        "regex": False,
        "ignore_case": True,
        "line_width": 120,
        "max_lines_per_session": 5,
        "include_metadata": False,
        "include_tools": False,
        "color": "never",
        "redaction": "...",
    }
    values.update(overrides)
    return SearchOptions(**values)


class SessionSearchTests(unittest.TestCase):
    def test_render_search_text_flattens_embedded_json(self) -> None:
        self.assertEqual(
            render_search_text('{"command":"echo hi","items":["a","b"],"empty":null}'),
            "command: echo hi\nitems:\na\nb",
        )

    def test_render_search_line_groups_filters_visible_metadata_and_tools(self) -> None:
        self.assertEqual(
            render_search_line_groups(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                }
            ),
            [("visible", ["User: hello"])],
        )
        self.assertEqual(
            render_search_line_groups(
                {
                    "type": "session_meta",
                    "payload": {"cwd": "D:/repo", "git": {"branch": "main"}},
                }
            ),
            [
                (
                    "metadata",
                    ["Session metadata: cwd: D:/repo", "Session metadata: branch: main"],
                )
            ],
        )

    def test_search_document_lines_deduplicates_and_honors_options(self) -> None:
        document = SearchDocument(
            session_id="id",
            thread_name=None,
            started_at=None,
            ended_at=None,
            visible_lines=("User: needle", "User: needle"),
            metadata_lines=("Session metadata: cwd: needle",),
            tool_lines=("Tool call: shell_command: needle",),
        )

        self.assertEqual(search_document_lines(document, search_options()), ["User: needle"])
        self.assertEqual(
            search_document_lines(
                document,
                search_options(include_metadata=True, include_tools=True),
            ),
            [
                "User: needle",
                "Session metadata: cwd: needle",
                "Tool call: shell_command: needle",
            ],
        )

    def test_render_tool_call_search_lines_extracts_command_preview(self) -> None:
        lines = render_tool_call_search_lines(
            {
                "type": "function_call",
                "name": "shell_command",
                "arguments": json.dumps({"command": "echo needle"}),
            }
        )

        self.assertEqual(lines, ["Tool call: shell_command: echo needle"])

    def test_build_search_document_and_search_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir)
            sessions_dir = codex_home / "sessions" / "2026" / "04" / "30"
            sessions_dir.mkdir(parents=True)
            session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            rollout = sessions_dir / f"rollout-2026-04-30T18-20-39-{session_id}.jsonl"
            rollout.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
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
                                "content": "needle in message",
                            },
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            document = build_search_document(rollout, "...")
            results, warnings = search_sessions(codex_home, search_options())

        self.assertEqual(document.session_id, session_id)
        self.assertEqual(warnings, [])
        self.assertEqual(len(results), 1)
        self.assertIn(session_id, results[0].session_info)
        self.assertEqual(results[0].lines[0].text, "User: needle in message")


if __name__ == "__main__":
    unittest.main()
