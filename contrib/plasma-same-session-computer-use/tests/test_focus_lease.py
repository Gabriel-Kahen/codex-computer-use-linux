import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import coordination
from plasma_same_session import focus_lease
from plasma_same_session import kwin


WINDOW = {
    "id": "{target}",
    "capture_id": "target",
    "title": "Editor",
    "class": "code",
    "pid": 123,
    "desktop": 3,
    "active": False,
    "minimized": False,
    "fullscreen": False,
    "excluded_from_capture": False,
    "geometry": {"x": 0, "y": 0, "width": 1000, "height": 700},
}
SESSION = {
    "uid": os.getuid(),
    "boot_id": "boot-test",
    "wayland_display": "wayland-test",
    "wayland_socket": {"device": 1, "inode": 2},
    "session_id": "session-test",
    "kwin_service_owner": ":1.42",
}


def bound_focus_state(claim: dict, lease_token: str = "A" * 24) -> dict:
    target = coordination.window_for_model(WINDOW)
    return {
        "version": 3,
        "token": lease_token,
        "phase": "active",
        "created_at": 1,
        "expires_at": 2**62,
        "session_identity": SESSION,
        "owner": {"thread_id": "owner-a", "process": coordination.process_identity()},
        "window_claim": {
            "claim_token": claim["claim_token"],
            "window_id": WINDOW["id"],
            "implicit": False,
        },
        "target": target,
        "original": {
            "active_window": "{original}",
            "desktop": 1,
            "target_desktop": 3,
            "target_minimized": False,
            "pointer": {"x": 5, "y": 7},
        },
        "binding": {
            "target_window_id": WINDOW["id"],
            "owner_thread_id": "owner-a",
            "session_identity": SESSION,
            "claim_token": claim["claim_token"],
        },
    }


class FocusLeaseTests(TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.lease_file = Path(self.directory.name) / "lease.json"
        self.patch = patch.object(focus_lease, "LEASE_FILE", self.lease_file)
        self.patch.start()
        self.coordination_lease_patch = patch.object(coordination, "FOCUS_LEASE_FILE", self.lease_file)
        self.coordination_lease_patch.start()
        self.lease_lock_patch = patch.object(focus_lease, "LEASE_LOCK", Path(self.directory.name) / "lease.lock")
        self.lease_lock_patch.start()
        self.journal_lock_patch = patch.object(
            focus_lease,
            "JOURNAL_LOCK",
            Path(self.directory.name) / "journal.lock",
        )
        self.journal_lock_patch.start()
        self.claims_patch = patch.object(coordination, "CLAIMS_DIR", Path(self.directory.name) / "claims")
        self.claims_patch.start()
        self.session_patch = patch.object(coordination, "current_session_identity", return_value=SESSION)
        self.session_patch.start()
        self.lock_patch = patch.object(kwin, "screen_locked", return_value=False)
        self.lock_patch.start()

    def tearDown(self) -> None:
        self.lock_patch.stop()
        self.session_patch.stop()
        self.claims_patch.stop()
        self.journal_lock_patch.stop()
        self.lease_lock_patch.stop()
        self.coordination_lease_patch.stop()
        self.patch.stop()
        self.directory.cleanup()

    @patch.object(kwin, "activate")
    @patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7})
    @patch.object(kwin, "current_desktop", return_value=1)
    @patch.object(kwin, "active_window_id", side_effect=["{original}", "{target}"])
    @patch.object(kwin, "window_info", return_value={"uuid": "original"})
    @patch.object(kwin, "resolve_window", return_value=WINDOW)
    @patch.object(kwin, "screen_locked", return_value=False)
    def test_begin_journals_before_activating(
        self, _locked, _resolve, _info, _active, _desktop, _pointer, activate
    ) -> None:
        def assert_prepared_before_activation(window_id: str) -> None:
            self.assertEqual(window_id, "{target}")
            self.assertTrue(self.lease_file.is_file())
            prepared = json.loads(self.lease_file.read_text())
            self.assertEqual(prepared["phase"], "prepared")
            self.assertEqual(prepared["target"]["id"], "{target}")

        activate.side_effect = assert_prepared_before_activation
        result = focus_lease.begin({"window": "Editor", "acknowledge_interference": True, "max_seconds": 30})

        activate.assert_called_once_with("{target}")
        journal = json.loads(self.lease_file.read_text())
        self.assertEqual(journal["phase"], "active")
        self.assertTrue(journal["window_claim"]["implicit"])
        self.assertEqual(journal["window_claim"]["window_id"], "{target}")
        self.assertTrue(journal["owner"]["thread_id"].startswith("legacy-process:"))
        self.assertEqual(journal["session_identity"], SESSION)
        self.assertEqual(journal["original"], {
            "active_window": "{original}",
            "desktop": 1,
            "target_desktop": 3,
            "target_minimized": False,
            "pointer": {"x": 5, "y": 7},
        })
        self.assertEqual(result["pointer_before"], {"x": 5, "y": 7})
        self.assertEqual(stat.S_IMODE(self.lease_file.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.lease_file.stat().st_mode), 0o600)

    def test_prepared_journal_publish_failure_rolls_back_existing_claim(self) -> None:
        claim = coordination.claim_window(WINDOW, "owner-a", 30)
        key = claim["claim_token"].split(".", 1)[0]
        claim_path = coordination._session_claims_dir(SESSION) / f"{key}.json"
        before = coordination.read_private_json(claim_path)

        with (
            patch.object(kwin, "resolve_window", return_value=WINDOW),
            patch.object(kwin, "window_info", return_value={"uuid": "original"}),
            patch.object(kwin, "active_window_id", return_value="{original}"),
            patch.object(kwin, "current_desktop", return_value=1),
            patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7}),
            patch.object(kwin, "activate") as activate,
            patch.object(focus_lease, "save", side_effect=OSError("publish failed")),
        ):
            with self.assertRaisesRegex(OSError, "publish failed"):
                focus_lease.begin(
                    {
                        "window": "Editor",
                        "acknowledge_interference": True,
                        "max_seconds": 90,
                        "claim_token": claim["claim_token"],
                    },
                    "owner-a",
                )

        self.assertEqual(coordination.read_private_json(claim_path), before)
        self.assertFalse(self.lease_file.exists())
        activate.assert_not_called()

    def test_begin_rejects_session_change_across_initial_snapshot(self) -> None:
        changed = {**SESSION, "kwin_service_owner": ":1.99"}
        session_calls = 0

        def session_identity() -> dict:
            nonlocal session_calls
            session_calls += 1
            return SESSION if session_calls == 1 else changed

        def snapshot(value):
            self.assertEqual(session_calls, 1)
            return value

        with (
            patch.object(coordination, "current_session_identity", side_effect=session_identity),
            patch.object(kwin, "resolve_window", side_effect=lambda _query: snapshot(WINDOW)),
            patch.object(kwin, "pointer_position", side_effect=lambda: snapshot({"x": 5, "y": 7})),
            patch.object(kwin, "active_window_id", side_effect=lambda: snapshot("{original}")),
            patch.object(kwin, "window_info", side_effect=lambda _window_id: snapshot({"uuid": "original"})),
            patch.object(kwin, "current_desktop", side_effect=lambda: snapshot(1)),
            patch.object(kwin, "activate") as activate,
        ):
            with self.assertRaisesRegex(RuntimeError, "changed while snapshotting"):
                focus_lease.begin({"window": "Editor", "acknowledge_interference": True}, "owner-a")

        self.assertEqual(session_calls, 2)
        self.assertFalse(any(coordination.CLAIMS_DIR.rglob("*.json")))
        self.assertFalse(self.lease_file.exists())
        activate.assert_not_called()

    def test_begin_rejects_a_claim_from_another_session(self) -> None:
        changed = {**SESSION, "kwin_service_owner": ":1.99"}

        @contextmanager
        def mismatched_claim(*_args, **_kwargs):
            yield {
                "claim_token": f"{'a' * 64}.{'A' * 32}",
                "expires_at": 2**62,
                "implicit": True,
                "session_identity": changed,
            }

        with (
            patch.object(coordination, "focus_claim_transaction", mismatched_claim),
            patch.object(kwin, "resolve_window", return_value=WINDOW),
            patch.object(kwin, "window_info", return_value={"uuid": "original"}),
            patch.object(kwin, "active_window_id", return_value="{original}"),
            patch.object(kwin, "current_desktop", return_value=1),
            patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7}),
            patch.object(focus_lease, "save") as save,
            patch.object(kwin, "activate") as activate,
        ):
            with self.assertRaisesRegex(RuntimeError, "focus claim session identity"):
                focus_lease.begin({"window": "Editor", "acknowledge_interference": True}, "owner-a")

        save.assert_not_called()
        activate.assert_not_called()

    def test_begin_requires_explicit_acknowledgement(self) -> None:
        with self.assertRaisesRegex(ValueError, "acknowledge_interference"):
            focus_lease.begin({"window": "Editor", "acknowledge_interference": False})

    def test_begin_rejects_non_integer_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_seconds"):
            focus_lease.begin({
                "window": "Editor",
                "acknowledge_interference": True,
                "max_seconds": "60",
            })

    @patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7})
    @patch.object(kwin, "active_window_id", return_value="{original}")
    @patch.object(kwin, "current_desktop", return_value=1)
    @patch.object(kwin, "window_boolean", return_value=False)
    @patch.object(kwin, "window_desktop", return_value=3)
    @patch.object(kwin, "activate")
    @patch.object(kwin, "set_desktop")
    @patch.object(kwin, "set_window_minimized")
    @patch.object(kwin, "set_window_desktop")
    @patch.object(kwin, "list_windows", return_value=[WINDOW, {**WINDOW, "id": "{original}"}])
    def test_restore_reports_pointer_as_companion_responsibility(
        self,
        _windows,
        set_window_desktop,
        set_window_minimized,
        set_desktop,
        activate,
        _target_desktop,
        _minimized,
        _desktop,
        _active,
        _pointer,
    ) -> None:
        state = {
            "version": 1,
            "target": WINDOW,
            "original": {"active_window": "{original}", "desktop": 1, "target_desktop": 3, "target_minimized": False, "pointer": {"x": 5, "y": 7}},
        }
        self.lease_file.write_text(json.dumps(state))

        result = focus_lease.restore(state)

        set_window_desktop.assert_called_once_with("{target}", 3)
        set_window_minimized.assert_called_once_with("{target}", False)
        set_desktop.assert_called_once_with(1)
        activate.assert_called_once_with("{original}")
        self.assertEqual(result["pointer_restore_coordinate"], {"x": 5, "y": 7})
        self.assertFalse(result["pointer_restored_by_this_backend"])
        self.assertTrue(result["restored"])
        self.assertTrue(result["recovery_complete"])
        self.assertFalse(self.lease_file.exists())

    def test_tokens_use_constant_time_comparison_path(self) -> None:
        self.lease_file.write_text(json.dumps({"version": 1, "token": "A" * 24}))
        with self.assertRaisesRegex(ValueError, "does not match"):
            focus_lease.require("B" * 24)

    @patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7})
    @patch.object(kwin, "resolve_window", return_value={**WINDOW, "desktop": None})
    @patch.object(kwin, "screen_locked", return_value=False)
    def test_begin_refuses_unknown_target_desktop(self, _locked, _resolve, _pointer) -> None:
        with self.assertRaisesRegex(RuntimeError, "target desktop"):
            focus_lease.begin({"window": "Editor", "acknowledge_interference": True})
        self.assertFalse(self.lease_file.exists())

    @patch.object(kwin, "pointer_position", return_value={"x": 10**100, "y": 0})
    @patch.object(kwin, "resolve_window", return_value=WINDOW)
    @patch.object(kwin, "screen_locked", return_value=False)
    def test_begin_refuses_unbounded_live_pointer_snapshot(self, _locked, _resolve, _pointer) -> None:
        with self.assertRaisesRegex(RuntimeError, "pointer position is invalid"):
            focus_lease.begin({"window": "Editor", "acknowledge_interference": True})
        self.assertFalse(self.lease_file.exists())

    @patch.object(kwin, "active_window_id", return_value=None)
    @patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7})
    @patch.object(kwin, "resolve_window", return_value=WINDOW)
    @patch.object(kwin, "screen_locked", return_value=False)
    def test_begin_refuses_missing_original_active_window(self, _locked, _resolve, _pointer, _active) -> None:
        with self.assertRaisesRegex(RuntimeError, "original active window"):
            focus_lease.begin({"window": "Editor", "acknowledge_interference": True})
        self.assertFalse(self.lease_file.exists())

    @patch.object(focus_lease, "restore", return_value={"recovery_complete": True, "errors": []})
    @patch.object(kwin, "activate")
    @patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7})
    @patch.object(kwin, "current_desktop", return_value=1)
    @patch.object(kwin, "active_window_id", side_effect=["{original}", "{other}"])
    @patch.object(kwin, "window_info", return_value={"uuid": "original"})
    @patch.object(kwin, "resolve_window", return_value=WINDOW)
    @patch.object(kwin, "screen_locked", return_value=False)
    def test_begin_verifies_target_became_active(
        self, _locked, _resolve, _info, _active, _desktop, _pointer, _activate, restore
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "did not activate the target"):
            focus_lease.begin({"window": "Editor", "acknowledge_interference": True})
        restore.assert_called_once()

    def test_begin_rechecks_lock_before_activation_and_retains_prepared_journal(self) -> None:
        with (
            patch.object(kwin, "screen_locked", side_effect=[False, True, True]),
            patch.object(kwin, "resolve_window", return_value=WINDOW),
            patch.object(kwin, "window_info", return_value={"uuid": "original"}),
            patch.object(kwin, "active_window_id", return_value="{original}"),
            patch.object(kwin, "current_desktop", return_value=1),
            patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7}),
            patch.object(kwin, "activate") as activate,
        ):
            with self.assertRaisesRegex(RuntimeError, "session is locked"):
                focus_lease.begin({"window": "Editor", "acknowledge_interference": True})

        activate.assert_not_called()
        self.assertEqual(json.loads(self.lease_file.read_text())["phase"], "prepared")

    @patch.object(kwin, "active_window_id", return_value="{target}")
    @patch.object(kwin, "screen_locked", return_value=False)
    @patch.object(kwin, "list_windows", return_value=[WINDOW])
    def test_validate_rechecks_live_focus_and_is_explicitly_advisory(self, _windows, _locked, _active) -> None:
        state = {"version": 1, "phase": "active", "expires_at": 2**62, "target": WINDOW}

        result = focus_lease.validate(state)

        self.assertTrue(result["advisory_ready"])
        self.assertFalse(result["external_input_gated_by_broker"])
        self.assertIn("caller-enforced", result["required_caller_action"])

    @patch.object(kwin, "active_window_id", return_value="{other}")
    @patch.object(kwin, "screen_locked", return_value=False)
    @patch.object(kwin, "list_windows", return_value=[WINDOW])
    def test_validate_refuses_stale_or_lost_focus(self, _windows, _locked, _active) -> None:
        state = {"version": 1, "phase": "active", "expires_at": 0, "target": WINDOW}

        result = focus_lease.validate(state)

        self.assertFalse(result["advisory_ready"])
        self.assertTrue(result["expired"])
        self.assertFalse(result["target_active"])

    def test_validate_refuses_a_focus_lease_whose_window_claim_was_lost(self) -> None:
        claim = coordination.claim_window(WINDOW, "owner-a", 60)
        coordination.release_window_claim(claim["claim_token"], "owner-a")
        state = {
            "version": 1,
            "phase": "active",
            "expires_at": 2**62,
            "target": WINDOW,
            "owner": {"thread_id": "owner-a", "process": coordination.process_identity()},
            "window_claim": {
                "claim_token": claim["claim_token"],
                "window_id": WINDOW["id"],
                "implicit": False,
            },
        }

        with (
            patch.object(kwin, "list_windows", return_value=[WINDOW]),
            patch.object(kwin, "screen_locked", return_value=False),
            patch.object(kwin, "active_window_id", return_value="{target}"),
        ):
            result = focus_lease.validate(state, "owner-a")

        self.assertFalse(result["advisory_ready"])
        self.assertFalse(result["window_claim_active"])
        self.assertIn("window claim", result["errors"][0])

    def test_successful_restore_releases_an_implicit_focus_claim(self) -> None:
        claim = coordination.ensure_focus_claim(WINDOW, "owner-a", None, 60)
        state = {
            "version": 1,
            "token": "A" * 24,
            "owner": {"thread_id": "owner-a", "process": coordination.process_identity()},
            "window_claim": {
                "claim_token": claim["claim_token"],
                "window_id": WINDOW["id"],
                "implicit": True,
            },
            "original": {},
        }
        focus_lease.save(state)

        result = focus_lease._finish_restore(state, [], [], {}, None)

        self.assertTrue(result["window_claim_released"])
        self.assertEqual(coordination.list_claims("owner-a", 0, 20)["total"], 0)
        self.assertFalse(self.lease_file.exists())

    def test_failed_implicit_claim_cleanup_retains_the_journal(self) -> None:
        claim = coordination.ensure_focus_claim(WINDOW, "owner-a", None, 60)
        state = {
            "version": 1,
            "owner": {"thread_id": "owner-a", "process": coordination.process_identity()},
            "window_claim": {
                "claim_token": claim["claim_token"],
                "window_id": WINDOW["id"],
                "implicit": True,
            },
            "original": {},
        }
        focus_lease.save(state)

        with (
            patch.object(
                coordination,
                "discard_bound_claim",
                return_value=coordination.ClaimCleanupResult.BLOCKED,
            ),
            patch.object(focus_lease, "_delete_if_current") as delete_if_current,
        ):
            result = focus_lease._finish_restore(state, [], [], {}, None)

        delete_if_current.assert_not_called()
        self.assertFalse(result["recovery_complete"])
        self.assertFalse(result["window_claim_released"])
        self.assertEqual(result["window_claim_cleanup"], "blocked")
        self.assertIn("implicit window claim", result["errors"][0])
        self.assertTrue(result["journal_retained"])
        self.assertTrue(self.lease_file.exists())

    def test_implicit_claim_cleanup_retries_after_journal_delete_failure(self) -> None:
        claim = coordination.ensure_focus_claim(WINDOW, "owner-a", None, 60)
        state = {
            "version": 1,
            "token": "A" * 24,
            "owner": {"thread_id": "owner-a", "process": coordination.process_identity()},
            "window_claim": {
                "claim_token": claim["claim_token"],
                "window_id": WINDOW["id"],
                "implicit": True,
            },
            "original": {},
        }
        focus_lease.save(state)

        with patch.object(focus_lease, "_delete_if_current", return_value=False):
            first = focus_lease._finish_restore(state, [], [], {}, None)
        second = focus_lease._finish_restore(state, [], [], {}, None)

        self.assertFalse(first["recovery_complete"])
        self.assertTrue(second["recovery_complete"])
        self.assertFalse(second["window_claim_released"])
        self.assertEqual(second["window_claim_cleanup"], "already_absent")
        self.assertFalse(self.lease_file.exists())

    def test_different_session_v3_recovery_preserves_old_claim_and_clears_journal(self) -> None:
        old_session = {**SESSION, "kwin_service_owner": ":1.41"}
        with patch.object(coordination, "current_session_identity", return_value=old_session):
            claim = coordination.ensure_focus_claim(WINDOW, "owner-a", None, 60)
        claim_path = next(coordination.CLAIMS_DIR.rglob("*.json"))
        state = bound_focus_state(claim)
        state["session_identity"] = old_session
        state["binding"]["session_identity"] = old_session
        state["window_claim"]["implicit"] = True
        focus_lease.save(state)

        result = focus_lease.restore(state)

        self.assertTrue(result["recovery_complete"])
        self.assertFalse(result["window_claim_released"])
        self.assertEqual(result["window_claim_cleanup"], "already_absent")
        self.assertTrue(claim_path.is_file())
        self.assertFalse(self.lease_file.exists())

    @patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7})
    @patch.object(kwin, "current_desktop", return_value=1)
    @patch.object(kwin, "set_desktop")
    @patch.object(kwin, "list_windows", return_value=[])
    def test_closed_windows_are_reported_without_retaining_unactionable_journal(
        self, _windows, _set_desktop, _desktop, _pointer
    ) -> None:
        state = {
            "version": 1,
            "target": WINDOW,
            "original": {
                "active_window": "{original}",
                "desktop": 1,
                "target_desktop": 3,
                "target_minimized": False,
                "pointer": {"x": 5, "y": 7},
            },
        }
        self.lease_file.write_text(json.dumps(state))

        result = focus_lease.restore(state)

        self.assertFalse(result["restored"])
        self.assertTrue(result["recovery_complete"])
        self.assertEqual(result["missing_windows"], ["target:{target}", "original-active:{original}"])
        self.assertFalse(result["journal_retained"])
        self.assertFalse(self.lease_file.exists())

    @patch.object(kwin, "set_desktop", side_effect=RuntimeError("denied"))
    @patch.object(kwin, "list_windows", return_value=[])
    def test_material_restore_failure_retains_journal(self, _windows, _set_desktop) -> None:
        state = {"version": 1, "target": {}, "original": {"active_window": "", "desktop": 1}}
        self.lease_file.write_text(json.dumps(state))

        result = focus_lease.restore(state)

        self.assertFalse(result["restored"])
        self.assertFalse(result["recovery_complete"])
        self.assertTrue(result["journal_retained"])
        self.assertTrue(self.lease_file.exists())

    def test_restore_diagnostics_have_character_and_byte_bounds(self) -> None:
        state = {"version": 1, "target": WINDOW, "original": {"active_window": "{original}"}}
        self.lease_file.write_text(json.dumps(state))

        with patch.object(kwin, "list_windows", side_effect=RuntimeError("💥" * 50_000)):
            result = focus_lease.restore(state)

        self.assertFalse(result["recovery_complete"])
        self.assertTrue(result["errors"])
        for diagnostic in result["errors"]:
            self.assertLessEqual(len(diagnostic), focus_lease.MAX_DIAGNOSTIC_CHARS)
            self.assertLessEqual(len(json.dumps(diagnostic, ensure_ascii=False).encode()) - 2, focus_lease.MAX_DIAGNOSTIC_BYTES)

    def test_restore_result_has_an_aggregate_byte_bound(self) -> None:
        state = {
            "version": 1,
            "target": WINDOW,
            "original": {"active_window": "{original}", "desktop": 1, "pointer": {"x": 1, "y": 2}},
        }

        result = focus_lease._finish_restore(state, ["💥" * 512] * 8, [], {}, {"x": 1, "y": 2})

        self.assertLessEqual(coordination.serialized_size(result["errors"]), focus_lease.MAX_DIAGNOSTICS_BYTES)
        self.assertLessEqual(coordination.serialized_size(result), focus_lease.MAX_FOCUS_RESULT_BYTES)

    @patch.object(kwin, "list_windows")
    def test_restore_rejects_unbounded_persisted_metadata_before_mutation(self, list_windows) -> None:
        state = {
            "version": 1,
            "target": WINDOW,
            "original": {"active_window": "x" * 100_000, "desktop": 1, "pointer": {"x": 1, "y": 2}},
        }

        with self.assertRaisesRegex(RuntimeError, "original active window is invalid"):
            focus_lease.restore(state)

        list_windows.assert_not_called()

    @patch.object(kwin, "active_window_id", return_value="{target}")
    @patch.object(kwin, "screen_locked", return_value=False)
    @patch.object(kwin, "list_windows", return_value=[WINDOW])
    def test_validate_normalizes_untrusted_phase_and_bounds_result(self, *_mocks) -> None:
        state = {"version": 1, "phase": "x" * 100_000, "expires_at": 0, "target": WINDOW}

        result = focus_lease.validate(state, "owner-a")

        self.assertEqual(result["phase"], "unknown")
        self.assertLessEqual(coordination.serialized_size(result), focus_lease.MAX_FOCUS_RESULT_BYTES)

    @patch.object(kwin, "list_windows")
    @patch.object(kwin, "screen_locked", return_value=True)
    def test_restore_refuses_locked_session_and_retains_journal(self, _locked, list_windows) -> None:
        state = {"version": 1, "target": WINDOW, "original": {"active_window": "{original}", "desktop": 1}}
        self.lease_file.write_text(json.dumps(state))

        result = focus_lease.restore(state)

        self.assertFalse(result["recovery_complete"])
        self.assertTrue(result["journal_retained"])
        self.assertIn("session is locked", result["errors"][0])
        self.assertTrue(self.lease_file.exists())
        list_windows.assert_not_called()

    @patch.object(kwin, "pointer_position", return_value={"x": 99, "y": 100})
    @patch.object(kwin, "active_window_id", return_value="{original}")
    @patch.object(kwin, "current_desktop", return_value=1)
    @patch.object(kwin, "window_boolean", return_value=False)
    @patch.object(kwin, "window_desktop", return_value=3)
    @patch.object(kwin, "activate")
    @patch.object(kwin, "set_desktop")
    @patch.object(kwin, "set_window_minimized")
    @patch.object(kwin, "set_window_desktop")
    @patch.object(kwin, "list_windows", return_value=[WINDOW, {**WINDOW, "id": "{original}"}])
    def test_unrestored_pointer_is_material_and_retains_journal(self, *_mocks) -> None:
        state = {
            "version": 1,
            "target": WINDOW,
            "original": {
                "active_window": "{original}",
                "desktop": 1,
                "target_desktop": 3,
                "target_minimized": False,
                "pointer": {"x": 5, "y": 7},
            },
        }
        self.lease_file.write_text(json.dumps(state))

        result = focus_lease.restore(state)

        self.assertFalse(result["recovery_complete"])
        self.assertTrue(result["pointer_restore_required"])
        self.assertTrue(result["journal_retained"])
        self.assertTrue(self.lease_file.exists())

    @patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7})
    @patch.object(kwin, "active_window_id", return_value="{original}")
    @patch.object(kwin, "current_desktop", return_value=2)
    @patch.object(kwin, "window_boolean", return_value=False)
    @patch.object(kwin, "window_desktop", return_value=3)
    @patch.object(kwin, "activate")
    @patch.object(kwin, "set_desktop")
    @patch.object(kwin, "set_window_minimized")
    @patch.object(kwin, "set_window_desktop")
    @patch.object(kwin, "list_windows", return_value=[WINDOW, {**WINDOW, "id": "{original}"}])
    def test_restore_verifies_desktop_after_reactivating_original_window(self, *_mocks) -> None:
        state = {
            "version": 1,
            "target": WINDOW,
            "original": {
                "active_window": "{original}",
                "desktop": 1,
                "target_desktop": 3,
                "target_minimized": False,
                "pointer": {"x": 5, "y": 7},
            },
        }
        self.lease_file.write_text(json.dumps(state))

        result = focus_lease.restore(state)

        self.assertFalse(result["recovery_complete"])
        self.assertFalse(result["verified"]["desktop"])
        self.assertTrue(result["journal_retained"])

    def test_restore_does_not_replay_state_into_a_different_kwin_session(self) -> None:
        changed = {**SESSION, "kwin_service_owner": ":1.99"}
        state = {
            "version": 1,
            "session_identity": SESSION,
            "target": WINDOW,
            "original": {"active_window": "{original}", "desktop": 1},
        }
        focus_lease.save(state)

        with (
            patch.object(coordination, "current_session_identity", return_value=changed),
            patch.object(kwin, "list_windows") as list_windows,
        ):
            result = focus_lease.restore(state)

        list_windows.assert_not_called()
        self.assertTrue(result["recovery_complete"])
        self.assertFalse(result["restored"])
        self.assertIn("no longer active", result["missing_windows"][0])
        self.assertFalse(self.lease_file.exists())

    def test_restore_rechecks_session_after_window_enumeration(self) -> None:
        changed = {**SESSION, "kwin_service_owner": ":1.99"}
        claim = coordination.claim_window(WINDOW, "owner-a", 60)
        state = bound_focus_state(claim)
        focus_lease.save(state)

        with (
            patch.object(coordination, "current_session_identity", side_effect=[SESSION, changed]),
            patch.object(kwin, "list_windows", return_value=[WINDOW, {**WINDOW, "id": "{original}"}]),
            patch.object(kwin, "set_window_desktop") as set_window_desktop,
            patch.object(kwin, "set_window_minimized") as set_window_minimized,
            patch.object(kwin, "set_desktop") as set_desktop,
            patch.object(kwin, "activate") as activate,
        ):
            result = focus_lease.restore(state)

        set_window_desktop.assert_not_called()
        set_window_minimized.assert_not_called()
        set_desktop.assert_not_called()
        activate.assert_not_called()
        self.assertFalse(result["recovery_complete"])
        self.assertTrue(result["journal_retained"])
        self.assertIn("changed after window enumeration", result["errors"][0])
        self.assertTrue(self.lease_file.exists())

    def test_restore_rechecks_session_immediately_before_mutations(self) -> None:
        changed = {**SESSION, "kwin_service_owner": ":1.99"}
        claim = coordination.claim_window(WINDOW, "owner-a", 60)
        state = bound_focus_state(claim)
        focus_lease.save(state)

        with (
            patch.object(
                coordination,
                "current_session_identity",
                side_effect=[SESSION, SESSION, changed],
            ),
            patch.object(kwin, "list_windows", return_value=[WINDOW, {**WINDOW, "id": "{original}"}]),
            patch.object(kwin, "set_window_desktop") as set_window_desktop,
            patch.object(kwin, "set_window_minimized") as set_window_minimized,
            patch.object(kwin, "set_desktop") as set_desktop,
            patch.object(kwin, "activate") as activate,
        ):
            result = focus_lease.restore(state)

        set_window_desktop.assert_not_called()
        set_window_minimized.assert_not_called()
        set_desktop.assert_not_called()
        activate.assert_not_called()
        self.assertFalse(result["recovery_complete"])
        self.assertTrue(result["journal_retained"])
        self.assertIn("changed immediately before restoration mutations", result["errors"][0])
        self.assertTrue(self.lease_file.exists())

    def test_restore_rechecks_session_before_finalizing_the_journal(self) -> None:
        changed = {**SESSION, "kwin_service_owner": ":1.99"}
        claim = coordination.claim_window(WINDOW, "owner-a", 60)
        state = bound_focus_state(claim)
        focus_lease.save(state)

        with (
            patch.object(
                coordination,
                "current_session_identity",
                side_effect=[SESSION, SESSION, SESSION, changed],
            ),
            patch.object(kwin, "list_windows", return_value=[WINDOW, {**WINDOW, "id": "{original}"}]),
            patch.object(kwin, "set_window_desktop") as set_window_desktop,
            patch.object(kwin, "window_desktop", return_value=3),
            patch.object(kwin, "set_window_minimized"),
            patch.object(kwin, "window_boolean", return_value=False),
            patch.object(kwin, "set_desktop"),
            patch.object(kwin, "current_desktop", return_value=1),
            patch.object(kwin, "activate") as activate,
            patch.object(kwin, "active_window_id", return_value="{original}"),
            patch.object(kwin, "pointer_position", return_value={"x": 5, "y": 7}),
        ):
            result = focus_lease.restore(state)

        set_window_desktop.assert_called_once_with("{target}", 3)
        activate.assert_called_once_with("{original}")
        self.assertFalse(result["recovery_complete"])
        self.assertTrue(result["journal_retained"])
        self.assertIn("changed before restoration finalization", result["errors"][0])
        self.assertTrue(self.lease_file.exists())

    def test_unexpired_v1_journal_without_process_identity_remains_live(self) -> None:
        state = {
            "version": 1,
            "token": "A" * 24,
            "phase": "active",
            "created_at": 1,
            "expires_at": 2**62,
            "target": coordination.window_for_model(WINDOW),
            "original": {
                "active_window": "{original}",
                "desktop": 1,
                "target_desktop": 3,
                "target_minimized": False,
                "pointer": {"x": 5, "y": 7},
            },
        }
        focus_lease.save(state)

        with patch.object(focus_lease, "restore") as restore:
            with self.assertRaisesRegex(RuntimeError, "live focus lease"):
                focus_lease.begin(
                    {"window": "Editor", "acknowledge_interference": True},
                    "owner-b",
                )

        restore.assert_not_called()
        self.assertEqual(focus_lease.load(), state)

    def test_stale_restore_cannot_act_on_or_delete_a_newer_focus_journal(self) -> None:
        claim = coordination.claim_window(WINDOW, "owner-a", 60)

        def state(lease_token: str) -> dict:
            target = coordination.window_for_model(WINDOW)
            return {
                "version": 3,
                "token": lease_token,
                "phase": "active",
                "expires_at": 2**62,
                "session_identity": SESSION,
                "owner": {"thread_id": "owner-a", "process": coordination.process_identity()},
                "window_claim": {
                    "claim_token": claim["claim_token"],
                    "window_id": WINDOW["id"],
                    "implicit": False,
                },
                "target": target,
                "binding": {
                    "target_window_id": WINDOW["id"],
                    "owner_thread_id": "owner-a",
                    "session_identity": SESSION,
                    "claim_token": claim["claim_token"],
                },
            }

        stale = state("A" * 24)
        current = state("B" * 24)
        focus_lease.save(current)

        with patch.object(kwin, "list_windows") as list_windows, patch.object(kwin, "activate") as activate:
            result = focus_lease.restore(stale)

        list_windows.assert_not_called()
        activate.assert_not_called()
        self.assertFalse(result["recovery_complete"])
        self.assertIn("stale restoration was refused", result["errors"][0])
        self.assertEqual(focus_lease.load(), current)

    def test_mutating_an_immutable_focus_binding_fails_closed(self) -> None:
        claim = coordination.claim_window(WINDOW, "owner-a", 60)
        target = coordination.window_for_model(WINDOW)
        state = {
            "version": 3,
            "token": "A" * 24,
            "phase": "prepared",
            "expires_at": 2**62,
            "session_identity": SESSION,
            "owner": {"thread_id": "owner-a", "process": coordination.process_identity()},
            "window_claim": {
                "claim_token": claim["claim_token"],
                "window_id": "{mutated}",
                "implicit": False,
            },
            "target": target,
            "binding": {
                "target_window_id": WINDOW["id"],
                "owner_thread_id": "owner-a",
                "session_identity": SESSION,
                "claim_token": claim["claim_token"],
            },
        }

        with self.assertRaisesRegex(RuntimeError, "binding is invalid"):
            focus_lease.save(state)

        self.assertFalse(self.lease_file.exists())

    def test_version_three_focus_journal_requires_an_immutable_binding(self) -> None:
        claim = coordination.claim_window(WINDOW, "owner-a", 60)
        state = bound_focus_state(claim)
        del state["binding"]

        with self.assertRaisesRegex(RuntimeError, "binding is invalid"):
            focus_lease.save(state)

        self.assertFalse(self.lease_file.exists())
