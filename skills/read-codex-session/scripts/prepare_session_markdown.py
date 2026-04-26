#!/usr/bin/env python
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def iter_rollouts(codex_home: Path) -> list[Path]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.rglob("rollout-*.jsonl"))


def newest(paths: list[Path]) -> Path:
    if not paths:
        raise SystemExit("No matching Codex rollout JSONL files found.")
    return max(paths, key=lambda path: path.stat().st_mtime)


def index_session_ids(codex_home: Path, query: str) -> set[str]:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.exists():
        return set()

    query_lower = query.lower()
    matches: set[str] = set()
    with index_path.open("r", encoding="utf-8") as index_file:
        for raw_line in index_file:
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            session_id = str(record.get("id", ""))
            thread_name = str(record.get("thread_name", ""))
            if query_lower in session_id.lower() or query_lower in thread_name.lower():
                matches.add(session_id)
    return matches


def resolve_session(target: str, codex_home: Path) -> Path:
    direct_paths = [
        Path(target).expanduser(),
        codex_home / target,
    ]
    for direct_path in direct_paths:
        if direct_path.exists():
            return direct_path.resolve()

    rollouts = iter_rollouts(codex_home)
    if target.lower() in {"latest", "newest", "last"}:
        return newest(rollouts).resolve()

    target_lower = target.lower()
    candidates = [
        path
        for path in rollouts
        if target_lower in path.name.lower()
        or target_lower in str(path.relative_to(codex_home / "sessions")).lower()
    ]

    session_ids = index_session_ids(codex_home, target)
    if session_ids:
        candidates.extend(
            path for path in rollouts if any(session_id in path.name for session_id in session_ids)
        )

    unique_candidates = list(dict.fromkeys(candidates))
    if not unique_candidates:
        raise SystemExit(f"No rollout found for target: {target}")
    if len(unique_candidates) > 1:
        print(
            f"Found {len(unique_candidates)} matching rollouts; using newest.",
            file=sys.stderr,
        )
    return newest(unique_candidates).resolve()


def default_output_path(input_path: Path, codex_home: Path, output_format: str) -> Path:
    suffix = ".yaml" if output_format == "yaml" else ".md"
    try:
        relative_input = input_path.relative_to(codex_home)
    except ValueError:
        relative_input = Path(input_path.name)
    return (codex_home / "tmp" / relative_input).with_suffix(suffix)


def converter_command(converter: str) -> list[str]:
    resolved = shutil.which(converter)
    if resolved:
        return [resolved]
    return [sys.executable, "-m", "codex_sessions_converter"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve a Codex rollout and convert it to compact Markdown."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="latest",
        help="Rollout path, id/name fragment, or 'latest'. Default: latest.",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=default_codex_home(),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument("--output", type=Path, help="Output path. Defaults under tmp/sessions.")
    parser.add_argument(
        "--format",
        choices=("md", "yaml"),
        default="md",
        help="Output format. Default: md.",
    )
    parser.add_argument(
        "--md-include",
        default="default",
        help="Markdown include preset/modifiers passed to codex-sessions-converter.",
    )
    parser.add_argument(
        "--md-tools",
        choices=("auto", "none", "names", "preview", "full"),
        default="names",
        help="Markdown tool detail mode. Default: names.",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=700,
        help="Preview characters for --md-tools preview. Default: 700.",
    )
    parser.add_argument(
        "--converter",
        default="codex-sessions-converter",
        help="Converter command name/path. Default: codex-sessions-converter.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    input_path = resolve_session(args.target, codex_home)
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else default_output_path(input_path, codex_home, args.format).resolve()
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        *converter_command(args.converter),
        "--format",
        args.format,
        "--md-include",
        args.md_include,
        "--md-tools",
        args.md_tools,
        "--md-tool-preview-chars",
        str(args.preview_chars),
        str(input_path),
        str(output_path),
    ]
    subprocess.run(command, check=True)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
