import json
import os
import subprocess
from pathlib import Path
from unittest import TestCase

from support import ROOT


class McpSmokeTests(TestCase):
    def test_manifest_references_executable_launcher(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], ROOT.name)
        mcp = json.loads((ROOT / manifest["mcpServers"]).read_text())
        launcher = ROOT / mcp["mcpServers"]["plasma-same-session-computer-use"]["command"]
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))

    def test_initialize_tools_and_ping(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ]
        proc = subprocess.run(
            [str(ROOT / "bin/plasma-same-session-computer-use-mcp")],
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = {response["id"]: response for response in map(json.loads, proc.stdout.splitlines())}
        self.assertEqual(responses[1]["result"]["serverInfo"], {"name": "plasma-same-session-computer-use", "version": "0.1.0"})
        self.assertEqual(responses[3]["result"], {})
        tools = responses[2]["result"]["tools"]
        self.assertEqual(len(tools), 7)
        self.assertEqual(len(tools), len({tool["name"] for tool in tools}))
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "plasma_session_status",
                "list_plasma_windows",
                "capture_plasma_window",
                "begin_plasma_focus_lease",
                "validate_plasma_focus_lease",
                "end_plasma_focus_lease",
                "recover_plasma_focus_lease",
            },
        )
        self.assertIn("cannot gate", responses[1]["result"]["instructions"])
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertLessEqual(set(tool["inputSchema"].get("required", [])), set(tool["inputSchema"].get("properties", {})))
            self.assertIn("annotations", tool)
