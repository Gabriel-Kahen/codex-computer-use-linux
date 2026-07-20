import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "upstream_sync_conflict_policy.py"
SPEC = importlib.util.spec_from_file_location("upstream_sync_conflict_policy", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class ConflictActionTest(unittest.TestCase):
    def test_cache_miss_reports_new_or_changed_scheduled_conflict(self) -> None:
        self.assertEqual(POLICY.conflict_action("schedule", False), "report")

    def test_repeated_scheduled_conflict_is_suppressed(self) -> None:
        self.assertEqual(POLICY.conflict_action("schedule", True), "suppress")

    def test_manual_conflict_is_reported_after_cache_hit(self) -> None:
        self.assertEqual(POLICY.conflict_action("workflow_dispatch", True), "report")


if __name__ == "__main__":
    unittest.main()
