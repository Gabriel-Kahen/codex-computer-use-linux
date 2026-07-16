from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import call
from unittest.mock import patch

from support import MODULE_ROOT

import sys

sys.path.insert(0, str(MODULE_ROOT))

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


def focus_state() -> dict:
    claim_token = f"{'a' * 64}.{'B' * 32}"
    session = {"kwin_service_owner": ":1.42"}
    return {
        "version": 2,
        "token": "A" * 24,
        "owner": {"thread_id": "thread-a", "process": {"pid": 1}},
        "session_identity": session,
        "target": {"id": "{target}"},
        "window_claim": {"window_id": "{target}", "claim_token": claim_token},
        "binding": {
            "target_window_id": "{target}",
            "owner_thread_id": "thread-a",
            "session_identity": session,
            "claim_token": claim_token,
        },
    }


class ServerRouteTests(TestCase):
    @patch.object(server.kwin, "list_windows", return_value=[])
    def test_existing_window_list_accepts_large_nonnegative_offsets(self, _list_windows) -> None:
        schema = next(tool for tool in server.TOOLS if tool["name"] == "list_plasma_windows")["inputSchema"]

        result = server.call_tool("list_plasma_windows", {"offset": 5000})

        self.assertNotIn("maximum", schema["properties"]["offset"])
        self.assertEqual(result["structuredContent"]["windows"], [])

    @patch.object(server, "call_tool", return_value=server.text_result({"ok": True}))
    def test_dispatch_passes_host_thread_id_outside_model_arguments(self, call_tool) -> None:
        response = server.dispatch({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "claim_session_window",
                "arguments": {"window": "Editor"},
                "_meta": {"threadId": "thread-a"},
            },
        })

        self.assertNotIn("error", response)
        call_tool.assert_called_once_with(
            "claim_session_window",
            {"window": "Editor"},
            thread_id="thread-a",
        )

    def test_claim_capture_release_and_list_routes_owner_tokens_and_pages(self) -> None:
        with (
            patch.object(server, "capture_result", return_value={"isError": False}) as capture,
            patch.object(server.kwin, "resolve_window", return_value=WINDOW),
            patch.object(server.coordination, "claim_window", return_value={"claim_token": "new"}) as claim_window,
            patch.object(server.coordination, "require_claim_token", return_value="claim-token") as require_token,
            patch.object(server.coordination, "release_window_claim", return_value={"released": True}) as release,
            patch.object(server.coordination, "list_claims", return_value={"claims": []}) as list_claims,
        ):
            server.call_tool(
                "capture_plasma_window",
                {"window": "Editor", "claim_token": "claim-input"},
                thread_id="thread-a",
            )
            server.call_tool(
                "claim_session_window",
                {"window": "Editor", "lease_seconds": 45, "claim_token": "claim-input"},
                thread_id="thread-a",
            )
            server.call_tool("release_session_window", {"claim_token": "claim-input"}, thread_id="thread-a")
            server.call_tool("list_window_claims", {"offset": 2, "limit": 3}, thread_id="thread-a")

        capture.assert_called_once_with({"window": "Editor", "claim_token": "claim-input"}, "thread-a")
        claim_window.assert_called_once_with(
            server.coordination.window_for_model(WINDOW),
            "thread-a",
            45,
            "claim-input",
        )
        require_token.assert_called_once_with("claim-input")
        release.assert_called_once_with("claim-token", "thread-a")
        list_claims.assert_called_once_with("thread-a", 2, 3)

    def test_focus_routes_tokens_owner_validation_end_and_recovery(self) -> None:
        state = focus_state()
        with (
            patch.object(server.kwin, "file_guard", return_value=nullcontext()),
            patch.object(server.focus_lease, "load", return_value=state),
            patch.object(server.focus_lease, "require", return_value=state) as require,
            patch.object(server.focus_lease, "require_lease_token", side_effect=["validate", "end", "recover"]),
            patch.object(server.coordination, "current_process_identity", return_value={"pid": 42}),
            patch.object(server.focus_lease, "save"),
            patch.object(server.focus_lease, "validate", return_value={"valid": True}) as validate,
            patch.object(server.focus_lease, "restore", return_value={"restored": True}) as restore,
        ):
            server.call_tool(
                "validate_plasma_focus_lease",
                {"lease_token": "validate-input"},
                thread_id="thread-a",
            )
            server.call_tool(
                "end_plasma_focus_lease",
                {"lease_token": "end-input"},
                thread_id="thread-a",
            )
            server.call_tool("recover_plasma_focus_lease", {}, thread_id="thread-a")

        self.assertEqual(
            require.call_args_list,
            [
                call("validate", "thread-a"),
                call("end", "thread-a", allow_recovery=True),
                call("recover", "thread-a", allow_recovery=True),
            ],
        )
        validate.assert_called_once_with(state, "thread-a")
        self.assertEqual(restore.call_args_list, [call(state), call(state)])
