import argparse
import tempfile
import unittest
from pathlib import Path

from codex_sessions_converter.conversion_paths import (
    default_output_path,
    infer_output_format,
    output_filename,
    resolve_conversion_input,
    resolve_output_path,
)
from codex_sessions_converter.errors import CliError


class ConversionPathsTests(unittest.TestCase):
    def test_infer_output_format_prefers_explicit_flags_and_output_suffix(self) -> None:
        self.assertEqual(
            infer_output_format(
                argparse.Namespace(md=True, yaml=False, format=None, output=Path("out.yaml"))
            ),
            "md",
        )
        self.assertEqual(
            infer_output_format(
                argparse.Namespace(md=False, yaml=True, format="md", output=Path("out.md"))
            ),
            "yaml",
        )
        self.assertEqual(
            infer_output_format(
                argparse.Namespace(md=False, yaml=False, format="markdown", output=Path("out.yaml"))
            ),
            "md",
        )
        self.assertEqual(
            infer_output_format(
                argparse.Namespace(md=False, yaml=False, format=None, output=Path("out.md"))
            ),
            "md",
        )

    def test_output_filename_uses_stem_and_jsonl_suffix(self) -> None:
        self.assertEqual(output_filename(Path("rollout.jsonl")), "rollout.yaml")
        self.assertEqual(output_filename(Path("rollout.jsonl"), "md"), "rollout.md")
        self.assertEqual(output_filename(Path("rollout.txt"), "yaml"), "rollout.txt.yaml")
        self.assertEqual(
            output_filename(Path("rollout.jsonl"), "yaml", "session-id"), "session-id.yaml"
        )

    def test_default_and_directory_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            codex_home = root / ".codex"
            input_path = codex_home / "sessions" / "2026" / "04" / "30" / "rollout.jsonl"
            output_dir = root / "out"
            output_dir.mkdir()

            self.assertEqual(
                default_output_path(input_path, codex_home, "yaml"),
                codex_home / "tmp" / "sessions" / "2026" / "04" / "30" / "rollout.yaml",
            )
            self.assertEqual(
                resolve_output_path(output_dir, input_path, codex_home, "md", "abc"),
                output_dir / "abc.md",
            )

    def test_resolve_conversion_input_reports_missing_file_without_stack_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.jsonl"

            with self.assertRaises(CliError) as raised:
                resolve_conversion_input(missing, Path(tmpdir) / ".codex")

        self.assertEqual(str(raised.exception), f"Input file not found: {missing}")


if __name__ == "__main__":
    unittest.main()
