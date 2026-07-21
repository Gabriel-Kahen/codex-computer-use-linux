import json
import math
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import call
from unittest.mock import patch

from gnome_same_session import server
from gnome_same_session.claims import ClaimRegistry


WINDOWS = [
    {"id": "11", "title": "Editor", "app_id": "code.desktop", "wm_class": "Code", "focused": False, "frame": {"width": 800, "height": 600}},
    {"id": "12", "title": "Terminal", "app_id": "org.gnome.Terminal.desktop", "wm_class": "Gnome-terminal", "focused": True, "frame": {"width": 640, "height": 480}},
]
CAPABILITY = "c" * 64
SHELL_STATUS = {
    "shell_instance": "shell-1",
    "protocol_version": server.CLAIMED_LEASE_PROTOCOL_VERSION,
    "capabilities": [server.CLAIMED_LEASE_CAPABILITY],
}


class ResolutionTests(TestCase):
    def test_resolves_stable_id_before_title(self) -> None:
        with patch.object(server, "windows", return_value=WINDOWS):
            self.assertEqual(server.resolve_window("11"), WINDOWS[0])

    def test_rejects_ambiguous_title(self) -> None:
        duplicates = [dict(WINDOWS[0]), {**WINDOWS[0], "id": "13"}]
        with patch.object(server, "windows", return_value=duplicates):
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                server.resolve_window("Editor")

    def test_preserves_raw_window_text_for_matching_and_bounds_summaries(self) -> None:
        oversized = {
            **WINDOWS[0],
            "title": "t" * (server.MAX_WINDOW_TEXT_CHARS + 1),
            "wm_class": "w" * (server.MAX_WINDOW_TEXT_CHARS + 1),
            "app_id": "a" * (server.MAX_WINDOW_TEXT_CHARS + 1),
        }
        with patch.object(server, "dbus_call", return_value=[oversized]):
            raw = server.windows()[0]
            bounded = server.window_summary(raw)

        self.assertEqual(raw, oversized)
        self.assertEqual(len(bounded["title"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(bounded["wm_class"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(bounded["app_id"]), server.MAX_WINDOW_TEXT_CHARS)

    def test_matches_title_substring_beyond_summary_boundary(self) -> None:
        oversized = {**WINDOWS[0], "title": "x" * server.MAX_WINDOW_TEXT_CHARS + " deep marker"}
        with patch.object(server, "windows", return_value=[oversized]):
            selected = server.resolve_window("deep marker")

        self.assertEqual(selected, oversized)

    def test_bounds_ambiguous_window_error(self) -> None:
        oversized = {
            **WINDOWS[0],
            "title": "t" * server.MAX_MCP_STDOUT_LINE_BYTES,
            "app_id": "a" * server.MAX_MCP_STDOUT_LINE_BYTES,
        }
        duplicates = [oversized, {**oversized, "id": "13"}]
        with patch.object(server, "windows", return_value=duplicates):
            with self.assertRaises(RuntimeError) as raised:
                server.resolve_window("t")

        self.assertLessEqual(len(str(raised.exception)), server.MAX_ERROR_TEXT_CHARS)


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
        selected = {**WINDOWS[0], "title": "x" * server.MAX_MCP_STDOUT_LINE_BYTES}

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
                patch.object(server, "resolve_window", return_value=selected),
                patch.object(server, "dbus_call", side_effect=call),
            ):
                result = server.begin_lease({"window": "11", "acknowledge_interference": True})
                journal = json.loads(server.LEASE_FILE.read_text())

        self.assertEqual(events, ["BeginLease", "ActivateLease"])
        self.assertEqual(journal["phase"], "active")
        self.assertEqual(journal["token"], CAPABILITY)
        self.assertEqual(result["window"], server.window_summary(selected))
        self.assertEqual(len(result["window"]["title"]), server.MAX_WINDOW_TEXT_CHARS)

    def test_rejects_malformed_preparation_before_activation_or_journaling(self) -> None:
        invalid = (["not", "an", "object"], {"capability": "x" * 257})
        for prepared in invalid:
            with self.subTest(prepared=prepared), TemporaryDirectory() as directory:
                lease_file = Path(directory) / "lease.json"
                with (
                    patch.object(server, "LEASE_FILE", lease_file),
                    patch.object(server, "STATE_DIR", Path(directory)),
                    patch.object(server, "load_lease", return_value=None),
                    patch.object(server, "resolve_window", return_value=WINDOWS[0]),
                    patch.object(server, "dbus_call", return_value=prepared) as call,
                ):
                    with self.assertRaisesRegex(RuntimeError, "invalid lease"):
                        server.begin_lease({"window": "11", "acknowledge_interference": True})

                call.assert_called_once_with("BeginLease", "11")
                self.assertFalse(lease_file.exists())

    def test_protocol_five_requires_a_shell_generated_lease_generation(self) -> None:
        integration = {
            "shell_instance": "shell-a",
            "protocol_version": server.BRIDGE_CONTRACT_PROTOCOL_VERSION,
            "capabilities": [server.BRIDGE_CONTRACT_CAPABILITY],
            "bridge_contract": {
                **server.BRIDGE_CONTRACT,
                "role": "background-computer-use",
                "features": [],
            },
        }

        def call(method: str, *_args: str):
            if method == "Status":
                return integration
            if method == "BeginLease":
                return {
                    "capability": CAPABILITY,
                    "target": WINDOWS[0],
                    "shell_instance": "shell-a",
                }
            self.fail(f"unexpected D-Bus call {method}")

        with TemporaryDirectory() as directory:
            lease_file = Path(directory) / "lease.json"
            with (
                patch.object(server, "LEASE_FILE", lease_file),
                patch.object(server, "load_lease", return_value=None),
                patch.object(server, "dbus_call", side_effect=call),
            ):
                with self.assertRaisesRegex(RuntimeError, "invalid lease generation"):
                    server.begin_lease(
                        {"window": "11", "acknowledge_interference": True},
                        selected=WINDOWS[0],
                        expected_shell_instance="shell-a",
                    )

            self.assertFalse(lease_file.exists())

    def test_shell_restart_during_begin_cleans_pending_lease_without_activation(self) -> None:
        events: list[str] = []

        def call(method: str, *_args: str):
            events.append(method)
            if method == "Status":
                return {"shell_instance": "shell-a"}
            if method == "BeginLease":
                return {
                    "capability": CAPABILITY,
                    "target": WINDOWS[0],
                    "shell_instance": "shell-b",
                }
            if method == "RestoreLease":
                return {"restored": True, "recovery_complete": True, "errors": []}
            self.fail(f"unexpected D-Bus call {method}")

        with TemporaryDirectory() as directory:
            lease_file = Path(directory) / "lease.json"
            with (
                patch.object(server, "LEASE_FILE", lease_file),
                patch.object(server, "load_lease", return_value=None),
                patch.object(server, "dbus_call", side_effect=call),
            ):
                with self.assertRaisesRegex(RuntimeError, "restarted while preparing"):
                    server.begin_lease(
                        {"window": "11", "acknowledge_interference": True},
                        selected=WINDOWS[0],
                        expected_shell_instance="shell-a",
                    )

            self.assertFalse(lease_file.exists())

        self.assertEqual(events, ["Status", "BeginLease", "RestoreLease"])

    def test_changed_prepared_target_is_cleaned_without_activation(self) -> None:
        events: list[str] = []

        def call(method: str, *_args: str):
            events.append(method)
            if method == "Status":
                return {"shell_instance": "shell-a"}
            if method == "BeginLease":
                return {
                    "capability": CAPABILITY,
                    "target": WINDOWS[1],
                    "shell_instance": "shell-a",
                }
            if method == "RestoreLease":
                return {"restored": True, "recovery_complete": True, "errors": []}
            self.fail(f"unexpected D-Bus call {method}")

        with TemporaryDirectory() as directory:
            lease_file = Path(directory) / "lease.json"
            with (
                patch.object(server, "LEASE_FILE", lease_file),
                patch.object(server, "load_lease", return_value=None),
                patch.object(server, "dbus_call", side_effect=call),
            ):
                with self.assertRaisesRegex(RuntimeError, "different focus-lease target"):
                    server.begin_lease(
                        {"window": "11", "acknowledge_interference": True},
                        selected=WINDOWS[0],
                        expected_shell_instance="shell-a",
                    )

            self.assertFalse(lease_file.exists())

        self.assertEqual(events, ["Status", "BeginLease", "RestoreLease"])

    def test_claimed_lease_is_bound_to_thread_and_claim_expiry(self) -> None:
        claim = {"claim_token": "w" * 64, "expires_at": 120.0}

        def call(method: str, *args: str):
            if method == "BeginClaimedLease":
                self.assertEqual(args, ("11", "20.000000"))
                return {"capability": CAPABILITY, "target": WINDOWS[0], "shell_instance": "shell-1"}
            if method == "ActivateLease":
                return {"state": {"focused_window": "11"}}
            self.fail(f"unexpected D-Bus call {method}")

        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "STATE_DIR", Path(directory)),
                patch.object(server.time, "time", return_value=100.0),
                patch.object(server, "dbus_call", side_effect=call),
            ):
                server.begin_lease(
                    {"window": "11", "acknowledge_interference": True},
                    "thread-a",
                    WINDOWS[0],
                    claim,
                )
                state = json.loads(server.LEASE_FILE.read_text())

        self.assertEqual(state["owner_thread_id"], "thread-a")
        self.assertEqual(state["claim_token"], claim["claim_token"])

    def test_focus_journal_reserves_expired_claim_until_cleanup(self) -> None:
        now = [100.0]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ClaimRegistry(
                root / "session",
                server.SESSION_IDENTITY,
                clock=lambda: now[0],
                process_alive=lambda _broker: True,
                broker=server.BROKER_IDENTITY,
            )
            with (
                patch.object(server, "CLAIMS", registry),
                patch.object(server, "LEASE_FILE", root / "session" / "focus-lease.json"),
                patch.object(server, "LOCK_FILE", root / "session" / "focus-lease.lock"),
                patch.object(server, "LEGACY_LEASE_FILE", root / "legacy.json"),
                patch.object(server, "LEGACY_LOCK_FILE", root / "legacy.lock"),
                patch.object(server, "MIGRATION_LOCK_FILE", root / "migration.lock"),
                patch.object(server, "resolve_window", return_value=WINDOWS[0]),
                patch.object(server, "shell_status", return_value=SHELL_STATUS),
            ):
                first = server.claim_window({"window": "11", "lease_seconds": 5}, "thread-a")
                server.save_lease({
                    "version": 3,
                    "token": CAPABILITY,
                    "target": WINDOWS[0],
                    "owner_thread_id": "thread-a",
                    "claim_token": first["claim_token"],
                })
                now[0] = 106.0
                for owner in ("thread-a", "thread-b"):
                    with self.subTest(owner=owner), self.assertRaisesRegex(
                        RuntimeError, "reserved by an older claim"
                    ):
                        server.claim_window({"window": "11", "lease_seconds": 5}, owner)

                server.LEASE_FILE.unlink()
                replacement = server.claim_window(
                    {"window": "11", "lease_seconds": 5}, "thread-a"
                )

        self.assertNotEqual(replacement["claim_token"], first["claim_token"])
        self.assertFalse(replacement["renewed"])

    def test_claim_rejects_shell_restart_without_retaining_claim(self) -> None:
        cases = (
            ("during resolution", ["shell-a", "shell-b"]),
            ("before commit", ["shell-a", "shell-a", "shell-a", "shell-b"]),
        )
        for label, instances in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                registry = ClaimRegistry(
                    Path(directory),
                    server.SESSION_IDENTITY,
                    process_alive=lambda _broker: True,
                    broker=server.BROKER_IDENTITY,
                )
                statuses = [
                    {**SHELL_STATUS, "shell_instance": shell_instance}
                    for shell_instance in instances
                ]
                with (
                    patch.object(server, "CLAIMS", registry),
                    patch.object(server, "resolve_window", return_value=WINDOWS[0]),
                    patch.object(server, "shell_status", side_effect=statuses),
                    patch.object(server, "renew_focus_lease_for_claim"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "GNOME Shell restarted"):
                        server.claim_window(
                            {"window": "11", "lease_seconds": 60}, "thread-a"
                        )

                state = (
                    json.loads(registry.claims_file.read_text())
                    if registry.claims_file.exists()
                    else {"claims": {}}
                )
                self.assertEqual(state["claims"], {})

    def test_matching_legacy_journal_is_adopted_but_foreign_state_is_ignored(self) -> None:
        cases = (
            ("shell-1", None, True),
            ("shell-2", None, False),
            ("shell-1", "another-session", False),
        )
        for current_shell, journal_session, adopted in cases:
            with self.subTest(
                current_shell=current_shell, journal_session=journal_session
            ), TemporaryDirectory() as directory:
                root = Path(directory)
                local = root / "sessions" / "current" / "focus-lease.json"
                legacy = root / "focus-lease.json"
                journal = {
                    "version": 2,
                    "token": CAPABILITY,
                    "target": WINDOWS[0],
                    "shell_instance": "shell-1",
                }
                if journal_session:
                    journal["session_identity"] = journal_session
                legacy.write_text(json.dumps(journal))
                with (
                    patch.object(server, "LEASE_FILE", local),
                    patch.object(server, "LEGACY_LEASE_FILE", legacy),
                    patch.object(server, "LEGACY_LOCK_FILE", root / "focus-lease.lock"),
                    patch.object(server, "MIGRATION_LOCK_FILE", root / "migration.lock"),
                    patch.object(
                        server,
                        "dbus_call",
                        return_value={"shell_instance": current_shell},
                    ),
                ):
                    state = server.load_lease()

                self.assertEqual(state is not None, adopted)
                self.assertEqual(local.exists(), adopted)
                self.assertEqual(legacy.exists(), not adopted)
                if state:
                    self.assertEqual(state["session_identity"], server.SESSION_IDENTITY)

    def test_nonowner_cannot_use_live_focus_lease(self) -> None:
        state = {
            "token": CAPABILITY,
            "target": WINDOWS[0],
            "owner_thread_id": "thread-a",
        }
        with patch.object(server, "load_lease", return_value=state):
            with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
                server.require_lease(CAPABILITY, "thread-b")

    def test_nonowner_recovery_requires_bound_claim_to_be_stale(self) -> None:
        claim_token = "w" * 64
        state = {
            "token": CAPABILITY,
            "target": WINDOWS[0],
            "owner_thread_id": "thread-a",
            "claim_token": claim_token,
        }
        with (
            patch.object(server, "file_guard"),
            patch.object(server, "load_lease", return_value=state),
            patch.object(server, "restore_lease", return_value={"restored": True}) as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "still owns"):
                server.recover_lease("thread-b", {"claim_token": claim_token})
            result = server.recover_lease("thread-b", None)

        self.assertEqual(result, {"restored": True})
        restore.assert_called_once_with(state, recovery=True)

    def test_nonowner_cannot_recover_live_unclaimed_broker(self) -> None:
        state = {
            "token": CAPABILITY,
            "target": WINDOWS[0],
            "owner_thread_id": "thread-a",
            "claim_token": None,
            "broker": server.BROKER_IDENTITY,
        }
        with (
            patch.object(server, "file_guard"),
            patch.object(server, "load_lease", return_value=state),
            patch.object(server, "restore_lease") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "original unclaimed"):
                server.recover_lease("thread-b", None)

        restore.assert_not_called()

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

    def test_restore_discards_journal_after_non_actionable_closed_window(self) -> None:
        state = {"token": CAPABILITY, "target": {"id": "11"}, "original": {"focused_window": "12"}}
        shell_result = {
            "restored": False,
            "recovery_complete": True,
            "errors": [],
            "missing_windows": ["original-focused:12"],
            "state": {"focused_window": None, "workspace": 0, "pointer": {"x": 3, "y": 4}},
        }
        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "dbus_call", return_value=shell_result),
            ):
                server.LEASE_FILE.write_text("{}")
                result = server.restore_lease(state)
                remains = server.LEASE_FILE.exists()

        self.assertFalse(result["restored"])
        self.assertTrue(result["recovery_complete"])
        self.assertEqual(result["missing_windows"], ["original-focused:12"])
        self.assertFalse(result["journal_retained"])
        self.assertFalse(remains)

    def test_restore_discards_journal_when_lease_target_closed(self) -> None:
        state = {"token": CAPABILITY, "target": {"id": "11"}, "original": {"focused_window": "12"}}
        shell_result = {
            "restored": True,
            "recovery_complete": True,
            "errors": [],
            "missing_windows": ["target:11"],
            "state": {"focused_window": "12", "workspace": 0, "pointer": {"x": 3, "y": 4}},
        }
        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "dbus_call", return_value=shell_result),
            ):
                server.LEASE_FILE.write_text("{}")
                result = server.restore_lease(state)
                remains = server.LEASE_FILE.exists()

        self.assertEqual(result, {
            "restored": False,
            "recovery_complete": True,
            "errors": [],
            "missing_windows": ["target:11"],
            "post_restore_state": shell_result["state"],
            "expired_pending_lease": False,
            "recovery_outcome_unknown": False,
            "target": "11",
            "journal_retained": False,
        })
        self.assertFalse(remains)

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

    def test_recovery_reconciles_missing_shell_lease_by_phase_and_instance(self) -> None:
        cases = (
            (
                "prepared",
                "shell-1",
                {
                    "restored": True,
                    "recovery_complete": True,
                    "errors": [],
                    "missing_windows": [],
                    "post_restore_state": {"shell_instance": "shell-1", "lease_phase": None},
                    "expired_pending_lease": True,
                    "recovery_outcome_unknown": False,
                    "target": "11",
                    "journal_retained": False,
                },
            ),
            (
                "active",
                "shell-1",
                {
                    "restored": False,
                    "recovery_complete": True,
                    "errors": [],
                    "missing_windows": [],
                    "post_restore_state": {"shell_instance": "shell-1", "lease_phase": None},
                    "expired_pending_lease": False,
                    "recovery_outcome_unknown": True,
                    "target": "11",
                    "journal_retained": False,
                },
            ),
            (
                "active",
                "shell-2",
                {
                    "restored": False,
                    "recovery_complete": False,
                    "errors": ["invalid Shell lease capability"],
                    "missing_windows": [],
                    "post_restore_state": None,
                    "expired_pending_lease": False,
                    "recovery_outcome_unknown": False,
                    "target": "11",
                    "journal_retained": True,
                },
            ),
        )
        for phase, current_shell, expected in cases:
            with self.subTest(phase=phase, current_shell=current_shell):
                state = {
                    "token": CAPABILITY,
                    "phase": phase,
                    "shell_instance": "shell-1",
                    "target": {"id": "11"},
                }

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

                self.assertEqual(result, expected)
                self.assertEqual(remains, expected["journal_retained"])

    def test_bounds_restoration_fields_from_shell_and_journal(self) -> None:
        oversized = "x" * server.MAX_MCP_STDOUT_LINE_BYTES
        state = {"token": CAPABILITY, "target": {"id": oversized}}
        shell_result = {
            "restored": False,
            "recovery_complete": True,
            "errors": [],
            "missing_windows": [oversized],
            "state": {"focused_window": oversized},
        }
        with TemporaryDirectory() as directory:
            with (
                patch.object(server, "LEASE_FILE", Path(directory) / "lease.json"),
                patch.object(server, "dbus_call", return_value=shell_result),
            ):
                server.LEASE_FILE.write_text("{}")
                result = server.restore_lease(state)

        self.assertEqual(len(result["missing_windows"][0]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(result["post_restore_state"]["focused_window"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(result["target"]), server.MAX_WINDOW_TEXT_CHARS)


class InputTests(TestCase):
    def test_pointer_request_is_bound_to_journaled_target(self) -> None:
        state = {
            "token": CAPABILITY,
            "target": {**WINDOWS[0], "title": "x" * server.MAX_MCP_STDOUT_LINE_BYTES},
        }
        with (
            patch.object(server, "require_lease", return_value=state),
            patch.object(server, "file_guard"),
            patch.object(
                server,
                "dbus_call",
                return_value={"pointer_restored": True, "detail": "x" * server.MAX_MCP_STDOUT_LINE_BYTES},
            ) as call,
        ):
            result = server.pointer_action({"lease_token": CAPABILITY, "x": 10, "y": 20}, "click")

        request = json.loads(call.call_args.args[2])
        self.assertEqual(call.call_args.args[1], CAPABILITY)
        self.assertEqual(request["point"], {"x": 10.0, "y": 20.0})
        self.assertTrue(result["global_seat_used"])
        self.assertEqual(len(result["window"]["title"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(result["transaction"]["detail"]), server.MAX_WINDOW_TEXT_CHARS)

    def test_lease_lock_covers_token_validation_and_input(self) -> None:
        events: list[str] = []

        @contextmanager
        def guard(path: Path):
            if path == server.LOCK_FILE:
                events.append("lock-enter")
            yield
            if path == server.LOCK_FILE:
                events.append("lock-exit")

        def require(_token: str, _owner: str | None = None):
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

    def test_shortcut_response_bounds_window_and_transaction(self) -> None:
        oversized = "x" * server.MAX_MCP_STDOUT_LINE_BYTES
        state = {"token": CAPABILITY, "target": {**WINDOWS[0], "title": oversized}}
        with (
            patch.object(server, "require_lease", return_value=state),
            patch.object(server, "file_guard"),
            patch.object(server, "dbus_call", return_value={"detail": oversized, "items": [oversized] * 20}),
        ):
            result = server.send_shortcut({"lease_token": CAPABILITY, "key": "x", "modifiers": ["CTRL"]})

        self.assertEqual(len(result["window"]["title"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(result["transaction"]["detail"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(result["transaction"]["items"]), server.MAX_RESPONSE_COLLECTION_ITEMS)

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
    def test_protocol_five_requires_the_shared_bridge_contract(self) -> None:
        invalid = {
            "shell_instance": "shell-1",
            "protocol_version": server.BRIDGE_CONTRACT_PROTOCOL_VERSION,
            "capabilities": [server.BRIDGE_CONTRACT_CAPABILITY],
        }
        with patch.object(server, "dbus_call", return_value=invalid):
            with self.assertRaisesRegex(RuntimeError, "incompatible Shell bridge identity"):
                server.shell_status()

        valid = {
            **invalid,
            "bridge_contract": {
                **server.BRIDGE_CONTRACT,
                "role": "background-computer-use",
                "features": [],
            },
        }
        with patch.object(server, "dbus_call", return_value=valid):
            self.assertEqual(server.shell_status(), valid)

        malformed_capabilities = {**valid, "capabilities": None}
        with patch.object(server, "dbus_call", return_value=malformed_capabilities):
            with self.assertRaisesRegex(RuntimeError, "incompatible Shell bridge identity"):
                server.shell_status()

    def test_does_not_claim_background_capture_or_targeted_input(self) -> None:
        with (
            patch.dict(server.os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}),
            patch.object(server, "Gio", object()),
            patch.object(server, "GLib", object()),
            patch.object(server.shutil, "which", return_value="/usr/bin/gdbus"),
            patch.object(server, "dbus_call", return_value={"shell_version": "45", **SHELL_STATUS}),
            patch.object(server, "run", return_value=subprocess.CompletedProcess([], 0, "ScreenshotWindow", "")),
            patch.object(server, "load_lease", return_value=None),
            patch.object(server.CLAIMS, "list", return_value=[]),
        ):
            result = server.status()

        capabilities = result["capabilities"]
        self.assertFalse(capabilities["exact_background_window_capture"])
        self.assertFalse(capabilities["targeted_background_pointer"])
        self.assertFalse(capabilities["targeted_background_keyboard"])
        self.assertTrue(capabilities["recoverable_focus_lease"])
        self.assertTrue(capabilities["parallel_window_claims"])

    def test_reports_window_actor_capture_without_claiming_background_input(self) -> None:
        integration = {
            "shell_version": "45",
            "shell_instance": "shell-1",
            "protocol_version": server.WINDOW_ACTOR_CAPTURE_PROTOCOL_VERSION,
            "capabilities": [
                server.CLAIMED_LEASE_CAPABILITY,
                server.WINDOW_ACTOR_CAPTURE_CAPABILITY,
            ],
        }
        with (
            patch.dict(server.os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}),
            patch.object(server, "Gio", object()),
            patch.object(server, "GLib", object()),
            patch.object(server.shutil, "which", return_value=None),
            patch.object(server, "dbus_call", return_value=integration),
            patch.object(server, "load_lease", return_value=None),
            patch.object(server.CLAIMS, "list", return_value=[]),
        ):
            result = server.status()

        capabilities = result["capabilities"]
        self.assertTrue(capabilities["exact_background_window_capture"])
        self.assertTrue(capabilities["exact_focused_window_capture"])
        self.assertFalse(capabilities["targeted_background_pointer"])
        self.assertFalse(capabilities["targeted_background_keyboard"])
        self.assertTrue(result["requirements"]["window_actor_capture_protocol"])

    def test_old_extension_keeps_legacy_leases_but_disables_claimed_leases(self) -> None:
        legacy = {"shell_version": "45", "shell_instance": "shell-1"}
        with (
            patch.dict(server.os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}),
            patch.object(server, "Gio", object()),
            patch.object(server, "GLib", object()),
            patch.object(server.shutil, "which", return_value="/usr/bin/gdbus"),
            patch.object(server, "dbus_call", return_value=legacy),
            patch.object(server, "run", return_value=subprocess.CompletedProcess([], 0, "ScreenshotWindow", "")),
            patch.object(server, "load_lease", return_value=None),
            patch.object(server.CLAIMS, "list", return_value=[]),
        ):
            result = server.status()

        self.assertTrue(result["capabilities"]["recoverable_focus_lease"])
        self.assertFalse(result["capabilities"]["parallel_window_claims"])
        self.assertFalse(result["requirements"]["claimed_focus_lease_protocol"])

        with (
            patch.object(server, "shell_status", return_value=legacy),
            patch.object(server, "resolve_window") as resolve,
        ):
            with self.assertRaisesRegex(RuntimeError, "install-gnome-integration"):
                server.claim_window({"window": "11"}, "thread-a")
        resolve.assert_not_called()

    def test_bounds_integration_status(self) -> None:
        oversized = "x" * server.MAX_MCP_STDOUT_LINE_BYTES
        with (
            patch.dict(server.os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}),
            patch.object(server, "Gio", object()),
            patch.object(server, "GLib", object()),
            patch.object(server.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(server, "dbus_call", return_value={"detail": oversized, "items": [oversized] * 20}),
            patch.object(server, "run", return_value=subprocess.CompletedProcess([], 0, "ScreenshotWindow", "")),
            patch.object(server, "load_lease", return_value=None),
        ):
            result = server.status()

        self.assertEqual(len(result["integration"]["detail"]), server.MAX_WINDOW_TEXT_CHARS)
        self.assertEqual(len(result["integration"]["items"]), server.MAX_RESPONSE_COLLECTION_ITEMS)
        self.assertTrue(result["capabilities"]["recoverable_focus_lease"])

    def test_bounds_integration_status_error_and_remains_unready(self) -> None:
        oversized = "x" * server.MAX_MCP_STDOUT_LINE_BYTES
        with (
            patch.dict(server.os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}),
            patch.object(server, "Gio", object()),
            patch.object(server, "GLib", object()),
            patch.object(server.shutil, "which", return_value="/usr/bin/tool"),
            patch.object(server, "dbus_call", side_effect=RuntimeError(oversized)),
            patch.object(server, "run", return_value=subprocess.CompletedProcess([], 0, "ScreenshotWindow", "")),
            patch.object(server, "load_lease", return_value=None),
        ):
            result = server.status()

        self.assertIsNone(result["integration"])
        self.assertEqual(len(result["integration_error"]), server.MAX_ERROR_TEXT_CHARS)
        self.assertFalse(result["capabilities"]["recoverable_focus_lease"])


class McpTests(TestCase):
    def test_read_only_capture_rejects_save_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "save_session_window_capture"):
            server.call_tool(
                "get_session_window_capture",
                {"window": "11", "save_path": "/tmp/capture.png"},
            )

    def test_initialize_does_not_claim_unknown_protocol_version(self) -> None:
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        })

        self.assertEqual(response["result"]["protocolVersion"], server.PROTOCOL_VERSION)

    def test_window_listing_is_paginated_below_transport_limit(self) -> None:
        listed = [{**WINDOWS[0], "id": str(index)} for index in range(server.MAX_WINDOWS_PER_PAGE + 1)]
        with patch.object(server, "windows", return_value=listed):
            first = server.call_tool("list_session_windows", {"limit": 2})
            second = server.call_tool("list_session_windows", {"limit": 2, "cursor": "2"})

        self.assertEqual([window["id"] for window in first["structuredContent"]["windows"]], ["0", "1"])
        self.assertEqual(first["structuredContent"]["next_cursor"], "2")
        self.assertEqual([window["id"] for window in second["structuredContent"]["windows"]], ["2", "3"])
        encoded = json.dumps({"jsonrpc": "2.0", "id": 1, "result": first}, separators=(",", ":")).encode()
        self.assertLess(len(encoded), server.MAX_MCP_STDOUT_LINE_BYTES)

    def test_default_window_listing_is_exhaustive_or_fails_explicitly(self) -> None:
        with patch.object(server, "windows", return_value=WINDOWS):
            complete = server.call_tool("list_session_windows", {})

        self.assertEqual([window["id"] for window in complete["structuredContent"]["windows"]], ["11", "12"])
        self.assertIsNone(complete["structuredContent"]["next_cursor"])

        oversized = [{**WINDOWS[0], "id": str(index)} for index in range(30)]
        with patch.object(server, "windows", return_value=oversized):
            with self.assertRaisesRegex(RuntimeError, "retry with limit"):
                server.call_tool("list_session_windows", {})

    def test_window_listing_truncates_by_size_without_skips(self) -> None:
        bounded = {
            "title": "t" * server.MAX_WINDOW_TEXT_CHARS,
            "app_id": "a" * server.MAX_WINDOW_TEXT_CHARS,
            "wm_class": "w" * server.MAX_WINDOW_TEXT_CHARS,
        }
        listed = [{**WINDOWS[0], **bounded, "id": str(index)} for index in range(30)]
        pages = []
        cursor = None
        with patch.object(server, "windows", return_value=listed):
            while True:
                arguments = {"limit": server.MAX_WINDOWS_PER_PAGE}
                if cursor is not None:
                    arguments["cursor"] = cursor
                page = server.call_tool("list_session_windows", arguments)
                pages.append(page)
                cursor = page["structuredContent"]["next_cursor"]
                if cursor is None:
                    break

        first = pages[0]
        first_windows = first["structuredContent"]["windows"]
        self.assertGreater(len(first_windows), 0)
        self.assertLess(len(first_windows), 18)
        self.assertEqual(first["structuredContent"]["next_cursor"], str(len(first_windows)))
        self.assertEqual(pages[1]["structuredContent"]["windows"][0]["id"], str(len(first_windows)))
        self.assertEqual(
            [window["id"] for page in pages for window in page["structuredContent"]["windows"]],
            [str(index) for index in range(30)],
        )
        for page in pages:
            structured = json.dumps(page["structuredContent"], ensure_ascii=False, separators=(",", ":")).encode()
            response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": page}, ensure_ascii=False, separators=(",", ":")).encode()
            self.assertLessEqual(len(structured), server.MAX_WINDOW_RESULT_BYTES)
            self.assertLess(len(response), 12 * 1024)

    def test_oversized_title_is_bounded_before_mcp_serialization(self) -> None:
        oversized = {**WINDOWS[0], "title": "x" * server.MAX_MCP_STDOUT_LINE_BYTES}
        with patch.object(server, "dbus_call", return_value=[oversized]):
            result = server.dispatch({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_session_windows", "arguments": {}},
            })

        title = result["result"]["structuredContent"]["windows"][0]["title"]
        encoded = json.dumps(result, separators=(",", ":")).encode()
        self.assertEqual(len(title), server.MAX_WINDOW_TEXT_CHARS)
        self.assertLess(len(encoded), server.MAX_MCP_STDOUT_LINE_BYTES)

    def test_window_listing_rejects_invalid_page_arguments(self) -> None:
        for arguments in ({"limit": True}, {"limit": server.MAX_WINDOWS_PER_PAGE + 1}, {"cursor": "-1"}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    server.call_tool("list_session_windows", arguments)

    def test_dispatch_bounds_tool_and_unknown_method_errors(self) -> None:
        oversized = "x" * server.MAX_MCP_STDOUT_LINE_BYTES
        requests = (
            {"jsonrpc": "2.0", "id": 1, "method": oversized},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": oversized, "arguments": {}}},
        )
        for request in requests:
            with self.subTest(method=request["method"]):
                result = server.dispatch(request)
                encoded = json.dumps(result, separators=(",", ":")).encode()

                self.assertLessEqual(len(result["error"]["message"]), server.MAX_ERROR_TEXT_CHARS)
                self.assertLess(len(encoded), 4096)

    def test_host_metadata_is_the_only_claim_owner_source(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "claim_session_window",
                "_meta": {"threadId": "trusted-thread"},
                "arguments": {"window": "11", "threadId": "spoofed-thread"},
            },
        }
        with patch.object(server, "claim_window", return_value={"claim_token": "w" * 64}) as claim:
            response = server.dispatch(request)

        self.assertNotIn("error", response)
        claim.assert_called_once_with(request["params"]["arguments"], "trusted-thread")
        self.assertIsNone(
            server.owner_from_params({"arguments": {"_meta": {"threadId": "spoofed-thread"}}})
        )

    def test_call_tool_routes_claimed_capture_through_session_and_input_fences(self) -> None:
        events: list[str] = []
        claim = {
            "claim_token": "w" * 64,
            "owner_thread_id": "thread-a",
            "window": WINDOWS[0],
            "lease_seconds": 60,
        }

        @contextmanager
        def authorize(window_id, owner, token, shell_instance, *, on_complete):
            self.assertEqual(
                (window_id, owner, token, shell_instance),
                ("11", "thread-a", claim["claim_token"], "shell-1"),
            )
            events.append("claim-enter")
            yield claim
            on_complete(claim)
            events.append("claim-exit")

        @contextmanager
        def guard(path):
            events.append(f"{path.name}-enter")
            yield
            events.append(f"{path.name}-exit")

        @contextmanager
        def input_lock():
            events.append("input-thread-enter")
            yield
            events.append("input-thread-exit")

        def capture(*args, **kwargs):
            events.append("capture")
            self.assertEqual(args[:4], ({"window": "11", "claim_token": claim["claim_token"]}, "thread-a", WINDOWS[0], claim))
            self.assertEqual(kwargs, {"expected_shell_instance": "shell-1"})
            return {"content": [], "isError": False}

        with (
            patch.object(server, "resolve_window_for_shell", return_value=(WINDOWS[0], "shell-1")),
            patch.object(server, "shell_status", return_value=SHELL_STATUS),
            patch.object(server.CLAIMS, "authorize", side_effect=authorize),
            patch.object(server, "file_guard", side_effect=guard),
            patch.object(server, "INPUT_LOCK", input_lock()),
            patch.object(server, "capture_window", side_effect=capture),
            patch.object(
                server,
                "renew_focus_lease_for_claim",
                side_effect=lambda _claim: events.append("renew"),
            ),
        ):
            result = server.call_tool(
                "get_session_window_capture",
                {"window": "11", "claim_token": claim["claim_token"]},
                "thread-a",
            )

        self.assertFalse(result["isError"])
        self.assertEqual(
            events,
            [
                "claim-enter",
                "focus-lease.lock-enter",
                "input-thread-enter",
                "input.lock-enter",
                "capture",
                "input.lock-exit",
                "input-thread-exit",
                "focus-lease.lock-exit",
                "renew",
                "claim-exit",
            ],
        )

    def test_call_tool_keeps_unclaimed_capture_compatible_with_old_extension(self) -> None:
        @contextmanager
        def authorize(*_args, **_kwargs):
            yield None

        @contextmanager
        def unlocked(*_args, **_kwargs):
            yield

        with (
            patch.object(server, "resolve_window_for_shell", return_value=(WINDOWS[1], "legacy-shell")),
            patch.object(server, "shell_status") as shell_status,
            patch.object(server.CLAIMS, "authorize", side_effect=authorize),
            patch.object(server, "file_guard", side_effect=unlocked),
            patch.object(server, "capture_window", return_value={"content": [], "isError": False}) as capture,
        ):
            legacy_result = server.call_tool(
                "capture_session_window",
                {"window": "12", "save_path": "/tmp/legacy-capture.png"},
                None,
            )
            save_result = server.call_tool(
                "save_session_window_capture",
                {"window": "12", "save_path": "/tmp/new-capture.png"},
                None,
            )

        self.assertFalse(legacy_result["isError"])
        self.assertFalse(save_result["isError"])
        shell_status.assert_not_called()
        self.assertEqual(
            capture.call_args_list,
            [
                call(
                    {"window": "12", "save_path": "/tmp/legacy-capture.png"},
                    None,
                    WINDOWS[1],
                    None,
                    expected_shell_instance="legacy-shell",
                ),
                call(
                    {"window": "12", "save_path": "/tmp/new-capture.png"},
                    None,
                    WINDOWS[1],
                    None,
                    expected_shell_instance="legacy-shell",
                ),
            ],
        )

    def test_call_tool_forwards_claim_to_begin_pointer_and_shortcut(self) -> None:
        claim = {
            "claim_token": "w" * 64,
            "owner_thread_id": "thread-a",
            "window": WINDOWS[0],
            "lease_seconds": 60,
        }
        authorizations: list[tuple[str, str | None, Any, str]] = []

        @contextmanager
        def authorize(window_id, owner, token, shell_instance, *, on_complete):
            authorizations.append((window_id, owner, token, shell_instance))
            if owner != "thread-a" or token != claim["claim_token"]:
                raise ValueError("claim authorization rejected")
            yield claim
            on_complete(claim)

        @contextmanager
        def unlocked(*_args, **_kwargs):
            yield

        with (
            patch.object(server, "resolve_window_for_shell", return_value=(WINDOWS[0], "shell-1")),
            patch.object(server, "shell_status", return_value=SHELL_STATUS),
            patch.object(server, "lease_target_id", return_value="11"),
            patch.object(server.CLAIMS, "authorize", side_effect=authorize),
            patch.object(server, "file_guard", side_effect=unlocked),
            patch.object(server, "begin_lease", return_value={"lease_token": CAPABILITY}) as begin,
            patch.object(server, "pointer_action", return_value={"clicked": True}) as pointer,
            patch.object(server, "send_shortcut", return_value={"sent": True}) as shortcut,
            patch.object(server, "renew_focus_lease_for_claim") as renew,
        ):
            server.call_tool(
                "begin_focus_lease",
                {"window": "11", "acknowledge_interference": True, "claim_token": claim["claim_token"]},
                "thread-a",
            )
            server.call_tool(
                "lease_pointer_click",
                {"lease_token": CAPABILITY, "claim_token": claim["claim_token"], "x": 1, "y": 2},
                "thread-a",
            )
            server.call_tool(
                "send_lease_shortcut",
                {"lease_token": CAPABILITY, "claim_token": claim["claim_token"], "key": "F6"},
                "thread-a",
            )
            with self.assertRaisesRegex(ValueError, "authorization rejected"):
                server.call_tool(
                    "lease_pointer_click",
                    {"lease_token": CAPABILITY, "claim_token": "x" * 64, "x": 1, "y": 2},
                    "thread-b",
                )

        self.assertEqual(authorizations, [
            ("11", "thread-a", claim["claim_token"], "shell-1"),
            ("11", "thread-a", claim["claim_token"], "shell-1"),
            ("11", "thread-a", claim["claim_token"], "shell-1"),
            ("11", "thread-b", "x" * 64, "shell-1"),
        ])
        begin.assert_called_once_with(
            {"window": "11", "acknowledge_interference": True, "claim_token": claim["claim_token"]},
            "thread-a",
            WINDOWS[0],
            claim,
            expected_shell_instance="shell-1",
        )
        pointer.assert_called_once_with(
            {"lease_token": CAPABILITY, "claim_token": claim["claim_token"], "x": 1, "y": 2},
            "click",
            "thread-a",
            claim,
            "11",
        )
        shortcut.assert_called_once_with(
            {"lease_token": CAPABILITY, "claim_token": claim["claim_token"], "key": "F6"},
            "thread-a",
            claim,
            "11",
        )
        self.assertEqual(renew.call_count, 3)

    def test_call_tool_end_and_recovery_preserve_owner_and_token_fencing(self) -> None:
        state = {
            "token": CAPABILITY,
            "target": WINDOWS[0],
            "owner_thread_id": "thread-a",
            "claim_token": "w" * 64,
            "broker": server.BROKER_IDENTITY,
        }

        @contextmanager
        def unlocked(*_args, **_kwargs):
            yield

        @contextmanager
        def inspect(*_args, **_kwargs):
            yield {"claim_token": "w" * 64, "owner_thread_id": "thread-a"}

        with (
            patch.object(server, "file_guard", side_effect=unlocked),
            patch.object(server, "load_lease", return_value=state),
            patch.object(server, "shell_status", return_value=SHELL_STATUS),
            patch.object(server.CLAIMS, "inspect", side_effect=inspect),
            patch.object(server, "restore_lease", return_value={"restored": True}) as restore,
        ):
            ended = server.call_tool("end_focus_lease", {"lease_token": CAPABILITY}, "thread-a")
            with self.assertRaisesRegex(ValueError, "does not match"):
                server.call_tool("end_focus_lease", {"lease_token": "x" * 64}, "thread-a")
            with self.assertRaisesRegex(RuntimeError, "another computer-use agent"):
                server.call_tool("recover_focus_lease", {}, "thread-b")

        self.assertTrue(ended["structuredContent"]["restored"])
        restore.assert_called_once_with(state, recovery=False)

    def test_unicode_claim_pages_are_byte_bounded_without_token_disclosure(self) -> None:
        claims = [
            {
                "window": {
                    "id": str(index),
                    "title": "🧪" * 128,
                    "app_id": "🖥️" * 96,
                },
                "owner_thread_id": "🤖" * 128,
                "claimed_at": 100.0,
                "expires_at": 160.0,
                "lease_seconds": 60,
            }
            for index in range(12)
        ]
        with (
            patch.object(server, "shell_status", return_value={"shell_instance": "shell-1"}),
            patch.object(server.CLAIMS, "list", return_value=claims),
        ):
            with self.assertRaisesRegex(RuntimeError, "retry with limit"):
                server.call_tool("list_window_claims", {})

            pages = []
            cursor = None
            while True:
                arguments = {"limit": server.MAX_CLAIMS_PER_PAGE}
                if cursor is not None:
                    arguments["cursor"] = cursor
                page = server.call_tool("list_window_claims", arguments)
                pages.append(page)
                cursor = page["structuredContent"]["next_cursor"]
                if cursor is None:
                    break

        self.assertEqual(
            [claim["window"]["id"] for page in pages for claim in page["structuredContent"]["claims"]],
            sorted(str(index) for index in range(12)),
        )
        for page in pages:
            structured = json.dumps(
                page["structuredContent"], ensure_ascii=False, separators=(",", ":")
            ).encode()
            response = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": page},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            self.assertLessEqual(len(structured), server.MAX_CLAIM_RESULT_BYTES)
            self.assertLess(len(response), 12 * 1024)
            self.assertNotIn("claim_token", page["structuredContent"]["claims"][0])

    def test_invalid_claim_token_is_bounded_and_not_echoed(self) -> None:
        untrusted = "untrusted-secret-" + "x" * 300
        with patch.object(server, "shell_status", return_value={"shell_instance": "shell-1"}):
            response = server.dispatch({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "release_session_window",
                    "_meta": {"threadId": "thread-a"},
                    "arguments": {"claim_token": untrusted},
                },
            })

        encoded = json.dumps(response, separators=(",", ":"))
        self.assertNotIn(untrusted, encoded)
        self.assertLessEqual(len(response["error"]["message"]), server.MAX_ERROR_TEXT_CHARS)

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
        self.assertEqual(len(tools), 16)
        annotations = {item["name"]: item["annotations"] for item in tools}
        self.assertFalse(annotations["end_focus_lease"]["idempotentHint"])
        self.assertEqual(
            annotations["get_session_window_capture"],
            {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        )
        self.assertEqual(
            annotations["capture_session_window"],
            {
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )
        self.assertEqual(
            annotations["save_session_window_capture"],
            {
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": False,
                "openWorldHint": False,
            },
        )
        schemas = {item["name"]: item["inputSchema"] for item in tools}
        self.assertIn("save_path", schemas["capture_session_window"]["properties"])
        self.assertNotIn("save_path", schemas["get_session_window_capture"]["properties"])
        self.assertEqual(
            schemas["save_session_window_capture"]["required"],
            ["window", "save_path"],
        )
        self.assertEqual(schemas["release_session_window"]["required"], ["claim_token"])
        self.assertEqual(schemas["release_session_window"]["properties"]["claim_token"]["maxLength"], 256)
        self.assertEqual(schemas["list_window_claims"]["properties"]["cursor"]["maxLength"], 20)
        self.assertEqual(
            schemas["claim_session_window"]["properties"]["lease_seconds"],
            {"type": "integer", "minimum": 5, "maximum": 300, "default": 60},
        )
