import argparse
import base64
import binascii
import errno
import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

from codex_sessions_converter.codex_state import (
    CodexStateError,
    StateCacheBackup,
    backup_dir_for,
    backup_file,
    backup_label,
    backup_session_index,
    remove_backup_dir_if_empty,
    reset_codex_state_cache,
    restore_file_backup,
    restore_session_index_backup,
)
from codex_sessions_converter.json_streams import iter_jsonl_objects
from codex_sessions_converter.search import (
    SearchOptions,
    SearchResult,
    compile_search_pattern,
    match_spans,
    search_matching_lines,
)
from codex_sessions_converter.search_cache import (
    cached_search_document,
    prune_missing_search_cache_entries,
    read_search_cache,
    search_cache_entry,
    search_cache_key,
    search_cache_path,
    write_search_cache,
)
from codex_sessions_converter.search_output import encode_for_output, render_search_results
from codex_sessions_converter.session_documents import (
    SearchDocument,
    infer_search_document_title,
    inferred_thread_name,
    sanitize,
)
from codex_sessions_converter.session_documents import (
    build_search_document as build_session_document,
)
from codex_sessions_converter.session_files import (
    SessionFile,
    discover_session_files,
    discover_session_paths,
    format_session_file_path,
    session_id_from_path,
)
from codex_sessions_converter.session_index import (
    SessionIndexEntry,
    append_session_index_records,
    format_session_index_timestamp,
    is_session_id,
    normalize_session_id,
    read_session_index,
    resolve_session_index_record,
    session_index_record_id,
    session_index_record_thread_name,
    session_index_records,
    write_session_index_records,
)
from codex_sessions_converter.transfer import (
    ExportSessionPlan,
    ExportSessionResult,
    ImportSessionPlan,
    ImportSessionResult,
    export_title_slug,
    file_fingerprint,
    format_fingerprint,
    read_rollout_records,
    renamed_rollout_records,
    resolve_export_output_path,
    rollout_filename_date,
    write_rollout_records,
)
from codex_sessions_converter.yaml_output import convert_jsonl_to_yaml_stream

__version__ = "0.1.0"


NO_ROLLOUT_FILE = "NO ROLLOUT FILE"
NO_SESSION_INDEX_ENTRY = "NO ENTRY IN session_index.jsonl"
MARKDOWN_FEATURES = {"tools", "metadata", "raw"}
MARKDOWN_TOOL_MODES = {"auto", "none", "names", "smart", "preview", "full"}
MARKDOWN_IMAGE_MODES = {"truncate", "extract", "inline"}
DEFAULT_TOOL_PREVIEW_CHARS = 700
DATA_IMAGE_PREFIX_CHARS = 24
DATA_IMAGE_URL_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)
IMAGE_EXTENSION_BY_MIME_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}
MARKDOWN_PRESETS = {
    "dialogue": set(),
    "minimal": set(),
    "default": {"tools"},
    "tools": {"tools"},
    "metadata": {"tools", "metadata"},
    "full": {"tools", "metadata", "raw"},
}
DEFAULT_CLI_PROG = "codex-sessions"
CLI_PROG_ALIASES = {DEFAULT_CLI_PROG, "codex-sessions-converter"}
MARKDOWN_INCLUDE_ALIASES = {
    "all": "all",
    "none": "none",
    "tool": "tools",
    "tools": "tools",
    "tool-call": "tools",
    "tool-calls": "tools",
    "tool_call": "tools",
    "tool_calls": "tools",
    "meta": "metadata",
    "metadata": "metadata",
    "raw": "raw",
    "unhandled": "raw",
}


@dataclass(frozen=True)
class MarkdownOptions:
    tool_mode: str
    tool_preview_chars: int
    include_metadata: bool
    include_raw: bool
    redaction: str
    image_mode: str = "truncate"


@dataclass(frozen=True)
class ConversionInput:
    path: Path
    output_stem: str | None


@dataclass(frozen=True)
class RepairIndexCandidate:
    session_id: str
    thread_name: str
    updated_at: datetime | None
    relative_path: str


@dataclass(frozen=True)
class RepairIndexResult:
    candidates: tuple[RepairIndexCandidate, ...]
    warnings: tuple[str, ...]
    skipped_without_id: int
    session_index_backup_path: Path | None
    state_cache_backups: tuple[StateCacheBackup, ...]


@dataclass(frozen=True)
class RenameSessionResult:
    session_id: str
    old_thread_name: str
    new_thread_name: str
    index_changed: bool
    rollout_changed: bool
    rollout_path: Path | None
    rollout_backup_path: Path | None
    rollout_thread_name: str | None
    changed: bool
    session_index_backup_path: Path | None
    state_cache_backups: tuple[StateCacheBackup, ...]


class CliError(Exception):
    pass


def cli_prog_from_argv0(argv0: str | None = None) -> str:
    stem = Path(sys.argv[0] if argv0 is None else argv0).stem
    if stem in CLI_PROG_ALIASES:
        return stem
    return DEFAULT_CLI_PROG


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def parse_list_args(
    argv: Sequence[str] | None = None, prog: str = DEFAULT_CLI_PROG
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{prog} list",
        description=(
            "List Codex sessions and cross-check session_index.jsonl against rollout JSONL files."
        ),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        help="Path to session_index.jsonl. Defaults to <codex-home>/session_index.jsonl.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Path to Codex sessions directory. Defaults to <codex-home>/sessions.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the extracted session metadata cache.",
    )
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Ignore existing cached session metadata and rewrite cache entries.",
    )
    return parser.parse_args(argv)


def parse_search_args(
    command: str, argv: Sequence[str] | None = None, prog: str = DEFAULT_CLI_PROG
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{prog} {command}",
        description="Search Codex session rollout JSONL files.",
    )
    parser.add_argument("pattern", help="Text or regex pattern to search for.")
    parser.add_argument(
        "-i",
        "--ignore-case",
        action="store_true",
        help="Match case-insensitively.",
    )
    parser.add_argument(
        "-r",
        "-E",
        "--regex",
        action="store_true",
        help="Treat the pattern as a Python regular expression.",
    )
    parser.add_argument(
        "--line-width",
        type=int,
        default=160,
        metavar="N",
        help="Maximum visible characters per matching line. Default: %(default)s.",
    )
    parser.add_argument(
        "-m",
        "--max-lines-per-session",
        type=int,
        default=5,
        metavar="N",
        help=(
            "Maximum matching lines to show per session. Use 0 for no limit. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="Highlight matches with terminal colors. Default: auto.",
    )
    parser.add_argument(
        "--metadata",
        action="store_true",
        help="Also search compact session metadata such as cwd, branch, and repository URL.",
    )
    parser.add_argument(
        "--tools",
        action="store_true",
        help="Also search concise tool call previews such as shell commands.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Search visible messages, compact metadata, and concise tool call previews.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        help="Path to session_index.jsonl. Defaults to <codex-home>/session_index.jsonl.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Path to Codex sessions directory. Defaults to <codex-home>/sessions.",
    )
    parser.add_argument(
        "--redact-encrypted",
        default="...",
        help="Replacement text for any encrypted_content field.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the extracted search text cache.",
    )
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Ignore existing cached search text and rewrite cache entries.",
    )
    return parser.parse_args(argv)


def parse_repair_index_args(
    argv: Sequence[str] | None = None, prog: str = DEFAULT_CLI_PROG
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{prog} repair-index",
        description="Repair missing session_index.jsonl entries for rollout JSONL files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show missing session_index.jsonl entries without modifying Codex state.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        help="Path to session_index.jsonl. Defaults to <codex-home>/session_index.jsonl.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Path to Codex sessions directory. Defaults to <codex-home>/sessions.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the extracted session metadata cache.",
    )
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Ignore existing cached session metadata and rewrite cache entries.",
    )
    return parser.parse_args(argv)


def parse_rename_args(
    argv: Sequence[str] | None = None, prog: str = DEFAULT_CLI_PROG
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{prog} rename",
        description="Rename a session_index.jsonl entry and reset Codex state cache.",
    )
    parser.add_argument("target", help="Session ID or exact current session title.")
    parser.add_argument("name", nargs="+", help="New session title.")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        help="Path to session_index.jsonl. Defaults to <codex-home>/session_index.jsonl.",
    )
    return parser.parse_args(argv)


def parse_import_args(
    argv: Sequence[str] | None = None, prog: str = DEFAULT_CLI_PROG
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{prog} import",
        description="Import a bare Codex rollout JSONL file into Codex home.",
    )
    parser.add_argument("input", type=Path, help="Path to a rollout JSONL file.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the import plan without modifying Codex state.",
    )
    parser.add_argument(
        "--name",
        "--rename",
        dest="name",
        help="Title to use for the imported session.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        help="Path to session_index.jsonl. Defaults to <codex-home>/session_index.jsonl.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Path to Codex sessions directory. Defaults to <codex-home>/sessions.",
    )
    return parser.parse_args(argv)


def parse_export_args(
    argv: Sequence[str] | None = None, prog: str = DEFAULT_CLI_PROG
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"{prog} export",
        description="Export one Codex session as a transferable rollout JSONL file.",
    )
    parser.add_argument("target", help="Session ID or exact session title to export.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help=(
            "Output .jsonl path or directory. Defaults to a readable file in the current directory."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the export plan without writing anything.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        help="Path to session_index.jsonl. Defaults to <codex-home>/session_index.jsonl.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Path to Codex sessions directory. Defaults to <codex-home>/sessions.",
    )
    return parser.parse_args(argv)


def parse_args(
    argv: Sequence[str] | None = None, prog: str = DEFAULT_CLI_PROG
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Convert Codex session rollout JSONL files to YAML or Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            "  list       list sessions and cross-check session_index.jsonl with rollout files\n\n"
            "  find       search sessions under Codex home\n"
            "  grep       alias for find\n\n"
            "  repair-index\n"
            "             inspect missing session_index.jsonl entries\n\n"
            "  rename     rename a session_index.jsonl entry\n\n"
            "  import     import a bare rollout JSONL file\n\n"
            "  export     export one session as a rollout JSONL file\n\n"
            "Markdown include presets:\n"
            "  dialogue   visible user/Codex messages, reasoning, progress messages\n"
            "  default    dialogue plus tool calls and tool outputs\n"
            "  metadata   default plus metadata tables such as turn_context/token_count\n"
            "  full       metadata plus raw blocks for unhandled records\n\n"
            "Markdown tool detail modes:\n"
            "  auto       smart when tools are included by --md-include, otherwise none\n"
            "  none       omit tool call/output sections\n"
            "  names      show only tool names and call IDs\n"
            "  smart      show useful previews for known tool calls, otherwise names\n"
            "  preview    show names plus truncated arguments/outputs\n"
            "  full       show full arguments/outputs\n\n"
            "Markdown image modes:\n"
            "  truncate   replace base64 data images with compact placeholders\n"
            "  extract    write base64 data images next to the Markdown and link them\n"
            "  inline     keep base64 data images inline\n\n"
            "The --md-include value can also use modifiers, for example:\n"
            "  default,-tools\n"
            "  dialogue,+metadata\n"
            "  full,-raw\n\n"
            "Explicit --md-tools values override the tools setting from --md-include.\n"
        ),
    )
    parser.add_argument("input", type=Path, help="Path to the source JSONL file.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help=("Path to the output file or directory. Defaults under <codex-home>/tmp."),
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help=(
            "Codex home directory for session ID lookup and default output. "
            "Defaults to CODEX_HOME or ~/.codex."
        ),
    )
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument(
        "--format",
        choices=("yaml", "md", "markdown"),
        help="Output format. Defaults to Markdown for .md/.markdown output paths, otherwise YAML.",
    )
    format_group.add_argument(
        "--md",
        action="store_true",
        help="Write Markdown output without specifying an .md output path.",
    )
    format_group.add_argument(
        "--yaml",
        action="store_true",
        help="Write YAML output explicitly.",
    )
    parser.add_argument(
        "--md-include",
        default="default",
        metavar="SPEC",
        help="Markdown preset/modifiers controlling optional content. Default: default.",
    )
    parser.add_argument(
        "--md-tools",
        choices=tuple(sorted(MARKDOWN_TOOL_MODES)),
        default="auto",
        help="Markdown tool detail mode. Default: auto.",
    )
    parser.add_argument(
        "--md-tool-preview-chars",
        type=int,
        default=DEFAULT_TOOL_PREVIEW_CHARS,
        metavar="N",
        help=(
            "Maximum characters per tool argument/output preview when "
            "--md-tools=preview. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--md-images",
        choices=tuple(sorted(MARKDOWN_IMAGE_MODES)),
        default="truncate",
        help="Markdown handling for base64 data images. Default: %(default)s.",
    )
    parser.add_argument(
        "--redact-encrypted",
        default="...",
        help="Replacement text for any encrypted_content field.",
    )
    return parser.parse_args(argv)


def normalize_output_format(output_format: str | None) -> str | None:
    if output_format == "markdown":
        return "md"
    return output_format


def infer_output_format(args: argparse.Namespace) -> str:
    if args.md:
        return "md"
    if args.yaml:
        return "yaml"
    explicit_format = normalize_output_format(args.format)
    if explicit_format:
        return explicit_format
    if args.output and args.output.suffix.lower() in {".md", ".markdown"}:
        return "md"
    return "yaml"


def output_filename(input_path: Path, output_format: str = "yaml", stem: str | None = None) -> str:
    suffix = ".md" if output_format == "md" else ".yaml"
    if stem:
        return f"{stem}{suffix}"
    if input_path.suffix.lower() == ".jsonl":
        return input_path.with_suffix(suffix).name
    return input_path.with_suffix(input_path.suffix + suffix).name


def default_output_path(
    input_path: Path,
    codex_home: Path,
    output_format: str = "yaml",
    stem: str | None = None,
) -> Path:
    output_name = output_filename(input_path, output_format, stem)
    try:
        relative_input = input_path.resolve().relative_to(codex_home.resolve())
    except ValueError:
        return codex_home / "tmp" / output_name
    return (codex_home / "tmp" / relative_input).with_name(output_name)


def resolve_output_path(
    output_arg: Path | None,
    input_path: Path,
    codex_home: Path,
    output_format: str,
    stem: str | None = None,
) -> Path:
    if output_arg is None:
        return default_output_path(input_path, codex_home, output_format, stem).resolve()

    expanded_output = output_arg.expanduser()
    if expanded_output.exists() and expanded_output.is_dir():
        return (expanded_output / output_filename(input_path, output_format, stem)).resolve()
    return expanded_output.resolve()


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


def resolve_session_id(session_id: str, codex_home: Path) -> Path:
    sessions_dir = codex_home / "sessions"
    normalized_id = normalize_session_id(session_id)
    matches = [
        session_file.path
        for session_file in discover_session_files(sessions_dir)
        if (
            session_file.session_id
            and normalize_session_id(session_file.session_id) == normalized_id
        )
    ]
    if not matches:
        raise CliError(f"No Codex session found for ID: {session_id}")
    if len(matches) > 1:
        rendered_matches = ", ".join(
            format_session_file_path(path, sessions_dir) for path in matches
        )
        raise CliError(
            f"Multiple Codex session files found for ID {session_id}: {rendered_matches}"
        )
    return matches[0].resolve()


def resolve_conversion_input(raw_input: Path, codex_home: Path) -> ConversionInput:
    input_text = str(raw_input)
    if is_session_id(input_text):
        return ConversionInput(
            path=resolve_session_id(input_text, codex_home),
            output_stem=normalize_session_id(input_text),
        )

    expanded_input = raw_input.expanduser()
    if not expanded_input.exists():
        raise CliError(f"Input file not found: {raw_input}")
    if not expanded_input.is_file():
        raise CliError(f"Input path is not a file: {raw_input}")
    return ConversionInput(path=expanded_input.resolve(), output_stem=None)


def list_session_lines(
    codex_home: Path,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    use_cache: bool = True,
    rebuild_cache: bool = False,
) -> list[str]:
    lines, _ = list_session_lines_with_warnings(
        codex_home=codex_home,
        session_index_path=session_index_path,
        sessions_dir=sessions_dir,
        use_cache=use_cache,
        rebuild_cache=rebuild_cache,
    )
    return lines


def list_session_lines_with_warnings(
    codex_home: Path,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    use_cache: bool = True,
    rebuild_cache: bool = False,
) -> tuple[list[str], list[str]]:
    index_path = session_index_path or codex_home / "session_index.jsonl"
    resolved_sessions_dir = sessions_dir or codex_home / "sessions"

    index_entries = read_session_index(index_path)
    documents, warnings = load_search_documents(
        codex_home=codex_home,
        sessions_dir=resolved_sessions_dir,
        redaction="...",
        use_cache=use_cache,
        rebuild_cache=rebuild_cache,
    )
    session_files_with_titles = [
        (
            SessionFile(
                path=session_path,
                relative_path=format_session_file_path(session_path, resolved_sessions_dir),
                session_id=document.session_id,
                started_at=document.started_at,
                ended_at=document.ended_at,
            ),
            infer_search_document_title(document),
        )
        for session_path, document in documents
    ]
    session_files_by_id: dict[str, list[SessionFile]] = {}
    for session_file, _inferred_title in session_files_with_titles:
        if session_file.session_id:
            normalized_id = normalize_session_id(session_file.session_id)
            session_files_by_id.setdefault(normalized_id, []).append(session_file)

    indexed_ids = {normalize_session_id(entry.session_id) for entry in index_entries}
    lines = []
    for entry in index_entries:
        matching_files = session_files_by_id.get(normalize_session_id(entry.session_id), [])
        if not matching_files:
            lines.append(f"{entry.session_id} - {entry.thread_name} - {NO_ROLLOUT_FILE}")
            continue
        session_file = matching_files[0]
        lines.append(format_indexed_session_line(entry, session_file))

    for session_file, inferred_title in session_files_with_titles:
        if session_file.session_id and normalize_session_id(session_file.session_id) in indexed_ids:
            continue
        lines.append(format_unindexed_session_line(session_file, inferred_title))

    return lines, warnings


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


def parse_embedded_json(value: str) -> Any:
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def render_search_text(value: Any) -> str:
    if isinstance(value, str):
        parsed = parse_embedded_json(value)
        if parsed is not value:
            return render_search_text(parsed)
        return str(value)

    if isinstance(value, dict):
        lines = []
        for key, inner in value.items():
            rendered = render_search_text(inner)
            if not rendered:
                continue
            if "\n" in rendered:
                lines.extend([f"{key}:", rendered])
            else:
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)

    if isinstance(value, list):
        return "\n".join(rendered for item in value if (rendered := render_search_text(item)))

    if value is None:
        return ""
    return str(value)


def session_search_text(input_path: Path, options: SearchOptions) -> str:
    return "\n".join(session_search_lines(input_path, options))


def session_search_lines(input_path: Path, options: SearchOptions) -> list[str]:
    document = build_search_document(input_path, options.redaction)
    return search_document_lines(document, options)


def search_document_lines(document: SearchDocument, options: SearchOptions) -> list[str]:
    lines: list[str] = []
    seen_lines = set()
    line_groups = [document.visible_lines]
    if options.include_metadata:
        line_groups.append(document.metadata_lines)
    if options.include_tools:
        line_groups.append(document.tool_lines)

    for group in line_groups:
        for line in group:
            if line and line not in seen_lines:
                seen_lines.add(line)
                lines.append(line)
    return lines


def build_search_document(input_path: Path, redaction: str) -> SearchDocument:
    return build_session_document(
        input_path,
        redaction,
        session_id_from_path=session_id_from_path,
        render_line_groups=render_search_line_groups,
    )


def render_search_line_groups(record: dict[str, Any]) -> list[tuple[str, list[str]]]:
    record_type = record.get("type")
    payload = record.get("payload")

    if record_type == "session_meta" and isinstance(payload, dict):
        return [("metadata", render_session_metadata_search_lines(payload))]

    if record_type == "response_item" and isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "message":
            role = payload.get("role")
            text = content_to_text(payload.get("content"))
            if role == "assistant":
                return [("visible", render_labeled_search_lines("Codex", text))]
            if role == "user":
                searchable_text = searchable_user_message_text(text)
                if searchable_text:
                    return [("visible", render_labeled_search_lines("User", searchable_text))]
            return []
        if payload_type == "reasoning":
            return []
        if payload_type in {"function_call", "tool_search_call", "custom_tool_call"}:
            return [("tools", render_tool_call_search_lines(payload))]
        return []

    if record_type == "event_msg" and isinstance(payload, dict):
        payload_type = payload.get("type")
        if payload_type == "user_message":
            searchable_text = searchable_user_message_text(str(payload.get("message", "")))
            if searchable_text:
                return [("visible", render_labeled_search_lines("User", searchable_text))]
            return []
        if payload_type == "agent_message":
            return [("visible", render_labeled_search_lines("Codex", payload.get("message", "")))]

    return []


def render_search_lines(record: dict[str, Any], options: SearchOptions) -> list[str]:
    lines = []
    for group, group_lines in render_search_line_groups(record):
        if group == "metadata" and not options.include_metadata:
            continue
        if group == "tools" and not options.include_tools:
            continue
        lines.extend(group_lines)
    return lines


def render_labeled_search_lines(label: str, text: str) -> list[str]:
    normalized_text = text.strip()
    if not normalized_text:
        return []
    return [f"{label}: {line.strip()}" for line in normalized_text.splitlines() if line.strip()]


def import_target_date(source_path: Path, document: SearchDocument) -> tuple[str, str, str]:
    filename_date = rollout_filename_date(source_path)
    if filename_date is not None:
        return filename_date
    if document.started_at is None:
        raise CliError(
            f"Cannot infer session date from rollout filename or timestamps: {source_path}"
        )
    local_started_at = document.started_at.astimezone()
    return (
        f"{local_started_at.year:04d}",
        f"{local_started_at.month:02d}",
        f"{local_started_at.day:02d}",
    )


def import_target_filename(source_path: Path, document: SearchDocument) -> str:
    if document.session_id is None:
        raise CliError(f"Cannot infer session id from rollout: {source_path}")
    if source_path.name.startswith("rollout-") and session_id_from_path(source_path):
        return source_path.name
    if document.started_at is None:
        raise CliError(
            f"Cannot infer rollout filename from source name or timestamps: {source_path}"
        )
    local_started_at = document.started_at.astimezone()
    timestamp = local_started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return f"rollout-{timestamp}-{document.session_id}.jsonl"


def import_target_path(source_path: Path, sessions_dir: Path, document: SearchDocument) -> Path:
    year, month, day = import_target_date(source_path, document)
    return sessions_dir / year / month / day / import_target_filename(source_path, document)


def existing_session_files_for_id(session_id: str, sessions_dir: Path) -> list[SessionFile]:
    normalized_id = normalize_session_id(session_id)
    return [
        session_file
        for session_file in discover_session_files(sessions_dir)
        if session_file.session_id
        and normalize_session_id(session_file.session_id) == normalized_id
    ]


def existing_index_record_for_id(
    records: Sequence[Any], session_id: str
) -> tuple[int, dict[str, Any]] | None:
    normalized_id = normalize_session_id(session_id)
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        record_id = session_index_record_id(record)
        if record_id and normalize_session_id(record_id) == normalized_id:
            return index, record
    return None


def first_existing_rollout_for_import(
    existing_files: Sequence[SessionFile], target_path: Path
) -> Path | None:
    if target_path.exists():
        return target_path
    return existing_files[0].path if existing_files else None


def prepare_import_rollout_records(
    source_path: Path, session_id: str, thread_name: str
) -> tuple[list[dict[str, Any]], bool]:
    records = read_rollout_records(source_path)
    updated_records, _, changed = renamed_rollout_records(records, session_id, thread_name)
    return updated_records, changed


def plan_bare_rollout_import(
    source_path: Path,
    codex_home: Path,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    name: str | None = None,
) -> ImportSessionPlan:
    expanded_source_path = source_path.expanduser().resolve()
    if not expanded_source_path.exists():
        raise CliError(f"Input file not found: {source_path}")
    if not expanded_source_path.is_file():
        raise CliError(f"Input path is not a file: {source_path}")

    resolved_sessions_dir = sessions_dir or codex_home / "sessions"
    document = build_search_document(expanded_source_path, "...")
    if document.session_id is None:
        raise CliError(f"Cannot infer session id from rollout: {source_path}")

    target_path = import_target_path(expanded_source_path, resolved_sessions_dir, document)
    source_fingerprint = file_fingerprint(expanded_source_path)
    existing_files = existing_session_files_for_id(document.session_id, resolved_sessions_dir)
    existing_rollout_path = first_existing_rollout_for_import(existing_files, target_path)
    existing_rollout_fingerprint = (
        file_fingerprint(existing_rollout_path) if existing_rollout_path is not None else None
    )

    if existing_rollout_path is not None:
        if source_fingerprint == existing_rollout_fingerprint:
            raise CliError(
                "Session already imported with identical rollout file: "
                f"{existing_rollout_path} ({format_fingerprint(source_fingerprint)})"
            )
        existing_fingerprint = (
            format_fingerprint(existing_rollout_fingerprint)
            if existing_rollout_fingerprint
            else "UNKNOWN"
        )
        raise CliError(
            "Session already imported, but rollout file differs. "
            f"Existing: {existing_rollout_path} "
            f"({existing_fingerprint}); "
            f"import: {expanded_source_path} ({format_fingerprint(source_fingerprint)})."
        )

    index_path = session_index_path or codex_home / "session_index.jsonl"
    index_records = session_index_records(index_path) if index_path.exists() else []
    existing_index_match = existing_index_record_for_id(index_records, document.session_id)
    existing_index_thread_name = (
        session_index_record_thread_name(existing_index_match[1])
        if existing_index_match is not None
        else None
    )

    if name is not None:
        normalized_name = name.strip()
    elif existing_index_thread_name:
        normalized_name = existing_index_thread_name
    else:
        normalized_name = inferred_thread_name(document)
    if not normalized_name:
        raise CliError("Imported session title must not be empty.")

    if existing_index_match is None:
        index_action = "add"
    elif existing_index_thread_name != normalized_name and name is not None:
        index_action = "update"
    else:
        index_action = "keep"

    _, rollout_will_be_rewritten = prepare_import_rollout_records(
        expanded_source_path, document.session_id, normalized_name
    )

    return ImportSessionPlan(
        source_path=expanded_source_path,
        target_path=target_path,
        session_index_path=index_path,
        session_id=document.session_id,
        thread_name=normalized_name,
        started_at=document.started_at,
        ended_at=document.ended_at,
        index_action=index_action,
        existing_index_thread_name=existing_index_thread_name,
        source_fingerprint=source_fingerprint,
        rollout_will_be_rewritten=rollout_will_be_rewritten,
    )


def session_index_record_for_import_plan(plan: ImportSessionPlan) -> dict[str, str]:
    return {
        "id": plan.session_id,
        "thread_name": plan.thread_name,
        "updated_at": format_session_index_timestamp(plan.ended_at or plan.started_at),
    }


def session_index_records_for_import(plan: ImportSessionPlan) -> list[Any]:
    records = (
        session_index_records(plan.session_index_path) if plan.session_index_path.exists() else []
    )
    existing_index_match = existing_index_record_for_id(records, plan.session_id)
    if plan.index_action == "add":
        if existing_index_match is not None:
            return records
        return [*records, session_index_record_for_import_plan(plan)]
    if plan.index_action == "update":
        if existing_index_match is None:
            raise CliError(f"No session_index.jsonl entry found for ID: {plan.session_id}")
        record_index, record = existing_index_match
        updated_records = list(records)
        updated_record = dict(record)
        updated_record["thread_name"] = plan.thread_name
        updated_records[record_index] = updated_record
        return updated_records
    return records


def copy_or_rewrite_import_rollout(plan: ImportSessionPlan) -> None:
    plan.target_path.parent.mkdir(parents=True, exist_ok=True)
    if plan.rollout_will_be_rewritten:
        records, _ = prepare_import_rollout_records(
            plan.source_path, plan.session_id, plan.thread_name
        )
        write_rollout_records(plan.target_path, records)
        return
    shutil.copy2(plan.source_path, plan.target_path)


def import_bare_rollout(
    source_path: Path,
    codex_home: Path,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    name: str | None = None,
) -> ImportSessionResult:
    plan = plan_bare_rollout_import(
        source_path=source_path,
        codex_home=codex_home,
        session_index_path=session_index_path,
        sessions_dir=sessions_dir,
        name=name,
    )
    index_changed = plan.index_action in {"add", "update"}
    updated_index_records = session_index_records_for_import(plan) if index_changed else None

    label = backup_label()
    backup_dir = backup_dir_for(codex_home, label)
    index_backup_path = (
        backup_session_index(plan.session_index_path, backup_dir) if index_changed else None
    )
    rollout_written = False
    try:
        if index_changed:
            if updated_index_records is None:
                raise CliError("Could not prepare session_index.jsonl update.")
            plan.session_index_path.parent.mkdir(parents=True, exist_ok=True)
            write_session_index_records(plan.session_index_path, updated_index_records)
        copy_or_rewrite_import_rollout(plan)
        rollout_written = True
        state_cache_backups = reset_codex_state_cache(codex_home, backup_dir)
    except (CliError, CodexStateError, OSError) as exc:
        try:
            if rollout_written or plan.target_path.exists():
                restore_file_backup(plan.target_path, None)
            if index_changed:
                restore_session_index_backup(plan.session_index_path, index_backup_path)
            remove_backup_dir_if_empty(backup_dir)
        except OSError as restore_exc:
            raise CliError(
                f"{exc} Also failed to restore Codex session files from backup: {restore_exc}"
            ) from restore_exc
        raise CliError(
            f"{exc} Rolled back imported Codex session files. Close all Codex sessions and retry."
        ) from exc

    return ImportSessionResult(
        plan=plan,
        session_index_backup_path=index_backup_path,
        state_cache_backups=state_cache_backups,
    )


def export_filename_date(source_path: Path, document: SearchDocument) -> str:
    filename_date = rollout_filename_date(source_path)
    if filename_date is not None:
        return "-".join(filename_date)
    if document.started_at is not None:
        return document.started_at.astimezone().strftime("%Y-%m-%d")
    return "unknown-date"


def default_export_filename(
    source_path: Path, document: SearchDocument, session_id: str, thread_name: str
) -> str:
    return (
        f"{export_filename_date(source_path, document)}--"
        f"{export_title_slug(thread_name)}--{session_id}.jsonl"
    )


def resolve_single_session_file_for_export(session_id: str, sessions_dir: Path) -> SessionFile:
    matches = existing_session_files_for_id(session_id, sessions_dir)
    if not matches:
        raise CliError(f"No Codex session file found for ID: {session_id}")
    if len(matches) > 1:
        rendered_matches = ", ".join(session_file.relative_path for session_file in matches)
        raise CliError(
            f"Multiple Codex session files found for ID {session_id}: {rendered_matches}"
        )
    return matches[0]


def export_session_index_record(
    target: str, index_path: Path
) -> tuple[str | None, dict[str, Any] | None]:
    if is_session_id(target):
        if not index_path.exists():
            return target, None
        records = session_index_records(index_path)
        match = existing_index_record_for_id(records, target)
        if match is None:
            return target, None
        record_id = session_index_record_id(match[1])
        return record_id or target, match[1]

    records = session_index_records(index_path)
    _, record = resolve_session_index_record(records, target)
    session_id = session_index_record_id(record)
    if session_id is None:
        raise CliError(f"Matched session_index.jsonl entry has no session id: {target}")
    return session_id, record


def plan_session_export(
    target: str,
    codex_home: Path,
    output: Path | None = None,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    force: bool = False,
) -> ExportSessionPlan:
    resolved_sessions_dir = sessions_dir or codex_home / "sessions"
    if not resolved_sessions_dir.exists():
        raise CliError(f"Sessions directory not found: {resolved_sessions_dir}")

    index_path = session_index_path or codex_home / "session_index.jsonl"
    session_id, index_record = export_session_index_record(target, index_path)
    if session_id is None:
        raise CliError(f"Could not resolve session ID for export target: {target}")

    session_file = resolve_single_session_file_for_export(session_id, resolved_sessions_dir)
    source_path = session_file.path.resolve()
    document = build_search_document(source_path, "...")
    resolved_session_id = document.session_id or session_file.session_id or session_id
    index_thread_name = (
        session_index_record_thread_name(index_record) if index_record is not None else ""
    )
    thread_name = index_thread_name or inferred_thread_name(document)
    if not thread_name:
        raise CliError("Exported session title must not be empty.")

    _, rollout_will_be_rewritten = prepare_import_rollout_records(
        source_path, resolved_session_id, thread_name
    )
    output_path = resolve_export_output_path(
        output, default_export_filename(source_path, document, resolved_session_id, thread_name)
    )
    if output_path.exists():
        if output_path.resolve() == source_path:
            raise CliError(f"Export output path is the source rollout file: {output_path}")
        if not force:
            raise CliError(f"Output file already exists: {output_path}. Use --force to overwrite.")

    return ExportSessionPlan(
        source_path=source_path,
        output_path=output_path,
        session_id=resolved_session_id,
        thread_name=thread_name,
        started_at=document.started_at,
        ended_at=document.ended_at,
        rollout_will_be_rewritten=rollout_will_be_rewritten,
        overwrite=output_path.exists(),
    )


def export_session(
    target: str,
    codex_home: Path,
    output: Path | None = None,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    force: bool = False,
) -> ExportSessionResult:
    plan = plan_session_export(
        target=target,
        codex_home=codex_home,
        output=output,
        session_index_path=session_index_path,
        sessions_dir=sessions_dir,
        force=force,
    )
    plan.output_path.parent.mkdir(parents=True, exist_ok=True)
    if plan.rollout_will_be_rewritten:
        records, _ = prepare_import_rollout_records(
            plan.source_path, plan.session_id, plan.thread_name
        )
        write_rollout_records(plan.output_path, records)
    else:
        shutil.copy2(plan.source_path, plan.output_path)
    return ExportSessionResult(plan=plan)


def render_session_metadata_search_lines(payload: dict[str, Any]) -> list[str]:
    lines = []
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        lines.append(f"Session metadata: cwd: {cwd}")

    git = payload.get("git")
    if isinstance(git, dict):
        branch = git.get("branch")
        if isinstance(branch, str) and branch:
            lines.append(f"Session metadata: branch: {branch}")
        repository_url = git.get("repository_url")
        if isinstance(repository_url, str) and repository_url:
            lines.append(f"Session metadata: repository_url: {repository_url}")

    return lines


def render_tool_call_search_lines(payload: dict[str, Any]) -> list[str]:
    full_name = tool_display_name(payload)
    arguments = payload.get("arguments") if "arguments" in payload else payload.get("input")
    short_name = normalized_tool_short_name(full_name)

    args = parse_json_object_maybe(arguments)
    if short_name == "shell_command" and args is not None:
        command = args.get("command")
        if command:
            return render_labeled_search_lines(f"Tool call: {full_name}", str(command))
        return [f"Tool call: {full_name}"]

    if short_name == "apply_patch" and arguments:
        patch_preview = truncate_preview(str(arguments), DEFAULT_TOOL_PREVIEW_CHARS)
        return render_labeled_search_lines(f"Tool call: {full_name}", patch_preview)

    preview = render_smart_tool_call_preview(full_name, arguments, DEFAULT_TOOL_PREVIEW_CHARS)
    if not preview:
        return [f"Tool call: {full_name}"]

    preview_lines = [
        line
        for line in preview
        if not line.startswith("Workdir:")
        and not line.startswith("Timeout ms:")
        and not line.startswith("Call ID:")
    ]
    return [
        f"Tool call: {full_name}: {line.strip()}"
        for line in preview_lines
        if line.strip() and not line.startswith("```")
    ]


def search_document_for_file(
    path: Path,
    redaction: str,
    cache_entries: dict[str, Any] | None,
    *,
    rebuild_cache: bool,
) -> tuple[SearchDocument, os.stat_result, bool]:
    stat_result = path.stat()
    cache_key = search_cache_key(path)
    if cache_entries is not None and not rebuild_cache:
        document = cached_search_document(
            cache_entries.get(cache_key), path, stat_result, redaction
        )
        if document is not None:
            return document, stat_result, False

    document = build_search_document(path, redaction)
    return document, stat_result, True


def load_search_documents(
    codex_home: Path,
    sessions_dir: Path,
    redaction: str,
    *,
    use_cache: bool = True,
    rebuild_cache: bool = False,
) -> tuple[list[tuple[Path, SearchDocument]], list[str]]:
    session_paths = discover_session_paths(sessions_dir)
    cache_path = search_cache_path(codex_home)
    cache_entries = read_search_cache(cache_path) if use_cache else None
    cache_dirty = False
    documents: list[tuple[Path, SearchDocument]] = []
    warnings: list[str] = []

    for session_path in session_paths:
        try:
            document, stat_result, document_rebuilt = search_document_for_file(
                session_path,
                redaction,
                cache_entries,
                rebuild_cache=rebuild_cache,
            )
            if cache_entries is not None and document_rebuilt:
                cache_entries[search_cache_key(session_path)] = search_cache_entry(
                    session_path, stat_result, document, redaction
                )
                cache_dirty = True
            documents.append((session_path, document))
        except (OSError, ValueError) as exc:
            relative_path = format_session_file_path(session_path, sessions_dir)
            warnings.append(f"{relative_path}: {exc}")
            if cache_entries is not None:
                cache_entries.pop(search_cache_key(session_path), None)
                cache_dirty = True

    if cache_entries is not None:
        cache_dirty = prune_missing_search_cache_entries(cache_entries) or cache_dirty
        if cache_dirty:
            try:
                write_search_cache(cache_path, cache_entries)
            except OSError as exc:
                warnings.append(f"Could not write search cache {cache_path}: {exc}")

    return documents, warnings


def search_sessions(
    codex_home: Path,
    options: SearchOptions,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    use_cache: bool = True,
    rebuild_cache: bool = False,
) -> tuple[list[SearchResult], list[str]]:
    resolved_sessions_dir = sessions_dir or codex_home / "sessions"
    if not resolved_sessions_dir.exists():
        raise CliError(f"Sessions directory not found: {resolved_sessions_dir}")

    index_path = session_index_path or codex_home / "session_index.jsonl"
    index_entries = read_session_index(index_path)
    entries_by_id = {normalize_session_id(entry.session_id): entry for entry in index_entries}
    search_pattern = compile_search_pattern(options)
    documents, warnings = load_search_documents(
        codex_home=codex_home,
        sessions_dir=resolved_sessions_dir,
        redaction=options.redaction,
        use_cache=use_cache,
        rebuild_cache=rebuild_cache,
    )

    results = []
    for session_path, document in documents:
        search_lines = search_document_lines(document, options)
        all_lines = search_matching_lines(search_lines, search_pattern, options.line_width)
        if options.max_lines_per_session:
            lines = all_lines[: options.max_lines_per_session]
            omitted_occurrence_count = max(
                0,
                sum(line.occurrence_count for line in all_lines)
                - sum(line.occurrence_count for line in lines),
            )
        else:
            lines = all_lines
            omitted_occurrence_count = 0

        session_file = SessionFile(
            path=session_path,
            relative_path=format_session_file_path(session_path, resolved_sessions_dir),
            session_id=document.session_id,
            started_at=document.started_at,
            ended_at=document.ended_at,
        )
        inferred_title = infer_search_document_title(document)
        session_info = session_info_for_search(session_file, entries_by_id, inferred_title)
        title = session_title_for_search(session_file, entries_by_id, inferred_title)
        session_info_matches = session_info_title_match_spans(session_info, title, search_pattern)
        if lines or session_info_matches:
            results.append(
                SearchResult(
                    session_info=session_info,
                    session_info_matches=session_info_matches,
                    lines=lines,
                    omitted_occurrence_count=omitted_occurrence_count,
                )
            )

    return results, warnings


def missing_session_index_candidates(
    codex_home: Path,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    use_cache: bool = True,
    rebuild_cache: bool = False,
) -> tuple[list[RepairIndexCandidate], list[str], int]:
    index_path = session_index_path or codex_home / "session_index.jsonl"
    resolved_sessions_dir = sessions_dir or codex_home / "sessions"
    if not resolved_sessions_dir.exists():
        raise CliError(f"Sessions directory not found: {resolved_sessions_dir}")

    index_entries = read_session_index(index_path)
    indexed_ids = {normalize_session_id(entry.session_id) for entry in index_entries}
    documents, warnings = load_search_documents(
        codex_home=codex_home,
        sessions_dir=resolved_sessions_dir,
        redaction="...",
        use_cache=use_cache,
        rebuild_cache=rebuild_cache,
    )

    candidates = []
    skipped_without_id = 0
    for session_path, document in documents:
        if not document.session_id:
            skipped_without_id += 1
            continue
        if normalize_session_id(document.session_id) in indexed_ids:
            continue
        candidates.append(
            RepairIndexCandidate(
                session_id=document.session_id,
                thread_name=inferred_thread_name(document),
                updated_at=document.ended_at or document.started_at,
                relative_path=format_session_file_path(session_path, resolved_sessions_dir),
            )
        )

    return candidates, warnings, skipped_without_id


def format_repair_index_candidate(candidate: RepairIndexCandidate) -> str:
    updated_at = candidate.updated_at.isoformat() if candidate.updated_at else "UNKNOWN"
    return (
        f"{candidate.session_id} - {candidate.thread_name} - "
        f"{candidate.relative_path} - updated_at: {updated_at}"
    )


def optional_session_file_for_id(session_id: str, codex_home: Path) -> Path | None:
    try:
        return resolve_session_id(session_id, codex_home)
    except CliError as exc:
        if str(exc).startswith("No Codex session found for ID:"):
            return None
        raise


def rename_session_index_entry(
    codex_home: Path,
    session_index_path: Path | None,
    target: str,
    new_thread_name: str,
) -> RenameSessionResult:
    normalized_new_thread_name = new_thread_name.strip()
    if not normalized_new_thread_name:
        raise CliError("New session title must not be empty.")

    index_path = session_index_path or codex_home / "session_index.jsonl"
    records = session_index_records(index_path)
    record_index, record = resolve_session_index_record(records, target)
    session_id = session_index_record_id(record)
    if session_id is None:
        raise CliError(f"Matched session_index.jsonl entry has no session id: {target}")

    old_thread_name = session_index_record_thread_name(record)
    index_changed = old_thread_name != normalized_new_thread_name
    rollout_path = optional_session_file_for_id(session_id, codex_home)
    rollout_records: list[dict[str, Any]] | None = None
    updated_rollout_records: list[dict[str, Any]] | None = None
    rollout_thread_name: str | None = None
    rollout_changed = False
    if rollout_path is not None:
        rollout_records = read_rollout_records(rollout_path)
        updated_rollout_records, rollout_thread_name, rollout_changed = renamed_rollout_records(
            rollout_records, session_id, normalized_new_thread_name
        )

    if not index_changed and not rollout_changed:
        return RenameSessionResult(
            session_id=session_id,
            old_thread_name=old_thread_name,
            new_thread_name=normalized_new_thread_name,
            index_changed=False,
            rollout_changed=False,
            rollout_path=rollout_path,
            rollout_backup_path=None,
            rollout_thread_name=rollout_thread_name,
            changed=False,
            session_index_backup_path=None,
            state_cache_backups=(),
        )

    updated_records = list(records)
    if index_changed:
        updated_record = dict(record)
        updated_record["thread_name"] = normalized_new_thread_name
        updated_records[record_index] = updated_record

    label = backup_label()
    backup_dir = backup_dir_for(codex_home, label)
    index_backup_path = backup_session_index(index_path, backup_dir) if index_changed else None
    rollout_backup_path = (
        backup_file(rollout_path, backup_dir)
        if rollout_changed and rollout_path is not None
        else None
    )
    try:
        if index_changed:
            write_session_index_records(index_path, updated_records)
        if rollout_changed:
            if rollout_path is None or updated_rollout_records is None:
                raise CliError(f"No Codex rollout file found for ID: {session_id}")
            write_rollout_records(rollout_path, updated_rollout_records)
        state_cache_backups = reset_codex_state_cache(codex_home, backup_dir)
    except (CliError, CodexStateError, OSError) as exc:
        try:
            if rollout_path is not None and rollout_changed:
                restore_file_backup(rollout_path, rollout_backup_path)
            if index_changed:
                restore_session_index_backup(index_path, index_backup_path)
            remove_backup_dir_if_empty(backup_dir)
        except OSError as restore_exc:
            raise CliError(
                f"{exc} Also failed to restore Codex session files from backup: {restore_exc}"
            ) from restore_exc
        raise CliError(
            f"{exc} Rolled back Codex session files. Close all Codex sessions and retry."
        ) from exc

    return RenameSessionResult(
        session_id=session_id,
        old_thread_name=old_thread_name,
        new_thread_name=normalized_new_thread_name,
        index_changed=index_changed,
        rollout_changed=rollout_changed,
        rollout_path=rollout_path,
        rollout_backup_path=rollout_backup_path,
        rollout_thread_name=rollout_thread_name,
        changed=True,
        session_index_backup_path=index_backup_path,
        state_cache_backups=state_cache_backups,
    )


def repair_session_index(
    codex_home: Path,
    session_index_path: Path | None = None,
    sessions_dir: Path | None = None,
    *,
    use_cache: bool = True,
    rebuild_cache: bool = False,
) -> RepairIndexResult:
    index_path = session_index_path or codex_home / "session_index.jsonl"
    candidates, warnings, skipped_without_id = missing_session_index_candidates(
        codex_home=codex_home,
        session_index_path=index_path,
        sessions_dir=sessions_dir,
        use_cache=use_cache,
        rebuild_cache=rebuild_cache,
    )
    if not candidates:
        return RepairIndexResult(
            candidates=(),
            warnings=tuple(warnings),
            skipped_without_id=skipped_without_id,
            session_index_backup_path=None,
            state_cache_backups=(),
        )

    label = backup_label()
    backup_dir = backup_dir_for(codex_home, label)
    index_backup_path = backup_session_index(index_path, backup_dir)
    try:
        append_session_index_records(index_path, candidates)
        state_cache_backups = reset_codex_state_cache(codex_home, backup_dir)
    except (CliError, CodexStateError, OSError) as exc:
        try:
            restore_session_index_backup(index_path, index_backup_path)
            remove_backup_dir_if_empty(backup_dir)
        except OSError as restore_exc:
            raise CliError(
                f"{exc} Also failed to restore session_index.jsonl from backup: {restore_exc}"
            ) from restore_exc
        raise CliError(
            f"{exc} Rolled back session_index.jsonl. Close all Codex sessions and retry."
        ) from exc

    return RepairIndexResult(
        candidates=tuple(candidates),
        warnings=tuple(warnings),
        skipped_without_id=skipped_without_id,
        session_index_backup_path=index_backup_path,
        state_cache_backups=state_cache_backups,
    )


def parse_markdown_include(spec: str) -> set[str]:
    parts = [part.strip().lower() for part in spec.split(",") if part.strip()]
    if not parts:
        parts = ["default"]

    first = parts[0]
    if first in MARKDOWN_PRESETS:
        features = set(MARKDOWN_PRESETS[first])
        parts = parts[1:]
    else:
        alias = MARKDOWN_INCLUDE_ALIASES.get(first)
        if alias == "all":
            features = set(MARKDOWN_FEATURES)
            parts = parts[1:]
        elif alias == "none":
            features = set()
            parts = parts[1:]
        else:
            features = set(MARKDOWN_PRESETS["default"])

    for raw_part in parts:
        include = True
        part = raw_part
        if part.startswith("+"):
            part = part[1:]
        elif part.startswith("-"):
            include = False
            part = part[1:]

        alias = MARKDOWN_INCLUDE_ALIASES.get(part)
        if alias is None:
            allowed = sorted(set(MARKDOWN_PRESETS) | set(MARKDOWN_INCLUDE_ALIASES))
            raise ValueError(
                f"Unknown --md-include item {raw_part!r}. Allowed values: {', '.join(allowed)}"
            )
        if alias == "all":
            if include:
                features.update(MARKDOWN_FEATURES)
            else:
                features.clear()
            continue
        if alias == "none":
            if include:
                features.clear()
            continue
        if include:
            features.add(alias)
        else:
            features.discard(alias)

    return features


def resolve_markdown_tool_mode(markdown_features: set[str], requested_mode: str) -> str:
    if requested_mode == "auto":
        return "smart" if "tools" in markdown_features else "none"
    return requested_mode


@dataclass(frozen=True)
class DataImageUrl:
    media_type: str
    encoded_data: str


class MarkdownImageHandler:
    def __init__(self, mode: str, output_path: Path, input_path: Path) -> None:
        self.mode = mode
        self.output_path = output_path
        self.input_path = input_path
        self.asset_dir = output_path.with_name(f"{output_path.stem}_assets")
        self._links_by_url: dict[str, str] = {}
        self.source_line_number: int | None = None

    def set_source_line(self, line_number: int) -> None:
        self.source_line_number = line_number

    def render_image(self, image_url: Any, label: str = "image") -> str:
        if not isinstance(image_url, str):
            return f"[{label}: missing image_url]"

        data_image = parse_data_image_url(image_url)
        if data_image is None:
            return f"[{label}: {image_url}]"

        if self.mode == "inline":
            return "\n".join(
                [
                    self.inline_image_comment(),
                    f"![{label}]({image_url})",
                ]
            )
        if self.mode == "extract":
            link = self.extract_data_image(image_url, data_image)
            if link:
                return f"![{label}]({link})"
            return f"[{label}: invalid {data_image.media_type} data URL]"
        return f"[{label}: {self.describe_truncated_image(data_image)}]"

    def transform_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.transform_value(inner) for key, inner in value.items()}
        if isinstance(value, list):
            return [self.transform_value(item) for item in value]
        if isinstance(value, str):
            data_image = parse_data_image_url(value)
            if data_image is None:
                return value
            if self.mode == "inline":
                return value
            if self.mode == "extract":
                link = self.extract_data_image(value, data_image)
                return link if link else f"[invalid {data_image.media_type} data URL]"
            return (
                f"data:{data_image.media_type};base64,{self.describe_truncated_image(data_image)}"
            )
        return value

    def extract_data_image(self, image_url: str, data_image: DataImageUrl) -> str | None:
        if image_url in self._links_by_url:
            return self._links_by_url[image_url]

        try:
            image_bytes = base64.b64decode("".join(data_image.encoded_data.split()), validate=True)
        except (binascii.Error, ValueError):
            return None

        digest = hashlib.sha256(image_bytes).hexdigest()[:12]
        extension = image_extension(data_image.media_type)
        filename = f"image-{len(self._links_by_url) + 1:03d}-{digest}.{extension}"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        image_path = self.asset_dir / filename
        if not image_path.exists():
            image_path.write_bytes(image_bytes)

        link = markdown_relative_link(image_path, self.output_path)
        self._links_by_url[image_url] = link
        return link

    def describe_truncated_image(self, data_image: DataImageUrl) -> str:
        return describe_data_image(data_image, self.source_reference())

    def source_reference(self) -> str:
        source = str(self.input_path)
        if self.source_line_number is not None:
            source = f"{source}:{self.source_line_number}"
        return markdown_code_span(source)

    def source_comment_reference(self) -> str:
        source = str(self.input_path)
        if self.source_line_number is not None:
            source = f"{source}:{self.source_line_number}"
        return escape_markdown_reference_comment_text(source)

    def inline_image_comment(self) -> str:
        return (
            "[//]: # (Inline image; use --md-images truncate or --md-images extract; "
            f"Source: {self.source_comment_reference()}.)"
        )


def parse_data_image_url(value: str) -> DataImageUrl | None:
    match = DATA_IMAGE_URL_RE.match(value)
    if not match:
        return None
    return DataImageUrl(
        media_type=match.group(1).lower(),
        encoded_data=match.group(2),
    )


def describe_data_image(data_image: DataImageUrl, source_reference: str | None = None) -> str:
    compact_data = "".join(data_image.encoded_data.split())
    base64_prefix = compact_data[:DATA_IMAGE_PREFIX_CHARS]
    if len(compact_data) > DATA_IMAGE_PREFIX_CHARS:
        base64_prefix = f"{base64_prefix}..."
    parts = [
        f"{data_image.media_type} data URL",
        f"{len(compact_data)} base64 chars truncated",
    ]
    if source_reference:
        parts.append(f"source {source_reference}")
    parts.append(f"base64 prefix {markdown_code_span(base64_prefix)}")
    return "; ".join(parts)


def image_extension(media_type: str) -> str:
    extension = IMAGE_EXTENSION_BY_MIME_TYPE.get(media_type)
    if extension:
        return extension
    subtype = media_type.split("/", 1)[-1].split("+", 1)[0].lower()
    sanitized = re.sub(r"[^a-z0-9]+", "", subtype)
    return sanitized or "bin"


def markdown_relative_link(target_path: Path, markdown_path: Path) -> str:
    relative_path = os.path.relpath(target_path, start=markdown_path.parent)
    return quote(Path(relative_path).as_posix(), safe="/._-")


def markdown_code_span(text: str) -> str:
    if "`" not in text:
        return f"`{text}`"
    return f"`` {text} ``"


def escape_markdown_reference_comment_text(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace(")", r"\)")


def content_to_text(content: Any, image_handler: MarkdownImageHandler | None = None) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        value = image_handler.transform_value(content) if image_handler else content
        return render_json_block_content(value)

    has_image_item = any(is_image_content_item(item) for item in content)
    parts = []
    for item in content:
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                text = item["text"]
                if has_image_item and is_image_wrapper_text(text):
                    continue
                parts.append(text)
            elif is_image_content_item(item):
                if image_handler:
                    parts.append(image_handler.render_image(item.get("image_url"), "input image"))
                else:
                    parts.append(f"[input image: {item.get('image_url', '')}]")
            elif item.get("type") == "local_image":
                parts.append(f"[local image: {item.get('path') or item.get('name') or ''}]")
            else:
                value = image_handler.transform_value(item) if image_handler else item
                parts.append(render_json_block_content(value))
        else:
            parts.append(str(item))
    return "\n\n".join(part for part in parts if part)


def is_image_content_item(item: Any) -> bool:
    return isinstance(item, dict) and (
        item.get("type") in {"image_url", "input_image"} or "image_url" in item
    )


def is_image_wrapper_text(text: str) -> bool:
    return text.strip().lower() in {"<image>", "</image>"}


def is_injected_user_context(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("# AGENTS.md instructions", "<environment_context>"))


def searchable_user_message_text(text: str) -> str:
    if is_injected_user_context(text):
        return ""

    stripped = text.lstrip()
    if stripped.startswith("# Context from my IDE setup:"):
        marker = "## My request for Codex:"
        marker_index = text.find(marker)
        if marker_index == -1:
            return ""
        return text[marker_index + len(marker) :].strip()

    return text


def render_json_block_content(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_markdown_table_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif value is None:
        text = "null"
    elif value is True:
        text = "true"
    elif value is False:
        text = "false"
    else:
        text = str(value)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("|", r"\|").replace("\n", "<br>")


def flatten_table_rows(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        if not value:
            return [(prefix, {})]
        rows = []
        for key, inner in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(flatten_table_rows(inner, child_prefix))
        return rows

    if isinstance(value, list):
        if not value:
            return [(prefix, [])]
        rows = []
        for index, inner in enumerate(value):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            rows.extend(flatten_table_rows(inner, child_prefix))
        return rows

    return [(prefix, value)]


def render_markdown_table(value: Any) -> str:
    rows = flatten_table_rows(value)
    lines = ["| Field | Value |", "| --- | --- |"]
    for key, inner in rows:
        rendered_key = render_markdown_table_value(key)
        rendered_value = render_markdown_table_value(inner)
        lines.append(f"| {rendered_key} | {rendered_value} |")
    return "\n".join(lines)


def fenced_block(content: str, language: str = "") -> str:
    max_backticks = 2
    for match in re.finditer(r"`+", content):
        max_backticks = max(max_backticks, len(match.group(0)))
    fence = "`" * max(3, max_backticks + 1)
    suffix = language if language else ""
    return f"{fence}{suffix}\n{content}\n{fence}"


def parse_json_maybe(
    value: Any, image_handler: MarkdownImageHandler | None = None
) -> tuple[str, str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return value, "text"
            if image_handler:
                parsed = image_handler.transform_value(parsed)
            return render_json_block_content(parsed), "json"
        if image_handler:
            transformed = image_handler.transform_value(value)
            if transformed != value:
                return str(transformed), "text"
        return value, "text"
    if image_handler:
        value = image_handler.transform_value(value)
    return render_json_block_content(value), "json"


def parse_json_object_maybe(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def normalized_tool_short_name(full_name: str) -> str:
    short_name = full_name.rsplit(".", 1)[-1]
    for prefix in (
        "mcp__playwright__",
        "mcp__codex_apps__",
        "mcp__ask_human_for_context__",
    ):
        if short_name.startswith(prefix):
            return short_name[len(prefix) :]
    return short_name


def tool_display_name(payload: dict[str, Any]) -> str:
    call_type = payload.get("type", "tool_call")
    name = payload.get("name") or call_type
    namespace = payload.get("namespace")
    return f"{namespace}.{name}" if namespace else name


def append_tool_identity(lines: list[str], payload: dict[str, Any]) -> None:
    if payload.get("call_id"):
        lines.append(f"Call ID: `{payload['call_id']}`")
    if payload.get("status"):
        lines.append(f"Status: `{payload['status']}`")
    if payload.get("execution"):
        lines.append(f"Execution: `{payload['execution']}`")


def truncate_preview(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars].rstrip()}\n\n... [truncated, {omitted} characters omitted]"


def render_preview_block(
    label: str,
    value: Any,
    max_chars: int,
    image_handler: MarkdownImageHandler | None = None,
) -> list[str]:
    body, language = parse_json_maybe(value, image_handler)
    return ["", label, fenced_block(truncate_preview(body, max_chars), language)]


def append_inline_preview(lines: list[str], label: str, value: Any, max_chars: int) -> None:
    if value is None:
        return
    text = str(value)
    if not text:
        return
    lines.append(f"{label}: `{truncate_preview(text, max_chars)}`")


def append_sequence_preview(
    lines: list[str], label: str, value: Any, max_chars: int, max_items: int = 8
) -> None:
    if value is None:
        return
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        append_inline_preview(lines, label, value, max_chars)
        return

    items = list(value)
    rendered_items = []
    for item in items[:max_items]:
        if isinstance(item, (dict, list)):
            rendered_items.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        else:
            rendered_items.append(str(item))
    if not rendered_items:
        return
    if len(items) > max_items:
        rendered_items.append(f"... [{len(items) - max_items} more omitted]")
    append_fenced_preview(lines, label, "\n".join(rendered_items), max_chars)


def append_fenced_preview(
    lines: list[str],
    label: str,
    value: Any,
    max_chars: int,
    language: str = "text",
) -> None:
    if value is None:
        return
    text = str(value)
    if not text:
        return
    lines.extend(["", label, fenced_block(truncate_preview(text, max_chars), language)])


def render_smart_tool_call_preview(
    full_name: str, arguments: Any, preview_chars: int
) -> list[str] | None:
    short_name = normalized_tool_short_name(full_name)
    lines: list[str] = []

    if short_name == "apply_patch":
        append_fenced_preview(lines, "Patch preview:", arguments, preview_chars, "diff")
        return lines or None

    args = parse_json_object_maybe(arguments)
    if args is None:
        return None

    if short_name == "shell_command":
        append_fenced_preview(lines, "Command preview:", args.get("command"), preview_chars)
        append_inline_preview(lines, "Workdir", args.get("workdir"), preview_chars)
        append_inline_preview(lines, "Timeout ms", args.get("timeout_ms"), preview_chars)
        return lines or None

    if short_name in {"browser_run_code", "browser_evaluate"}:
        label = "Code preview:" if short_name == "browser_run_code" else "Function preview:"
        append_fenced_preview(
            lines,
            label,
            args.get("code") or args.get("function"),
            preview_chars,
            "javascript",
        )
        append_inline_preview(lines, "Target", args.get("target"), preview_chars)
        append_inline_preview(lines, "Filename", args.get("filename"), preview_chars)
        return lines or None

    if short_name == "update_plan":
        explanation = args.get("explanation")
        append_inline_preview(lines, "Explanation", explanation, preview_chars)
        plan = args.get("plan")
        if isinstance(plan, list):
            preview_lines = []
            for item in plan[:8]:
                if not isinstance(item, dict):
                    continue
                status = item.get("status", "unknown")
                step = item.get("step", "")
                preview_lines.append(f"- {status}: {step}")
            if len(plan) > 8:
                preview_lines.append(f"... [{len(plan) - 8} more steps omitted]")
            append_fenced_preview(lines, "Plan preview:", "\n".join(preview_lines), preview_chars)
        return lines or None

    if full_name == "tool_search_call":
        append_inline_preview(lines, "Query", args.get("query"), preview_chars)
        append_inline_preview(lines, "Limit", args.get("limit"), preview_chars)
        return lines or None

    if short_name in {
        "request_user_input",
        "asking_user_missing_context",
    }:
        questions = args.get("questions")
        if isinstance(questions, list) and questions:
            first_question = questions[0]
            if isinstance(first_question, dict):
                append_inline_preview(
                    lines, "Question", first_question.get("question"), preview_chars
                )
        append_inline_preview(lines, "Question", args.get("question"), preview_chars)
        append_inline_preview(lines, "Context", args.get("context"), preview_chars)
        return lines or None

    if short_name in {"spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent"}:
        append_inline_preview(lines, "Target", args.get("target"), preview_chars)
        append_inline_preview(lines, "ID", args.get("id"), preview_chars)
        append_inline_preview(lines, "Agent type", args.get("agent_type"), preview_chars)
        append_sequence_preview(lines, "Targets preview:", args.get("targets"), preview_chars)
        append_fenced_preview(lines, "Message preview:", args.get("message"), preview_chars)
        append_inline_preview(lines, "Timeout ms", args.get("timeout_ms"), preview_chars)
        return lines or None

    if short_name == "parallel":
        tool_uses = args.get("tool_uses")
        if isinstance(tool_uses, list):
            preview_lines = []
            for tool_use in tool_uses[:8]:
                if not isinstance(tool_use, dict):
                    continue
                name = tool_use.get("recipient_name", "unknown")
                parameters = tool_use.get("parameters")
                if isinstance(parameters, dict):
                    keys = ", ".join(parameters.keys())
                    preview_lines.append(f"- {name} ({keys})" if keys else f"- {name}")
                else:
                    preview_lines.append(f"- {name}")
            if len(tool_uses) > 8:
                preview_lines.append(f"... [{len(tool_uses) - 8} more tool uses omitted]")
            append_fenced_preview(
                lines, "Tool uses preview:", "\n".join(preview_lines), preview_chars
            )
        return lines or None

    if short_name in {
        "browser_click",
        "browser_close",
        "browser_console_messages",
        "browser_drag",
        "browser_drop",
        "browser_file_upload",
        "browser_fill_form",
        "browser_handle_dialog",
        "browser_hover",
        "browser_navigate",
        "browser_navigate_back",
        "browser_network_request",
        "browser_network_requests",
        "browser_press_key",
        "browser_resize",
        "browser_select_option",
        "browser_snapshot",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_type",
        "browser_wait_for",
    }:
        for key in (
            "url",
            "action",
            "target",
            "ref",
            "element",
            "button",
            "key",
            "text",
            "textGone",
            "filename",
            "filter",
            "part",
            "index",
            "time",
            "width",
            "height",
            "fullPage",
            "type",
            "level",
            "all",
            "depth",
            "boxes",
            "accept",
            "promptText",
            "submit",
            "slowly",
            "static",
        ):
            append_inline_preview(
                lines, key.replace("_", " ").title(), args.get(key), preview_chars
            )
        append_sequence_preview(lines, "Paths preview:", args.get("paths"), preview_chars)
        append_sequence_preview(lines, "Values preview:", args.get("values"), preview_chars)
        fields = args.get("fields")
        if isinstance(fields, list):
            field_lines = []
            for field in fields[:8]:
                if not isinstance(field, dict):
                    continue
                name = field.get("name", "field")
                field_type = field.get("type")
                target = field.get("target")
                value = field.get("value")
                details = [str(name)]
                if field_type:
                    details.append(f"type={field_type}")
                if target:
                    details.append(f"target={target}")
                if value is not None:
                    details.append(f"value={value}")
                field_lines.append(" - ".join(details))
            if len(fields) > 8:
                field_lines.append(f"... [{len(fields) - 8} more fields omitted]")
            append_fenced_preview(lines, "Fields preview:", "\n".join(field_lines), preview_chars)
        return lines or None

    if short_name == "view_image":
        append_inline_preview(lines, "Path", args.get("path"), preview_chars)
        append_sequence_preview(lines, "Paths preview:", args.get("paths"), preview_chars)
        append_inline_preview(lines, "Detail", args.get("detail"), preview_chars)
        return lines or None

    if short_name in {"github_fetch", "github_search"}:
        append_inline_preview(lines, "URL", args.get("url"), preview_chars)
        append_inline_preview(lines, "Query", args.get("query"), preview_chars)
        append_inline_preview(lines, "Repository name", args.get("repository_name"), preview_chars)
        append_inline_preview(lines, "Top N", args.get("topn"), preview_chars)
        return lines or None

    if short_name in {"tool_suggest"}:
        append_inline_preview(lines, "Tool ID", args.get("tool_id"), preview_chars)
        append_inline_preview(lines, "Tool type", args.get("tool_type"), preview_chars)
        append_inline_preview(lines, "Action type", args.get("action_type"), preview_chars)
        append_inline_preview(lines, "Reason", args.get("suggest_reason"), preview_chars)
        return lines or None

    if short_name in {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}:
        append_inline_preview(lines, "Server", args.get("server"), preview_chars)
        append_inline_preview(lines, "URI", args.get("uri"), preview_chars)
        append_inline_preview(lines, "Cursor", args.get("cursor"), preview_chars)
        return lines or None

    return None


def render_tool_call(
    payload: dict[str, Any],
    mode: str,
    preview_chars: int,
    image_handler: MarkdownImageHandler | None = None,
) -> tuple[str, str | None]:
    full_name = tool_display_name(payload)
    lines = [f"**Tool call:** `{full_name}`"]
    append_tool_identity(lines, payload)

    if mode == "names":
        return "\n".join(lines), full_name

    arguments = payload.get("arguments") if "arguments" in payload else payload.get("input")
    if mode == "smart":
        smart_preview = render_smart_tool_call_preview(full_name, arguments, preview_chars)
        if smart_preview:
            lines.extend(smart_preview)
        return "\n".join(lines), full_name

    if arguments is not None:
        if mode == "preview":
            lines.extend(
                render_preview_block("Arguments preview:", arguments, preview_chars, image_handler)
            )
        else:
            body, language = parse_json_maybe(arguments, image_handler)
            lines.extend(["", "Arguments:", fenced_block(body, language)])
    else:
        remaining = {
            key: value
            for key, value in payload.items()
            if key not in {"type", "name", "namespace", "call_id", "status", "execution"}
        }
        if remaining:
            if mode == "preview":
                lines.extend(
                    render_preview_block(
                        "Payload preview:", remaining, preview_chars, image_handler
                    )
                )
            else:
                if image_handler:
                    remaining = image_handler.transform_value(remaining)
                lines.extend(["", fenced_block(render_json_block_content(remaining), "json")])
    return "\n".join(lines), full_name


def render_tool_output(
    payload: dict[str, Any],
    mode: str,
    preview_chars: int,
    tool_names_by_call_id: dict[str, str],
    image_handler: MarkdownImageHandler | None = None,
) -> str:
    call_type = payload.get("type", "tool_output")
    call_id = payload.get("call_id")
    tool_name = tool_names_by_call_id.get(call_id, call_type) if call_id else call_type
    lines = [f"**Tool output:** `{tool_name}`"]
    append_tool_identity(lines, payload)

    if mode in {"names", "smart"}:
        return "\n".join(lines)

    if "output" in payload:
        body, language = parse_json_maybe(payload["output"], image_handler)
    else:
        remaining = {
            key: value
            for key, value in payload.items()
            if key not in {"type", "call_id", "status", "execution"}
        }
        if image_handler:
            remaining = image_handler.transform_value(remaining)
        body = render_json_block_content(remaining)
        language = "json"
    if mode == "preview":
        lines.extend(
            [
                "",
                "Output preview:",
                fenced_block(truncate_preview(body, preview_chars), language),
            ]
        )
    else:
        lines.extend(["", fenced_block(body, language)])
    return "\n".join(lines)


def render_reasoning(
    payload: dict[str, Any],
    redaction: str,
    image_handler: MarkdownImageHandler | None = None,
) -> str:
    if (
        not payload.get("summary")
        and not payload.get("content")
        and payload.get("encrypted_content") is not None
    ):
        return f"**Reasoning (encrypted_content) {redaction}**"

    lines = ["**Reasoning**"]

    summary = payload.get("summary")
    if summary:
        lines.extend(["", "Summary:"])
        if isinstance(summary, list):
            for item in summary:
                text = (
                    content_to_text(item.get("content"), image_handler)
                    if isinstance(item, dict)
                    else str(item)
                )
                if text:
                    lines.append(f"- {text}")
        else:
            lines.append(str(summary))

    content = payload.get("content")
    if content:
        lines.extend(["", content_to_text(content, image_handler)])
    elif payload.get("encrypted_content") is not None:
        lines.extend(["", f"`encrypted_content`: {redaction}"])

    return "\n".join(lines)


def metadata_title(record: dict[str, Any]) -> str:
    record_type = record.get("type", "metadata")
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("type"):
        return f"Metadata: `{record_type}.{payload['type']}`"
    return f"Metadata: `{record_type}`"


def render_metadata(
    record: dict[str, Any],
    image_handler: MarkdownImageHandler | None = None,
) -> str:
    rendered_record = image_handler.transform_value(record) if image_handler else record
    return "\n".join(
        [
            f"Timestamp: `{record.get('timestamp', '')}`",
            "",
            render_markdown_table(rendered_record),
        ]
    )


def render_raw_record(
    line_number: int,
    record: dict[str, Any],
    image_handler: MarkdownImageHandler | None = None,
) -> str:
    rendered_record = image_handler.transform_value(record) if image_handler else record
    return "\n".join(
        [
            f"Line: `{line_number}`",
            "",
            fenced_block(render_json_block_content(rendered_record), "json"),
        ]
    )


def is_metadata_record(record: dict[str, Any]) -> bool:
    record_type = record.get("type")
    if record_type in {"session_meta", "turn_context"}:
        return True
    if record_type == "event_msg":
        payload = record.get("payload")
        if isinstance(payload, dict):
            return payload.get("type") in {
                "mcp_tool_call_end",
                "task_complete",
                "task_started",
                "thread_name_updated",
                "token_count",
            }
    return False


def write_markdown_section(dst: TextIO, title: str, body: str) -> None:
    dst.write(f"# {title}:\n\n")
    dst.write(body.rstrip())
    dst.write("\n\n---\n\n")


def convert_jsonl_to_markdown(input_path: Path, output_path: Path, options: MarkdownOptions) -> int:
    count = 0
    seen_dialogue: set[tuple[str, str]] = set()
    tool_names_by_call_id: dict[str, str] = {}
    image_handler = MarkdownImageHandler(options.image_mode, output_path, input_path)

    def write_dialogue(dst: TextIO, title: str, body: str) -> None:
        nonlocal count
        normalized_body = body.strip()
        if not normalized_body:
            return
        key = (title, normalized_body)
        if key in seen_dialogue:
            return
        seen_dialogue.add(key)
        write_markdown_section(dst, title, normalized_body)
        count += 1

    def write_section(dst: TextIO, title: str, body: str) -> None:
        nonlocal count
        normalized_body = body.strip()
        if not normalized_body:
            return
        write_markdown_section(dst, title, normalized_body)
        count += 1

    with output_path.open("w", encoding="utf-8", newline="\n") as dst:
        for line_number, raw_record in iter_jsonl_objects(input_path):
            image_handler.set_source_line(line_number)
            record = sanitize(raw_record, options.redaction)
            record_type = record.get("type")
            payload = record.get("payload")
            handled = False

            if record_type == "response_item" and isinstance(payload, dict):
                payload_type = payload.get("type")
                if payload_type == "message":
                    role = payload.get("role")
                    text = content_to_text(payload.get("content"), image_handler)
                    if role == "assistant":
                        write_dialogue(dst, "Codex", text)
                        handled = True
                    elif role == "user" and not is_injected_user_context(text):
                        write_dialogue(dst, "User", text)
                        handled = True
                elif payload_type == "reasoning":
                    write_section(
                        dst,
                        "Codex",
                        render_reasoning(payload, options.redaction, image_handler),
                    )
                    handled = True
                elif payload_type in {"function_call", "tool_search_call", "custom_tool_call"}:
                    if options.tool_mode != "none":
                        rendered_tool_call, tool_name = render_tool_call(
                            payload,
                            options.tool_mode,
                            options.tool_preview_chars,
                            image_handler,
                        )
                        call_id = payload.get("call_id")
                        if call_id and tool_name:
                            tool_names_by_call_id[call_id] = tool_name
                        write_section(dst, "Codex", rendered_tool_call)
                    handled = True
                elif payload_type in {
                    "function_call_output",
                    "tool_search_output",
                    "custom_tool_call_output",
                }:
                    if options.tool_mode != "none":
                        write_section(
                            dst,
                            "Codex",
                            render_tool_output(
                                payload,
                                options.tool_mode,
                                options.tool_preview_chars,
                                tool_names_by_call_id,
                                image_handler,
                            ),
                        )
                    handled = True

            elif record_type == "event_msg" and isinstance(payload, dict):
                payload_type = payload.get("type")
                if payload_type == "user_message":
                    write_dialogue(dst, "User", payload.get("message", ""))
                    handled = True
                elif payload_type == "agent_message":
                    write_dialogue(dst, "Codex", payload.get("message", ""))
                    handled = True

            if not handled and options.include_metadata and is_metadata_record(record):
                write_section(
                    dst,
                    metadata_title(record),
                    render_metadata(record, image_handler),
                )
                handled = True

            if not handled and options.include_raw:
                write_section(
                    dst,
                    "Raw",
                    render_raw_record(line_number, record, image_handler),
                )

    return count


def run_list_command(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.expanduser().resolve()
    session_index_path = (
        args.session_index.expanduser().resolve()
        if args.session_index
        else codex_home / "session_index.jsonl"
    )
    sessions_dir = (
        args.sessions_dir.expanduser().resolve() if args.sessions_dir else codex_home / "sessions"
    )

    try:
        lines, warnings = list_session_lines_with_warnings(
            codex_home=codex_home,
            session_index_path=session_index_path,
            sessions_dir=sessions_dir,
            use_cache=not args.no_cache,
            rebuild_cache=args.rebuild_cache,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    for warning in warnings:
        print(
            encode_for_output(f"Warning: {warning}", sys.stderr.encoding),
            file=sys.stderr,
        )
    for line in lines:
        print(encode_for_output(line, sys.stdout.encoding))
    return 0


def run_search_command(args: argparse.Namespace) -> int:
    if args.line_width < 20:
        raise SystemExit("--line-width must be at least 20")
    if args.max_lines_per_session < 0:
        raise SystemExit("--max-lines-per-session must be zero or greater")

    codex_home = args.codex_home.expanduser().resolve()
    session_index_path = (
        args.session_index.expanduser().resolve()
        if args.session_index
        else codex_home / "session_index.jsonl"
    )
    sessions_dir = (
        args.sessions_dir.expanduser().resolve() if args.sessions_dir else codex_home / "sessions"
    )
    options = SearchOptions(
        pattern=args.pattern,
        regex=args.regex,
        ignore_case=args.ignore_case,
        line_width=args.line_width,
        max_lines_per_session=args.max_lines_per_session,
        include_metadata=args.metadata or args.all,
        include_tools=args.tools or args.all,
        color=args.color,
        redaction=args.redact_encrypted,
    )

    try:
        results, warnings = search_sessions(
            codex_home=codex_home,
            options=options,
            session_index_path=session_index_path,
            sessions_dir=sessions_dir,
            use_cache=not args.no_cache,
            rebuild_cache=args.rebuild_cache,
        )
    except (CliError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    try:
        render_search_results(results, warnings, args.color)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.EPIPE}:
            raise
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    return 0 if results else 1


def run_repair_index_command(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.expanduser().resolve()
    session_index_path = (
        args.session_index.expanduser().resolve()
        if args.session_index
        else codex_home / "session_index.jsonl"
    )
    sessions_dir = (
        args.sessions_dir.expanduser().resolve() if args.sessions_dir else codex_home / "sessions"
    )

    if args.dry_run:
        try:
            candidates, warnings, skipped_without_id = missing_session_index_candidates(
                codex_home=codex_home,
                session_index_path=session_index_path,
                sessions_dir=sessions_dir,
                use_cache=not args.no_cache,
                rebuild_cache=args.rebuild_cache,
            )
        except (CliError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        for warning in warnings:
            print(
                encode_for_output(f"Warning: {warning}", sys.stderr.encoding),
                file=sys.stderr,
            )
        print(f"Missing session_index.jsonl entries: {len(candidates)}")
        if candidates:
            print("Would add:")
            for candidate in candidates:
                print(
                    encode_for_output(format_repair_index_candidate(candidate), sys.stdout.encoding)
                )
            print("State cache reset required after repair.")
        else:
            print("No missing session_index.jsonl entries found.")
        if skipped_without_id:
            print(f"Skipped rollout files without session id: {skipped_without_id}")
        return 0

    try:
        result = repair_session_index(
            codex_home=codex_home,
            session_index_path=session_index_path,
            sessions_dir=sessions_dir,
            use_cache=not args.no_cache,
            rebuild_cache=args.rebuild_cache,
        )
    except (CliError, CodexStateError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    for warning in result.warnings:
        print(
            encode_for_output(f"Warning: {warning}", sys.stderr.encoding),
            file=sys.stderr,
        )
    print(f"Added session_index.jsonl entries: {len(result.candidates)}")
    if result.candidates:
        print("Added:")
        for candidate in result.candidates:
            print(encode_for_output(format_repair_index_candidate(candidate), sys.stdout.encoding))
        if result.session_index_backup_path is not None:
            print(f"Session index backup: {result.session_index_backup_path}")
        if result.state_cache_backups:
            print("State cache backups:")
            for backup in result.state_cache_backups:
                print(f"{backup.original_path} -> {backup.backup_path}")
        else:
            print("No Codex state cache files found to reset.")
    else:
        print("No missing session_index.jsonl entries found.")
    if result.skipped_without_id:
        print(f"Skipped rollout files without session id: {result.skipped_without_id}")
    return 0


def run_rename_command(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.expanduser().resolve()
    session_index_path = (
        args.session_index.expanduser().resolve()
        if args.session_index
        else codex_home / "session_index.jsonl"
    )
    new_thread_name = " ".join(args.name).strip()

    try:
        result = rename_session_index_entry(
            codex_home=codex_home,
            session_index_path=session_index_path,
            target=args.target,
            new_thread_name=new_thread_name,
        )
    except (CliError, CodexStateError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    if not result.changed:
        print(
            encode_for_output(
                f"Session title already set: {result.session_id} - {result.new_thread_name}",
                sys.stdout.encoding,
            )
        )
        return 0

    print(encode_for_output(f"Renamed session: {result.session_id}", sys.stdout.encoding))
    if result.index_changed:
        print(encode_for_output(f"From: {result.old_thread_name}", sys.stdout.encoding))
    else:
        print(encode_for_output("Session index title was already set.", sys.stdout.encoding))
    print(encode_for_output(f"To: {result.new_thread_name}", sys.stdout.encoding))
    if result.rollout_changed:
        rollout_from = result.rollout_thread_name or "NO ROLLOUT TITLE EVENT"
        print(encode_for_output(f"Rollout title from: {rollout_from}", sys.stdout.encoding))
        if result.rollout_path is not None:
            print(f"Rollout file: {result.rollout_path}")
    if result.session_index_backup_path is not None:
        print(f"Session index backup: {result.session_index_backup_path}")
    if result.rollout_backup_path is not None:
        print(f"Rollout backup: {result.rollout_backup_path}")
    if result.state_cache_backups:
        print("State cache backups:")
        for backup in result.state_cache_backups:
            print(f"{backup.original_path} -> {backup.backup_path}")
    else:
        print("No Codex state cache files found to reset.")
    return 0


def import_index_action_label(action: str) -> str:
    if action == "add":
        return "add session_index.jsonl entry"
    if action == "update":
        return "update session_index.jsonl title"
    if action == "keep":
        return "keep existing session_index.jsonl entry"
    return action


def import_rollout_action_label(plan: ImportSessionPlan) -> str:
    if plan.rollout_will_be_rewritten:
        return "copy with rollout title event update"
    return "copy unchanged"


def format_import_plan_lines(plan: ImportSessionPlan) -> list[str]:
    lines = [
        f"Import source: {plan.source_path}",
        f"Session: {plan.session_id} - {plan.thread_name}",
        (
            "Started: "
            f"{format_local_timestamp(plan.started_at)} - "
            f"Updated: {format_local_timestamp(plan.ended_at)} "
            f"({local_timezone_offset_label(plan.ended_at or plan.started_at)})"
        ),
        f"Target rollout: {plan.target_path}",
        f"Source fingerprint: {format_fingerprint(plan.source_fingerprint)}",
        f"Index action: {import_index_action_label(plan.index_action)}",
        f"Rollout action: {import_rollout_action_label(plan)}",
        "State cache reset required after import.",
    ]
    if plan.existing_index_thread_name and plan.existing_index_thread_name != plan.thread_name:
        lines.insert(
            3,
            f"Existing session_index.jsonl title: {plan.existing_index_thread_name}",
        )
    return lines


def run_import_command(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.expanduser().resolve()
    session_index_path = (
        args.session_index.expanduser().resolve()
        if args.session_index
        else codex_home / "session_index.jsonl"
    )
    sessions_dir = (
        args.sessions_dir.expanduser().resolve() if args.sessions_dir else codex_home / "sessions"
    )

    if args.dry_run:
        try:
            plan = plan_bare_rollout_import(
                source_path=args.input,
                codex_home=codex_home,
                session_index_path=session_index_path,
                sessions_dir=sessions_dir,
                name=args.name,
            )
        except (CliError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        for line in format_import_plan_lines(plan):
            print(encode_for_output(line, sys.stdout.encoding))
        return 0

    try:
        result = import_bare_rollout(
            source_path=args.input,
            codex_home=codex_home,
            session_index_path=session_index_path,
            sessions_dir=sessions_dir,
            name=args.name,
        )
    except (CliError, CodexStateError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    plan = result.plan
    print(
        encode_for_output(
            f"Imported session: {plan.session_id} - {plan.thread_name}",
            sys.stdout.encoding,
        )
    )
    print(
        encode_for_output(f"Rollout: {plan.source_path} -> {plan.target_path}", sys.stdout.encoding)
    )
    print(
        encode_for_output(
            f"Index action: {import_index_action_label(plan.index_action)}", sys.stdout.encoding
        )
    )
    print(
        encode_for_output(
            f"Rollout action: {import_rollout_action_label(plan)}", sys.stdout.encoding
        )
    )
    if result.session_index_backup_path is not None:
        print(f"Session index backup: {result.session_index_backup_path}")
    if result.state_cache_backups:
        print("State cache backups:")
        for backup in result.state_cache_backups:
            print(f"{backup.original_path} -> {backup.backup_path}")
    else:
        print("No Codex state cache files found to reset.")
    return 0


def export_rollout_action_label(plan: ExportSessionPlan) -> str:
    if plan.rollout_will_be_rewritten:
        return "copy with rollout title event update"
    return "copy unchanged"


def format_export_plan_lines(plan: ExportSessionPlan) -> list[str]:
    lines = [
        f"Export source: {plan.source_path}",
        f"Session: {plan.session_id} - {plan.thread_name}",
        (
            "Started: "
            f"{format_local_timestamp(plan.started_at)} - "
            f"Updated: {format_local_timestamp(plan.ended_at)} "
            f"({local_timezone_offset_label(plan.ended_at or plan.started_at)})"
        ),
        f"Output rollout: {plan.output_path}",
        f"Rollout action: {export_rollout_action_label(plan)}",
    ]
    if plan.overwrite:
        lines.append("Overwrite: yes")
    return lines


def run_export_command(args: argparse.Namespace) -> int:
    codex_home = args.codex_home.expanduser().resolve()
    session_index_path = (
        args.session_index.expanduser().resolve()
        if args.session_index
        else codex_home / "session_index.jsonl"
    )
    sessions_dir = (
        args.sessions_dir.expanduser().resolve() if args.sessions_dir else codex_home / "sessions"
    )

    if args.dry_run:
        try:
            plan = plan_session_export(
                target=args.target,
                codex_home=codex_home,
                output=args.output,
                session_index_path=session_index_path,
                sessions_dir=sessions_dir,
                force=args.force,
            )
        except (CliError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        for line in format_export_plan_lines(plan):
            print(encode_for_output(line, sys.stdout.encoding))
        return 0

    try:
        result = export_session(
            target=args.target,
            codex_home=codex_home,
            output=args.output,
            session_index_path=session_index_path,
            sessions_dir=sessions_dir,
            force=args.force,
        )
    except (CliError, ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc

    plan = result.plan
    print(
        encode_for_output(
            f"Exported session: {plan.session_id} - {plan.thread_name}",
            sys.stdout.encoding,
        )
    )
    print(
        encode_for_output(f"Rollout: {plan.source_path} -> {plan.output_path}", sys.stdout.encoding)
    )
    print(
        encode_for_output(
            f"Rollout action: {export_rollout_action_label(plan)}", sys.stdout.encoding
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    prog = cli_prog_from_argv0()
    if raw_argv[:1] == ["list"]:
        return run_list_command(parse_list_args(raw_argv[1:], prog))
    if raw_argv[:1] in (["find"], ["grep"]):
        return run_search_command(parse_search_args(raw_argv[0], raw_argv[1:], prog))
    if raw_argv[:1] == ["repair-index"]:
        return run_repair_index_command(parse_repair_index_args(raw_argv[1:], prog))
    if raw_argv[:1] == ["rename"]:
        return run_rename_command(parse_rename_args(raw_argv[1:], prog))
    if raw_argv[:1] == ["import"]:
        return run_import_command(parse_import_args(raw_argv[1:], prog))
    if raw_argv[:1] == ["export"]:
        return run_export_command(parse_export_args(raw_argv[1:], prog))

    args = parse_args(raw_argv, prog)
    codex_home = args.codex_home.expanduser().resolve()
    try:
        markdown_features = parse_markdown_include(args.md_include)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.md_tool_preview_chars < 1:
        raise SystemExit("--md-tool-preview-chars must be greater than zero")

    output_format = infer_output_format(args)
    try:
        conversion_input = resolve_conversion_input(args.input, codex_home)
    except CliError as exc:
        raise SystemExit(str(exc)) from exc

    input_path = conversion_input.path
    output_path = resolve_output_path(
        args.output,
        input_path,
        codex_home,
        output_format,
        conversion_input.output_stem,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "md":
        tool_mode = resolve_markdown_tool_mode(markdown_features, args.md_tools)
        try:
            count = convert_jsonl_to_markdown(
                input_path=input_path,
                output_path=output_path,
                options=MarkdownOptions(
                    tool_mode=tool_mode,
                    tool_preview_chars=args.md_tool_preview_chars,
                    include_metadata="metadata" in markdown_features,
                    include_raw="raw" in markdown_features,
                    redaction=args.redact_encrypted,
                    image_mode=args.md_images,
                ),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Wrote {count} Markdown sections to {output_path}")
        return 0

    try:
        count = convert_jsonl_to_yaml_stream(
            input_path=input_path,
            output_path=output_path,
            redaction=args.redact_encrypted,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Wrote {count} YAML documents to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
