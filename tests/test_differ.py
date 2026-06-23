import unittest

from backup.differ import compare
from backup.models import FileEntry, ScanResult


class DifferTests(unittest.TestCase):
    def test_compare_upload_move_and_trash(self):
        scan_a = ScanResult(
            files={
                "photos/new.txt": FileEntry("photos", "new.txt", 3, 1, "h1"),
                "photos/moved.txt": FileEntry("photos", "moved.txt", 3, 1, "h2"),
            }
        )
        scan_b = ScanResult(
            files={
                "photos/old.txt": FileEntry("photos", "old.txt", 3, 1, "h2"),
                "photos/extra.txt": FileEntry("photos", "extra.txt", 5, 1, "h3"),
            }
        )

        plan = compare(scan_a, scan_b)

        self.assertEqual([op.path for op in plan.uploads], ["photos/new.txt"])
        self.assertEqual([(op.old_path, op.new_path) for op in plan.moves], [("photos/old.txt", "photos/moved.txt")])
        self.assertEqual([op.path for op in plan.trashes], ["photos/extra.txt"])

    def test_same_path_same_hash_is_unchanged(self):
        entry = FileEntry("docs", "a.txt", 1, 1, "same")
        plan = compare(ScanResult(files={entry.path_key: entry}), ScanResult(files={entry.path_key: entry}))

        self.assertFalse(plan.uploads)
        self.assertFalse(plan.moves)
        self.assertFalse(plan.trashes)


if __name__ == "__main__":
    unittest.main()
