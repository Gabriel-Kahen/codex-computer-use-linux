import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import server


BINDING = {
    "uid": 1000,
    "xdg_runtime_dir": "/run/user/1000",
    "wayland_display": "wayland-1",
    "hyprland_instance": "instance",
}
OUTPUT = f"{server.CONTINUITY_OUTPUT_PREFIX}deadbeef"


class HeadlessContinuityTests(TestCase):
    def paths(self, root: Path) -> tuple[Path, Path]:
        return root / "state.json", root / "output.lock"

    def test_enable_creates_owned_output_once(self) -> None:
        with TemporaryDirectory() as directory:
            state_path, lock_path = self.paths(Path(directory))
            monitors: list[dict[str, object]] = [{"name": "HDMI-A-1"}]
            commands: list[list[str]] = []

            def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                commands.append(args)
                monitors.append({"name": OUTPUT})
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(server, "session_binding", return_value=BINDING),
                patch.object(server, "continuity_state_file", return_value=state_path),
                patch.object(server, "continuity_lock_file", return_value=lock_path),
                patch.object(server, "hypr_json", side_effect=lambda _: list(monitors)),
                patch.object(server, "wait_for_monitor"),
                patch.object(server.secrets, "token_hex", return_value="deadbeef"),
                patch.object(server, "run", side_effect=run),
            ):
                created = server.enable_headless_continuity()
                repeated = server.enable_headless_continuity()

            self.assertTrue(created["enabled"])
            self.assertTrue(created["created"])
            self.assertFalse(repeated["created"])
            self.assertEqual(
                commands,
                [
                    [
                        "hyprctl",
                        "output",
                        "create",
                        "headless",
                        OUTPUT,
                    ]
                ],
            )
            self.assertTrue(state_path.is_file())

    def test_enable_refuses_unowned_name_collision(self) -> None:
        with TemporaryDirectory() as directory:
            state_path, lock_path = self.paths(Path(directory))
            with (
                patch.object(server, "session_binding", return_value=BINDING),
                patch.object(server, "continuity_state_file", return_value=state_path),
                patch.object(server, "continuity_lock_file", return_value=lock_path),
                patch.object(
                    server,
                    "hypr_json",
                    return_value=[{"name": OUTPUT}],
                ),
                patch.object(server, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "unowned"):
                    server.enable_headless_continuity()

            run.assert_not_called()

    def test_disable_refuses_to_remove_only_output(self) -> None:
        with TemporaryDirectory() as directory:
            state_path, lock_path = self.paths(Path(directory))
            server.coordination.atomic_write_json(
                state_path,
                {
                    "version": 1,
                    "session": BINDING,
                    "output": OUTPUT,
                    "phase": "active",
                },
            )
            with (
                patch.object(server, "session_binding", return_value=BINDING),
                patch.object(server, "continuity_state_file", return_value=state_path),
                patch.object(server, "continuity_lock_file", return_value=lock_path),
                patch.object(
                    server,
                    "hypr_json",
                    return_value=[{"name": OUTPUT}],
                ),
                patch.object(server, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "durable output"):
                    server.disable_headless_continuity()

            run.assert_not_called()
            self.assertTrue(state_path.is_file())

    def test_disable_removes_owned_output_when_another_is_active(self) -> None:
        with TemporaryDirectory() as directory:
            state_path, lock_path = self.paths(Path(directory))
            server.coordination.atomic_write_json(
                state_path,
                {
                    "version": 1,
                    "session": BINDING,
                    "output": OUTPUT,
                    "phase": "active",
                },
            )
            monitors = [
                {"name": "HDMI-A-1"},
                {"name": OUTPUT},
            ]

            def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                monitors.pop()
                return subprocess.CompletedProcess(args, 0, "", "")

            with (
                patch.object(server, "session_binding", return_value=BINDING),
                patch.object(server, "continuity_state_file", return_value=state_path),
                patch.object(server, "continuity_lock_file", return_value=lock_path),
                patch.object(server, "hypr_json", side_effect=lambda _: list(monitors)),
                patch.object(server, "wait_for_monitor"),
                patch.object(server, "run", side_effect=run) as run,
            ):
                result = server.disable_headless_continuity()

            self.assertTrue(result["removed"])
            self.assertFalse(result["enabled"])
            self.assertFalse(state_path.exists())
            run.assert_called_once_with(
                ["hyprctl", "output", "remove", OUTPUT]
            )

    def test_disable_ignores_temporary_coordinate_lease_output(self) -> None:
        with TemporaryDirectory() as directory:
            state_path, lock_path = self.paths(Path(directory))
            server.coordination.atomic_write_json(
                state_path,
                {
                    "version": 1,
                    "session": BINDING,
                    "output": OUTPUT,
                    "phase": "active",
                },
            )
            monitors = [{"name": OUTPUT}, {"name": "CODEX-CU-temporary"}]
            with (
                patch.object(server, "session_binding", return_value=BINDING),
                patch.object(server, "continuity_state_file", return_value=state_path),
                patch.object(server, "continuity_lock_file", return_value=lock_path),
                patch.object(server, "hypr_json", return_value=monitors),
                patch.object(server, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "durable output"):
                    server.disable_headless_continuity()

            run.assert_not_called()
            self.assertTrue(state_path.is_file())
