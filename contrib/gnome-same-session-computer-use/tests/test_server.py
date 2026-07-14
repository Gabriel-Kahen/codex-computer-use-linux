import json
import math
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from gnome_same_session import server


WINDOWS = [
    {"id": "11", "title": "Editor", "app_id": "code.desktop", "wm_class": "Code", "focused": False, "frame": {"width": 800, "height": 600}},
    {"id": "12", "title": "Terminal", "app_id": "org.gnome.Terminal.desktop", "wm_class": "Gnome-terminal", "focused": True, "frame": {"width": 640, "height": 480}},
]
CAPABILITY = "c" * 64


class ResolutionTests(TestCase):
    def test_resolves_stable_id_before_title(self) -> None:
        with patch.object(server, "windows", return_value=WINDOWS):
            self.assertEqual(server.resolve_window("11"), WINDOWS[0])

    def test_rejects_ambiguous_title(self) -> None:
        duplicates = [dict(WINDOWS[0]), {**WINDOWS[0], "id": "13"}]
        with patch.object(server, "windows", return_value=duplicates):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                server.resolve_window("Editor")


class DbusTests(TestCase):
    def test_reuses_one_connection_for_caller_bound_lease_calls(self) -> None:
        calls: list[tuple[str, str, tuple[str, ...]]] = []

        class Variant:
            def __init__(self, signature: str, values: tuple[str, ...]):
                self.signature = signature
                self.values = values

            def unpack(self):
                return (json.dumps({"ok": True}),)

        class Connection:
            def is_closed(self):
                return False

            def call_sync(self, _bus, _path, _interface, method, parameters, *_rest):
                calls.append((method, parameters.signature, parameters.values))
                return Variant("(s)", ())

        connection = Connection()
        bus_get_calls: list[object] = []
        gio = SimpleNamespace(
            BusType=SimpleNamespace(SESSION=object()),
            DBusCallFlags=SimpleNamespace(NONE=0),
            bus_get_sync=lambda *_args: bus_get_calls.append(object()) or connection,
        )
        glib = SimpleNamespace(
            Variant=Variant,
            VariantType=SimpleNamespace(new=lambda value: value),
        )
        with (
            patch.object(server, "Gio", gio),
            patch.object(server, "GLib", glib),
            patch.object(server, "_DBUS_CONNECTION", None),
        ):
            server.dbus_call("BeginLease", "11")
            server.dbus_call("ActivateLease", CAPABILITY)

        self.assertEqual(len(bus_get_calls), 1)
        self.assertEqual(calls, [("BeginLease", "(s)", ("11",)), ("ActivateLease", "(s)", (CAPABILITY,))])


class LeaseTests(TestCase):
    def test_requires_explicit_interference_acknowledgment(self) -> None:
        with self.assertRaisesRegex(ValueError, "acknowledge_interference"):
            server.begin_lease({"window": "11", "acknowledge_interference": False})

    def test_journals_original_state_before_focus(self) -> None:
        events: list[str] = []

        def call(method: str, *args: str):
            events.append(method)
            if method == "BeginLease":
                return {
                    "capability": CAPABILITY,
                    "target": WINDOWS[0],
                    "original": {"focused_window": "12", "workspace": 0, "pointer": {"x": 3, "y": 4}},
                }
            if method == "ActivateLease":
                self.assertTrue(server.LEASE_FILE.exists())
                self.assertEqual(json.loads(server.LEASE_FILE.read_text())["phase"], "prepared")
                return {"state": {"focused_window": "11"}}
            return {}

        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "STATE_DIR", Path(directory)),
                patch.object(server, "resolve_window", return_value=WINDOWS[0]),
                patch.object(server, "dbus_call", side_effect=call),
            ):
                result = server.begin_lease({"window": "11", "acknowledge_interference": True})
                journal = json.loads(server.LEASE_FILE.read_text())

        self.assertEqual(events, ["BeginLease", "ActivateLease"])
        self.assertEqual(journal["phase"], "active")
        self.assertEqual(journal["token"], CAPABILITY)
        self.assertEqual(result["window"], WINDOWS[0])

    def test_restore_keeps_journal_when_shell_restore_fails(self) -> None:
        state = {"token": CAPABILITY, "target": {"id": "11"}, "original": {"workspace": 0}}
        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "dbus_call", side_effect=RuntimeError("offline")),
            ):
                server.LEASE_FILE.write_text("{}")
                result = server.restore_lease(state)
                remains = server.LEASE_FILE.exists()

        self.assertFalse(result["restored"])
        self.assertTrue(remains)
        self.assertTrue(result["journal_retained"])

    def test_restore_keeps_journal_on_postcondition_mismatch(self) -> None:
        state = {"token": CAPABILITY, "target": {"id": "11"}, "original": {"workspace": 0}}
        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "dbus_call", return_value={"restored": False, "errors": ["pointer restoration mismatch"]}),
            ):
                server.LEASE_FILE.write_text("{}")
                result = server.restore_lease(state)
                remains = server.LEASE_FILE.exists()

        self.assertFalse(result["restored"])
        self.assertTrue(remains)
        self.assertEqual(result["errors"], ["pointer restoration mismatch"])

    def test_recovery_uses_explicit_rebind_method(self) -> None:
        state = {"token": CAPABILITY, "target": {"id": "11"}}
        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "dbus_call", return_value={"restored": True, "errors": []}) as call,
            ):
                server.LEASE_FILE.write_text("{}")
                result = server.restore_lease(state, recovery=True)

        self.assertTrue(result["restored"])
        call.assert_called_once_with("RecoverLease", CAPABILITY)

    def test_recovery_only_discards_expired_pending_journal_from_same_shell(self) -> None:
        state = {
            "token": CAPABILITY,
            "phase": "prepared",
            "shell_instance": "shell-1",
            "target": {"id": "11"},
        }
        cases = (("shell-1", True), ("shell-2", False))
        for current_shell, expected_restored in cases:
            with self.subTest(current_shell=current_shell):
                def call(method: str, *_args: str):
                    if method == "RecoverLease":
                        raise RuntimeError("invalid Shell lease capability")
                    if method == "Status":
                        return {"shell_instance": current_shell, "lease_phase": None}
                    self.fail(f"unexpected D-Bus method {method}")

                with TemporaryDirectory() as directory:
                    with (
                        patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                        patch.object(server, "dbus_call", side_effect=call),
                    ):
                        server.LEASE_FILE.write_text("{}")
                        result = server.restore_lease(state, recovery=True)
                        remains = server.LEASE_FILE.exists()

                expected_errors = [] if expected_restored else ["invalid Shell lease capability"]
                self.assertEqual(
                    result,
                    {
                        "restored": expected_restored,
                        "errors": expected_errors,
                        "post_restore_state": (
                            {"shell_instance": current_shell, "lease_phase": None}
                            if expected_restored
                            else None
                        ),
                        "expired_pending_lease": expected_restored,
                        "target": "11",
                        "journal_retained": not expected_restored,
                    },
                )
                self.assertEqual(remains, not expected_restored)


class InputTests(TestCase):
    def test_pointer_request_is_bound_to_journaled_target(self) -> None:
        state = {"token": CAPABILITY, "target": WINDOWS[0]}
        with (
            patch.object(server, "require_lease", return_value=state),
            patch.object(server, "file_guard"),
            patch.object(server, "dbus_call", return_value={"pointer_restored": True}) as call,
        ):
            result = server.pointer_action({"lease_token": CAPABILITY, "x": 10, "y": 20}, "click")

        request = json.loads(call.call_args.args[2])
        self.assertEqual(call.call_args.args[1], CAPABILITY)
        self.assertEqual(request["point"], {"x": 10.0, "y": 20.0})
        self.assertTrue(result["global_seat_used"])

    def test_lease_lock_covers_token_validation_and_input(self) -> None:
        events: list[str] = []

        @contextmanager
        def guard(path: Path):
            if path == server.LOCK_FILE:
                events.append("lock-enter")
            yield
            if path == server.LOCK_FILE:
                events.append("lock-exit")

        def require(_token: str):
            events.append("validate")
            return {"token": CAPABILITY, "target": WINDOWS[0]}

        def call(*_args: str):
            events.append("input")
            return {}

        with (
            patch.object(server, "file_guard", side_effect=guard),
            patch.object(server, "require_lease", side_effect=require),
            patch.object(server, "dbus_call", side_effect=call),
        ):
            server.pointer_action({"lease_token": CAPABILITY, "x": 1, "y": 2}, "click")

        self.assertEqual(events, ["lock-enter", "validate", "input", "lock-exit"])

    def test_rejects_unbounded_or_non_finite_pointer_values(self) -> None:
        invalid = (
            ({"lease_token": CAPABILITY, "x": math.nan, "y": 2}, "click"),
            ({"lease_token": CAPABILITY, "x": 1, "y": 2, "count": 4}, "click"),
            ({"lease_token": CAPABILITY, "x": 1, "y": 2, "steps": 0}, "scroll"),
        )
        for arguments, action in invalid:
            with self.subTest(action=action, arguments=arguments):
                with self.assertRaises(ValueError):
                    server.pointer_action(arguments, action)

    def test_rejects_duplicate_modifiers_and_unbounded_keys(self) -> None:
        with self.assertRaises(ValueError):
            server.send_shortcut({"lease_token": CAPABILITY, "key": "x", "modifiers": ["CTRL", "CTRL"]})
        with self.assertRaises(ValueError):
            server.send_shortcut({"lease_token": CAPABILITY, "key": "x" * 65})


class StatusTests(TestCase):
    def test_does_not_claim_background_capture_or_targeted_input(self) -> None:
        with (
            patch.dict(server.os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}),
            patch.object(server, "Gio", object()),
            patch.object(server, "GLib", object()),
            patch.object(server.shutil, "which", return_value="/usr/bin/gdbus"),
            patch.object(server, "dbus_call", return_value={"shell_version": "45"}),
            patch.object(server, "run", return_value=subprocess.CompletedProcess([], 0, "ScreenshotWindow", "")),
            patch.object(server, "load_lease", return_value=None),
        ):
            result = server.status()

        capabilities = result["capabilities"]
        self.assertFalse(capabilities["exact_background_window_capture"])
        self.assertFalse(capabilities["targeted_background_pointer"])
        self.assertFalse(capabilities["targeted_background_keyboard"])
        self.assertTrue(capabilities["recoverable_focus_lease"])


class McpTests(TestCase):
    def test_initialize_does_not_claim_unknown_protocol_version(self) -> None:
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        })

        self.assertEqual(response["result"]["protocolVersion"], server.PROTOCOL_VERSION)

    def test_manifest_launcher_and_protocol_smoke(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / ".codex-plugin/plugin.json").read_text())
        mcp = json.loads((root / manifest["mcpServers"]).read_text())
        launcher = root / mcp["mcpServers"]["gnome-same-session-computer-use"]["command"]
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        proc = subprocess.run([str(launcher)], input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=10, check=True)
        tools = json.loads(proc.stdout)["result"]["tools"]

        self.assertEqual(manifest["name"], "gnome-same-session-computer-use")
        self.assertEqual(len(tools), len({item["name"] for item in tools}))
        self.assertEqual(len(tools), 10)
        annotations = {item["name"]: item["annotations"] for item in tools}
        self.assertFalse(annotations["end_focus_lease"]["idempotentHint"])
