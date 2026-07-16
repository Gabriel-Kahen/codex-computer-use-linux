import json
import os
import subprocess
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from same_session_computer_use import server


ROOT = Path(__file__).resolve().parents[1]


class RepositorySmokeTests(TestCase):
    def test_plugin_metadata_references_an_executable_launcher(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        mcp = json.loads((ROOT / manifest["mcpServers"]).read_text())
        command = mcp["mcpServers"]["same-session-computer-use"]["command"]
        launcher = ROOT / command

        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertEqual(manifest["version"], server.SERVER_INFO["version"])

    def test_mcp_initialize_tools_and_ping(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "unsupported-version"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        proc = subprocess.run(
            [str(ROOT / "bin/same-session-computer-use-mcp")],
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = {response["id"]: response for response in map(json.loads, proc.stdout.splitlines())}

        self.assertEqual(responses[1]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(responses[1]["result"]["serverInfo"]["version"], "0.2.0")
        self.assertIn("separate Computer Use plugin", responses[1]["result"]["instructions"])
        self.assertEqual(responses[3]["result"], {})
        tools = responses[2]["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(len(names), 14)
        self.assertEqual(len(names), len(set(names)))
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertLessEqual(set(schema.get("required", [])), set(schema.get("properties", {})))
            self.assertIn("annotations", tool)

        annotations = {tool["name"]: tool["annotations"] for tool in tools}
        self.assertTrue(annotations["capture_session_window"]["destructiveHint"])
        self.assertTrue(annotations["send_window_shortcut"]["destructiveHint"])
        self.assertTrue(annotations["send_window_shortcut"]["openWorldHint"])
        self.assertTrue(annotations["begin_coordinate_lease"]["destructiveHint"])
        self.assertFalse(annotations["begin_coordinate_lease"]["openWorldHint"])
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        for name in (
            "capture_session_window",
            "begin_coordinate_lease",
        ):
            self.assertIn("claim_token", schemas[name]["properties"])
        lease_seconds = schemas["claim_session_window"]["properties"]["lease_seconds"]
        self.assertEqual(
            (lease_seconds["default"], lease_seconds["minimum"], lease_seconds["maximum"]),
            (60, 5, 300),
        )
        self.assertEqual(
            schemas["release_session_window"]["required"], ["claim_token"]
        )

    def test_tools_call_uses_only_the_host_metadata_owner(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "claim_session_window",
                "arguments": {"window": "0x1", "_meta": {"threadId": "spoofed"}},
                "_meta": {"threadId": "trusted-owner"},
            },
        }
        with patch.object(server, "call_tool", return_value={"ok": True}) as call:
            response = server.dispatch(request)

        self.assertEqual(response["result"], {"ok": True})
        call.assert_called_once_with(
            "claim_session_window",
            {"window": "0x1", "_meta": {"threadId": "spoofed"}},
            "trusted-owner",
        )

    def test_claim_tool_rejects_argument_metadata_spoofing(self) -> None:
        response = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "claim_session_window",
                    "arguments": {"window": "0x1", "_meta": {"threadId": "spoofed"}},
                },
            }
        )

        self.assertIn("requires host-provided _meta.threadId", response["error"]["message"])
