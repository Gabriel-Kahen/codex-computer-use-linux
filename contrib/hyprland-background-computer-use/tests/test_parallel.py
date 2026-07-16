import json
import subprocess
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import coordination, server


BINDING = {
    "uid": 1000,
    "xdg_runtime_dir": "/run/user/1000",
    "wayland_display": "wayland-1",
    "hyprland_instance": "hypr-instance",
    "xwayland_display": ":1",
}


def completed(args: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, stdout, "")


class ParallelBackendTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.patches = (
            patch.object(coordination, "CLAIMS_FILE", root / "claims.json"),
            patch.object(coordination, "CLAIMS_LOCK_FILE", root / "claims.lock"),
            patch.object(coordination, "WINDOW_LOCK_DIR", root / "window-locks"),
            patch.object(coordination, "GLOBAL_INPUT_LOCK_FILE", root / "global.lock"),
            patch.object(server, "LEASE_FILE", root / "coordinate-lease.json"),
            patch.object(server, "LOCK_FILE", root / "coordinate-lease.lock"),
            patch.object(server, "session_binding", return_value=BINDING),
        )
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def window(address: str, capture_id: str, *, xwayland: bool = False) -> dict[str, object]:
        return {
            "address": address,
            "capture_id": capture_id,
            "class": "demo",
            "title": address,
            "pid": int(capture_id),
            "workspace": 1,
            "size": [100, 100],
            "xwayland": xwayland,
        }

    def test_different_wayland_windows_execute_without_a_global_broker_lock(self) -> None:
        windows = {
            "0x1": self.window("0x1", "1"),
            "0x2": self.window("0x2", "2"),
        }
        overlap = threading.Barrier(2)
        errors: list[Exception] = []

        def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            overlap.wait(timeout=2)
            return completed(args, '{"ok":true}')

        def act(address: str) -> None:
            try:
                server.targeted_pointer(
                    {"window": address, "x": 10, "y": 10}, "click", f"owner-{address}"
                )
            except Exception as exc:
                errors.append(exc)

        with (
            patch.object(server, "resolve_window", side_effect=lambda query: windows[query]),
            patch.object(server, "ensure_target_pointer_plugin"),
            patch.object(server, "physical_snapshot", return_value={}),
            patch.object(server, "run", side_effect=run),
        ):
            threads = [threading.Thread(target=act, args=(address,)) for address in windows]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_same_wayland_window_mutations_are_serialized(self) -> None:
        window = self.window("0x1", "1")
        state = {"active": 0, "maximum": 0}
        state_lock = threading.Lock()
        errors: list[Exception] = []

        def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.05)
            with state_lock:
                state["active"] -= 1
            return completed(args, '{"ok":true}')

        def act() -> None:
            try:
                server.targeted_pointer({"window": "0x1", "x": 10, "y": 10}, "click")
            except Exception as exc:
                errors.append(exc)

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "ensure_target_pointer_plugin"),
            patch.object(server, "physical_snapshot", return_value={}),
            patch.object(server, "run", side_effect=run),
        ):
            threads = [threading.Thread(target=act) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(state["maximum"], 1)

    def test_xwayland_windows_keep_the_global_input_lock(self) -> None:
        windows = {
            "0x1": self.window("0x1", "1", xwayland=True),
            "0x2": self.window("0x2", "2", xwayland=True),
        }
        state = {"active": 0, "maximum": 0}
        state_lock = threading.Lock()
        errors: list[Exception] = []

        def target(*_: object, **__: object) -> dict[str, str]:
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.05)
            with state_lock:
                state["active"] -= 1
            return {"backend": "xwayland-xtest"}

        def act(address: str) -> None:
            try:
                server.targeted_pointer(
                    {"window": address, "x": 10, "y": 10}, "click"
                )
            except Exception as exc:
                errors.append(exc)

        with (
            patch.object(server, "resolve_window", side_effect=lambda query: windows[query]),
            patch.object(server, "ensure_native_input_safe"),
            patch.object(server, "physical_snapshot", return_value={}),
            patch.object(server, "resolve_xwindow_id", return_value="10"),
            patch.object(server, "xdotool_target", side_effect=target),
        ):
            threads = [
                threading.Thread(target=act, args=(address,))
                for address in windows
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(state["maximum"], 1)

    def test_foreign_claim_blocks_capture_and_mutation_without_a_token(self) -> None:
        window = self.window("0x1", "1")
        coordination.claim_window(BINDING, window, "owner-a")

        with patch.object(server, "resolve_window", return_value=window):
            with self.assertRaisesRegex(RuntimeError, "actively claimed"):
                server.call_tool("capture_session_window", {"window": "0x1"}, "owner-b")
            with self.assertRaisesRegex(RuntimeError, "actively claimed"):
                server.targeted_pointer(
                    {"window": "0x1", "x": 10, "y": 10}, "click", "owner-b"
                )

    def test_fencing_token_cannot_be_used_by_another_owner(self) -> None:
        window = self.window("0x1", "1")
        claim = coordination.claim_window(BINDING, window, "owner-a")

        with patch.object(server, "resolve_window", return_value=window):
            with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
                server.call_tool(
                    "capture_session_window",
                    {"window": "0x1", "claim_token": claim["claim_token"]},
                    "owner-b",
                )

    def test_release_tool_keeps_claim_while_coordinate_lease_is_active(self) -> None:
        window = self.window("0x1", "1")
        claim = coordination.claim_window(BINDING, window, "owner-a")
        listed_claims = coordination.list_claims(BINDING)
        server.save_lease(
            {
                "version": 3,
                "session": BINDING,
                "token": "lease-token",
                "owner_thread_id": "owner-a",
                "target": window,
            }
        )

        with self.assertRaisesRegex(RuntimeError, "end the active coordinate lease"):
            server.call_tool(
                "release_session_window",
                {"claim_token": claim["claim_token"]},
                "owner-a",
            )

        self.assertEqual(coordination.list_claims(BINDING), listed_claims)

    def test_release_tool_succeeds_after_coordinate_lease_cleanup(self) -> None:
        window = self.window("0x1", "1")
        claim = coordination.claim_window(BINDING, window, "owner-a")

        result = server.call_tool(
            "release_session_window",
            {"claim_token": claim["claim_token"]},
            "owner-a",
        )

        self.assertTrue(result["structuredContent"]["released"])
        self.assertEqual(coordination.list_claims(BINDING), [])

    def test_coordinate_setup_pins_a_minimum_ttl_claim_until_completion(self) -> None:
        window = self.window("0x1", "1")
        claim = coordination.claim_window(BINDING, window, "owner-a", 5)

        def begin(*_: object, **__: object) -> dict[str, bool]:
            active = coordination.list_claims(
                BINDING, now=float(claim["expires_at"]) + 1
            )
            self.assertEqual(len(active), 1)
            return {"started": True}

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "require_global_input_available"),
            patch.object(server, "begin_lease", side_effect=begin),
        ):
            result = server.call_tool(
                "begin_coordinate_lease",
                {
                    "window": "0x1",
                    "acknowledge_interference": True,
                    "claim_token": claim["claim_token"],
                },
                "owner-a",
            )

        self.assertTrue(result["structuredContent"]["started"])

    def test_coordinate_owner_deadline_starts_after_unclaimed_setup(self) -> None:
        window = self.window("0x1", "1")

        def begin(*_: object, **__: object) -> dict[str, bool]:
            server.save_lease(
                {
                    "version": 3,
                    "session": BINDING,
                    "token": "lease-token",
                    "owner_thread_id": "owner-a",
                    "owner_expires_at": 100.0,
                    "target": window,
                }
            )
            return {"started": True}

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "require_global_input_available"),
            patch.object(server, "begin_lease", side_effect=begin),
            patch.object(server.time, "time", return_value=200.0),
        ):
            server.call_tool(
                "begin_coordinate_lease",
                {"window": "0x1", "acknowledge_interference": True},
                "owner-a",
            )

        self.assertEqual(server.load_lease()["owner_expires_at"], 260.0)

    def test_same_owner_reclaim_rebinds_the_active_coordinate_lease(self) -> None:
        window = self.window("0x1", "1")
        expired = coordination.claim_window(
            BINDING, window, "owner-a", 5, now=time.time() - 10
        )
        server.save_lease(
            {
                "version": 3,
                "session": BINDING,
                "token": "lease-token",
                "owner_thread_id": "owner-a",
                "owner_expires_at": time.time() - 1,
                "claim_token": expired["claim_token"],
                "target": window,
            }
        )

        with patch.object(server, "resolve_window", return_value=window):
            result = server.call_tool(
                "claim_session_window", {"window": "0x1", "lease_seconds": 5}, "owner-a"
            )

        replacement = result["structuredContent"]
        state = server.load_lease()
        self.assertNotEqual(replacement["claim_token"], expired["claim_token"])
        self.assertEqual(state["claim_token"], replacement["claim_token"])
        with self.assertRaisesRegex(RuntimeError, "owns the live coordinate lease"):
            server.require_recovery_access(state, "owner-b")

    def test_coordinate_capture_pins_and_completion_renews_its_claim(self) -> None:
        window = self.window("0x1", "1")
        claim = coordination.claim_window(BINDING, window, "owner-a", 5)
        server.save_lease(
            {
                "version": 3,
                "session": BINDING,
                "token": "lease-token",
                "owner_thread_id": "owner-a",
                "owner_expires_at": time.time() + 60,
                "claim_token": claim["claim_token"],
                "target": window,
            }
        )

        def capture(*_: object) -> dict[str, bool]:
            active = coordination.list_claims(
                BINDING, now=float(claim["expires_at"]) + 1
            )
            self.assertEqual(len(active), 1)
            return {"captured": True}

        with patch.object(server, "capture_lease", side_effect=capture):
            result = server.call_tool(
                "capture_coordinate_desktop",
                {"lease_token": "lease-token"},
                "owner-a",
            )

        renewed = coordination.list_claims(BINDING)[0]
        self.assertTrue(result["captured"])
        self.assertGreater(renewed["expires_at"], claim["expires_at"])

    def test_coordinate_capture_refreshes_an_unclaimed_owner_after_completion(self) -> None:
        window = self.window("0x1", "1")
        server.save_lease(
            {
                "version": 3,
                "session": BINDING,
                "token": "lease-token",
                "owner_thread_id": "owner-a",
                "owner_expires_at": 100.0,
                "target": window,
            }
        )

        with (
            patch.object(server, "capture_lease", return_value={"captured": True}),
            patch.object(server.time, "time", return_value=200.0),
        ):
            server.call_tool(
                "capture_coordinate_desktop",
                {"lease_token": "lease-token"},
                "owner-a",
            )

        self.assertEqual(server.load_lease()["owner_expires_at"], 260.0)

    def test_nonowner_cannot_end_capture_or_recover_a_live_coordinate_lease(self) -> None:
        window = self.window("0x1", "1")
        state = {
            "version": 3,
            "session": BINDING,
            "token": "lease-token",
            "owner_thread_id": "owner-a",
            "owner_expires_at": time.time() + 60,
            "target": window,
        }
        coordination.atomic_write_json(server.LEASE_FILE, state)

        with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
            server.call_tool(
                "capture_coordinate_desktop", {"lease_token": "lease-token"}, "owner-b"
            )
        with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
            server.call_tool(
                "end_coordinate_lease", {"lease_token": "lease-token"}, "owner-b"
            )
        with self.assertRaisesRegex(RuntimeError, "owns the live coordinate lease"):
            server.call_tool("recover_coordinate_lease", {}, "owner-b")

    def test_nonowner_can_recover_after_coordinate_owner_expires(self) -> None:
        window = self.window("0x1", "1")
        state = {
            "version": 3,
            "session": BINDING,
            "token": "lease-token",
            "owner_thread_id": "owner-a",
            "owner_expires_at": time.time() - 1,
            "target": window,
        }
        coordination.atomic_write_json(server.LEASE_FILE, state)

        with patch.object(server, "restore_lease", return_value={"restored": True}):
            result = server.call_tool("recover_coordinate_lease", {}, "owner-b")

        self.assertTrue(result["structuredContent"]["restored"])

    def test_recovery_never_restores_a_replacement_lease_under_the_old_window_lock(self) -> None:
        first = {
            "version": 3,
            "session": BINDING,
            "token": "lease-a",
            "owner_thread_id": "owner-a",
            "target": self.window("0x1", "1"),
        }
        replacement = {
            **first,
            "token": "lease-b",
            "target": self.window("0x2", "2"),
        }
        with (
            patch.object(server, "load_lease", side_effect=[first, replacement]),
            patch.object(server, "restore_lease") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "coordinate lease changed"):
                server.call_tool("recover_coordinate_lease", {}, "owner-a")

        restore.assert_not_called()

    def test_claim_listing_is_paginated_by_serialized_bytes(self) -> None:
        claims = [
            {
                "window": {
                    "address": f"0x{index:x}",
                    "capture_id": str(index),
                    "class": "界" * 80,
                    "title": "界" * 160,
                },
                "owner_thread_id": "o" * coordination.MAX_OWNER_LENGTH,
                "claimed_at": 1000.0,
                "expires_at": 1060.0,
                "lease_seconds": 60,
            }
            for index in range(coordination.MAX_ACTIVE_CLAIMS)
        ]
        with patch.object(coordination, "list_claims", return_value=claims):
            result = server.call_tool("list_window_claims", {})

        structured = result["structuredContent"]
        encoded = json.dumps(
            structured, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertLessEqual(len(encoded), server.MAX_CLAIM_RESULT_BYTES)
        self.assertIsNotNone(structured["next_cursor"])

    def test_foreign_legacy_coordinate_state_does_not_wedge_current_session(self) -> None:
        state = {
            "version": 3,
            "session": {**BINDING, "hyprland_instance": "other-instance"},
            "token": "lease-token",
        }
        server.LEASE_FILE.write_text(json.dumps(state))

        self.assertIsNone(server.load_lease())
        self.assertEqual(server.LEASE_FILE.stat().st_mode & 0o777, 0o600)

        current = {"version": 3, "session": BINDING, "token": "current-token"}
        server.save_lease(current)

        self.assertEqual(server.load_lease(), current)
        self.assertNotEqual(server.lease_file(), server.LEASE_FILE)

    def test_matching_legacy_coordinate_state_migrates_to_session_namespace(self) -> None:
        state = {"version": 3, "session": BINDING, "token": "lease-token"}
        coordination.atomic_write_json(server.LEASE_FILE, state)

        self.assertEqual(server.load_lease(), state)
        self.assertFalse(server.LEASE_FILE.exists())
        self.assertEqual(server.lease_file().stat().st_mode & 0o777, 0o600)

    def test_unbound_legacy_state_migrates_only_with_live_session_artifacts(self) -> None:
        window = self.window("0x1", "1")
        state = {
            "version": 2,
            "token": "lease-token",
            "output": "CODEX-CU-legacy",
            "target": window,
        }
        coordination.atomic_write_json(server.LEASE_FILE, state)

        with (
            patch.object(server, "hypr_json", return_value=[]),
            patch.object(server, "combine_windows", return_value=[window]),
        ):
            migrated = server.load_lease()

        self.assertEqual(migrated["session"], BINDING)
        self.assertFalse(server.LEASE_FILE.exists())

    def test_unbound_foreign_legacy_state_is_left_for_its_owning_session(self) -> None:
        state = {
            "version": 2,
            "token": "lease-token",
            "output": "CODEX-CU-foreign",
            "target": self.window("0x1", "1"),
        }
        coordination.atomic_write_json(server.LEASE_FILE, state)

        with (
            patch.object(server, "hypr_json", return_value=[]),
            patch.object(
                server, "combine_windows", return_value=[self.window("0x2", "2")]
            ),
        ):
            self.assertIsNone(server.load_lease())

        self.assertTrue(server.LEASE_FILE.exists())
        self.assertFalse(server.lease_file().exists())
