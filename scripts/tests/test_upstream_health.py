import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "health", Path(__file__).resolve().parents[1] / "report_upstream_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class HealthTests(unittest.TestCase):
    def run_report(self, issues, failed=True):
        calls = []
        bodies = []

        def command(*args):
            calls.append(args)
            if args[0] == "api":
                return json.dumps([issues])
            if "--body-file" in args:
                bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
            return ""

        result = health.reconcile(failed, "owner/repo", "https://example/run/1",
                                  {"prepare": {"result": "failure"}}, command)
        return result, calls, bodies

    def issue(self, number=1, state="open"):
        return {"number": number, "state": state, "body": health.MARKER}

    def test_first_failure_creates_actionable_issue_and_fails(self):
        result, calls, bodies = self.run_report([])
        self.assertEqual(result, 1)
        self.assertEqual(calls[-1][:2], ("issue", "create"))
        self.assertIn("https://example/run/1", bodies[0])
        self.assertIn("sync_upstream.py prepare", bodies[0])
        self.assertIn("**failure**", bodies[0])

    def test_repeated_failure_updates_single_issue_and_stays_failed(self):
        result, calls, _ = self.run_report([self.issue()])
        self.assertEqual(result, 1)
        self.assertEqual(calls[-1][:3], ("issue", "edit", "1"))
        self.assertFalse(any(call[:2] == ("issue", "create") for call in calls))

    def test_new_failure_reopens_existing_issue(self):
        result, calls, _ = self.run_report([self.issue(state="closed")])
        self.assertEqual(result, 1)
        self.assertEqual(calls[1][:3], ("issue", "reopen", "1"))
        self.assertEqual(calls[2][:3], ("issue", "edit", "1"))

    def test_recovery_closes_open_issues_only(self):
        result, calls, _ = self.run_report([self.issue(), self.issue(2, "closed")], False)
        self.assertEqual(result, 0)
        self.assertEqual(calls[-1][:3], ("issue", "close", "1"))
        self.assertEqual(len(calls), 2)

    def test_healthy_without_issue_does_not_create_one(self):
        result, calls, _ = self.run_report([], False)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)

    def test_prefers_open_issue_over_closed_history(self):
        _, calls, _ = self.run_report([self.issue(1, "closed"), self.issue(2)])
        self.assertEqual(calls[-1][:3], ("issue", "edit", "2"))

    def test_ignores_pull_requests_with_marker(self):
        issue = {**self.issue(), "pull_request": {"url": "example"}}
        _, calls, _ = self.run_report([issue])
        self.assertEqual(calls[-1][:2], ("issue", "create"))

    def test_api_failure_is_not_hidden(self):
        def broken(*args):
            raise RuntimeError("API permission denied")
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            health.reconcile(True, "owner/repo", "url", {}, broken)


if __name__ == "__main__":
    unittest.main()
