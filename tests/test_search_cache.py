import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_sessions_converter.search_cache import (
    SEARCH_CACHE_VERSION,
    cached_search_document,
    prune_missing_search_cache_entries,
    read_search_cache,
    search_cache_entry,
    search_cache_key,
    search_cache_path,
    write_search_cache,
)
from codex_sessions_converter.session_documents import SearchDocument


class SearchCacheTests(unittest.TestCase):
    def test_search_cache_path_uses_codex_cache_directory(self) -> None:
        self.assertEqual(
            search_cache_path(Path("/tmp/codex")).as_posix(),
            "/tmp/codex/cache/codex-sessions/search-v3.json",
        )

    def test_read_search_cache_returns_entries_for_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "search.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "version": SEARCH_CACHE_VERSION,
                        "entries": {"key": {"path": "value"}},
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(read_search_cache(cache_path), {"key": {"path": "value"}})

    def test_read_search_cache_ignores_invalid_or_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "search.json"
            cache_path.write_text('{"version":1,"entries":{"stale":{}}}', encoding="utf-8")
            self.assertEqual(read_search_cache(cache_path), {})

            cache_path.write_text("not json", encoding="utf-8")
            self.assertEqual(read_search_cache(cache_path), {})

            self.assertEqual(read_search_cache(Path(tmpdir) / "missing.json"), {})

    def test_write_search_cache_round_trips_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "nested" / "search.json"
            entries = {"key": {"visible_lines": ["Codex: hello"]}}

            write_search_cache(cache_path, entries)

            self.assertEqual(read_search_cache(cache_path), entries)

    def test_search_cache_entry_and_cached_search_document_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout_path = Path(tmpdir) / "rollout.jsonl"
            rollout_path.write_text("content", encoding="utf-8")
            stat_result = rollout_path.stat()
            document = SearchDocument(
                session_id="11111111-1111-1111-1111-111111111111",
                thread_name="Cached title",
                started_at=datetime(2026, 4, 30, 18, 20, 39, tzinfo=timezone.utc),
                ended_at=datetime(2026, 4, 30, 18, 21, 39, tzinfo=timezone.utc),
                visible_lines=("Codex: cached line",),
                metadata_lines=("Session metadata: cwd: repo",),
                tool_lines=("Tool call: shell_command",),
            )

            entry = search_cache_entry(rollout_path, stat_result, document, "...")
            cached_document = cached_search_document(entry, rollout_path, stat_result, "...")

            self.assertEqual(cached_document, document)
            self.assertEqual(search_cache_key(rollout_path), search_cache_key(rollout_path))

    def test_cached_search_document_rejects_mismatched_cache_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rollout_path = Path(tmpdir) / "rollout.jsonl"
            rollout_path.write_text("content", encoding="utf-8")
            stat_result = rollout_path.stat()
            entry = {
                "path": str(rollout_path.resolve()),
                "size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "redaction": "...",
                "visible_lines": ["Codex: cached line"],
                "metadata_lines": [],
                "tool_lines": [],
            }

            self.assertIsNone(
                cached_search_document(
                    {**entry, "redaction": "[redacted]"}, rollout_path, stat_result, "..."
                )
            )
            self.assertIsNone(
                cached_search_document(
                    {**entry, "visible_lines": [1]}, rollout_path, stat_result, "..."
                )
            )

    def test_prune_missing_search_cache_entries_removes_invalid_and_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_path = Path(tmpdir) / "existing.jsonl"
            existing_path.write_text("content", encoding="utf-8")
            entries = {
                "existing": {"path": str(existing_path)},
                "missing": {"path": str(Path(tmpdir) / "missing.jsonl")},
                "invalid": [],
            }

            removed = prune_missing_search_cache_entries(entries)

            self.assertTrue(removed)
            self.assertEqual(entries, {"existing": {"path": str(existing_path)}})


if __name__ == "__main__":
    unittest.main()
