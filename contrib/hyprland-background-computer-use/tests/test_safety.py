import json
import os
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import native_plugin, server


def completed(args: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def native_transaction(
    before: dict[str, object], after: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "ok": True,
        "action": "click",
        "physical_state_before": before,
        "physical_state_after": before if after is None else after,
    }


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
    def test_batch_command_is_registered_before_legacy_prefix(self) -> None:
        source = (
            Path(native_plugin.__file__).resolve().parents[2]
            / "hyprland/target-pointer.cpp"
        ).read_text()

        batch_registration = source.index('.name = "cutargetbatch"')
        legacy_registration = source.index('.name = "cutarget"')
        self.assertLess(batch_registration, legacy_registration)

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
            command = build.call_args.args[0]
            self.assertIn(
                f'-DCU_PLUGIN_VERSION="{native_plugin.NATIVE_PLUGIN_VERSION}"',
                command,
            )
            self.assertTrue(
                any(flag.startswith('-DCU_SOURCE_SHA256="') for flag in command)
            )
            self.assertTrue(
                any(
                    flag.startswith('-DCU_HYPRLAND_BUILD_SHA256="')
                    for flag in command
                )
            )

    def test_plugin_build_and_load_use_the_plugin_file_guard(self) -> None:
        events: list[tuple[str, Path]] = []

        @contextmanager
        def guard(path: Path):
            events.append(("enter", path))
            yield
            events.append(("exit", path))

        responses = iter(
            (
                completed([], "Hyprland version"),
                completed([], "no plugins loaded"),
                completed([], "ok"),
                completed(
                    [],
                    json.dumps(
                        {
                            "ok": True,
                            "plugin_version": native_plugin.NATIVE_PLUGIN_VERSION,
                            "source_sha256": native_plugin.plugin_identity(
                                "Hyprland version", b"int plugin_source;"
                            )["source_sha256"],
                            "hyprland_build_sha256": native_plugin.plugin_identity(
                                "Hyprland version", b"int plugin_source;"
                            )["hyprland_build_sha256"],
                            "hyprland_build_abi": "abi",
                            "hyprland_runtime_abi": "abi",
                        }
                    ),
                ),
            )
        )
        library = Path("/cache/same-session-target-pointer.so")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "hyprland/target-pointer.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("int plugin_source;")
            with (
                patch.object(native_plugin, "ROOT", root),
                patch.object(native_plugin, "file_guard", side_effect=guard),
                patch.object(native_plugin, "run", side_effect=lambda *_args, **_kwargs: next(responses)),
                patch.object(native_plugin, "build_target_pointer_plugin", return_value=library),
            ):
                native_plugin.ensure_target_pointer_plugin()

        self.assertEqual(events, [("enter", native_plugin.PLUGIN_LOCK_FILE), ("exit", native_plugin.PLUGIN_LOCK_FILE)])

    def test_rejects_an_already_loaded_plugin_with_the_wrong_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "hyprland/target-pointer.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("current source")
            responses = iter(
                (
                    completed([], "Hyprland version"),
                    completed([], "same-session-target-pointer"),
                    completed([], '{"ok":true,"plugin_version":"stale"}'),
                )
            )
            with (
                patch.object(native_plugin, "ROOT", root),
                patch.object(native_plugin, "file_guard", return_value=nullcontext()),
                patch.object(native_plugin, "run", side_effect=lambda *_args, **_kwargs: next(responses)),
            ):
                with self.assertRaisesRegex(RuntimeError, "identity does not match"):
                    native_plugin.ensure_target_pointer_plugin()

    def test_rejects_matching_plugin_identity_with_a_runtime_abi_mismatch(self) -> None:
        expected = native_plugin.plugin_identity("Hyprland version", b"source")
        status = {
            **expected,
            "hyprland_build_abi": "build-abi",
            "hyprland_runtime_abi": "runtime-abi",
        }

        with self.assertRaisesRegex(RuntimeError, "ABI does not match"):
            native_plugin._validate_plugin_identity(status, expected)

    def test_native_action_identity_is_cached_per_instance_socket(self) -> None:
        identity = native_plugin.plugin_identity("version", b"source")
        status = {
            **identity,
            "hyprland_build_abi": "abi",
            "hyprland_runtime_abi": "abi",
        }
        with (
            patch.object(native_plugin, "_IDENTITY_CACHE", {}),
            patch.object(
                native_plugin,
                "ensure_target_pointer_plugin",
                return_value=status,
            ) as ensure,
            patch.dict(
                os.environ,
                {
                    "HYPRLAND_INSTANCE_SIGNATURE": "instance-a",
                    "XDG_RUNTIME_DIR": "/run/user/1000",
                    "WAYLAND_DISPLAY": "wayland-1",
                },
            ),
        ):
            self.assertEqual(native_plugin.native_action_identity(), identity)
            self.assertEqual(native_plugin.native_action_identity(), identity)
            os.environ["WAYLAND_DISPLAY"] = "wayland-2"
            self.assertEqual(native_plugin.native_action_identity(), identity)

        self.assertEqual(ensure.call_count, 2)

    def test_native_action_sends_identity_and_accepts_atomic_state(self) -> None:
        identity = native_plugin.plugin_identity("version", b"source")
        state = {
            "active_address": "0x2",
            "workspace": 1,
            "cursor": {"x": 3, "y": 4},
        }
        response = {
            "ok": True,
            "identity": {
                **identity,
                "hyprland_build_abi": "abi",
                "hyprland_runtime_abi": "abi",
            },
            "physical_state_before": state,
            "physical_state_after": state,
        }
        with (
            patch.object(native_plugin, "native_action_identity", return_value=identity),
            patch.object(
                native_plugin,
                "run",
                return_value=completed([], json.dumps(response)),
            ) as dispatch,
        ):
            result = native_plugin.run_target_pointer_action(
                "click", ["0x1", "10", "20", "left", "1"]
            )

        self.assertEqual(result, response)
        self.assertEqual(
            dispatch.call_args.args[0],
            [
                "hyprctl",
                "-j",
                "cutarget",
                "click",
                native_plugin.plugin_identity_token(identity),
                "0x1",
                "10",
                "20",
                "left",
                "1",
            ],
        )

    def test_native_action_reloads_once_after_explicit_unknown_request(self) -> None:
        identity = native_plugin.plugin_identity("version", b"source")
        status = {
            **identity,
            "hyprland_build_abi": "abi",
            "hyprland_runtime_abi": "abi",
        }
        response = {
            "ok": True,
            "identity": status,
            "physical_state_before": {},
            "physical_state_after": {},
        }
        with (
            patch.object(native_plugin, "native_action_identity", return_value=identity),
            patch.object(native_plugin, "invalidate_plugin_identity") as invalidate,
            patch.object(
                native_plugin,
                "ensure_target_pointer_plugin",
                return_value=status,
            ) as ensure,
            patch.object(
                native_plugin,
                "run",
                side_effect=(
                    completed([], "unknown request"),
                    completed([], json.dumps(response)),
                ),
            ) as dispatch,
        ):
            result = native_plugin.run_target_pointer_action(
                "click", ["0x1", "10", "20", "left", "1"]
            )

        self.assertEqual(result, response)
        invalidate.assert_called_once_with()
        ensure.assert_called_once_with()
        self.assertEqual(dispatch.call_count, 2)

    def test_native_action_does_not_replay_an_ambiguous_transport_failure(self) -> None:
        identity = native_plugin.plugin_identity("version", b"source")
        with (
            patch.object(native_plugin, "native_action_identity", return_value=identity),
            patch.object(native_plugin, "ensure_target_pointer_plugin") as ensure,
            patch.object(
                native_plugin,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 1, "", "connection closed after dispatch"
                ),
            ) as dispatch,
        ):
            with self.assertRaisesRegex(RuntimeError, "connection closed"):
                native_plugin.run_target_pointer_action(
                    "click", ["0x1", "10", "20", "left", "1"]
                )

        ensure.assert_not_called()
        dispatch.assert_called_once()


class SafetyProbeTests(TestCase):
    def test_rejects_an_unsafe_native_status(self) -> None:
        status = {"safe_to_inject": False, "pointer_seat": True, "held_buttons": True}
        with patch.object(native_plugin, "native_input_status", return_value=status):
            with self.assertRaisesRegex(RuntimeError, "held_buttons"):
                native_plugin.ensure_native_input_safe()

    def test_rejects_an_active_pointer_grab(self) -> None:
        status = {"safe_to_inject": False, "pointer_seat": True, "pointer_grab": True}
        with patch.object(native_plugin, "native_input_status", return_value=status):
            with self.assertRaisesRegex(RuntimeError, "pointer_grab"):
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

    def test_wayland_pointer_uses_one_native_transaction(self) -> None:
        events: list[str] = []
        window = {"address": "0x1", "size": [100, 100], "xwayland": False}
        state = {
            "active_address": "0x2",
            "workspace": 1,
            "cursor": {"x": 3, "y": 4},
        }

        def record(event: str):
            return lambda *_args, **_kwargs: events.append(event)

        with (
            patch.object(server, "validate_point", side_effect=record("validate")),
            patch.object(
                server,
                "run_target_pointer_action",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("transaction") or native_transaction(state)
                ),
            ),
            patch.object(
                server,
                "physical_snapshot",
                side_effect=AssertionError("native action used external snapshots"),
            ),
        ):
            server._targeted_pointer(
                {"window": "0x1", "x": 10, "y": 10}, "click", window=window
            )

        self.assertEqual(events, ["validate", "transaction"])

    def test_targeted_pointer_rejects_changed_physical_state(self) -> None:
        window = {"address": "0x1", "size": [100, 100], "xwayland": False}
        before = {
            "active_address": "0xphysical",
            "workspace": 1,
            "cursor": {"x": 10, "y": 20},
        }
        after = {**before, "workspace": 2}

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(
                server,
                "run_target_pointer_action",
                return_value=native_transaction(before, after),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "workspace_changed_by_backend"
            ):
                server._targeted_pointer(
                    {"window": "0x1", "x": 10, "y": 10}, "click"
                )

    def test_targeted_pointer_reports_observed_state_changes(self) -> None:
        window = {"address": "0x1", "size": [100, 100], "xwayland": False}
        snapshot = {
            "active_address": "0xphysical",
            "workspace": 1,
            "cursor": {"x": 10, "y": 20},
        }

        with (
            patch.object(server, "resolve_window", return_value=window),
            patch.object(
                server,
                "run_target_pointer_action",
                return_value=native_transaction(snapshot),
            ),
        ):
            result = server._targeted_pointer(
                {"window": "0x1", "x": 10, "y": 10}, "click"
            )

        self.assertEqual(
            {
                key: result[key]
                for key in (
                    "observed_physical_state_unchanged",
                    "cursor_moved_by_backend",
                    "keyboard_focus_changed_by_backend",
                    "workspace_changed_by_backend",
                )
            },
            {
                "observed_physical_state_unchanged": True,
                "cursor_moved_by_backend": False,
                "keyboard_focus_changed_by_backend": False,
                "workspace_changed_by_backend": False,
            },
        )

    def test_shortcut_checks_safety_before_dispatch(self) -> None:
        events: list[str] = []
        snapshot = {
            "active_address": "0xphysical",
            "workspace": 1,
            "cursor": {"x": 10, "y": 20},
        }

        def safe() -> dict[str, bool]:
            events.append("safe")
            return {"safe_to_inject": True}

        def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            events.append("dispatch")
            return completed(args, "ok")

        with (
            patch.object(server, "hypr_windows", return_value=[{"address": "0x1"}]),
            patch.object(server, "session_binding", return_value={}),
            patch.object(server.coordination, "window_guard", return_value=nullcontext()),
            patch.object(server, "require_window_mutation_access", return_value=None),
            patch.object(server, "ensure_native_input_safe", side_effect=safe),
            patch.object(server, "physical_snapshot", return_value=snapshot),
            patch.object(server, "run", side_effect=run),
        ):
            result = server.send_window_shortcut(
                {"address": "0x1", "key": "RETURN"}
            )

        self.assertEqual(events, ["safe", "dispatch"])
        self.assertEqual(
            result,
            {
                "sent": True,
                "address": "0x1",
                "key": "RETURN",
                "modifiers": "",
                "focus_changed": False,
                "pointer_moved": False,
                "observed_physical_state_unchanged": True,
                "physical_state_before": snapshot,
                "physical_state_after": snapshot,
                "cursor_moved_by_backend": False,
                "keyboard_focus_changed_by_backend": False,
                "workspace_changed_by_backend": False,
            },
        )

    def test_shortcut_rejects_changed_physical_state(self) -> None:
        before = {
            "active_address": "0xphysical",
            "workspace": 1,
            "cursor": {"x": 10, "y": 20},
        }
        after = {**before, "cursor": {"x": 11, "y": 20}}
        with (
            patch.object(server, "hypr_windows", return_value=[{"address": "0x1"}]),
            patch.object(server, "session_binding", return_value={}),
            patch.object(server.coordination, "window_guard", return_value=nullcontext()),
            patch.object(server, "require_window_mutation_access", return_value=None),
            patch.object(server, "ensure_native_input_safe"),
            patch.object(server, "physical_snapshot", side_effect=(before, after)),
            patch.object(server, "run", return_value=completed([], "ok")),
        ):
            with self.assertRaisesRegex(RuntimeError, "cursor_moved_by_backend"):
                server.call_tool(
                    "send_window_shortcut", {"address": "0x1", "key": "RETURN"}
                )

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
    def test_reports_loaded_native_identity_match(self) -> None:
        source = (
            Path(server.__file__).resolve().parents[2]
            / "hyprland/target-pointer.cpp"
        ).read_bytes()
        expected = native_plugin.plugin_identity("Hyprland version", source)
        native_status = {
            "ok": True,
            **expected,
            "hyprland_build_abi": "abi",
            "hyprland_runtime_abi": "abi",
            "safe_to_inject": True,
        }
        responses = iter(
            (
                completed([], "same-session-target-pointer"),
                completed([], json.dumps(native_status)),
                completed([], "Hyprland version"),
            )
        )
        with (
            patch.object(server.shutil, "which", return_value="/bin/tool"),
            patch.object(server, "plugin_build_requirements", return_value={}),
            patch.object(server, "combine_windows", return_value=[]),
            patch.object(
                server,
                "continuity_status",
                return_value={
                    "enabled": False,
                    "owned": False,
                    "conflict": False,
                    "output": None,
                    "active_output_count": 1,
                    "durable_output_count": 1,
                    "safe_to_disable": False,
                },
            ),
            patch.object(
                server,
                "run",
                side_effect=lambda *_args, **_kwargs: next(responses),
            ),
        ):
            result = server.status()

        self.assertEqual(
            result["versions"],
            {
                "companion": "0.3.0",
                "native_extension_expected": native_plugin.NATIVE_PLUGIN_VERSION,
                "native_extension_loaded": native_plugin.NATIVE_PLUGIN_VERSION,
                "native_source_sha256_expected": expected["source_sha256"],
                "native_source_sha256_loaded": expected["source_sha256"],
                "hyprland_build_sha256_expected": expected[
                    "hyprland_build_sha256"
                ],
                "hyprland_build_sha256_loaded": expected[
                    "hyprland_build_sha256"
                ],
                "hyprland_build_abi": "abi",
                "hyprland_runtime_abi": "abi",
                "native_identity_matches": True,
            },
        )
        self.assertEqual(
            result["capabilities"],
            {
                "exact_background_window_capture": False,
                "targeted_background_shortcuts": True,
                "background_semantic_actions": False,
                "targeted_wayland_pointer": True,
                "targeted_xwayland_pointer": True,
                "cross_process_window_claims": True,
                "parallel_native_wayland_windows": True,
                "broker_global_input_lane_serialized": True,
                "native_input_currently_safe": True,
                "physical_pointer_seat_is_independent": False,
            },
        )

    def test_stale_loaded_plugin_is_not_reported_as_available_or_safe(self) -> None:
        native_status = {
            "ok": True,
            "plugin_version": "stale",
            "safe_to_inject": True,
        }
        responses = iter(
            (
                completed([], "same-session-target-pointer"),
                completed([], json.dumps(native_status)),
                completed([], "Hyprland version"),
            )
        )
        with (
            patch.object(server.shutil, "which", return_value="/bin/tool"),
            patch.object(server, "plugin_build_requirements", return_value={}),
            patch.object(server, "combine_windows", return_value=[]),
            patch.object(
                server,
                "continuity_status",
                return_value={
                    "enabled": False,
                    "owned": False,
                    "conflict": False,
                    "output": None,
                    "active_output_count": 1,
                    "durable_output_count": 1,
                    "safe_to_disable": False,
                },
            ),
            patch.object(
                server,
                "run",
                side_effect=lambda *_args, **_kwargs: next(responses),
            ),
        ):
            result = server.status()

        self.assertEqual(
            result["capabilities"],
            {
                "exact_background_window_capture": False,
                "targeted_background_shortcuts": False,
                "background_semantic_actions": False,
                "targeted_wayland_pointer": False,
                "targeted_xwayland_pointer": False,
                "cross_process_window_claims": True,
                "parallel_native_wayland_windows": True,
                "broker_global_input_lane_serialized": True,
                "native_input_currently_safe": False,
                "physical_pointer_seat_is_independent": False,
            },
        )
        self.assertFalse(result["versions"]["native_identity_matches"])

    def test_reports_buildable_native_capabilities_without_claiming_at_spi(self) -> None:
        requirements = {"compiler": True, "headers": True}
        with (
            patch.object(server.shutil, "which", return_value="/bin/tool"),
            patch.object(server, "plugin_build_requirements", return_value=requirements),
            patch.object(
                server,
                "combine_windows",
                return_value=[{"capture_id": "42", "monitor": 0}],
            ),
            patch.object(
                server,
                "continuity_status",
                return_value={
                    "enabled": False,
                    "owned": False,
                    "conflict": False,
                    "output": None,
                    "active_output_count": 1,
                    "durable_output_count": 1,
                    "safe_to_disable": False,
                },
            ),
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
                "cross_process_window_claims": True,
                "parallel_native_wayland_windows": True,
                "broker_global_input_lane_serialized": True,
                "native_input_currently_safe": False,
                "physical_pointer_seat_is_independent": False,
            },
        )
        self.assertEqual(result["requirements"]["native_plugin_build"], requirements)
        self.assertIn("separate Computer Use plugin", result["requirements"]["background_semantic_actions"])
        self.assertEqual(
            result["semantic_actions"],
            {
                "available": None,
                "claim_enforced": False,
                "provider": "computer-use-linux",
                "note": "Availability is unknown because semantic actions are provided by a separate MCP server.",
            },
        )
        self.assertEqual(result["versions"]["companion"], "0.3.0")
        self.assertEqual(
            result["versions"]["native_extension_expected"],
            native_plugin.NATIVE_PLUGIN_VERSION,
        )
