import json
import multiprocessing
import stat
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import coordination


BINDING = {
    "uid": 1000,
    "xdg_runtime_dir": "/run/user/1000",
    "wayland_display": "wayland-1",
    "hyprland_instance": "hypr-instance",
    "xwayland_display": ":1",
}
WINDOW = {
    "address": "0x1",
    "capture_id": "42",
    "class": "demo",
    "title": "Demo",
    "pid": 123,
    "process_start_time": 456,
    "workspace": 1,
    "xwayland": False,
}


def claim_in_process(
    directory: str,
    owner: str,
    start: Any,
    results: Any,
) -> None:
    root = Path(directory)
    coordination.CLAIMS_FILE = root / "claims.json"
    coordination.CLAIMS_LOCK_FILE = root / "claims.lock"
    coordination.WINDOW_LOCK_DIR = root / "window-locks"
    start.wait()
    try:
        claim = coordination.claim_window(BINDING, WINDOW, owner, now=1000)
        results.put(("claimed", claim["owner_thread_id"]))
    except Exception as exc:
        results.put(("rejected", str(exc)))


class ClaimStoreTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.patches = (
            patch.object(coordination, "CLAIMS_FILE", root / "claims.json"),
            patch.object(coordination, "CLAIMS_LOCK_FILE", root / "claims.lock"),
            patch.object(coordination, "WINDOW_LOCK_DIR", root / "window-locks"),
        )
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temporary.cleanup()

    def test_window_lock_key_matches_generic_backend_address_identity(self) -> None:
        self.assertEqual(coordination.window_lock_key(WINDOW), "address:0x1")
        self.assertEqual(
            coordination.window_lock_key({**WINDOW, "capture_id": "replacement"}),
            "address:0x1",
        )
        self.assertEqual(coordination.window_key(WINDOW), "capture:42")

    def test_same_owner_renews_without_changing_the_fencing_token(self) -> None:
        first = coordination.claim_window(BINDING, WINDOW, "thread-a", 30, now=1000)
        with self.assertRaisesRegex(ValueError, "current claim_token is required"):
            coordination.claim_window(BINDING, WINDOW, "thread-a", 60, now=1010)
        renewed = coordination.claim_window(
            BINDING, WINDOW, "thread-a", 60, first["claim_token"], now=1010
        )

        self.assertEqual(first["claim_token"], renewed["claim_token"])
        self.assertFalse(first["renewed"])
        self.assertTrue(renewed["renewed"])
        self.assertEqual(renewed["expires_at"], 1070)
        self.assertEqual((first["fencing_token"], renewed["fencing_token"]), (1, 1))

    def test_v2_state_uses_canonical_session_window_keys(self) -> None:
        coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)
        state = json.loads(coordination.CLAIMS_FILE.read_text())
        session = state["sessions"][coordination.session_key(BINDING)]
        claim = session["claims"][coordination.protocol_window_key(BINDING, WINDOW)]
        self.assertEqual((state["version"], claim["window"]["identity"]["id"]), (2, "0x1"))
        claim["window"].pop("summary")
        coordination._validate_state(state)

    def test_live_v1_state_fails_closed_and_expired_v1_migrates_one_way(self) -> None:
        legacy_claim = {
            "owner_thread_id": "legacy-owner", "claim_token": "legacy-token",
            "claimed_at": time.time(), "expires_at": time.time() + 60,
            "lease_seconds": 60, "window": {"address": "0x1"},
        }
        legacy = {
            "version": 1,
            "sessions": {"legacy": {"binding": BINDING, "claims": {"capture:42": legacy_claim}}},
        }
        coordination.atomic_write_json(coordination.CLAIMS_FILE, legacy)
        with self.assertRaisesRegex(RuntimeError, "version-1 claims are still active"):
            coordination.claim_window(BINDING, WINDOW, "thread-a")
        legacy_claim.pop("expires_at")
        coordination.atomic_write_json(coordination.CLAIMS_FILE, legacy)
        with self.assertRaisesRegex(RuntimeError, "legacy window claim state is malformed"):
            coordination.claim_window(BINDING, WINDOW, "thread-a")
        legacy_claim["expires_at"] = 0
        coordination.atomic_write_json(coordination.CLAIMS_FILE, legacy)
        coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)

        self.assertEqual(
            json.loads(coordination.CLAIMS_FILE.read_text())["version"], 2
        )

    def test_capture_identity_rotation_keeps_one_address_claim(self) -> None:
        first = coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)
        replacement = {**WINDOW, "capture_id": "43"}
        renewed = coordination.claim_window(
            BINDING, replacement, "thread-a", claim_token=first["claim_token"], now=1001
        )

        self.assertEqual(renewed["claim_token"], first["claim_token"])
        self.assertEqual(len(coordination.list_claims(BINDING, now=1002)), 1)

    def test_claimed_operation_renews_ownership(self) -> None:
        claim = coordination.claim_window(BINDING, WINDOW, "thread-a", 30, now=1000)

        coordination.require_window_access(
            BINDING, WINDOW, "thread-a", claim["claim_token"], now=1020
        )

        self.assertEqual(coordination.list_claims(BINDING, now=1040)[0]["expires_at"], 1050)

    def test_active_claim_requires_its_current_fencing_token(self) -> None:
        coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)

        with self.assertRaisesRegex(ValueError, "claim_token is required"):
            coordination.require_window_access(
                BINDING, WINDOW, "thread-a", None, now=1001
            )

    def test_inflight_operation_survives_minimum_ttl_and_renews_on_completion(self) -> None:
        claim = coordination.claim_window(BINDING, WINDOW, "thread-a", 5, now=1000)
        access = coordination.require_window_access(
            BINDING,
            WINDOW,
            "thread-a",
            claim["claim_token"],
            mark_inflight=True,
            now=1004,
        )

        self.assertEqual(len(coordination.list_claims(BINDING, now=1010)), 1)
        finished = coordination.finish_window_access(
            BINDING,
            WINDOW,
            "thread-a",
            access["claim_token"],
            renew=True,
            now=1010,
        )

        self.assertEqual(finished["expires_at"], 1015)

    def test_claim_ttl_starts_after_blocking_synchronization(self) -> None:
        with patch.object(coordination.time, "time", side_effect=[1000, 1010]):
            claim = coordination.claim_window(
                BINDING,
                WINDOW,
                "thread-a",
                5,
                after_claim=lambda _: None,
            )

        self.assertEqual(claim["expires_at"], 1015)
        self.assertEqual(len(coordination.list_claims(BINDING, now=1011)), 1)

    def test_live_claim_rejects_another_owner_and_foreign_release(self) -> None:
        claim = coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)

        with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
            coordination.claim_window(BINDING, WINDOW, "thread-b", now=1001)
        with self.assertRaisesRegex(RuntimeError, "owns a live window claim"):
            coordination.release_claim(
                BINDING, claim["claim_token"], "thread-b", now=1001
            )

    def test_expiry_allows_another_owner_to_claim(self) -> None:
        first = coordination.claim_window(BINDING, WINDOW, "thread-a", 5, now=1000)
        second = coordination.claim_window(BINDING, WINDOW, "thread-b", 5, now=1005)

        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertEqual(second["owner_thread_id"], "thread-b")
        self.assertEqual((first["fencing_token"], second["fencing_token"]), (1, 2))

    def test_release_is_idempotent_and_list_does_not_disclose_tokens(self) -> None:
        claim = coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)
        listed = coordination.list_claims(BINDING, now=1001)
        first = coordination.release_claim(
            BINDING, claim["claim_token"], "thread-a", now=1001
        )
        second = coordination.release_claim(
            BINDING, claim["claim_token"], "thread-a", now=1001
        )

        self.assertNotIn("claim_token", listed[0])
        self.assertTrue(first["released"])
        self.assertFalse(second["released"])
        self.assertNotIn("claim_token", second)

    def test_release_rejects_overlong_untrusted_tokens(self) -> None:
        with self.assertRaisesRegex(ValueError, "1..256"):
            coordination.release_claim(BINDING, "x" * 257, "thread-a", now=1000)

    def test_release_waits_for_an_inflight_window_operation(self) -> None:
        claim = coordination.claim_window(BINDING, WINDOW, "thread-a")
        operation_started = threading.Event()
        allow_completion = threading.Event()
        released: list[dict[str, object]] = []

        def operate() -> None:
            with coordination.window_guard(BINDING, WINDOW):
                access = coordination.require_window_access(
                    BINDING,
                    WINDOW,
                    "thread-a",
                    claim["claim_token"],
                    mark_inflight=True,
                )
                operation_started.set()
                allow_completion.wait(timeout=2)
                coordination.finish_window_access(
                    BINDING,
                    WINDOW,
                    "thread-a",
                    access["claim_token"],
                    renew=True,
                )

        operation = threading.Thread(target=operate)
        release = threading.Thread(
            target=lambda: released.append(
                coordination.release_claim(
                    BINDING, claim["claim_token"], "thread-a"
                )
            )
        )
        operation.start()
        self.assertTrue(operation_started.wait(timeout=2))
        release.start()
        time.sleep(0.05)
        self.assertTrue(release.is_alive())
        allow_completion.set()
        operation.join(timeout=2)
        release.join(timeout=2)

        self.assertFalse(operation.is_alive())
        self.assertFalse(release.is_alive())
        self.assertTrue(released[0]["released"])

    def test_state_and_lock_files_are_private(self) -> None:
        coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)

        claim_mode = stat.S_IMODE(coordination.CLAIMS_FILE.stat().st_mode)
        claim_lock_mode = stat.S_IMODE(coordination.CLAIMS_LOCK_FILE.stat().st_mode)
        window_lock = next(coordination.WINDOW_LOCK_DIR.glob("*.lock"))
        window_lock_mode = stat.S_IMODE(window_lock.stat().st_mode)

        self.assertEqual((claim_mode, claim_lock_mode, window_lock_mode), (0o600, 0o600, 0o600))

    def test_active_claims_are_hard_capped(self) -> None:
        second_window = {**WINDOW, "address": "0x2", "capture_id": "43"}
        third_window = {**WINDOW, "address": "0x3", "capture_id": "44"}
        with patch.object(coordination, "MAX_ACTIVE_CLAIMS", 2):
            coordination.claim_window(BINDING, WINDOW, "thread-a", now=1000)
            coordination.claim_window(BINDING, second_window, "thread-b", now=1000)

            with self.assertRaisesRegex(RuntimeError, "active window claim limit"):
                coordination.claim_window(BINDING, third_window, "thread-c", now=1000)
            claims = coordination.list_claims(BINDING, now=1001)

        self.assertEqual(len(claims), 2)

    def test_only_one_process_wins_a_same_window_claim_race(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=claim_in_process,
                args=(self.temporary.name, owner, start, results),
            )
            for owner in ("thread-a", "thread-b")
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=5) for _ in processes]
        for process in processes:
            process.join(timeout=5)

        self.assertEqual([outcome[0] for outcome in outcomes].count("claimed"), 1)
        self.assertEqual([outcome[0] for outcome in outcomes].count("rejected"), 1)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
