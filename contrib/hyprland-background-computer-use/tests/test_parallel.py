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

    def test_session_environment_attachment_is_serialized(self) -> None:
        original_attached = server._SESSION_ATTACHED
        server._SESSION_ATTACHED = False
        start = threading.Barrier(3)
        calls: list[tuple[str, str | None]] = []
        errors: list[Exception] = []

        def find(instance: str, wayland_display: str | None) -> None:
            calls.append((instance, wayland_display))
            time.sleep(0.05)

        def attach() -> None:
            try:
                start.wait(timeout=2)
                server.ensure_session_environment()
            except Exception as exc:
                errors.append(exc)

        environment = {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-1",
            "HYPRLAND_INSTANCE_SIGNATURE": "hypr-instance",
        }
        try:
            with (
                patch.dict(server.os.environ, environment, clear=True),
                patch.object(server, "find_xwayland_display", side_effect=find),
            ):
                threads = [threading.Thread(target=attach) for _ in range(2)]
                for thread in threads:
                    thread.start()
                start.wait(timeout=2)
                for thread in threads:
                    thread.join(timeout=2)
        finally:
            server._SESSION_ATTACHED = original_attached

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(calls, [("hypr-instance", "wayland-1")])

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

    def test_capture_call_tool_renews_claim_after_success(self) -> None:
        window = self.window("0x1", "1")
        claim = {"claim_token": "claim-token"}
        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "require_window_access", return_value=claim),
            patch.object(server, "capture_result", return_value={"captured": True}),
            patch.object(server, "finish_claimed_window_access") as finish,
        ):
            result = server.call_tool(
                "capture_session_window",
                {"window": "0x1", "claim_token": "claim-token"},
                "owner-a",
            )

        self.assertEqual(result, {"captured": True})
        finish.assert_called_once_with(
            BINDING, window, "owner-a", claim, renew=True
        )

    def test_capture_call_tool_does_not_renew_claim_after_failure(self) -> None:
        window = self.window("0x1", "1")
        claim = {"claim_token": "claim-token"}
        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "require_window_access", return_value=claim),
            patch.object(
                server, "capture_result", side_effect=RuntimeError("capture failed")
            ),
            patch.object(server, "finish_claimed_window_access") as finish,
        ):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                server.call_tool(
                    "capture_session_window",
                    {"window": "0x1", "claim_token": "claim-token"},
                    "owner-a",
                )

        finish.assert_called_once_with(
            BINDING, window, "owner-a", claim, renew=False
        )


