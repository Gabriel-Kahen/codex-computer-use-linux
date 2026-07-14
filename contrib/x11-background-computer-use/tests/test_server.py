import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from x11_session_computer_use import server


def completed(args, stdout="", stderr="", returncode=0):
    return CompletedProcess(args, returncode, stdout, stderr)


class WindowTests(TestCase):
    def test_display_screen_zero_is_same_x_server(self) -> None:
        self.assertEqual(server._normalized_display(":0.0"), server._normalized_display(":0"))

    def test_process_start_time_handles_spaces_in_process_name(self) -> None:
        stat = "42 (process with spaces) S " + " ".join([*(str(index) for index in range(4, 22)), "start-time"])
        with patch.object(Path, "read_text", return_value=stat):
            self.assertEqual(server._process_start_time(42), "start-time")

    def test_parses_ewmh_windows_and_marks_same_uid(self) -> None:
        pid = os.getpid()
        advertised_pid = pid + 1000
        wmctrl = f"0x01200007  2 {advertised_pid} 10 20 800 600 code.Code host Existing project - Visual Studio Code\n"

        def fake_run(args, **_kwargs):
            if args[:2] == ["xprop", "-root"]:
                return completed(args, "_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x1\n")
            if args[:2] == ["wmctrl", "-lpGx"]:
                return completed(args, wmctrl)
            if args[:2] == ["xdotool", "getactivewindow"]:
                return completed(args, str(int("01200007", 16)))
            if args[:2] == ["xprop", "-id"]:
                return completed(args, "WM_STATE(WM_STATE): window state: Normal\n_NET_WM_WINDOW_TYPE_NORMAL\n")
            raise AssertionError(args)

        with patch.dict(os.environ, {"DISPLAY": ":42", "XDG_SESSION_TYPE": "x11", "XDG_SESSION_ID": ""}, clear=False), patch.object(server, "run", side_effect=fake_run), patch.object(server, "ensure_session"), patch.object(server, "_authenticated_pid", return_value=pid), patch.object(server, "_pid_belongs_to_session", return_value=True):
            windows = server.list_windows()

        self.assertEqual(windows, [{
            "xid": "0x01200007",
            "capture_id": "0x01200007",
            "pid": pid,
            "advertised_pid": advertised_pid,
            "pid_authenticated_by": "XRes",
            "same_uid": True,
            "desktop": 2,
            "x": 10,
            "y": 20,
            "width": 800,
            "height": 600,
            "wm_class": "code.Code",
            "host": "host",
            "title": "Existing project - Visual Studio Code",
            "focused": True,
            "mapped": True,
            "minimized": False,
            "window_type": "_NET_WM_WINDOW_TYPE_NORMAL",
            "xid_lifetime": "stable until this X11 client window is destroyed",
            "at_spi_correlation": {"pid": pid, "title": "Existing project - Visual Studio Code", "wm_class": "code.Code"},
            "controllable": True,
        }])

    def test_refuses_window_without_same_uid_proof(self) -> None:
        window = {"xid": "0x1", "wm_class": "app.App", "title": "App", "same_uid": False}
        with patch.object(server, "list_windows", return_value=[window]):
            with self.assertRaisesRegex(RuntimeError, "not proven"):
                server.resolve_window("0x1")

    def test_list_omits_windows_without_same_uid_proof(self) -> None:
        wmctrl = "0x01200007  2 123 10 20 800 600 app.App host Private title\n"

        with patch.object(server, "ensure_session"), patch.object(server, "run", return_value=completed([], wmctrl)), patch.object(server, "_active_window", return_value=None), patch.object(server, "_authenticated_pid", return_value=None), patch.object(server, "_window_state", return_value={"mapped": True, "minimized": False, "window_type": None}):
            self.assertEqual(server.list_windows(), [])

    def test_session_requires_positive_local_logind_proof(self) -> None:
        with patch.dict(os.environ, {"DISPLAY": ":1", "XDG_SESSION_ID": "7"}, clear=False), patch.object(server, "_local_display_socket", return_value=Path("/tmp")), patch.object(server.shutil, "which", return_value="/usr/bin/loginctl"), patch.object(server, "run", return_value=completed([], "Active=no\nRemote=no\nType=x11\nLeader=2\n")):
            with self.assertRaisesRegex(RuntimeError, "not positively verified"):
                server.ensure_session()

    def test_session_rejects_display_not_registered_with_logind(self) -> None:
        info = "Active=yes\nRemote=no\nType=x11\nDisplay=:0\nLeader=2\n"
        with patch.dict(os.environ, {"DISPLAY": ":99", "XDG_SESSION_ID": "7"}, clear=False), patch.object(server, "_local_display_socket", return_value=Path("/tmp")), patch.object(server.shutil, "which", return_value="/usr/bin/loginctl"), patch.object(server, "run", return_value=completed([], info)):
            with self.assertRaisesRegex(RuntimeError, "registered for the logind session"):
                server.ensure_session()

    def test_xinput_checks_each_non_xtest_slave_and_fails_closed(self) -> None:
        listing = """
⎜   ↳ Virtual core XTEST pointer          id=4    [slave  pointer  (2)]
⎜   ↳ USB Mouse                           id=10   [slave  pointer  (2)]
    ↳ USB Keyboard                        id=11   [slave  keyboard (3)]
"""

        def fake_run(args, **_kwargs):
            if args == ["xinput", "list", "--short"]:
                return completed(args, listing)
            if args == ["xinput", "query-state", "10"]:
                return completed(args, "button[1]=down\n")
            if args == ["xinput", "query-state", "11"]:
                return completed(args, stderr="device vanished", returncode=1)
            raise AssertionError(args)

        with patch.object(server.shutil, "which", return_value="/usr/bin/xinput"), patch.object(server, "run", side_effect=fake_run):
            held = server._held_physical_input()

        self.assertEqual(len(held), 2)
        self.assertIn("USB Mouse", held[0])
        self.assertIn("device vanished", held[1])


class LeaseTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        self.patchers = [
            patch.object(server, "STATE_DIR", directory),
            patch.object(server, "LEASE_FILE", directory / "input-lease.json"),
            patch.object(server, "LOCK_FILE", directory / "input-lease.lock"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_acknowledgment_is_required_before_any_mutation(self) -> None:
        with patch.object(server, "resolve_window") as resolve:
            with self.assertRaisesRegex(ValueError, "acknowledge_interference"):
                server.begin_lease({"window": "0x1", "acknowledge_interference": False})
        resolve.assert_not_called()

    def test_begin_journals_before_focus_and_restore_releases_button(self) -> None:
        target = {"xid": "0x00000020", "pid": 20, "minimized": True, "width": 100, "height": 80}
        target_identity = {"xid": "0x00000020", "pid": 20, "process_start_time": "1", "wm_class": "app"}
        calls = []

        def checked(*args, **_kwargs):
            calls.append(args)
            if args[0] == "windowactivate":
                self.assertTrue(server.LEASE_FILE.exists())

        active_identity = {"xid": "0x00000010", "pid": 10, "process_start_time": "1", "wm_class": "other"}
        with patch.object(server, "ensure_session", return_value={"session": "same"}), patch.object(server, "_ensure_input_safe"), patch.object(server, "resolve_window", return_value=target), patch.object(server, "_window_identity", side_effect=[target_identity, active_identity]), patch.object(server, "_identity_matches", return_value=True), patch.object(server, "_active_window", return_value="0x00000010"), patch.object(server, "_desktop", return_value=3), patch.object(server, "_pointer", return_value={"x": 4, "y": 5}), patch.object(server, "_checked_xdotool", side_effect=checked), patch.object(server, "_validate_lease_binding"), patch.object(server, "_validate_session_binding"), patch.object(server, "run", return_value=completed([])) as run:
            result = server.begin_lease({"window": "0x20", "acknowledge_interference": True})
            state = json.loads(server.LEASE_FILE.read_text())
            state["pressed_button"] = "1"
            server.save_lease(state)
            state_mode = server.STATE_DIR.stat().st_mode & 0o777
            lease_mode = server.LEASE_FILE.stat().st_mode & 0o777
            restored = server.restore_lease(state)

        self.assertIn("lease_token", result)
        self.assertEqual(state_mode, 0o700)
        self.assertEqual(lease_mode, 0o600)
        self.assertTrue(restored["restored"])
        self.assertFalse(server.LEASE_FILE.exists())
        self.assertIn(["xdotool", "mouseup", "1"], [call.args[0] for call in run.call_args_list])
        self.assertIn(("windowactivate", "--sync", "0x00000010"), calls)

    def test_drag_journals_pressed_button_and_clears_it(self) -> None:
        state = {"token": "secret", "target": {"xid": "0x20", "width": 100, "height": 80}, "pressed_button": None}
        server.save_lease(state)
        seen_pressed = False

        def checked(*args, **_kwargs):
            nonlocal seen_pressed
            if args[0] == "mousedown":
                seen_pressed = json.loads(server.LEASE_FILE.read_text())["pressed_button"] == "1"

        with patch.object(server, "_validate_lease_binding"), patch.object(server, "_ensure_target_active"), patch.object(server, "_pointer", return_value={"x": 9, "y": 9}), patch.object(server, "_checked_xdotool", side_effect=checked), patch.object(server, "run", return_value=completed([])):
            result = server.lease_pointer({"lease_token": "secret", "start_x": 1, "start_y": 2, "end_x": 20, "end_y": 30}, "drag")

        self.assertTrue(seen_pressed)
        self.assertIsNone(json.loads(server.LEASE_FILE.read_text())["pressed_button"])
        self.assertTrue(result["pointer_restored"])

    def test_unsafe_held_input_blocks_focus_lease(self) -> None:
        with patch.object(server, "_lock_state", return_value=False), patch.object(server, "_held_physical_input", return_value=["button 1"]):
            with self.assertRaisesRegex(RuntimeError, "button 1"):
                server._ensure_input_safe()

    def test_unknown_lock_state_blocks_focus_lease(self) -> None:
        with patch.object(server, "_lock_state", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cannot verify"):
                server._ensure_input_safe()

    def test_failed_drag_release_remains_journaled_for_recovery(self) -> None:
        state = {"token": "secret", "target": {"xid": "0x20", "width": 100, "height": 80}, "pressed_button": None}
        server.save_lease(state)
        failed = completed([], stderr="device disappeared", returncode=1)
        with patch.object(server, "_validate_lease_binding"), patch.object(server, "_ensure_target_active"), patch.object(server, "_pointer", return_value={"x": 9, "y": 9}), patch.object(server, "_checked_xdotool"), patch.object(server, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "device disappeared"):
                server.lease_pointer({"lease_token": "secret", "start_x": 1, "start_y": 2, "end_x": 20, "end_y": 30}, "drag")

        self.assertEqual(json.loads(server.LEASE_FILE.read_text())["pressed_button"], "1")

    def test_pointer_restore_failure_is_not_reported_as_success(self) -> None:
        with patch.object(server, "_pointer", return_value={"x": 1, "y": 2}), patch.object(server, "_checked_xdotool", side_effect=RuntimeError("restore failed")):
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                server._with_pointer_restore(lambda: None)

    def test_pointer_action_is_not_started_without_restore_snapshot(self) -> None:
        action_called = False

        def action() -> None:
            nonlocal action_called
            action_called = True

        with patch.object(server, "_pointer", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "was not started"):
                server._with_pointer_restore(action)

        self.assertFalse(action_called)

    def test_recovery_fingerprint_mismatch_mutates_nothing(self) -> None:
        state = {"session_fingerprint": {"socket_inode": 1}, "target_identity": {"xid": "0x20"}}
        with patch.object(server, "ensure_session", return_value={"socket_inode": 2}), patch.object(server, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "fingerprint changed"):
                server.restore_lease(state)
        run.assert_not_called()

    def test_changed_target_identity_does_not_block_button_release(self) -> None:
        fingerprint = {"socket_inode": 1}
        state = {"session_fingerprint": fingerprint, "target_identity": {"xid": "0x20"}, "target": {"xid": "0x20"}, "original": {}, "pressed_button": "1"}
        with patch.object(server, "ensure_session", return_value=fingerprint), patch.object(server, "_ensure_input_safe"), patch.object(server, "_identity_matches", return_value=False), patch.object(server, "run", return_value=completed([])) as run:
            restored = server.restore_lease(state)

        self.assertTrue(restored["restored"])
        self.assertEqual(run.call_args.args[0], ["xdotool", "mouseup", "1"])

    def test_begin_refuses_unrestorable_active_window(self) -> None:
        target = {"xid": "0x20", "pid": 20, "minimized": False, "width": 100, "height": 80}
        target_identity = {"xid": "0x20", "pid": 20, "process_start_time": "1", "wm_class": "app"}
        with patch.object(server, "ensure_session", return_value={"session": "same"}), patch.object(server, "_ensure_input_safe"), patch.object(server, "resolve_window", return_value=target), patch.object(server, "_window_identity", side_effect=[target_identity, None]), patch.object(server, "_active_window", return_value="0x10"), patch.object(server, "_pointer", return_value={"x": 1, "y": 2}), patch.object(server, "_checked_xdotool") as xdotool:
            with self.assertRaisesRegex(RuntimeError, "active window"):
                server.begin_lease({"window": "0x20", "acknowledge_interference": True})

        xdotool.assert_not_called()


class StatusTests(TestCase):
    def test_invalid_session_disables_input_capabilities(self) -> None:
        with patch.object(server.shutil, "which", return_value="/bin/tool"), patch.object(server, "list_windows", side_effect=RuntimeError("not an EWMH session")), patch.object(server, "build_requirements", return_value={"capture": True}):
            result = server.status()

        self.assertEqual(result["session_error"], "not an EWMH session")
        self.assertFalse(result["capabilities"]["best_effort_no_focus_shortcuts"])
        self.assertFalse(result["capabilities"]["reliable_journaled_focus_pointer_lease"])


class McpErrorTests(TestCase):
    def test_expected_tool_failure_is_an_is_error_result(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "missing", "arguments": {}}}
        response = server.dispatch(request)

        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])

    def test_direct_shortcut_checks_safety_without_clearing_modifiers(self) -> None:
        window = {"xid": "0x20"}
        with patch.object(server, "lease_guard", return_value=contextlib.nullcontext()), patch.object(server, "load_lease", return_value=None), patch.object(server, "resolve_window", return_value=window), patch.object(server, "ensure_session"), patch.object(server, "_ensure_input_safe") as safety, patch.object(server, "run", return_value=completed([])) as run:
            result = server.call_tool("send_window_shortcut", {"window": "0x20", "key": "x", "modifiers": "CTRL"})

        safety.assert_called_once_with()
        self.assertEqual(run.call_args.args[0], ["xdotool", "key", "--window", "0x20", "ctrl+x"])
        self.assertFalse(result["isError"])
