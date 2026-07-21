import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "upstream_sync_conflict_policy.py"
PROTECTED_PATHS_FILE = Path(__file__).parents[2] / "upstream-sync-protected-paths.txt"
REPO_ROOT = Path(__file__).parents[3]
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


class ProtectedPathsTest(unittest.TestCase):
    def test_fork_owned_project_documents_are_protected(self) -> None:
        protected_paths = {
            line
            for raw_line in PROTECTED_PATHS_FILE.read_text().splitlines()
            if (line := raw_line.strip()) and not line.startswith("#")
        }
        expected_paths = {
            ".github/pull_request_template.md",
            ".github/upstream-sync-protected-paths.txt",
            ".github/workflows/upstream-sync.yml",
            "CONTRIBUTING.md",
            "LICENSE",
            "NOTICE",
            "README.md",
            "SECURITY.md",
            "docs/contributing.md",
        }

        self.assertEqual(protected_paths, expected_paths)
        self.assertEqual(
            [path for path in sorted(protected_paths) if not (REPO_ROOT / path).is_file()],
            [],
        )


if __name__ == "__main__":
    unittest.main()
