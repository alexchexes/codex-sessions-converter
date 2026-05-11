import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_sessions_converter.session_display import (
    NO_SESSION_INDEX_ENTRY,
    format_indexed_session_line,
    format_session_timestamps,
    format_unindexed_session_line,
    session_info_for_search,
    session_info_title_match_spans,
    session_title_for_search,
)
from codex_sessions_converter.session_files import SessionFile
from codex_sessions_converter.session_index import SessionIndexEntry


class SessionDisplayTests(unittest.TestCase):
    def test_format_session_timestamps_handles_full_and_partial_times(self) -> None:
        started_at = datetime(2026, 4, 30, 18, 20, tzinfo=timezone.utc)
        ended_at = datetime(2026, 4, 30, 19, 5, tzinfo=timezone.utc)

        self.assertIn(
            " - ",
            format_session_timestamps(
                SessionFile(
                    path=Path("rollout.jsonl"),
                    relative_path="rollout.jsonl",
                    session_id="id",
                    started_at=started_at,
                    ended_at=ended_at,
                )
            ),
        )
        self.assertNotIn(
            " - ",
            format_session_timestamps(
                SessionFile(
                    path=Path("rollout.jsonl"),
                    relative_path="rollout.jsonl",
                    session_id="id",
                    started_at=started_at,
                    ended_at=None,
                )
            ),
        )

    def test_format_indexed_and_unindexed_session_lines(self) -> None:
        session_file = SessionFile(
            path=Path("rollout.jsonl"),
            relative_path="2026/04/30/rollout.jsonl",
            session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            started_at=None,
            ended_at=None,
        )
        entry = SessionIndexEntry(
            session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            thread_name="Indexed title",
            updated_at=None,
        )

        self.assertEqual(
            format_indexed_session_line(entry, session_file),
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa - Indexed title",
        )
        self.assertEqual(
            format_unindexed_session_line(session_file, "Inferred title"),
            (f"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa - Inferred title - {NO_SESSION_INDEX_ENTRY}"),
        )
        self.assertEqual(
            format_unindexed_session_line(session_file, None),
            f"2026/04/30/rollout.jsonl - {NO_SESSION_INDEX_ENTRY}",
        )

    def test_session_info_and_title_for_search_prefer_index_entry(self) -> None:
        session_file = SessionFile(
            path=Path("rollout.jsonl"),
            relative_path="rollout.jsonl",
            session_id="AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            started_at=None,
            ended_at=None,
        )
        entry = SessionIndexEntry(
            session_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            thread_name="Indexed title",
            updated_at=None,
        )

        self.assertEqual(
            session_info_for_search(
                session_file,
                {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": entry},
                "Inferred title",
            ),
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa - Indexed title",
        )
        self.assertEqual(
            session_title_for_search(
                session_file,
                {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": entry},
                "Inferred title",
            ),
            "Indexed title",
        )

    def test_session_info_title_match_spans_offsets_title_matches(self) -> None:
        session_info = "2026-04-30 - aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa - Fix Search"

        spans = session_info_title_match_spans(
            session_info, "Fix Search", re.compile("search", re.I)
        )

        expected_start = session_info.index("Search")
        self.assertEqual(spans, ((expected_start, expected_start + len("Search")),))
        self.assertEqual(session_info_title_match_spans(session_info, None, re.compile("x")), ())
        self.assertEqual(
            session_info_title_match_spans(session_info, "Missing", re.compile("x")),
            (),
        )


if __name__ == "__main__":
    unittest.main()
