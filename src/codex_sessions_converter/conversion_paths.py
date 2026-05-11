import argparse
from dataclasses import dataclass
from pathlib import Path

from codex_sessions_converter.errors import CliError
from codex_sessions_converter.session_files import (
    discover_session_files,
    format_session_file_path,
)
from codex_sessions_converter.session_index import (
    is_session_id,
    normalize_session_id,
)


@dataclass(frozen=True)
class ConversionInput:
    path: Path
    output_stem: str | None


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
