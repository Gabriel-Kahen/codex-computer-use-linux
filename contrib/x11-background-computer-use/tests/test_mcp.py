import json
import os
import subprocess
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class McpSmokeTests(TestCase):
    def test_manifest_and_launcher_are_valid(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], ROOT.name)
        mcp = json.loads((ROOT / manifest["mcpServers"]).read_text())
        launcher = ROOT / mcp["mcpServers"]["x11-same-session-computer-use"]["command"]
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))

    def test_initialize_list_and_ping(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        proc = subprocess.run(
            [str(ROOT / "bin/x11-same-session-computer-use-mcp")],
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = {response["id"]: response for response in map(json.loads, proc.stdout.splitlines())}
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "x11-same-session-computer-use")
        self.assertIn("AT-SPI", responses[1]["result"]["instructions"])
        tools = responses[2]["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(len(names), 14)
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(
            {"claim_session_window", "release_session_window", "list_window_claims"},
            set(names),
        )
        release_tool = next(
            tool for tool in tools if tool["name"] == "release_session_window"
        )
        self.assertEqual(
            release_tool["inputSchema"]["properties"]["claim_token"]["maxLength"],
            128,
        )
        self.assertEqual(responses[3]["result"], {})
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertLessEqual(set(tool["inputSchema"].get("required", [])), set(tool["inputSchema"]["properties"]))
            self.assertIn("annotations", tool)
