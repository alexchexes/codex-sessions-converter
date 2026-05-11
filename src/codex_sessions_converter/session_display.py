import re
from datetime import datetime, timezone

from codex_sessions_converter.search import match_spans
from codex_sessions_converter.session_files import SessionFile
from codex_sessions_converter.session_index import SessionIndexEntry, normalize_session_id

NO_ROLLOUT_FILE = "NO ROLLOUT FILE"
NO_SESSION_INDEX_ENTRY = "NO ENTRY IN session_index.jsonl"


def format_local_timestamp(value: datetime | None) -> str:
    if value is None:
        return "UNKNOWN"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def local_timezone_offset_label(value: datetime | None) -> str:
    converted = (value or datetime.now(timezone.utc)).astimezone()
    offset = converted.utcoffset()
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    absolute_minutes = abs(total_minutes)
    hours, minutes = divmod(absolute_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def format_session_timestamps(session_file: SessionFile) -> str:
    timezone_source = session_file.ended_at or session_file.started_at
    if session_file.started_at is not None and session_file.ended_at is not None:
        return (
            f"{format_local_timestamp(session_file.started_at)} - "
            f"{format_local_timestamp(session_file.ended_at)} "
            f"({local_timezone_offset_label(timezone_source)})"
        )
    if session_file.started_at is not None:
        return (
            f"{format_local_timestamp(session_file.started_at)} "
            f"({local_timezone_offset_label(timezone_source)})"
        )
    if session_file.ended_at is not None:
        return (
            f"{format_local_timestamp(session_file.ended_at)} "
            f"({local_timezone_offset_label(timezone_source)})"
        )
    return ""


def format_indexed_session_line(entry: SessionIndexEntry, session_file: SessionFile) -> str:
    parts = []
    timestamp_text = format_session_timestamps(session_file)
    if timestamp_text:
        parts.append(timestamp_text)
    parts.extend([entry.session_id, entry.thread_name])
    return " - ".join(parts)


def format_unindexed_session_line(session_file: SessionFile, inferred_title: str | None) -> str:
    if not inferred_title:
        return f"{session_file.relative_path} - {NO_SESSION_INDEX_ENTRY}"

    parts = []
    timestamp_text = format_session_timestamps(session_file)
    if timestamp_text:
        parts.append(timestamp_text)
    parts.append(session_file.session_id or session_file.relative_path)
    parts.append(inferred_title)
    parts.append(NO_SESSION_INDEX_ENTRY)
    return " - ".join(parts)


def session_info_for_search(
    session_file: SessionFile,
    entries_by_id: dict[str, SessionIndexEntry],
    inferred_title: str | None = None,
) -> str:
    if session_file.session_id:
        entry = entries_by_id.get(normalize_session_id(session_file.session_id))
        if entry:
            return format_indexed_session_line(entry, session_file)
    return format_unindexed_session_line(session_file, inferred_title)


def session_title_for_search(
    session_file: SessionFile,
    entries_by_id: dict[str, SessionIndexEntry],
    inferred_title: str | None = None,
) -> str | None:
    if session_file.session_id:
        entry = entries_by_id.get(normalize_session_id(session_file.session_id))
        if entry:
            return entry.thread_name
    return inferred_title


def session_info_title_match_spans(
    session_info: str,
    title: str | None,
    search_pattern: re.Pattern[str],
) -> tuple[tuple[int, int], ...]:
    if not title:
        return ()
    title_offset = session_info.rfind(title)
    if title_offset == -1:
        return ()
    return tuple(
        (title_offset + start, title_offset + end)
        for start, end in match_spans(title, search_pattern)
    )
