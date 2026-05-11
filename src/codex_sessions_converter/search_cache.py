import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from codex_sessions_converter.session_documents import SearchDocument
from codex_sessions_converter.timestamps import parse_timestamp

SEARCH_CACHE_VERSION = 3
SEARCH_CACHE_RELATIVE_PATH = Path("cache") / "codex-sessions" / "search-v3.json"


def search_cache_path(codex_home: Path) -> Path:
    return codex_home / SEARCH_CACHE_RELATIVE_PATH


def search_cache_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def read_search_cache(cache_path: Path) -> dict[str, Any]:
    try:
        raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(raw_cache, dict):
        return {}
    if raw_cache.get("version") != SEARCH_CACHE_VERSION:
        return {}
    entries = raw_cache.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def write_search_cache(cache_path: Path, entries: Mapping[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    cache_data = {
        "version": SEARCH_CACHE_VERSION,
        "entries": entries,
    }
    temp_path.write_text(
        json.dumps(cache_data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temp_path.replace(cache_path)


def cached_search_document(
    entry: Any, path: Path, stat_result: os.stat_result, redaction: str
) -> SearchDocument | None:
    if not isinstance(entry, dict):
        return None
    if entry.get("path") != str(path.resolve()):
        return None
    if entry.get("size") != stat_result.st_size:
        return None
    if entry.get("mtime_ns") != stat_result.st_mtime_ns:
        return None
    if entry.get("redaction") != redaction:
        return None

    visible_lines = string_tuple(entry.get("visible_lines"))
    metadata_lines = string_tuple(entry.get("metadata_lines"))
    tool_lines = string_tuple(entry.get("tool_lines"))
    if visible_lines is None or metadata_lines is None or tool_lines is None:
        return None

    session_id = entry.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return None
    thread_name = entry.get("thread_name")
    if thread_name is not None and not isinstance(thread_name, str):
        return None

    return SearchDocument(
        session_id=session_id,
        thread_name=thread_name,
        started_at=parse_timestamp(entry.get("started_at")),
        ended_at=parse_timestamp(entry.get("ended_at")),
        visible_lines=visible_lines,
        metadata_lines=metadata_lines,
        tool_lines=tool_lines,
    )


def string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def search_cache_entry(
    path: Path, stat_result: os.stat_result, document: SearchDocument, redaction: str
) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
        "redaction": redaction,
        "session_id": document.session_id,
        "thread_name": document.thread_name,
        "started_at": document.started_at.isoformat() if document.started_at else None,
        "ended_at": document.ended_at.isoformat() if document.ended_at else None,
        "visible_lines": list(document.visible_lines),
        "metadata_lines": list(document.metadata_lines),
        "tool_lines": list(document.tool_lines),
    }


def prune_missing_search_cache_entries(entries: dict[str, Any]) -> bool:
    removed_any = False
    for key, entry in list(entries.items()):
        path_text = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path_text, str):
            del entries[key]
            removed_any = True
            continue
        if not Path(path_text).exists():
            del entries[key]
            removed_any = True
    return removed_any
