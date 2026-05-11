from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from codex_sessions_converter.json_streams import iter_jsonl_objects
from codex_sessions_converter.session_index import SESSION_ID_RE
from codex_sessions_converter.timestamps import parse_timestamp


@dataclass(frozen=True)
class SessionFile:
    path: Path
    relative_path: str
    session_id: str | None
    started_at: datetime | None
    ended_at: datetime | None


def session_id_from_path(path: Path) -> str | None:
    match = SESSION_ID_RE.search(path.stem)
    if not match:
        return None
    return match.group(0)


def session_id_from_metadata(path: Path) -> str | None:
    try:
        for count, (_, record) in enumerate(iter_jsonl_objects(path), start=1):
            payload = record.get("payload")
            if record.get("type") == "session_meta" and isinstance(payload, dict):
                session_id = payload.get("id")
                if isinstance(session_id, str) and session_id:
                    return session_id
            if count >= 20:
                break
    except (OSError, ValueError):
        return None
    return None


def session_file_metadata(
    path: Path, *, include_ended_at: bool = False
) -> tuple[str | None, datetime | None, datetime | None]:
    session_id = session_id_from_path(path)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    try:
        for count, (_, record) in enumerate(iter_jsonl_objects(path), start=1):
            record_timestamp = parse_timestamp(record.get("timestamp"))
            if include_ended_at and record_timestamp is not None:
                ended_at = record_timestamp

            payload = record.get("payload")
            if started_at is None:
                started_at = record_timestamp
                if started_at is None and isinstance(payload, dict):
                    started_at = parse_timestamp(payload.get("timestamp"))
            if (
                session_id is None
                and record.get("type") == "session_meta"
                and isinstance(payload, dict)
            ):
                payload_id = payload.get("id")
                if isinstance(payload_id, str) and payload_id:
                    session_id = payload_id
            if not include_ended_at and session_id is not None and started_at is not None:
                break
            if count >= 20:
                if include_ended_at:
                    continue
                break
    except (OSError, ValueError):
        return session_id, started_at, ended_at
    return session_id, started_at, ended_at


def format_session_file_path(path: Path, sessions_dir: Path) -> str:
    try:
        relative_path = path.resolve().relative_to(sessions_dir.resolve())
    except ValueError:
        relative_path = path
    return relative_path.as_posix()


def discover_session_files(
    sessions_dir: Path, *, include_ended_at: bool = False
) -> list[SessionFile]:
    if not sessions_dir.exists():
        return []

    paths = discover_session_paths(sessions_dir)
    session_files = []
    for path in paths:
        session_id, started_at, ended_at = session_file_metadata(
            path, include_ended_at=include_ended_at
        )
        session_files.append(
            SessionFile(
                path=path,
                relative_path=format_session_file_path(path, sessions_dir),
                session_id=session_id,
                started_at=started_at,
                ended_at=ended_at,
            )
        )
    return session_files


def discover_session_paths(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.exists():
        return []
    return sorted(candidate for candidate in sessions_dir.rglob("*.jsonl") if candidate.is_file())
