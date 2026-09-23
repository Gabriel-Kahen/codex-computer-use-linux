import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "sync_upstream.py"
SPEC = importlib.util.spec_from_file_location("sync_upstream", SCRIPT)
sync_upstream = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_upstream)


class MergeTextTests(unittest.TestCase):
    def test_merges_independent_local_and_remote_changes(self):
        merged, conflicted = sync_upstream.merge_text(
            b"ONE\ntwo\nthree\n",
            b"one\ntwo\nthree\n",
            b"one\ntwo\nTHREE\n",
        )

        self.assertFalse(conflicted)
        self.assertEqual(merged, b"ONE\ntwo\nTHREE\n")

    def test_marks_overlapping_changes_as_conflicts(self):
        merged, conflicted = sync_upstream.merge_text(
            b"LOCAL\n",
            b"base\n",
            b"REMOTE\n",
        )

        self.assertTrue(conflicted)
        self.assertIn(b"<<<<<<<", merged)
        self.assertIn(b"LOCAL", merged)
        self.assertIn(b"REMOTE", merged)

    def test_marks_multiple_overlapping_changes_as_conflicts(self):
        separator = b"same\n" * 10
        merged, conflicted = sync_upstream.merge_text(
            b"LOCAL ONE\n" + separator + b"LOCAL TWO\n",
            b"base one\n" + separator + b"base two\n",
            b"REMOTE ONE\n" + separator + b"REMOTE TWO\n",
        )

        self.assertTrue(conflicted)
        self.assertEqual(merged.count(b"<<<<<<<"), 2)


if __name__ == "__main__":
    unittest.main()
