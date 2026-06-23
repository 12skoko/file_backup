import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backup.config import SourcePath
from backup.ignore import load_ignore_rules
from backup.scanner import scan_paths


class ScannerTests(unittest.TestCase):
    def test_scan_paths_hashes_files_and_ignores_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "src"
            root.mkdir()
            (root / "keep.txt").write_text("hello", encoding="utf-8")
            (root / ".DS_Store").write_text("ignored", encoding="utf-8")
            (root / "empty").mkdir()

            result = scan_paths([SourcePath("main", root)], tmp_path / "cache", load_ignore_rules(tmp_path / ".backupignore"))

            self.assertIn("main/keep.txt", result.files)
            self.assertNotIn("main/.DS_Store", result.files)
            self.assertIn("main/empty", {entry.path_key for entry in result.dirs})

    def test_scan_paths_skips_files_that_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "src"
            root.mkdir()
            (root / "stale.txt").write_text("gone", encoding="utf-8")

            with patch("backup.scanner.hash_file", side_effect=OSError("stale file handle")):
                result = scan_paths([SourcePath("main", root)], tmp_path / "cache", load_ignore_rules(tmp_path / ".backupignore"))

            self.assertNotIn("main/stale.txt", result.files)


if __name__ == "__main__":
    unittest.main()
