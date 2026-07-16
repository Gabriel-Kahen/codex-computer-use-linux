import multiprocessing
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from gnome_same_session.claims import ClaimRegistry


SHELL = "shell-1"


def window(window_id: str) -> dict[str, object]:
    return {"id": window_id, "title": f"Window {window_id}", "focused": False}


def race_claim(
    state_dir: str,
    owner: str,
    start: Any,
    results: Any,
) -> None:
    registry = ClaimRegistry(
        Path(state_dir),
        "test-session",
        process_alive=lambda _broker: True,
        broker={"pid": os.getpid(), "start_time": None},
    )
    start.wait()
    try:
        claim = registry.claim(window("11"), owner, 60, SHELL)
        results.put(("claimed", claim["owner_thread_id"]))
    except Exception as exc:
        results.put(("blocked", str(exc)))


def hold_authorization(
    state_dir: str,
    window_id: str,
    owner: str,
    token: str,
    ready: Any,
    release: Any,
) -> None:
    registry = ClaimRegistry(
        Path(state_dir),
        "test-session",
        process_alive=lambda _broker: True,
    )
    with registry.authorize(window_id, owner, token, SHELL):
        ready.put(window_id)
        release.wait(5)


class ClaimRegistryTests(TestCase):
    def registry(self, directory: str, **kwargs: object) -> ClaimRegistry:
        return ClaimRegistry(
            Path(directory),
            "test-session",
            process_alive=lambda _broker: True,
            broker={"pid": os.getpid(), "start_time": None},
            **kwargs,
        )

    def test_different_windows_can_be_claimed_concurrently(self) -> None:
        with TemporaryDirectory() as directory:
            registry = self.registry(directory)
            first = registry.claim(window("11"), "thread-a", 60, SHELL)
            second = registry.claim(window("12"), "thread-b", 60, SHELL)
            context = multiprocessing.get_context("fork")
            ready = context.Queue()
            release = context.Event()
            processes = [
                context.Process(
                    target=hold_authorization,
                    args=(directory, "11", "thread-a", first["claim_token"], ready, release),
                ),
                context.Process(
                    target=hold_authorization,
                    args=(directory, "12", "thread-b", second["claim_token"], ready, release),
                ),
            ]
            for process in processes:
                process.start()
            entered = {ready.get(timeout=3), ready.get(timeout=3)}
            release.set()
            for process in processes:
                process.join(5)

        self.assertEqual(entered, {"11", "12"})
        self.assertEqual([process.exitcode for process in processes], [0, 0])

    def test_claim_refresh_and_token_free_list_use_shared_shape(self) -> None:
        with TemporaryDirectory() as directory:
            registry = self.registry(directory)
            first = registry.claim(window("11"), "thread-a", 60, SHELL)
            refreshed = registry.claim(window("11"), "thread-a", 120, SHELL)
            listed = registry.list(SHELL)

            self.assertEqual(first["renewed"], False)
            self.assertEqual(refreshed["renewed"], True)
            self.assertEqual(refreshed["claim_token"], first["claim_token"])
            self.assertEqual(
                set(listed[0]),
                {"window", "owner_thread_id", "claimed_at", "expires_at", "lease_seconds"},
            )
            self.assertNotIn("claim_token", listed[0])
            self.assertEqual(listed[0]["lease_seconds"], 120)
            self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(registry.claims_file.stat().st_mode), 0o600)

    def test_minimum_ttl_starts_after_blocking_claim_callback(self) -> None:
        now = [100.0]
        with TemporaryDirectory() as directory:
            registry = self.registry(directory, clock=lambda: now[0])

            def blocking_callback(_claim: dict[str, Any]) -> None:
                now[0] = 106.0
                self.assertEqual(len(registry.list(SHELL)), 1)

            claim = registry.claim(
                window("11"),
                "thread-a",
                5,
                SHELL,
                before_save=blocking_callback,
            )

        self.assertEqual(claim["claimed_at"], 106.0)
        self.assertEqual(claim["expires_at"], 111.0)

    def test_authorized_operation_is_pinned_and_renews_at_completion(self) -> None:
        now = [100.0]
        with TemporaryDirectory() as directory:
            registry = self.registry(directory, clock=lambda: now[0])
            claim = registry.claim(window("11"), "thread-a", 5, SHELL)
            now[0] = 104.0
            completed = []

            def blocking_dbus_completion(_claim: dict[str, Any]) -> None:
                completed.append(True)
                now[0] = 111.0

            with registry.authorize(
                "11",
                "thread-a",
                claim["claim_token"],
                SHELL,
                on_complete=blocking_dbus_completion,
            ):
                now[0] = 110.0
                self.assertEqual(len(registry.list(SHELL)), 1)
            listed = registry.list(SHELL)

        self.assertEqual(completed, [True])
        self.assertEqual(listed[0]["expires_at"], 116.0)

    def test_live_claim_requires_token_and_stale_generation_is_rejected(self) -> None:
        now = [100.0]
        with TemporaryDirectory() as directory:
            registry = self.registry(directory, clock=lambda: now[0])
            first = registry.claim(window("11"), "thread-a", 5, SHELL)
            with self.assertRaisesRegex(ValueError, "claim_token is required"):
                with registry.authorize("11", "thread-a", None, SHELL):
                    pass

            now[0] = 106.0
            second = registry.claim(window("11"), "thread-a", 5, SHELL)
            with self.assertRaisesRegex(ValueError, "does not match"):
                with registry.authorize("11", "thread-a", first["claim_token"], SHELL):
                    pass

        self.assertNotEqual(second["claim_token"], first["claim_token"])
        self.assertFalse(second["renewed"])

    def test_same_window_claim_race_has_one_winner(self) -> None:
        with TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(target=race_claim, args=(directory, f"thread-{index}", start, results))
                for index in range(6)
            ]
            for process in processes:
                process.start()
            start.set()
            outcomes = [results.get(timeout=5)[0] for _process in processes]
            for process in processes:
                process.join(5)

        self.assertEqual(outcomes.count("claimed"), 1)
        self.assertEqual(outcomes.count("blocked"), 5)
        self.assertEqual([process.exitcode for process in processes], [0] * 6)

    def test_active_claims_and_list_output_are_hard_capped(self) -> None:
        with TemporaryDirectory() as directory:
            registry = self.registry(directory)
            for index in range(128):
                registry.claim(window(str(index)), f"thread-{index}", 60, SHELL)

            with self.assertRaisesRegex(RuntimeError, "maximum 128"):
                registry.claim(window("overflow"), "overflow-thread", 60, SHELL)
            self.assertEqual(len(registry.list(SHELL)), 128)

    def test_nonowner_cannot_release_or_authorize_live_claim(self) -> None:
        with TemporaryDirectory() as directory:
            registry = self.registry(directory)
            claim = registry.claim(window("11"), "thread-a", 60, SHELL)

            with self.assertRaisesRegex(RuntimeError, "owning thread"):
                registry.release(claim["claim_token"], "thread-b", SHELL)
            with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
                with registry.authorize("11", "thread-b", None, SHELL):
                    pass

    def test_expired_claim_can_be_recovered_but_not_ended_by_nonowner(self) -> None:
        now = [100.0]
        with TemporaryDirectory() as directory:
            registry = self.registry(directory, clock=lambda: now[0])
            first = registry.claim(window("11"), "thread-a", 5, SHELL)
            with self.assertRaisesRegex(RuntimeError, "already claimed"):
                registry.claim(window("11"), "thread-b", 60, SHELL)

            now[0] = 106.0
            with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
                registry.release(first["claim_token"], "thread-b", SHELL)
            recovered = registry.claim(window("11"), "thread-b", 60, SHELL)

        self.assertFalse(recovered["renewed"])
        self.assertNotEqual(recovered["claim_token"], first["claim_token"])

    def test_dead_broker_claim_is_recoverable_before_expiry(self) -> None:
        with TemporaryDirectory() as directory:
            first_registry = self.registry(directory)
            first = first_registry.claim(window("11"), "thread-a", 300, SHELL)
            recovery_registry = ClaimRegistry(
                Path(directory),
                "test-session",
                process_alive=lambda _broker: False,
                broker={"pid": os.getpid(), "start_time": None},
            )
            recovered = recovery_registry.claim(window("11"), "thread-b", 60, SHELL)

        self.assertFalse(recovered["renewed"])
        self.assertNotEqual(recovered["claim_token"], first["claim_token"])

    def test_claims_are_bound_to_shell_instance(self) -> None:
        with TemporaryDirectory() as directory:
            registry = self.registry(directory)
            registry.claim(window("11"), "thread-a", 60, SHELL)
            recovered = registry.claim(window("11"), "thread-b", 60, "shell-2")

        self.assertFalse(recovered["renewed"])
