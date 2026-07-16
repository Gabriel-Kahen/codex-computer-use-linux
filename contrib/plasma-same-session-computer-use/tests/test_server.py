import os
import subprocess
from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

from plasma_same_session import kwin
from plasma_same_session import server


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


def focus_state(owner_id: str = "thread-a", token: str = "A" * 24) -> dict:
    claim_token = f"{'a' * 64}.{'B' * 32}"
    session = {"kwin_service_owner": ":1.42"}
    return {
        "version": 2,
        "token": token,
        "owner": {"thread_id": owner_id, "process": {"pid": 1}},
        "session_identity": session,
        "target": {"id": "{target}"},
        "window_claim": {"window_id": "{target}", "claim_token": claim_token},
        "binding": {
            "target_window_id": "{target}",
            "owner_thread_id": owner_id,
            "session_identity": session,
            "claim_token": claim_token,
        },
    }


class StatusTests(TestCase):
    @patch.object(kwin, "list_windows")
    def test_window_listing_is_paginated_and_truncates_model_visible_titles(self, list_windows) -> None:
        list_windows.return_value = [
            {**WINDOW, "id": f"{{{index}}}", "title": "t" * 700, "class": "c" * 300}
            for index in range(3)
        ]

        result = server.call_tool("list_plasma_windows", {"offset": 1, "limit": 1})
        value = result["structuredContent"]

        self.assertEqual(value, {
            "windows": [{
                **WINDOW,
                "id": "{1}",
                "title": "t" * server.MAX_WINDOW_TITLE_CHARS,
                "class": "c" * server.MAX_WINDOW_CLASS_CHARS,
            }],
            "total": 3,
            "next_offset": 2,
        })

    @patch.object(kwin, "list_windows")
    def test_unicode_window_pages_are_byte_bounded_and_advance(self, list_windows) -> None:
        list_windows.return_value = [
            {
                **WINDOW,
                "id": f"{{{index}}}",
                "capture_id": f"capture-{index}",
                "title": "\x01💥" * server.MAX_WINDOW_TITLE_CHARS,
                "class": "\\🧪" * server.MAX_WINDOW_CLASS_CHARS,
            }
            for index in range(10)
        ]

        offset = 0
        seen = 0
        while True:
            value = server.call_tool(
                "list_plasma_windows",
                {"offset": offset, "limit": 10},
            )["structuredContent"]
            self.assertTrue(value["windows"])
            self.assertLessEqual(
                server.coordination.serialized_size(value),
                server.MAX_WINDOW_LIST_BYTES,
            )
            seen += len(value["windows"])
            next_offset = value["next_offset"]
            if next_offset is None:
                break
            self.assertGreater(next_offset, offset)
            offset = next_offset

        self.assertEqual(seen, 10)

    @patch.object(kwin, "screen_locked", return_value=False)
    @patch.object(kwin, "helper_requirements")
    def test_status_bounds_wayland_display(self, requirements, _locked) -> None:
        requirements.return_value = {
            "kdotool": False,
            "gdbus": False,
            "qdbus": False,
            "cxx": False,
            "pkg_config": False,
            "qt6_development_files": False,
            "capture_helper_source": False,
        }
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "💥" * 50_000}, clear=False):
            display = server.session_status()["wayland_display"]

        self.assertLessEqual(len(display), server.MAX_STATUS_TEXT_CHARS)
        self.assertLessEqual(len(display.encode()), server.MAX_STATUS_TEXT_BYTES)

    @patch.object(kwin, "screen_locked", return_value=False)
    @patch.object(kwin, "capture_authorized_in_current_session", return_value=True)
    @patch.object(kwin, "run", return_value=subprocess.CompletedProcess([], 0, "CaptureWindow", ""))
    @patch.object(kwin, "helper_requirements")
    def test_exact_capture_is_gated_on_kdotool(self, requirements, _run, _authorized, _locked) -> None:
        requirements.return_value = {
            "kdotool": False,
            "gdbus": True,
            "qdbus": True,
            "cxx": True,
            "pkg_config": True,
            "qt6_development_files": True,
            "capture_helper_source": True,
        }
        environment = {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}

        with patch.dict(os.environ, environment, clear=False):
            capabilities = server.session_status()["capabilities"]

        self.assertFalse(capabilities["exact_capture_transport_available"])
        self.assertFalse(capabilities["exact_background_window_capture"])


class DispatchTests(TestCase):
    def test_claim_tool_rejects_missing_host_thread_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "_meta.threadId"):
            server.call_tool("claim_session_window", {"window": "Editor"})

    def test_list_claims_rejects_missing_host_thread_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "_meta.threadId"):
            server.call_tool("list_window_claims", {})

    @patch.object(server.coordination, "list_claims")
    def test_list_claims_rejects_invalid_pagination_before_reading_registry(self, list_claims) -> None:
        invalid_pages = [
            {"offset": -1},
            {"offset": server.MAX_CLAIM_PAGE_OFFSET + 1},
            {"limit": 0},
            {"limit": server.MAX_LIST_CLAIMS + 1},
        ]

        for arguments in invalid_pages:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, "must be between"):
                    server.call_tool("list_window_claims", arguments, thread_id="thread-a")

        list_claims.assert_not_called()

    @patch.object(server.kwin, "file_guard", return_value=nullcontext())
    @patch.object(server.focus_lease, "validate", return_value={"valid": True})
    @patch.object(server.focus_lease, "save")
    @patch.object(server.coordination, "current_process_identity", return_value={"pid": 42, "start_time": 7})
    @patch.object(server.focus_lease, "load")
    def test_validate_focus_lease_refreshes_and_saves_owner_process_identity(
        self,
        load,
        current_process_identity,
        save,
        validate,
        _file_guard,
    ) -> None:
        state = focus_state()
        load.return_value = state

        result = server.call_tool(
            "validate_plasma_focus_lease",
            {"lease_token": state["token"]},
            thread_id="thread-a",
        )

        self.assertEqual(result["structuredContent"], {"valid": True})
        current_process_identity.assert_called_once_with()
        save.assert_called_once_with(state)
        validate.assert_called_once_with(state, "thread-a")
        self.assertEqual(state["owner"]["process"], {"pid": 42, "start_time": 7})

    @patch.object(server.kwin, "file_guard", return_value=nullcontext())
    @patch.object(server.coordination, "current_session_identity", return_value={"kwin_service_owner": ":1.42"})
    @patch.object(server.focus_lease, "save")
    @patch.object(server.focus_lease, "load")
    def test_validate_focus_lease_rejects_foreign_owner_and_token(
        self,
        load,
        save,
        _current_session_identity,
        _file_guard,
    ) -> None:
        state = focus_state()
        load.return_value = state

        with self.assertRaisesRegex(RuntimeError, "owned by another agent"):
            server.call_tool(
                "validate_plasma_focus_lease",
                {"lease_token": state["token"]},
                thread_id="thread-b",
            )
        with self.assertRaisesRegex(ValueError, "token does not match"):
            server.call_tool(
                "validate_plasma_focus_lease",
                {"lease_token": "C" * 24},
                thread_id="thread-a",
            )

        save.assert_not_called()

    def test_dispatch_rejects_non_string_thread_id(self) -> None:
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "list_window_claims",
                "arguments": {},
                "_meta": {"threadId": 42},
            },
        })

        self.assertIn("must be a string", response["error"]["message"])

    def test_dispatch_rejects_byte_oversized_thread_id(self) -> None:
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "list_window_claims",
                "arguments": {},
                "_meta": {"threadId": "💥" * 200},
            },
        })

        self.assertIn("size limit", response["error"]["message"])

    def test_invalid_token_is_not_echoed_by_dispatch(self) -> None:
        invalid = "private-invalid-token"
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "release_session_window",
                "arguments": {"claim_token": invalid},
                "_meta": {"threadId": "thread-a"},
            },
        })

        self.assertNotIn(invalid, response["error"]["message"])

    def test_dispatch_bounds_unknown_names_and_arbitrary_errors(self) -> None:
        unknown_method = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "m" * 50_000})
        unknown_tool = server.dispatch({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "t" * 50_000, "arguments": {}},
        })
        with patch.object(server, "call_tool", side_effect=RuntimeError("e" * 50_000)):
            arbitrary = server.dispatch({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "plasma_session_status", "arguments": {}},
            })

        for response in (unknown_method, unknown_tool, arbitrary):
            message = response["error"]["message"]
            self.assertLessEqual(len(message), server.MAX_ERROR_TEXT_CHARS)
            self.assertLessEqual(len(message.encode()), server.MAX_ERROR_TEXT_BYTES)

    def test_claim_tool_schemas_bound_window_owner_tokens_and_pages(self) -> None:
        tools = {tool["name"]: tool for tool in server.TOOLS}
        claim_properties = tools["claim_session_window"]["inputSchema"]["properties"]
        release_properties = tools["release_session_window"]["inputSchema"]["properties"]
        list_properties = tools["list_window_claims"]["inputSchema"]["properties"]

        self.assertEqual(claim_properties["window"]["maxLength"], server.coordination.MAX_WINDOW_QUERY_CHARS)
        self.assertEqual(claim_properties["claim_token"]["maxLength"], server.coordination.MAX_CLAIM_TOKEN_CHARS)
        self.assertEqual(release_properties["claim_token"]["pattern"], f"^{server.coordination.CLAIM_TOKEN_PATTERN}$")
        self.assertEqual(list_properties["offset"]["maximum"], server.coordination.MAX_ACTIVE_CLAIMS)
