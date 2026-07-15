import json
import multiprocessing
import os
import stat
import tempfile
from pathlib import Path
from queue import Empty
from unittest import TestCase
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import coordination


SESSION = {
    "uid": os.getuid(),
    "boot_id": "boot-test",
    "wayland_display": "wayland-test",
    "wayland_socket": {"device": 1, "inode": 2},
    "session_id": "session-test",
    "kwin_service_owner": ":1.42",
}


def window(window_id: str) -> dict:
    return {"id": window_id, "capture_id": window_id.strip("{}"), "title": window_id, "class": "test"}


def use_test_state(claims_dir: str) -> None:
    coordination.CLAIMS_DIR = Path(claims_dir)
    coordination.FOCUS_LEASE_FILE = Path(claims_dir).parent / "focus-lease.json"


def claim_worker(claims_dir: str, window_id: str, owner_id: str, start, finish, results) -> None:
    use_test_state(claims_dir)
    with patch.object(coordination, "current_session_identity", return_value=SESSION):
        start.wait(10)
        try:
            claim = coordination.claim_window(window(window_id), owner_id, 60)
            results.put(("ok", claim["claim_token"]))
        except Exception as exc:
            results.put(("error", str(exc)))
        finish.wait(10)


def crash_after_claim_worker(claims_dir: str, window_id: str, results) -> None:
    use_test_state(claims_dir)
    with patch.object(coordination, "current_session_identity", return_value=SESSION):
        claim = coordination.claim_window(window(window_id), "crashed-owner", 60)
        results.put(claim["claim_token"])


def hold_window_action_worker(claims_dir: str, window_id: str, ready, finish) -> None:
    use_test_state(claims_dir)
    with coordination.window_action(window_id, "holder"):
        ready.set()
        finish.wait(10)


def probe_window_action_worker(claims_dir: str, window_id: str, results) -> None:
    use_test_state(claims_dir)
    with coordination.window_action(window_id, "probe"):
        results.put("entered")


def hold_claimed_action_worker(claims_dir: str, window_id: str, token: str, ready, finish) -> None:
    use_test_state(claims_dir)
    with (
        patch.object(coordination, "current_session_identity", return_value=SESSION),
        coordination.window_action(window_id, "owner-a", token),
    ):
        ready.set()
        finish.wait(10)


def release_claim_worker(claims_dir: str, token: str, started, results) -> None:
    use_test_state(claims_dir)
    with patch.object(coordination, "current_session_identity", return_value=SESSION):
        started.set()
        results.put(coordination.release_window_claim(token, "owner-a"))


class CoordinationTests(TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.claims_dir = Path(self.directory.name) / "claims"
        self.claims_patch = patch.object(coordination, "CLAIMS_DIR", self.claims_dir)
        self.claims_patch.start()
        self.focus_file = Path(self.directory.name) / "focus-lease.json"
        self.focus_patch = patch.object(coordination, "FOCUS_LEASE_FILE", self.focus_file)
        self.focus_patch.start()
        self.session_patch = patch.object(coordination, "current_session_identity", return_value=SESSION)
        self.session_patch.start()

    def tearDown(self) -> None:
        self.session_patch.stop()
        self.focus_patch.stop()
        self.claims_patch.stop()
        self.directory.cleanup()

    def test_claims_fail_closed_when_process_identity_is_unverifiable(self) -> None:
        with patch.object(
            coordination,
            "process_identity",
            return_value={"pid": 123, "start_time": None, "state": None},
        ):
            with self.assertRaisesRegex(RuntimeError, "positively verified"):
                coordination.claim_window(window("{shared}"), "owner-a", 60)

    def test_different_windows_can_be_claimed_by_parallel_processes(self) -> None:
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        finish = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=claim_worker,
                args=(str(self.claims_dir), f"{{window-{index}}}", f"owner-{index}", start, finish, results),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=10) for _process in processes]
        finish.set()
        for process in processes:
            process.join(10)

        self.assertEqual([outcome[0] for outcome in outcomes], ["ok", "ok"])
        self.assertEqual([process.exitcode for process in processes], [0, 0])

    def test_same_window_claim_race_has_exactly_one_winner(self) -> None:
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        finish = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=claim_worker,
                args=(str(self.claims_dir), "{shared}", f"owner-{index}", start, finish, results),
            )
            for index in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=10) for _process in processes]
        finish.set()
        for process in processes:
            process.join(10)

        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["error", "ok"])
        self.assertIn("another agent", next(outcome[1] for outcome in outcomes if outcome[0] == "error"))
        self.assertEqual([process.exitcode for process in processes], [0, 0])

    def test_different_window_actions_do_not_share_a_process_lock(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        finish = context.Event()
        results = context.Queue()
        holder = context.Process(
            target=hold_window_action_worker,
            args=(str(self.claims_dir), "{one}", ready, finish),
        )
        probe = context.Process(
            target=probe_window_action_worker,
            args=(str(self.claims_dir), "{two}", results),
        )
        holder.start()
        self.assertTrue(ready.wait(10))
        probe.start()

        self.assertEqual(results.get(timeout=5), "entered")
        finish.set()
        holder.join(10)
        probe.join(10)
        self.assertEqual([holder.exitcode, probe.exitcode], [0, 0])

    def test_dead_owner_claim_is_recovered_before_expiry(self) -> None:
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        process = context.Process(
            target=crash_after_claim_worker,
            args=(str(self.claims_dir), "{shared}", results),
        )
        process.start()
        old_token = results.get(timeout=10)
        process.join(10)

        replacement = coordination.claim_window(window("{shared}"), "replacement", 60)

        self.assertEqual(process.exitcode, 0)
        self.assertNotEqual(replacement["claim_token"], old_token)

    def test_nonowner_cannot_release_a_live_claim(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)

        with self.assertRaisesRegex(RuntimeError, "another agent"):
            coordination.release_window_claim(claim["claim_token"], "owner-b")

        self.assertEqual(coordination.list_claims("owner-a", 0, 20)["total"], 1)

    def test_same_owner_renews_the_existing_claim_token(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 30)

        renewed = coordination.claim_window(window("{shared}"), "owner-a", 90, claim["claim_token"])

        self.assertEqual(renewed["claim_token"], claim["claim_token"])
        self.assertTrue(renewed["renewed"])
        self.assertEqual(renewed["lease_seconds"], 90)

    def test_focus_cleanup_does_not_remove_an_implicit_claim_converted_to_explicit(self) -> None:
        implicit = coordination.ensure_focus_claim(window("{shared}"), "owner-a", None, 60)
        coordination.claim_window(window("{shared}"), "owner-a", 60, implicit["claim_token"])

        released = coordination.discard_bound_claim(
            implicit["claim_token"],
            "{shared}",
            "owner-a",
        )

        self.assertFalse(released)
        self.assertEqual(coordination.list_claims("owner-a", 0, 20)["total"], 1)

    def test_expired_claim_can_be_recovered_by_another_owner(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)
        key = claim["claim_token"].split(".", 1)[0]
        path = self.claims_dir / f"{key}.json"
        record = coordination.read_private_json(path)
        assert record is not None
        record["expires_at"] = 0
        coordination.write_private_json(path, record)

        replacement = coordination.claim_window(window("{shared}"), "owner-b", 60)

        self.assertNotEqual(replacement["claim_token"], claim["claim_token"])
        self.assertEqual(replacement["owner_thread_id"], "owner-b")
        self.assertFalse(replacement["renewed"])

    def test_list_redacts_tokens_and_state_is_private(self) -> None:
        coordination.claim_window(window("{shared}"), "private-thread-id", 60)

        result = coordination.list_claims("other-owner", 0, 20)

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["claims"][0]["owner_thread_id"], "private-thread-id")
        self.assertNotIn("claim_token", result["claims"][0])
        self.assertEqual(result["claims"][0]["lease_seconds"], 60)
        claim_file = next(self.claims_dir.glob("*.json"))
        self.assertEqual(stat.S_IMODE(self.claims_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(claim_file.stat().st_mode), 0o600)

    def test_claim_state_from_another_kwin_session_is_replaced(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)
        changed = {**SESSION, "kwin_service_owner": ":1.99"}

        with patch.object(coordination, "current_session_identity", return_value=changed):
            replacement = coordination.claim_window(window("{shared}"), "owner-b", 60)

        self.assertNotEqual(replacement["claim_token"], claim["claim_token"])

    def test_active_claim_registry_has_a_hard_cap(self) -> None:
        with patch.object(coordination, "MAX_ACTIVE_CLAIMS", 1):
            coordination.claim_window(window("{one}"), "owner-a", 60)
            with self.assertRaisesRegex(RuntimeError, "at most 1"):
                coordination.claim_window(window("{two}"), "owner-b", 60)

    def test_same_owner_actions_and_renewals_require_the_claim_token(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)

        with self.assertRaisesRegex(ValueError, "requires its claim_token"):
            coordination.claim_window(window("{shared}"), "owner-a", 60)
        with self.assertRaisesRegex(ValueError, "requires its claim_token"):
            coordination.authorize_window("{shared}", "owner-a")
        with self.assertRaisesRegex(ValueError, "requires its claim_token"):
            coordination.ensure_focus_claim(window("{shared}"), "owner-a", None, 60)

        self.assertIsNotNone(coordination.authorize_window("{shared}", "owner-a", claim["claim_token"]))

    def test_invalid_claim_token_is_bounded_and_never_echoed(self) -> None:
        invalid = "private-invalid-token"

        with self.assertRaises(ValueError) as caught:
            coordination.release_window_claim(invalid, "owner-a")

        self.assertNotIn(invalid, str(caught.exception))
        self.assertLess(len(str(caught.exception)), 100)

    def test_thread_and_window_inputs_have_character_and_byte_bounds(self) -> None:
        self.assertEqual(coordination.parse_thread_id("agent"), "agent")
        with self.assertRaisesRegex(ValueError, "size limit"):
            coordination.parse_thread_id("💥" * coordination.MAX_THREAD_ID_CHARS)
        with self.assertRaisesRegex(ValueError, "size limit"):
            coordination.require_window_query("💥" * (coordination.MAX_WINDOW_QUERY_CHARS + 1))
        with self.assertRaisesRegex(ValueError, "blank"):
            coordination.require_window_query("   ")

    def test_claim_list_is_compact_token_free_and_serialized_byte_bounded(self) -> None:
        self.assertEqual(coordination.MAX_CLAIM_LIST_BYTES, 2 * 1024)
        for index in range(3):
            details = window(f"{{window-{index}}}")
            details["title"] = "\x01" * coordination.MAX_WINDOW_TITLE_CHARS
            coordination.claim_window(details, f"owner-{index}", 60)
        first = coordination.list_claims("observer", 0, 1)["claims"][0]
        one_record_page = {
            "claims": [first],
            "total": 3,
            "next_offset": 1,
            "truncated": True,
            "registry_truncated": False,
        }
        cap = coordination.serialized_size(one_record_page)

        with patch.object(coordination, "MAX_CLAIM_LIST_BYTES", cap):
            result = coordination.list_claims("observer", 0, 20)

        self.assertEqual(result, one_record_page)
        self.assertLessEqual(coordination.serialized_size(result), cap)
        self.assertEqual(
            set(result["claims"][0]),
            {"window", "owner_thread_id", "claimed_at", "expires_at", "lease_seconds"},
        )
        self.assertEqual(set(result["claims"][0]["window"]), {"id", "capture_id", "title", "class"})

    def test_unicode_claim_pages_are_bounded_and_always_advance(self) -> None:
        rich = {
            "id": "💥" * coordination.MAX_WINDOW_ID_CHARS,
            "capture_id": "🧪" * coordination.MAX_WINDOW_ID_CHARS,
            "title": "\x01💥" * coordination.MAX_WINDOW_TITLE_CHARS,
            "class": "\\🧪" * coordination.MAX_WINDOW_CLASS_CHARS,
        }
        coordination.claim_window(
            rich,
            "👩‍💻" * (coordination.MAX_THREAD_ID_BYTES // len("👩‍💻".encode())),
            60,
        )
        coordination.claim_window(window("{ordinary}"), "owner-b", 60)

        offset = 0
        seen: list[str] = []
        while True:
            page = coordination.list_claims("observer", offset, 20)
            self.assertTrue(page["claims"])
            self.assertLessEqual(
                coordination.serialized_size(page),
                coordination.MAX_CLAIM_LIST_BYTES,
            )
            self.assertLessEqual(
                len(json.dumps(page, ensure_ascii=False, indent=2).encode()),
                coordination.MAX_CLAIM_LIST_BYTES,
            )
            seen.extend(claim["window"]["id"] for claim in page["claims"])
            next_offset = page["next_offset"]
            if next_offset is None:
                break
            self.assertGreater(next_offset, offset)
            offset = next_offset

        self.assertEqual(set(seen), {rich["id"], "{ordinary}"})

    def test_single_oversized_claim_fails_instead_of_repeating_the_cursor(self) -> None:
        coordination.claim_window(window("{shared}"), "owner-a", 60)

        with (
            patch.object(coordination, "MAX_CLAIM_LIST_BYTES", 1),
            self.assertRaisesRegex(RuntimeError, "serialized list size limit"),
        ):
            coordination.list_claims("observer", 0, 20)

    def test_focus_journal_reserves_claim_after_expiry_and_blocks_release(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)
        key = claim["claim_token"].split(".", 1)[0]
        path = self.claims_dir / f"{key}.json"
        record = coordination.read_private_json(path)
        assert record is not None
        record["expires_at"] = 0
        coordination.write_private_json(path, record)
        focus_state = {
            "version": 3,
            "token": "A" * 24,
            "phase": "active",
            "session_identity": SESSION,
            "owner": record["owner"],
            "window_claim": {
                "claim_token": claim["claim_token"],
                "window_id": "{shared}",
                "implicit": False,
            },
            "target": window("{shared}"),
            "binding": {
                "target_window_id": "{shared}",
                "owner_thread_id": "owner-a",
                "session_identity": SESSION,
                "claim_token": claim["claim_token"],
            },
        }
        coordination.write_private_json(self.focus_file, focus_state)

        with self.assertRaisesRegex(RuntimeError, "another agent"):
            coordination.claim_window(window("{shared}"), "owner-b", 60)
        with self.assertRaisesRegex(RuntimeError, "reserved"):
            coordination.release_window_claim(claim["claim_token"], "owner-a")

        renewed = coordination.claim_window(window("{shared}"), "owner-a", 60, claim["claim_token"])
        self.assertTrue(renewed["renewed"])

    def test_invalid_v3_focus_binding_is_checked_before_an_unrelated_target_shortcut(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)
        key = claim["claim_token"].split(".", 1)[0]
        path = self.claims_dir / f"{key}.json"
        record = coordination.read_private_json(path)
        assert record is not None
        coordination.write_private_json(
            self.focus_file,
            {
                "version": 3,
                "token": "A" * 24,
                "phase": "active",
                "session_identity": SESSION,
                "owner": record["owner"],
                "window_claim": {
                    "claim_token": claim["claim_token"],
                    "window_id": "{other}",
                    "implicit": False,
                },
                "target": window("{other}"),
            },
        )

        with self.assertRaisesRegex(RuntimeError, "focus lease binding is invalid"):
            coordination.release_window_claim(claim["claim_token"], "owner-a")

        self.assertTrue(path.exists())

    def test_mutating_an_immutable_claim_binding_fails_closed(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)
        key = claim["claim_token"].split(".", 1)[0]
        path = self.claims_dir / f"{key}.json"
        record = coordination.read_private_json(path)
        assert record is not None
        record["owner"]["thread_id"] = "owner-b"
        coordination.write_private_json(path, record)

        with self.assertRaisesRegex(RuntimeError, "binding is invalid"):
            coordination.list_claims("observer", 0, 20)

    def test_corrupt_focus_journal_fails_closed_before_expired_claim_takeover(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)
        key = claim["claim_token"].split(".", 1)[0]
        path = self.claims_dir / f"{key}.json"
        record = coordination.read_private_json(path)
        assert record is not None
        record["expires_at"] = 0
        coordination.write_private_json(path, record)
        self.focus_file.write_text("{")

        with self.assertRaises(json.JSONDecodeError):
            coordination.claim_window(window("{shared}"), "owner-b", 60)

        self.assertTrue(path.exists())

    def test_release_waits_for_an_in_flight_window_action(self) -> None:
        claim = coordination.claim_window(window("{shared}"), "owner-a", 60)
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        finish = context.Event()
        started = context.Event()
        results = context.Queue()
        holder = context.Process(
            target=hold_claimed_action_worker,
            args=(str(self.claims_dir), "{shared}", claim["claim_token"], ready, finish),
        )
        releaser = context.Process(
            target=release_claim_worker,
            args=(str(self.claims_dir), claim["claim_token"], started, results),
        )
        holder.start()
        self.assertTrue(ready.wait(10))
        releaser.start()
        self.assertTrue(started.wait(10))
        with self.assertRaises(Empty):
            results.get(timeout=0.5)

        finish.set()
        self.assertTrue(results.get(timeout=10)["released"])
        holder.join(10)
        releaser.join(10)
        self.assertEqual([holder.exitcode, releaser.exitcode], [0, 0])
