import contextlib
import multiprocessing
import sys
import tempfile
import threading
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from x11_session_computer_use.coordination import WindowClaimStore


SESSION = {"display": ":42", "socket_inode": 123, "wm_start_time": "456"}


def claim_worker(state_dir: str, owner: str, start, results) -> None:
    store = WindowClaimStore(Path(state_dir), SESSION)
    start.wait()
    try:
        claim = store.claim(owner, {"xid": "0x20"}, {"xid": "0x20", "pid": 20})
        results.put(("claimed", claim["owner_thread_id"]))
    except Exception as exc:
        results.put(("blocked", str(exc)))


def guard_worker(state_dir: str, session: dict, identity: dict, entered, release) -> None:
    store = WindowClaimStore(Path(state_dir), session)
    with store.window_guard(identity):
        entered.set()
        release.wait()


class WindowClaimTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.store = WindowClaimStore(self.state_dir, SESSION)
        self.window = {"xid": "0x20", "title": "Editor"}
        self.identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}

    def test_parallel_processes_cannot_both_claim_the_same_window(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(
                target=claim_worker,
                args=(str(self.state_dir), owner, start, results),
            )
            for owner in ("thread-a", "thread-b")
        ]
        for worker in workers:
            worker.start()
        start.set()
        outcomes = [results.get(timeout=5)[0] for _ in workers]
        for worker in workers:
            worker.join(timeout=5)
            self.assertEqual(worker.exitcode, 0)

        self.assertEqual(sorted(outcomes), ["blocked", "claimed"])
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state_dir / "window-claims.json").stat().st_mode & 0o777, 0o600)

    def test_distinct_window_guards_are_parallel_and_same_window_is_serialized(self) -> None:
        context = multiprocessing.get_context("fork")
        release_first = context.Event()
        first_entered = context.Event()
        first = context.Process(
            target=guard_worker,
            args=(str(self.state_dir), SESSION, self.identity, first_entered, release_first),
        )
        first.start()
        self.assertTrue(first_entered.wait(timeout=5))

        other_entered = context.Event()
        release_other = context.Event()
        other = context.Process(
            target=guard_worker,
            args=(str(self.state_dir), SESSION, {**self.identity, "xid": "0x30"}, other_entered, release_other),
        )
        other.start()
        self.assertTrue(other_entered.wait(timeout=2))

        same_entered = context.Event()
        release_same = context.Event()
        same = context.Process(
            target=guard_worker,
            args=(str(self.state_dir), SESSION, self.identity, same_entered, release_same),
        )
        same.start()
        self.assertFalse(same_entered.wait(timeout=0.2))

        release_first.set()
        self.assertTrue(same_entered.wait(timeout=2))
        release_other.set()
        release_same.set()
        for worker in (first, other, same):
            worker.join(timeout=5)
            self.assertEqual(worker.exitcode, 0)

    def test_window_guard_survives_a_window_manager_restart(self) -> None:
        context = multiprocessing.get_context("fork")
        release_first = context.Event()
        first_entered = context.Event()
        first = context.Process(
            target=guard_worker,
            args=(str(self.state_dir), SESSION, self.identity, first_entered, release_first),
        )
        first.start()
        self.assertTrue(first_entered.wait(timeout=5))

        restarted_session = {**SESSION, "wm_start_time": "replacement"}
        second_entered = context.Event()
        release_second = context.Event()
        second = context.Process(
            target=guard_worker,
            args=(str(self.state_dir), restarted_session, self.identity, second_entered, release_second),
        )
        second.start()
        self.assertFalse(second_entered.wait(timeout=0.2))

        release_first.set()
        self.assertTrue(second_entered.wait(timeout=2))
        release_second.set()
        for worker in (first, second):
            worker.join(timeout=5)
            self.assertEqual(worker.exitcode, 0)

        self.assertEqual(len(list((self.state_dir / "window-locks").glob("*.lock"))), 1)

    def test_foreign_access_and_release_are_rejected(self) -> None:
        claim = self.store.claim("thread-a", self.window, self.identity)

        with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
            self.store.assert_access("thread-b", self.identity)
        with self.assertRaisesRegex(RuntimeError, "owning computer-use agent"):
            self.store.release("thread-b", claim["claim_token"])

        self.assertEqual(self.store.list_active()[0]["owner_thread_id"], "thread-a")

    def test_release_waits_for_a_guarded_operation_then_revalidates(self) -> None:
        claim = self.store.claim("thread-a", self.window, self.identity)
        release_store = WindowClaimStore(self.state_dir, SESSION)
        release_attempted = threading.Event()
        release_result = []
        expected_release = []
        original_window_guard = release_store.window_guard

        @contextlib.contextmanager
        def observed_window_guard(identity):
            release_attempted.set()
            with original_window_guard(identity):
                yield

        release_store.window_guard = observed_window_guard

        def release() -> None:
            release_result.append(
                release_store.release("thread-a", claim["claim_token"])
            )

        with self.store.window_guard(self.identity):
            access = self.store.assert_access(
                "thread-a",
                self.identity,
                claim["claim_token"],
                mark_inflight=True,
            )
            worker = threading.Thread(target=release)
            worker.start()
            self.assertTrue(release_attempted.wait(timeout=2))
            self.assertTrue(worker.is_alive())
            self.store.finish_access(
                "thread-a",
                self.identity,
                access["claim_token"],
                renew=True,
            )
            expected_release.append(
                {
                    "released": True,
                    **self.store.list_active()[0],
                    "claim_token": claim["claim_token"],
                }
            )

        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(release_result, expected_release)
        self.assertEqual(self.store.list_active(), [])

    def test_unknown_and_oversized_release_tokens_are_not_reflected(self) -> None:
        self.assertEqual(
            self.store.release("thread-a", "unknown-sensitive-token"),
            {"released": False},
        )
        with self.assertRaisesRegex(ValueError, "at most 128"):
            self.store.release("thread-a", "x" * 129)

    def test_active_claim_requires_its_fencing_token(self) -> None:
        self.store.claim("thread-a", self.window, self.identity)

        with self.assertRaisesRegex(ValueError, "claim_token is required"):
            self.store.assert_access("thread-a", self.identity)

    def test_inflight_access_survives_short_ttl_and_renews_after_success(self) -> None:
        now = [100.0]
        store = WindowClaimStore(self.state_dir, SESSION, clock=lambda: now[0])
        claim = store.claim("thread-a", self.window, self.identity, lease_seconds=5)
        access = store.assert_access(
            "thread-a", self.identity, claim["claim_token"], mark_inflight=True
        )
        now[0] = 110.0

        self.assertEqual(store.list_active()[0]["owner_thread_id"], "thread-a")
        store.finish_access("thread-a", self.identity, access["claim_token"], renew=True)

        self.assertEqual(store.list_active()[0]["expires_at"], 115.0)

    def test_expired_claim_is_recoverable_by_another_agent(self) -> None:
        now = [100.0]
        store = WindowClaimStore(self.state_dir, SESSION, clock=lambda: now[0])
        first = store.claim("thread-a", self.window, self.identity, lease_seconds=5)
        now[0] = 106.0

        second = store.claim("thread-b", self.window, self.identity, lease_seconds=5)

        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertEqual(store.list_active()[0]["owner_thread_id"], "thread-b")
        self.assertNotIn("claim_token", store.list_active()[0])

    def test_same_xid_in_another_verified_session_has_an_independent_claim(self) -> None:
        first = self.store.claim("thread-a", self.window, self.identity)
        other_store = WindowClaimStore(self.state_dir, {**SESSION, "socket_inode": 999})

        second = other_store.claim("thread-b", self.window, self.identity)

        self.assertNotEqual(first["claim_token"], second["claim_token"])
        self.assertEqual(self.store.list_active()[0]["owner_thread_id"], "thread-a")
        self.assertEqual(other_store.list_active()[0]["owner_thread_id"], "thread-b")

    def test_active_claim_count_is_bounded(self) -> None:
        for index in range(20):
            identity = {**self.identity, "xid": f"0x{index:02x}"}
            self.store.claim("thread-a", {"xid": identity["xid"]}, identity)

        with self.assertRaisesRegex(RuntimeError, "at most 20"):
            self.store.claim(
                "thread-a",
                {"xid": "0xff"},
                {**self.identity, "xid": "0xff"},
            )

    def test_active_claim_count_is_scoped_to_the_verified_session(self) -> None:
        for index in range(20):
            identity = {**self.identity, "xid": f"0x{index:02x}"}
            self.store.claim("thread-a", {"xid": identity["xid"]}, identity)
        other_store = WindowClaimStore(
            self.state_dir,
            {**SESSION, "socket_inode": 999},
        )

        other_claim = other_store.claim(
            "thread-b",
            {"xid": "0xff"},
            {**self.identity, "xid": "0xff"},
        )

        self.assertEqual(len(self.store.list_active()), 20)
        self.assertEqual(
            other_store.list_active(),
            [
                {
                    "owner_thread_id": "thread-b",
                    "window": {
                        "xid": "0xff",
                        "pid": None,
                        "wm_class": "",
                        "title": "",
                    },
                    "lease_seconds": 60,
                    "claimed_at": other_claim["claimed_at"],
                    "expires_at": other_claim["expires_at"],
                }
            ],
        )

    def test_public_window_record_is_compact(self) -> None:
        claim = self.store.claim(
            "thread-a",
            {**self.window, "title": "t" * 1000, "wm_class": "c" * 1000, "host": "h" * 1000},
            self.identity,
        )

        self.assertEqual(set(claim["window"]), {"xid", "pid", "wm_class", "title"})
        self.assertEqual(len(claim["window"]["title"]), 160)
        self.assertEqual(len(claim["window"]["wm_class"]), 80)
