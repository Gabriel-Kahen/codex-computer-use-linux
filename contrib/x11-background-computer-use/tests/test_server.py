import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from subprocess import CompletedProcess
from unittest import TestCase
from unittest.mock import call
from unittest.mock import Mock
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from x11_session_computer_use import server


def completed(args, stdout="", stderr="", returncode=0):
    return CompletedProcess(args, returncode, stdout, stderr)


class WindowTests(TestCase):
    def test_display_screens_and_unix_prefix_are_same_x_server(self) -> None:
        displays = [":0", ":0.0", ":0.1", "unix:0", "unix:0.1"]
        self.assertEqual([server._normalized_display(display) for display in displays], [":0"] * 5)

    def test_process_start_time_handles_spaces_in_process_name(self) -> None:
        stat = "42 (process with spaces) S " + " ".join([*(str(index) for index in range(4, 22)), "start-time"])
        with patch.object(Path, "read_text", return_value=stat):
            self.assertEqual(server._process_start_time(42), "start-time")

    def test_mutable_wm_class_does_not_split_window_identity(self) -> None:
        expected = {
            "xid": "0x20",
            "pid": 20,
            "process_start_time": "1",
            "wm_class": "old.App",
        }
        current = {"xid": "0x20", "pid": 20, "process_start_time": "1"}

        with patch.object(server, "_window_identity", return_value=current):
            self.assertIs(server._identity_matches(expected), server.IdentityMatch.MATCH)

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

    def test_identity_probe_recognizes_a_replaced_window(self) -> None:
        expected = {"xid": "0x20", "pid": 20}
        replacement = {"xid": "0x20", "pid": 30}
        with patch.object(server, "_window_identity", return_value=replacement), patch.object(server, "run") as run:
            result = server._identity_matches(expected)

        self.assertEqual(result, server.IdentityMatch.CHANGED)
        run.assert_not_called()

    def test_identity_probe_recognizes_a_closed_window(self) -> None:
        expected = {"xid": "0x20", "pid": 20}
        closed = completed([], stderr="X Error of failed request: BadWindow", returncode=1)
        with patch.object(server, "_window_identity", return_value=None), patch.object(server, "run", return_value=closed):
            result = server._identity_matches(expected)

        self.assertEqual(result, server.IdentityMatch.CHANGED)

    def test_identity_probe_failure_is_indeterminate(self) -> None:
        expected = {"xid": "0x20", "pid": 20}
        failed = completed([], stderr="temporary XRes failure", returncode=1)
        with patch.object(server, "_window_identity", return_value=None), patch.object(server, "run", return_value=failed):
            result = server._identity_matches(expected)

        self.assertEqual(result, server.IdentityMatch.INDETERMINATE)


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

    def test_load_lease_treats_concurrent_unlink_as_absent(self) -> None:
        server.LEASE_FILE.write_text("{}")
        original_read_text = Path.read_text

        def unlink_before_read(path: Path, *args, **kwargs):
            if path == server.LEASE_FILE:
                path.unlink()
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", unlink_before_read):
            self.assertIsNone(server.load_lease())

    def test_load_lease_surfaces_malformed_journal(self) -> None:
        server.LEASE_FILE.write_text("not json")

        with self.assertRaises(json.JSONDecodeError):
            server.load_lease()

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
            if args[0] == "windowactivate" and args[-1] == target["xid"]:
                self.assertTrue(server.LEASE_FILE.exists())
                lease_state = json.loads(server.LEASE_FILE.read_text())
                claim_state = json.loads(
                    (server.STATE_DIR / "window-claims.json").read_text()
                )["claims"][0]
                self.assertGreater(lease_state["owner_inflight_until"], time.time())
                self.assertGreater(claim_state["inflight_until"], time.time())

        active_identity = {"xid": "0x00000010", "pid": 10, "process_start_time": "1", "wm_class": "other"}
        with patch.object(server, "ensure_session", return_value={"session": "same"}), patch.object(server, "_ensure_input_safe"), patch.object(server, "resolve_window", return_value=target), patch.object(server, "_window_identity", side_effect=[target_identity, active_identity]), patch.object(server, "_identity_matches", return_value=server.IdentityMatch.MATCH), patch.object(server, "_active_window", return_value="0x00000010"), patch.object(server, "_desktop", return_value=3), patch.object(server, "_pointer", return_value={"x": 4, "y": 5}), patch.object(server, "_checked_xdotool", side_effect=checked), patch.object(server, "_validate_lease_binding"), patch.object(server, "_validate_session_binding"), patch.object(server, "run", return_value=completed([])) as run:
            result = server.begin_lease({"window": "0x20", "acknowledge_interference": True})
            state = json.loads(server.LEASE_FILE.read_text())
            active_claim = json.loads(
                (server.STATE_DIR / "window-claims.json").read_text()
            )["claims"][0]
            self.assertNotIn("owner_inflight_until", state)
            self.assertNotIn("inflight_until", active_claim)
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

    def test_lease_operation_marks_both_owners_and_renews_after_success(self) -> None:
        fingerprint = {
            "display": ":42",
            "socket_inode": 123,
            "wm_start_time": "1",
        }
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        now = [100.0]
        store = server.WindowClaimStore(
            server.STATE_DIR,
            fingerprint,
            clock=lambda: now[0],
        )
        claim = store.claim(
            "thread-a",
            {"xid": "0x20"},
            identity,
            lease_seconds=5,
        )
        server.save_lease(
            {
                "token": "lease-token",
                "owner_thread_id": "thread-a",
                "owner_expires_at": 1.0,
                "session_fingerprint": fingerprint,
                "target_identity": identity,
                "target": {"xid": "0x20"},
                "window_claim_token": claim["claim_token"],
            }
        )
        now[0] = 102.0

        def checked(*_args, **_kwargs):
            lease_state = json.loads(server.LEASE_FILE.read_text())
            claim_state = json.loads(
                (server.STATE_DIR / "window-claims.json").read_text()
            )["claims"][0]
            self.assertGreater(lease_state["owner_inflight_until"], time.time())
            self.assertEqual(claim_state["inflight_until"], 402.0)

        with (
            patch.object(server, "_validate_lease_binding"),
            patch.object(server, "_ensure_target_active"),
            patch.object(server, "_claim_store", return_value=store),
            patch.object(server, "_checked_xdotool", side_effect=checked),
        ):
            server.lease_key(
                {
                    "lease_token": "lease-token",
                    "claim_token": claim["claim_token"],
                    "key": "x",
                },
                "thread-a",
            )

        lease_state = json.loads(server.LEASE_FILE.read_text())
        claim_state = json.loads(
            (server.STATE_DIR / "window-claims.json").read_text()
        )["claims"][0]
        self.assertNotIn("owner_inflight_until", lease_state)
        self.assertGreater(lease_state["owner_expires_at"], time.time())
        self.assertNotIn("inflight_until", claim_state)
        self.assertEqual(claim_state["expires_at"], 107.0)

    def test_failed_lease_operation_does_not_renew_either_owner(self) -> None:
        fingerprint = {"display": ":42", "socket_inode": 123}
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        now = [100.0]
        store = server.WindowClaimStore(
            server.STATE_DIR,
            fingerprint,
            clock=lambda: now[0],
        )
        claim = store.claim(
            "thread-a",
            {"xid": "0x20"},
            identity,
            lease_seconds=5,
        )
        server.save_lease(
            {
                "token": "lease-token",
                "owner_thread_id": "thread-a",
                "owner_expires_at": 50.0,
                "session_fingerprint": fingerprint,
                "target_identity": identity,
                "target": {"xid": "0x20"},
                "window_claim_token": claim["claim_token"],
            }
        )
        now[0] = 102.0

        with (
            patch.object(server, "_validate_lease_binding"),
            patch.object(server, "_ensure_target_active"),
            patch.object(server, "_claim_store", return_value=store),
            patch.object(server, "_checked_xdotool", side_effect=RuntimeError("failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                server.lease_key(
                    {
                        "lease_token": "lease-token",
                        "claim_token": claim["claim_token"],
                        "key": "x",
                    },
                    "thread-a",
                )

        lease_state = json.loads(server.LEASE_FILE.read_text())
        claim_state = json.loads(
            (server.STATE_DIR / "window-claims.json").read_text()
        )["claims"][0]
        self.assertNotIn("owner_inflight_until", lease_state)
        self.assertEqual(lease_state["owner_expires_at"], 50.0)
        self.assertNotIn("inflight_until", claim_state)
        self.assertEqual(claim_state["expires_at"], 105.0)

    def test_begin_tool_guards_revalidates_and_forwards_the_owner(self) -> None:
        target = {"xid": "0x20", "minimized": False, "width": 100, "height": 80}
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        fingerprint = {"display": ":42", "socket_inode": 123}
        arguments = {"window": "0x20", "acknowledge_interference": True}
        events = []
        store = Mock(unsafe=True)

        @contextlib.contextmanager
        def lease_guard():
            events.append("lease-enter")
            try:
                yield
            finally:
                events.append("lease-exit")

        @contextlib.contextmanager
        def window_guard(guarded_identity):
            self.assertEqual(guarded_identity, identity)
            events.append("window-enter")
            try:
                yield
            finally:
                events.append("window-exit")

        def resolve(_window):
            events.append("resolve")
            return target, identity, fingerprint

        def validate(validated_fingerprint):
            self.assertEqual(validated_fingerprint, fingerprint)
            events.append("validate")

        def begin(received_arguments, owner, resolved_target, session_fingerprint):
            self.assertEqual(
                (received_arguments, owner, resolved_target, session_fingerprint),
                (arguments, "thread-a", (target, identity), fingerprint),
            )
            events.append("begin")
            return {"lease_token": "secret"}

        store.window_guard.side_effect = window_guard
        with (
            patch.object(server, "_resolve_bound_target", side_effect=resolve),
            patch.object(server, "_claim_store", return_value=store),
            patch.object(server, "lease_guard", side_effect=lease_guard),
            patch.object(server, "_validate_session_fingerprint", side_effect=validate),
            patch.object(server, "begin_lease", side_effect=begin),
        ):
            result = server.call_tool("begin_input_lease", arguments, "thread-a")

        self.assertEqual(result["structuredContent"], {"lease_token": "secret"})
        self.assertEqual(
            events,
            [
                "resolve",
                "lease-enter",
                "window-enter",
                "validate",
                "begin",
                "window-exit",
                "lease-exit",
            ],
        )

    def test_begin_tool_stops_when_guarded_session_revalidation_fails(self) -> None:
        fingerprint = {"display": ":42", "socket_inode": 123}
        target = ({"xid": "0x20"}, {"xid": "0x20", "pid": 20})
        store = Mock(unsafe=True)
        store.window_guard.side_effect = lambda _identity: contextlib.nullcontext()
        with (
            patch.object(
                server,
                "_resolve_bound_target",
                return_value=(*target, fingerprint),
            ),
            patch.object(server, "_claim_store", return_value=store),
            patch.object(
                server,
                "_validate_session_fingerprint",
                side_effect=RuntimeError("session changed"),
            ),
            patch.object(server, "begin_lease") as begin,
        ):
            with self.assertRaisesRegex(RuntimeError, "session changed"):
                server.call_tool(
                    "begin_input_lease",
                    {"window": "0x20", "acknowledge_interference": True},
                    "thread-a",
                )

        begin.assert_not_called()

    def test_key_tool_orders_guards_revalidation_and_claim_fencing(self) -> None:
        fingerprint = {"display": ":42", "socket_inode": 123}
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        server.save_lease(
            {
                "token": "lease-token",
                "owner_thread_id": "thread-a",
                "session_fingerprint": fingerprint,
                "target_identity": identity,
                "target": {"xid": "0x20", "width": 100, "height": 80},
                "window_claim_token": "claim-token",
            }
        )
        events = []
        store = Mock(unsafe=True)

        @contextlib.contextmanager
        def lease_guard():
            events.append("lease-enter")
            try:
                yield
            finally:
                events.append("lease-exit")

        @contextlib.contextmanager
        def window_guard():
            events.append("window-enter")
            try:
                yield
            finally:
                events.append("window-exit")

        def assert_access(owner, target_identity, token, *, mark_inflight):
            self.assertEqual(
                (owner, target_identity, token, mark_inflight),
                ("thread-a", identity, "claim-token", True),
            )
            events.append("claim-access")
            return {"claim_token": "claim-token"}

        def finish_access(owner, target_identity, token, *, renew):
            self.assertEqual(
                (owner, target_identity, token, renew),
                ("thread-a", identity, "claim-token", True),
            )
            events.append("claim-finish")

        store.assert_access.side_effect = assert_access
        store.finish_access.side_effect = finish_access
        with (
            patch.object(server, "lease_guard", side_effect=lease_guard),
            patch.object(server, "_lease_window_guard", side_effect=window_guard),
            patch.object(server, "_validate_lease_binding", side_effect=lambda _state: events.append("revalidate")),
            patch.object(server, "_claim_store", return_value=store),
            patch.object(server, "_ensure_target_active", side_effect=lambda _state: events.append("target-active")),
            patch.object(server, "_checked_xdotool", side_effect=lambda *_args: events.append("key")),
        ):
            result = server.call_tool(
                "lease_key",
                {
                    "lease_token": "lease-token",
                    "claim_token": "claim-token",
                    "key": "x",
                },
                "thread-a",
            )

        self.assertTrue(result["structuredContent"]["sent"])
        self.assertEqual(
            events,
            [
                "lease-enter",
                "window-enter",
                "revalidate",
                "claim-access",
                "target-active",
                "key",
                "claim-finish",
                "window-exit",
                "lease-exit",
            ],
        )

    def test_pointer_tools_route_each_action_successfully(self) -> None:
        cases = {
            "lease_pointer_click": {
                "lease_token": "secret",
                "x": 1,
                "y": 2,
            },
            "lease_pointer_scroll": {
                "lease_token": "secret",
                "x": 1,
                "y": 2,
                "steps": 2,
            },
            "lease_pointer_drag": {
                "lease_token": "secret",
                "start_x": 1,
                "start_y": 2,
                "end_x": 20,
                "end_y": 30,
            },
        }
        for name, arguments in cases.items():
            with self.subTest(name=name):
                server.save_lease(
                    {
                        "token": "secret",
                        "owner_thread_id": "thread-a",
                        "target": {"xid": "0x20", "width": 100, "height": 80},
                        "pressed_button": None,
                    }
                )
                with (
                    patch.object(server, "_validate_lease_binding"),
                    patch.object(server, "_ensure_target_active"),
                    patch.object(server, "_pointer", return_value={"x": 9, "y": 9}),
                    patch.object(server, "_checked_xdotool"),
                    patch.object(server, "run", return_value=completed([])),
                ):
                    result = server.call_tool(name, arguments, "thread-a")

                self.assertEqual(
                    result["structuredContent"]["action"],
                    name.removeprefix("lease_pointer_"),
                )
                self.assertTrue(result["structuredContent"]["pointer_restored"])

    def test_lease_tools_reject_foreign_owners_and_tokens_before_input(self) -> None:
        cases = {
            "lease_key": {"lease_token": "secret", "key": "x"},
            "lease_pointer_click": {"lease_token": "secret", "x": 1, "y": 2},
            "lease_pointer_scroll": {
                "lease_token": "secret",
                "x": 1,
                "y": 2,
                "steps": 1,
            },
            "lease_pointer_drag": {
                "lease_token": "secret",
                "start_x": 1,
                "start_y": 2,
                "end_x": 20,
                "end_y": 30,
            },
        }
        server.save_lease(
            {
                "token": "secret",
                "owner_thread_id": "thread-a",
                "target": {"xid": "0x20", "width": 100, "height": 80},
            }
        )
        with patch.object(server, "_ensure_target_active") as ensure_target_active:
            for name, arguments in cases.items():
                with self.subTest(name=name, failure="owner"):
                    with self.assertRaisesRegex(RuntimeError, "belongs to another"):
                        server.call_tool(name, arguments, "thread-b")
                with self.subTest(name=name, failure="token"):
                    with self.assertRaisesRegex(ValueError, "lease token"):
                        server.call_tool(
                            name,
                            {**arguments, "lease_token": "wrong"},
                            "thread-a",
                        )

        ensure_target_active.assert_not_called()

    def test_pointer_tools_reject_invalid_actions_before_pointer_mutation(self) -> None:
        cases = {
            "lease_pointer_click": (
                {"lease_token": "secret", "x": 100, "y": 2},
                "outside window",
            ),
            "lease_pointer_scroll": (
                {"lease_token": "secret", "x": 1, "y": 2, "steps": 0},
                "excluding zero",
            ),
            "lease_pointer_drag": (
                {
                    "lease_token": "secret",
                    "start_x": 1,
                    "start_y": 2,
                    "end_x": 100,
                    "end_y": 30,
                },
                "outside window",
            ),
        }
        for name, (arguments, error) in cases.items():
            with self.subTest(name=name):
                server.save_lease(
                    {
                        "token": "secret",
                        "owner_thread_id": "thread-a",
                        "target": {"xid": "0x20", "width": 100, "height": 80},
                    }
                )
                with (
                    patch.object(server, "_validate_lease_binding"),
                    patch.object(server, "_ensure_target_active"),
                    patch.object(server, "_pointer") as pointer,
                ):
                    with self.assertRaisesRegex((ValueError, RuntimeError), error):
                        server.call_tool(name, arguments, "thread-a")

                pointer.assert_not_called()

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

    def test_end_restores_state_after_target_window_closes(self) -> None:
        fingerprint = {"socket_inode": 1}
        active_identity = {"xid": "0x10"}
        target_identity = {"xid": "0x20"}
        state = {
            "token": "secret",
            "session_fingerprint": fingerprint,
            "target_identity": target_identity,
            "target": {"xid": "0x20"},
            "original": {
                "active_xid": "0x10",
                "active_identity": active_identity,
                "desktop": 2,
                "pointer": {"x": 4, "y": 5},
                "target_minimized": True,
            },
            "pressed_button": "1",
        }
        server.save_lease(state)

        def identity(xid):
            return active_identity if xid == active_identity["xid"] else None

        def run_command(args, **_kwargs):
            if args[:2] == ["xprop", "-id"]:
                return completed(args, stderr="X Error of failed request: BadWindow", returncode=1)
            return completed(args)

        with (
            patch.object(server, "ensure_session", return_value=fingerprint),
            patch.object(server, "_ensure_input_safe"),
            patch.object(server, "_window_identity", side_effect=identity),
            patch.object(server, "run", side_effect=run_command) as run,
        ):
            result = server.call_tool("end_input_lease", {"lease_token": "secret"})

        self.assertTrue(result["structuredContent"]["restored"])
        self.assertFalse(server.LEASE_FILE.exists())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["xdotool", "mouseup", "1"], commands)
        self.assertIn(["xdotool", "windowactivate", "--sync", "0x10"], commands)
        self.assertIn(["xdotool", "mousemove", "--sync", "4", "5"], commands)
        self.assertNotIn(["xdotool", "windowminimize", "0x20"], commands)

    def test_end_retains_journal_when_target_identity_probe_is_indeterminate(self) -> None:
        fingerprint = {"socket_inode": 1}
        state = {
            "token": "secret",
            "session_fingerprint": fingerprint,
            "target_identity": {"xid": "0x20"},
            "target": {"xid": "0x20"},
            "original": {"desktop": 2, "target_minimized": True, "pointer": {"x": 4, "y": 5}},
            "pressed_button": "1",
        }
        server.save_lease(state)

        with (
            patch.object(server, "ensure_session", return_value=fingerprint),
            patch.object(server, "_ensure_input_safe"),
            patch.object(server, "_identity_matches", return_value=server.IdentityMatch.INDETERMINATE),
            patch.object(server, "run", return_value=completed([])) as run,
        ):
            result = server.call_tool("end_input_lease", {"lease_token": "secret"})

        self.assertFalse(result["structuredContent"]["restored"])
        self.assertIn("could not be verified", result["structuredContent"]["errors"][0])
        self.assertTrue(server.LEASE_FILE.exists())
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["xdotool", "mouseup", "1"], commands)
        self.assertIn(["xdotool", "set_desktop", "2"], commands)
        self.assertIn(["xdotool", "mousemove", "--sync", "4", "5"], commands)
        self.assertNotIn(["xdotool", "windowminimize", "0x20"], commands)

    def test_end_still_requires_the_lease_token(self) -> None:
        server.save_lease({"token": "secret"})

        with patch.object(server, "restore_lease") as restore:
            with self.assertRaisesRegex(ValueError, "lease token"):
                server.call_tool("end_input_lease", {"lease_token": "wrong"})

        restore.assert_not_called()
        self.assertTrue(server.LEASE_FILE.exists())

    def test_lease_actions_still_require_the_live_target_identity(self) -> None:
        server.save_lease({"token": "secret"})

        with (
            patch.object(server, "_validate_lease_binding", side_effect=RuntimeError("target identity changed")),
            patch.object(server, "_checked_xdotool") as xdotool,
        ):
            with self.assertRaisesRegex(RuntimeError, "target identity changed"):
                server.lease_key({"lease_token": "secret", "key": "x"})

        xdotool.assert_not_called()

    def test_begin_refuses_unrestorable_active_window(self) -> None:
        target = {"xid": "0x20", "pid": 20, "minimized": False, "width": 100, "height": 80}
        target_identity = {"xid": "0x20", "pid": 20, "process_start_time": "1", "wm_class": "app"}
        with patch.object(server, "ensure_session", return_value={"session": "same"}), patch.object(server, "_ensure_input_safe"), patch.object(server, "resolve_window", return_value=target), patch.object(server, "_window_identity", side_effect=[target_identity, None]), patch.object(server, "_active_window", return_value="0x10"), patch.object(server, "_pointer", return_value={"x": 1, "y": 2}), patch.object(server, "_checked_xdotool") as xdotool:
            with self.assertRaisesRegex(RuntimeError, "active window"):
                server.begin_lease({"window": "0x20", "acknowledge_interference": True})

        xdotool.assert_not_called()

    def test_foreign_agent_cannot_use_a_live_input_lease(self) -> None:
        state = {
            "token": "secret",
            "owner_thread_id": "thread-a",
            "owner_expires_at": time.time() + 60,
        }
        server.save_lease(state)

        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            server.require_lease("secret", "thread-b")

    def test_foreign_recovery_waits_for_owner_expiry(self) -> None:
        state = {
            "token": "secret",
            "owner_thread_id": "thread-a",
            "owner_expires_at": time.time() + 60,
        }
        server.save_lease(state)

        with self.assertRaisesRegex(RuntimeError, "still belongs"):
            server.call_tool("recover_input_lease", {}, "thread-b")

    def test_foreign_recovery_waits_for_an_inflight_owner(self) -> None:
        state = {
            "token": "secret",
            "owner_thread_id": "thread-a",
            "owner_expires_at": time.time() - 1,
            "owner_inflight_until": time.time() + 60,
        }
        server.save_lease(state)

        with self.assertRaisesRegex(RuntimeError, "still belongs"):
            server.call_tool("recover_input_lease", {}, "thread-b")

    def test_foreign_agent_recovers_only_after_owner_and_claim_expire(self) -> None:
        state = {
            "token": "secret",
            "owner_thread_id": "thread-a",
            "owner_expires_at": time.time() - 1,
            "session_fingerprint": {"display": ":42", "socket_inode": 123},
            "target_identity": {
                "xid": "0x20",
                "pid": 20,
                "process_start_time": "1",
            },
            "window_claim_token": "claim-token",
        }
        server.save_lease(state)
        restored = {"restored": True}
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()
        store.is_live.side_effect = [True, False]

        with (
            patch.object(server, "_claim_store", return_value=store),
            patch.object(server, "restore_lease", return_value=restored) as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "live window claim"):
                server.call_tool("recover_input_lease", {}, "thread-b")
            result = server.call_tool("recover_input_lease", {}, "thread-b")

        self.assertEqual(result["structuredContent"], restored)
        restore.assert_called_once_with(state)
        self.assertEqual(store.is_live.call_count, 2)

    def test_recovery_selects_and_revalidates_inside_the_same_window_guard(self) -> None:
        state = {
            "token": "secret",
            "owner_thread_id": "thread-a",
            "owner_expires_at": time.time() - 1,
            "session_fingerprint": {"display": ":42", "socket_inode": 123},
            "target_identity": {
                "xid": "0x20",
                "pid": 20,
                "process_start_time": "1",
            },
        }
        server.save_lease(state)
        inside_guard = False
        selected = []

        @contextlib.contextmanager
        def guarded(selected_state):
            nonlocal inside_guard
            selected.append(selected_state)
            inside_guard = True
            try:
                yield
            finally:
                inside_guard = False

        def recovery_allowed(revalidated_state, owner):
            self.assertTrue(inside_guard)
            self.assertEqual(revalidated_state, state)
            self.assertEqual(owner, "thread-b")

        restored = {"restored": True}
        with (
            patch.object(server, "_lease_window_guard", side_effect=guarded),
            patch.object(server, "_recovery_allowed", side_effect=recovery_allowed),
            patch.object(server, "restore_lease", return_value=restored),
        ):
            result = server.call_tool("recover_input_lease", {}, "thread-b")

        self.assertEqual(selected, [state])
        self.assertEqual(result["structuredContent"], restored)

    def test_recovery_rejects_a_journal_rebound_while_acquiring_its_guard(self) -> None:
        state = {
            "token": "secret",
            "owner_thread_id": "thread-a",
            "owner_expires_at": time.time() - 1,
            "session_fingerprint": {"display": ":42", "socket_inode": 123},
            "target_identity": {
                "xid": "0x20",
                "pid": 20,
                "process_start_time": "1",
            },
        }
        rebound = {
            **state,
            "session_fingerprint": {"display": ":42", "socket_inode": 999},
            "target_identity": {
                "xid": "0x30",
                "pid": 30,
                "process_start_time": "2",
            },
        }
        server.save_lease(state)

        @contextlib.contextmanager
        def guarded(selected_state):
            self.assertEqual(selected_state, state)
            server.save_lease(rebound)
            yield

        with (
            patch.object(server, "_lease_window_guard", side_effect=guarded),
            patch.object(server, "_recovery_allowed") as recovery_allowed,
            patch.object(server, "restore_lease") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "changed while its stable window guard"):
                server.call_tool("recover_input_lease", {}, "thread-b")

        recovery_allowed.assert_not_called()
        restore.assert_not_called()

    def test_unfinished_input_lease_blocks_foreign_reclaim_after_claim_expiry(self) -> None:
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        state = {
            "token": "lease-token",
            "owner_thread_id": "thread-a",
            "target_identity": identity,
        }
        server.save_lease(state)
        window = {"xid": "0x20", "pid": 20}

        fingerprint = {"display": ":42", "socket_inode": 123, "wm_start_time": "1"}
        with patch.object(server, "_resolve_bound_target", return_value=(window, identity, fingerprint)), patch.object(server, "ensure_session", return_value=fingerprint), patch.object(server, "_identity_matches", return_value=server.IdentityMatch.MATCH):
            with self.assertRaisesRegex(RuntimeError, "unfinished input lease"):
                server.call_tool("claim_session_window", {"window": "0x20"}, "thread-b")

    def test_same_owner_cannot_replace_claim_bound_to_an_unfinished_lease(self) -> None:
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        fingerprint = {"display": ":42", "socket_inode": 123, "wm_start_time": "1"}
        server.save_lease(
            {
                "token": "lease-token",
                "owner_thread_id": "thread-a",
                "owner_expires_at": time.time() - 1,
                "target_identity": identity,
                "window_claim_token": "expired-claim-token",
            }
        )
        window = {"xid": "0x20", "pid": 20}
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()

        with (
            patch.object(server, "_resolve_bound_target", return_value=(window, identity, fingerprint)),
            patch.object(server, "ensure_session", return_value=fingerprint),
            patch.object(server, "_identity_matches", return_value=server.IdentityMatch.MATCH),
            patch.object(server, "_claim_store", return_value=store),
        ):
            with self.assertRaisesRegex(RuntimeError, "end or recover"):
                server.call_tool("claim_session_window", {"window": "0x20"}, "thread-a")

        store.claim.assert_not_called()

    def test_bound_claim_cannot_be_released_before_input_lease_cleanup(self) -> None:
        server.save_lease({"token": "lease-token", "window_claim_token": "claim-token"})

        store = Mock()

        def release(_owner, _token, *, validate_guarded, before_release):
            validate_guarded()
            before_release(
                {
                    "token": "claim-token",
                    "window_identity": {
                        "xid": "0x20",
                        "pid": 20,
                        "process_start_time": "1",
                    },
                }
            )

        store.release.side_effect = release
        store.session_fingerprint = {"display": ":42", "socket_inode": 123}
        with patch.object(server, "_claim_store", return_value=store), patch.object(
            server,
            "ensure_session",
            return_value=store.session_fingerprint,
        ):
            with self.assertRaisesRegex(RuntimeError, "end or recover"):
                server.call_tool(
                    "release_session_window",
                    {"claim_token": "claim-token"},
                    "thread-a",
                )


class StatusTests(TestCase):
    def test_invalid_session_disables_input_capabilities(self) -> None:
        with patch.object(server.shutil, "which", return_value="/bin/tool"), patch.object(server, "list_windows", side_effect=RuntimeError("not an EWMH session")), patch.object(server, "build_requirements", return_value={"capture": True}):
            result = server.status()

        self.assertEqual(result["session_error"], "not an EWMH session")
        self.assertFalse(result["capabilities"]["best_effort_no_focus_shortcuts"])
        self.assertFalse(result["capabilities"]["reliable_journaled_focus_pointer_lease"])

    def test_session_and_claim_status_errors_are_bounded(self) -> None:
        huge_error = RuntimeError("é" * (server.MAX_ERROR_RESULT_BYTES * 4))
        with (
            patch.object(server.shutil, "which", return_value="/bin/tool"),
            patch.object(server, "list_windows", side_effect=huge_error),
            patch.object(server, "build_requirements", return_value={"capture": True}),
        ):
            session_result = server.status()

        store = Mock()
        store.list_active.side_effect = huge_error
        with (
            patch.object(server.shutil, "which", return_value="/bin/tool"),
            patch.object(server, "list_windows", return_value=[]),
            patch.object(server, "build_requirements", return_value={"capture": True}),
            patch.object(server, "_compositor_active", return_value=False),
            patch.object(server, "_lock_state", return_value=False),
            patch.object(server, "_claim_store", return_value=store),
        ):
            claim_result = server.status()

        for error in (
            session_result["session_error"],
            claim_result["window_claim_error"],
        ):
            self.assertLessEqual(
                server._serialized_size(error),
                server.MAX_ERROR_RESULT_BYTES,
            )
            self.assertTrue(error.endswith("…"))


class McpErrorTests(TestCase):
    def test_read_only_capture_rejects_save_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "save_session_window_capture"):
            server.call_tool(
                "get_session_window_capture",
                {"window": "App", "save_path": "/tmp/capture.png"},
            )

    def test_initialize_echoes_known_protocol_versions(self) -> None:
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", server.PROTOCOL_VERSION):
            with self.subTest(version=version):
                request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version}}
                response = server.dispatch(request)

                self.assertEqual(response["result"]["protocolVersion"], version)

    def test_initialize_falls_back_for_an_unknown_protocol_version(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "unsupported-version"}}
        response = server.dispatch(request)

        self.assertEqual(response["result"]["protocolVersion"], server.PROTOCOL_VERSION)

    def test_capture_tool_resolves_window_before_capture(self) -> None:
        window = {"xid": "0x20"}
        identity = {"xid": "0x20", "pid": 20}
        expected = {"content": [], "isError": False}
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()
        store.assert_access.return_value = None
        with patch.object(server, "ensure_session", return_value={"session": "same"}), patch.object(server, "_resolve_target", return_value=(window, identity)) as resolve, patch.object(server, "_identity_matches", return_value=server.IdentityMatch.MATCH), patch.object(server, "_claim_store", return_value=store), patch.object(server, "capture_window", return_value=expected) as capture_window:
            legacy_result = server.call_tool(
                "capture_session_window",
                {"window": "App", "save_path": "/tmp/legacy-capture.png"},
            )
            save_result = server.call_tool(
                "save_session_window_capture",
                {"window": "App", "save_path": "/tmp/new-capture.png"},
            )

        self.assertEqual((legacy_result, save_result), (expected, expected))
        self.assertEqual(resolve.call_args_list, [call("App"), call("App")])
        self.assertEqual(
            store.assert_access.call_args_list,
            [
                call(f"mcp-process:{os.getpid()}", identity, None, mark_inflight=True),
                call(f"mcp-process:{os.getpid()}", identity, None, mark_inflight=True),
            ],
        )
        self.assertEqual(
            capture_window.call_args_list,
            [
                call(window, "/tmp/legacy-capture.png"),
                call(window, "/tmp/new-capture.png"),
            ],
        )

    def test_read_only_capture_routes_without_a_save_path(self) -> None:
        window = {"xid": "0x20"}
        identity = {"xid": "0x20", "pid": 20}
        expected = {"content": [], "isError": False}
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()
        store.assert_access.return_value = None
        with patch.object(server, "ensure_session", return_value={"session": "same"}), patch.object(server, "_resolve_target", return_value=(window, identity)), patch.object(server, "_identity_matches", return_value=server.IdentityMatch.MATCH), patch.object(server, "_claim_store", return_value=store), patch.object(server, "capture_window", return_value=expected) as capture_window:
            result = server.call_tool("get_session_window_capture", {"window": "App"})

        self.assertEqual(result, expected)
        capture_window.assert_called_once_with(window, None)

    def test_window_listing_is_paginated(self) -> None:
        windows = [{"xid": f"0x{index:08x}"} for index in range(3)]
        with patch.object(server, "list_windows", return_value=windows):
            first = server.call_tool("list_session_windows", {"limit": 2})
            second = server.call_tool("list_session_windows", {"limit": 2, "cursor": "2"})

        self.assertEqual(first["structuredContent"], {"windows": windows[:2], "next_cursor": "2"})
        self.assertEqual(second["structuredContent"], {"windows": windows[2:], "next_cursor": None})

    def test_window_listing_rejects_invalid_page_arguments(self) -> None:
        with patch.object(server, "list_windows") as list_windows:
            with self.assertRaisesRegex(ValueError, "limit"):
                server.call_tool("list_session_windows", {"limit": True})
            with self.assertRaisesRegex(ValueError, "cursor"):
                server.call_tool("list_session_windows", {"cursor": "-1"})

        list_windows.assert_not_called()

    def test_window_listing_has_a_serialized_size_cap(self) -> None:
        windows = [{"xid": f"0x{index:08x}", "title": "x" * 100} for index in range(3)]
        first = {"windows": windows[:1], "next_cursor": "1"}
        size = len(json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode())
        with patch.object(server, "MAX_WINDOW_RESULT_BYTES", size), patch.object(server, "list_windows", return_value=windows):
            result = server.call_tool("list_session_windows", {})

        self.assertEqual(result["structuredContent"], first)

    def test_claim_listing_has_a_serialized_size_cap(self) -> None:
        store = Mock()
        store.list_active.return_value = [
            {"owner_thread_id": "o" * 128, "window": {"title": "é" * 160}}
            for _ in range(20)
        ]
        with patch.object(server, "_claim_store", return_value=store):
            result = server.call_tool("list_window_claims", {})

        self.assertLessEqual(
            server._serialized_size(result["structuredContent"]),
            server.MAX_CLAIM_RESULT_BYTES,
        )
        self.assertTrue(result["structuredContent"]["truncated"])

    def test_window_resolution_rejects_a_session_generation_change(self) -> None:
        before = {"wm_start_time": "1"}
        after = {"wm_start_time": "2"}
        target = ({"xid": "0x20"}, {"xid": "0x20", "pid": 20})
        with patch.object(server, "ensure_session", side_effect=[before, after]), patch.object(server, "_resolve_target", return_value=target):
            with self.assertRaisesRegex(RuntimeError, "changed during resolution"):
                server._resolve_bound_target("0x20")

    def test_window_manager_restart_after_resolution_is_rejected_inside_guard(self) -> None:
        resolved_fingerprint = {
            "display": ":42",
            "socket_inode": 123,
            "wm_start_time": "1",
        }
        restarted_fingerprint = {**resolved_fingerprint, "wm_start_time": "2"}
        window = {"xid": "0x20", "pid": 20}
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()

        with (
            patch.object(
                server,
                "_resolve_bound_target",
                return_value=(window, identity, resolved_fingerprint),
            ),
            patch.object(server, "_claim_store", return_value=store),
            patch.object(server, "ensure_session", return_value=restarted_fingerprint),
        ):
            with self.assertRaisesRegex(RuntimeError, "fingerprint changed"):
                server.call_tool(
                    "claim_session_window",
                    {"window": "0x20"},
                    "thread-a",
                )

        store.claim.assert_not_called()

    def test_claim_session_window_routes_verified_identity_and_owner(self) -> None:
        fingerprint = {
            "display": ":42",
            "socket_inode": 123,
            "wm_start_time": "1",
        }
        window = {"xid": "0x20", "pid": 20}
        identity = {"xid": "0x20", "pid": 20, "process_start_time": "1"}
        expected = {"claim_token": "claim-token", "renewed": False}
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()
        store.claim.return_value = expected

        with (
            patch.object(
                server,
                "_resolve_bound_target",
                return_value=(window, identity, fingerprint),
            ),
            patch.object(server, "_claim_store", return_value=store),
            patch.object(server, "ensure_session", return_value=fingerprint),
            patch.object(
                server,
                "_identity_matches",
                return_value=server.IdentityMatch.MATCH,
            ),
            patch.object(
                server,
                "_require_lease_reservation_access",
                return_value=None,
            ),
        ):
            result = server.call_tool(
                "claim_session_window",
                {"window": "0x20", "lease_seconds": 90},
                "thread-a",
            )

        self.assertEqual(result["structuredContent"], expected)
        store.claim.assert_called_once_with(
            "thread-a",
            window,
            identity,
            90,
        )

    def test_shortcut_inputs_are_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "key must contain"):
            server._shortcut({"key": "x" * (server.MAX_SHORTCUT_KEY_CHARS + 1)})
        with self.assertRaisesRegex(ValueError, "modifiers must contain"):
            server._shortcut(
                {
                    "key": "x",
                    "modifiers": "c" * (server.MAX_SHORTCUT_MODIFIERS_CHARS + 1),
                }
            )

    def test_expected_tool_failure_is_an_is_error_result(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "missing", "arguments": {}}}
        response = server.dispatch(request)

        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])

    def test_subprocess_stderr_tool_error_has_a_serialized_size_cap(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_window_shortcut", "arguments": {"window": "0x20", "key": "x"}}}
        failed = completed([], stderr="é" * (server.MAX_ERROR_RESULT_BYTES * 4), returncode=1)
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()
        store.assert_access.return_value = None
        fingerprint = {"display": ":42", "socket_inode": 123}
        with patch.object(server, "load_lease", return_value=None), patch.object(server, "_resolve_bound_target", return_value=({"xid": "0x20"}, {"xid": "0x20"}, fingerprint)), patch.object(server, "ensure_session", return_value=fingerprint), patch.object(server, "_identity_matches", return_value=server.IdentityMatch.MATCH), patch.object(server, "_claim_store", return_value=store), patch.object(server, "_ensure_input_safe"), patch.object(server, "run", return_value=failed):
            response = server.dispatch(request)

        self.assertTrue(response["result"]["isError"])
        self.assertLessEqual(server._serialized_size(response["result"]), server.MAX_ERROR_RESULT_BYTES)
        self.assertTrue(response["result"]["structuredContent"]["error"].endswith("…"))

    def test_oversized_release_token_is_rejected_without_reflection(self) -> None:
        token = "sensitive" * server.MAX_ERROR_RESULT_BYTES
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "release_session_window",
                "arguments": {"claim_token": token},
            },
        }

        with patch.object(server, "_claim_store") as claim_store:
            response = server.dispatch(request)

        self.assertTrue(response["result"]["isError"])
        self.assertLessEqual(
            server._serialized_size(response["result"]),
            server.MAX_ERROR_RESULT_BYTES,
        )
        self.assertNotIn(token, response["result"]["content"][0]["text"])
        claim_store.assert_not_called()

    def test_dispatch_error_has_a_serialized_size_cap(self) -> None:
        class BrokenParams(dict):
            def get(self, _key, _default=None):
                raise RuntimeError("é" * (server.MAX_ERROR_RESULT_BYTES * 4))

        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": BrokenParams({"present": True})}
        response = server.dispatch(request)

        self.assertLessEqual(server._serialized_size(response), server.MAX_ERROR_RESULT_BYTES)
        self.assertTrue(response["error"]["message"].endswith("…"))

    def test_error_collection_has_a_single_serialized_size_cap(self) -> None:
        errors = server._bounded_error_list(["é" * server.MAX_ERROR_RESULT_BYTES] * 4)

        self.assertLessEqual(server._serialized_size(errors), server.MAX_ERROR_RESULT_BYTES)
        self.assertTrue(errors[-1].endswith("…"))

    def test_direct_shortcut_checks_safety_without_clearing_modifiers(self) -> None:
        window = {"xid": "0x20"}
        identity = {"xid": "0x20", "pid": 20}
        store = Mock(unsafe=True)
        store.window_guard.return_value = contextlib.nullcontext()
        store.assert_access.return_value = None
        with patch.object(server, "_resolve_target", return_value=(window, identity)), patch.object(server, "_identity_matches", return_value=server.IdentityMatch.MATCH), patch.object(server, "_claim_store", return_value=store), patch.object(server, "ensure_session", return_value={"session": "same"}), patch.object(server, "_ensure_input_safe") as safety, patch.object(server, "run", return_value=completed([])) as run:
            result = server.call_tool("send_window_shortcut", {"window": "0x20", "key": "x", "modifiers": "CTRL"})

        safety.assert_called_once_with()
        store.assert_access.assert_called_once_with(f"mcp-process:{os.getpid()}", identity, None, mark_inflight=True)
        self.assertEqual(run.call_args.args[0], ["xdotool", "key", "--window", "0x20", "ctrl+x"])
        self.assertFalse(result["isError"])

    def test_dispatch_uses_host_thread_id_as_tool_owner(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "list_window_claims",
                "arguments": {},
                "_meta": {"threadId": "thread-a"},
            },
        }
        expected = server.text_result({"claims": []})
        with patch.object(server, "call_tool", return_value=expected) as call_tool:
            response = server.dispatch(request)

        self.assertEqual(response["result"], expected)
        call_tool.assert_called_once_with("list_window_claims", {}, "thread-a")
