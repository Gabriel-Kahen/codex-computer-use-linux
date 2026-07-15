import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import native_plugin, server


def completed(args: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


class FileGuardTests(TestCase):
    def test_file_guard_excludes_another_process(self) -> None:
        with TemporaryDirectory() as directory:
            lock = Path(directory) / "transaction.lock"
            script = "\n".join(
                (
                    "import fcntl, sys",
                    "handle = open(sys.argv[1], 'a+')",
                    "print('ready', flush=True)",
                    "fcntl.flock(handle, fcntl.LOCK_EX)",
                    "print('acquired', flush=True)",
                )
            )
            with native_plugin.file_guard(lock):
                proc = subprocess.Popen(
                    [sys.executable, "-c", script, str(lock)],
                    text=True,
                    stdout=subprocess.PIPE,
                )
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                self.assertIsNone(proc.poll())
            self.assertEqual(proc.communicate(timeout=5)[0].strip(), "acquired")


class PluginBuildTests(TestCase):
    def test_builds_versioned_plugin_in_xdg_cache(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            source = root / "hyprland/target-pointer.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("int plugin_source;")
            cache = Path(directory) / "cache"

            def compile_plugin(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                Path(args[-1]).write_bytes(b"shared object")
                return completed(args)

            with (
                patch.object(native_plugin, "ROOT", root),
                patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}),
                patch.object(native_plugin, "plugin_build_requirements", return_value={"ready": True}),
                patch.object(native_plugin, "run", return_value=completed([], "-I/includes\n")),
                patch.object(subprocess, "run", side_effect=compile_plugin) as build,
            ):
                first = native_plugin.build_target_pointer_plugin("hyprland-v1")
                second = native_plugin.build_target_pointer_plugin("hyprland-v1")
                other_version = native_plugin.plugin_cache_directory("hyprland-v2", source.read_bytes())

            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(first.is_relative_to(cache))
            self.assertNotEqual(first.parent, other_version)
            self.assertFalse((source.parent / first.name).exists())
            self.assertEqual(build.call_count, 1)

    def test_plugin_build_and_load_use_the_plugin_file_guard(self) -> None:
        events: list[tuple[str, Path]] = []

        @contextmanager
        def guard(path: Path):
            events.append(("enter", path))
            yield
            events.append(("exit", path))

        responses = iter(
            (
                completed([], "no plugins loaded"),
                completed([], "Hyprland version"),
                completed([], "ok"),
            )
        )
        library = Path("/cache/same-session-target-pointer.so")
        with (
            patch.object(native_plugin, "file_guard", side_effect=guard),
            patch.object(native_plugin, "run", side_effect=lambda *_args, **_kwargs: next(responses)),
            patch.object(native_plugin, "build_target_pointer_plugin", return_value=library),
        ):
            native_plugin.ensure_target_pointer_plugin()

        self.assertEqual(events, [("enter", native_plugin.PLUGIN_LOCK_FILE), ("exit", native_plugin.PLUGIN_LOCK_FILE)])


class SafetyProbeTests(TestCase):
    def test_rejects_an_unsafe_native_status(self) -> None:
        status = {"ok": True, "safe_to_inject": False, "pointer_seat": True, "held_buttons": True}
        with patch.object(native_plugin, "native_input_status", return_value=status):
            with self.assertRaisesRegex(RuntimeError, "held_buttons"):
                native_plugin.ensure_native_input_safe()

    def test_xwayland_pointer_checks_safety_before_snapshot(self) -> None:
        events: list[str] = []
        window = {"address": "0x1", "size": [100, 100], "xwayland": True}

        def safe() -> dict[str, bool]:
            events.append("safe")
            return {"safe_to_inject": True}

        def snapshot() -> dict[str, object]:
            events.append("snapshot")
            return {}

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "ensure_native_input_safe", side_effect=safe),
            patch.object(server, "physical_snapshot", side_effect=snapshot),
            patch.object(server, "resolve_xwindow_id", return_value="10"),
            patch.object(server, "xdotool_target", return_value={"backend": "xwayland-xtest"}),
        ):
            server._targeted_pointer({"window": "0x1", "x": 10, "y": 10}, "click")

        self.assertEqual(events, ["safe", "snapshot", "snapshot"])

    def test_shortcut_checks_safety_before_dispatch(self) -> None:
        events: list[str] = []

        def safe() -> dict[str, bool]:
            events.append("safe")
            return {"safe_to_inject": True}

        def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            events.append("dispatch")
            return completed(args, "ok")

        with (
            patch.object(server, "hypr_windows", return_value=[{"address": "0x1"}]),
            patch.object(server, "ensure_native_input_safe", side_effect=safe),
            patch.object(server, "run", side_effect=run),
        ):
            server.call_tool("send_window_shortcut", {"address": "0x1", "key": "RETURN"})

        self.assertEqual(events, ["safe", "dispatch"])

    def test_lease_checks_safety_before_reading_desktop_state(self) -> None:
        window = {"address": "0x1", "workspace": 1, "size": [100, 100]}
        with (
            patch.object(server, "load_lease", return_value=None),
            patch.object(server, "resolve_window", return_value=window),
            patch.object(server, "ensure_native_input_safe", side_effect=RuntimeError("unsafe")),
            patch.object(server, "hypr_json") as hypr_json,
        ):
            with self.assertRaisesRegex(RuntimeError, "unsafe"):
                server.begin_lease({"window": "0x1", "acknowledge_interference": True})

        hypr_json.assert_not_called()


class RestoreTests(TestCase):
    def test_restores_workspace_then_cursor_when_active_window_is_gone(self) -> None:
        state = {
            "target": {"address": "0xtarget"},
            "original": {
                "active_address": "0xclosed",
                "active_workspace": 7,
                "cursor": {"x": 12, "y": 34},
            },
        }
        dispatched: list[str] = []
        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "hypr_windows", return_value=[]),
                patch.object(server, "hypr_json", return_value=[]),
                patch.object(server, "hypr_dispatch", side_effect=dispatched.append),
            ):
                result = server.restore_lease(state)

        self.assertTrue(result["restored"])
        self.assertEqual(
            dispatched,
            [
                "hl.dsp.focus({ workspace = 7 })",
                "hl.dsp.cursor.move({ x = 12, y = 34 })",
            ],
        )

    def test_bounds_restoration_error_collection(self) -> None:
        state = {
            "target": {"address": "0xtarget"},
            "original": {"target_workspace": 7},
        }
        with (
            patch.object(server, "hypr_windows", return_value=[{"address": "0xtarget"}]),
            patch.object(server, "hypr_json", return_value=[]),
            patch.object(server, "hypr_dispatch", side_effect=RuntimeError("x" * 100_000)),
        ):
            result = server.restore_lease(state)

        self.assertFalse(result["restored"])
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(all(len(error) == server.MAX_ERROR_TEXT_CHARS for error in result["errors"]))


class StatusTests(TestCase):
    def test_reports_buildable_native_capabilities_without_claiming_at_spi(self) -> None:
        requirements = {"compiler": True, "headers": True}
        with (
            patch.object(server.shutil, "which", return_value="/bin/tool"),
            patch.object(server, "plugin_build_requirements", return_value=requirements),
            patch.object(server, "combine_windows", return_value=[{"capture_id": "42"}]),
            patch.object(server, "run", return_value=completed([], "no plugins loaded")),
        ):
            result = server.status()

        self.assertEqual(
            result["capabilities"],
            {
                "exact_background_window_capture": True,
                "targeted_background_shortcuts": True,
                "background_semantic_actions": False,
                "targeted_wayland_pointer": True,
                "targeted_xwayland_pointer": True,
                "native_input_currently_safe": False,
                "physical_pointer_seat_is_independent": False,
            },
        )
        self.assertEqual(result["requirements"]["native_plugin_build"], requirements)
        self.assertIn("separate Computer Use plugin", result["requirements"]["background_semantic_actions"])
