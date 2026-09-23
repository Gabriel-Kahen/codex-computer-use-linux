#!/usr/bin/env python3
"""Contract and safety smoke test for the computer-use-linux MCP surface."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import select
import subprocess
import sys
from typing import Any


EXPECTED_TOOLS = {
    "doctor",
    "setup_accessibility",
    "setup_window_targeting",
    "list_apps",
    "get_app_state",
    "list_windows",
    "focused_window",
    "claim_window",
    "list_window_claims",
    "renew_window_claim",
    "release_window_claim",
    "screenshot",
    "activate_window",
    "move_window",
    "resize_window",
    "click",
    "drag",
    "scroll",
    "press_key",
    "type_text",
    "run_action_batch",
    "run_action_batch_and_observe",
    "perform_action",
    "set_value",
}

INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"you\s+are\s+now\s+a",
        r"your\s+new\s+(task|role|instructions?)\s+(is|are)",
        r"system\s*:",
        r"<\s*(system|human|assistant|user)\s*>",
        r"do\s+not\s+(tell|inform|mention|reveal)",
        r"(curl|wget|fetch)\s+https?://",
        r"base64\.(b64decode|decodebytes)",
        r"\b(exec|eval)\s*\(",
    ]
]

DANGEROUS_TOOL_NAMES = {
    "exec",
    "eval",
    "shell",
    "run_command",
    "terminal",
    "read_file",
    "write_file",
    "delete_file",
}

FOCUS_SELECTORS = {
    "window_id",
    "pid",
    "app_id",
    "wm_class",
    "title",
    "tty",
    "terminal_pid",
    "terminal_command",
    "terminal_cwd",
}

SEMANTIC_SELECTORS = {
    "element_index",
    "role",
    "name",
    "text",
    "states",
}

OBJECT_REF_SELECTORS = SEMANTIC_SELECTORS | {"element_identifier"}

READ_ONLY_TOOLS = {
    "doctor",
    "list_apps",
    "get_app_state",
    "list_windows",
    "focused_window",
    "list_window_claims",
}

DESTRUCTIVE_MUTATING_TOOLS = {
    "click",
    "drag",
    "press_key",
    "type_text",
    "run_action_batch",
    "run_action_batch_and_observe",
    "perform_action",
    "set_value",
}

NON_DESTRUCTIVE_MUTATING_TOOLS = EXPECTED_TOOLS - READ_ONLY_TOOLS - DESTRUCTIVE_MUTATING_TOOLS

IDEMPOTENT_TOOLS = READ_ONLY_TOOLS | {
    "setup_accessibility",
    "setup_window_targeting",
    "activate_window",
    "move_window",
    "resize_window",
    "release_window_claim",
}

OPEN_WORLD_TOOLS = EXPECTED_TOOLS - {
    "doctor",
    "setup_accessibility",
    "setup_window_targeting",
    "claim_window",
    "list_window_claims",
    "renew_window_claim",
    "release_window_claim",
}


class McpClient:
    def __init__(self, binary: pathlib.Path):
        self.process = subprocess.Popen(
            [str(binary), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 1

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self.next_id,
            "method": method,
        }
        self.next_id += 1
        if params is not None:
            message["params"] = params
        self._write(message)
        return self._read_response(message["id"])

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _write(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read_response(self, request_id: int) -> dict[str, Any]:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], 5)
        if not ready:
            stderr = self._stderr_tail()
            raise AssertionError(f"timed out waiting for MCP response {request_id}; stderr={stderr!r}")
        line = self.process.stdout.readline()
        if not line:
            stderr = self._stderr_tail()
            raise AssertionError(f"MCP server closed stdout; stderr={stderr!r}")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise AssertionError(f"expected response id {request_id}, got {response!r}")
        if "error" in response:
            raise AssertionError(f"MCP request {request_id} failed: {response['error']!r}")
        return response

    def _stderr_tail(self) -> str:
        if self.process.stderr is None:
            return ""
        ready, _, _ = select.select([self.process.stderr], [], [], 0)
        if not ready:
            return ""
        return self.process.stderr.read()[-2000:]


def package_version(repo: pathlib.Path) -> str:
    cargo = (repo / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.MULTILINE)
    if not match:
        raise AssertionError("Cargo.toml does not contain a package version")
    return match.group(1)


def assert_no_injection_text(label: str, text: str) -> None:
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            raise AssertionError(f"{label} contains suspicious MCP prompt text matching {pattern.pattern!r}")


def schema_properties(tool: dict[str, Any]) -> set[str]:
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise AssertionError(f"{tool.get('name')} inputSchema.properties is not an object")
    return set(properties)


def assert_tool_annotations(tool: dict[str, Any]) -> None:
    name = tool["name"]
    annotations = tool.get("annotations")
    if not isinstance(annotations, dict):
        raise AssertionError(f"{name} is missing MCP tool annotations")

    expected = {
        "readOnlyHint": name in READ_ONLY_TOOLS,
        "destructiveHint": name in DESTRUCTIVE_MUTATING_TOOLS,
        "idempotentHint": name in IDEMPOTENT_TOOLS,
        "openWorldHint": name in OPEN_WORLD_TOOLS,
    }
    for key, value in expected.items():
        if annotations.get(key) is not value:
            raise AssertionError(
                f"{name} annotation {key}={annotations.get(key)!r}, expected {value!r}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default="target/debug/computer-use-linux")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    binary = pathlib.Path(args.binary).resolve()
    if not binary.exists():
        raise AssertionError(f"binary does not exist: {binary}")

    version = package_version(repo)
    annotation_partition = READ_ONLY_TOOLS | NON_DESTRUCTIVE_MUTATING_TOOLS | DESTRUCTIVE_MUTATING_TOOLS
    if annotation_partition != EXPECTED_TOOLS:
        raise AssertionError(
            "tool annotation classes do not cover the expected MCP tool set: "
            f"missing={EXPECTED_TOOLS - annotation_partition}, extra={annotation_partition - EXPECTED_TOOLS}"
        )

    client = McpClient(binary)
    try:
        initialize = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "computer-use-linux-ci", "version": "0"},
            },
        )["result"]
        client.notify("notifications/initialized", {})

        server_info = initialize.get("serverInfo") or {}
        if server_info.get("name") != "computer-use-linux":
            raise AssertionError(f"unexpected server name: {server_info!r}")
        if server_info.get("version") != version:
            raise AssertionError(f"MCP server version {server_info.get('version')!r} != Cargo version {version!r}")

        capabilities = initialize.get("capabilities") or {}
        if set(capabilities) != {"tools"}:
            raise AssertionError(f"unexpected MCP capabilities: {capabilities!r}")

        instructions = initialize.get("instructions") or ""
        assert_no_injection_text("server instructions", instructions)
        for required in [
            "Begin every turn that uses Computer Use by calling get_app_state",
            "Use list_windows/focused_window before targeted keyboard input",
            "exhaust list_window_claims pages before sustained exact-window work",
            "Tools with readOnlyHint=false may mutate local desktop or application state",
            "refuse targeted input if focus cannot be verified",
        ]:
            if required not in instructions:
                raise AssertionError(f"server instructions are missing safety guidance: {required!r}")

        tools = client.request("tools/list", {})["result"].get("tools") or []
        names = {tool.get("name") for tool in tools}
        if names != EXPECTED_TOOLS:
            raise AssertionError(f"unexpected tools: missing={EXPECTED_TOOLS - names}, extra={names - EXPECTED_TOOLS}")

        for tool in tools:
            name = tool["name"]
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise AssertionError(f"tool name is not provider-safe snake_case: {name!r}")
            if name in DANGEROUS_TOOL_NAMES:
                raise AssertionError(f"unexpected dangerous tool name exposed: {name}")
            description = tool.get("description") or ""
            assert_no_injection_text(f"{name} description", description)
            assert_tool_annotations(tool)
            props = schema_properties(tool)
            if "env" in props or "shell" in props or "command" in props:
                raise AssertionError(f"{name} exposes a raw process-control parameter: {sorted(props)}")
            if name in {"press_key", "type_text", "activate_window"} and not FOCUS_SELECTORS <= props:
                raise AssertionError(f"{name} is missing focus target selectors: {sorted(FOCUS_SELECTORS - props)}")
            if name == "click" and not SEMANTIC_SELECTORS <= props:
                raise AssertionError(f"{name} is missing semantic element selectors: {sorted(SEMANTIC_SELECTORS - props)}")
            if name == "run_action_batch" and props != {"window_id", "actions", "owner_thread_id", "claim_token"}:
                raise AssertionError(f"{name} exposes unexpected parameters: {sorted(props)}")
            if name == "claim_window" and props != {"window_id", "lease_seconds"}:
                raise AssertionError(f"{name} exposes unexpected parameters: {sorted(props)}")
            if name == "list_window_claims" and props != {"cursor"}:
                raise AssertionError(f"{name} exposes unexpected parameters: {sorted(props)}")
            if name in {"renew_window_claim", "release_window_claim"} and "owner_thread_id" in props:
                raise AssertionError(f"{name} must derive its owner from request metadata")
            if name == "release_window_claim" and props != {"claim_token"}:
                raise AssertionError(f"{name} exposes unexpected parameters: {sorted(props)}")
            schema_props = (tool.get("inputSchema") or {}).get("properties") or {}
            if name in {"claim_window", "renew_window_claim"}:
                lease = schema_props.get("lease_seconds") or {}
                if lease.get("minimum") != 5 or lease.get("maximum") != 300:
                    raise AssertionError(f"{name} must publish the 5..300 second lease bound")
            if name in {"renew_window_claim", "release_window_claim"}:
                token = schema_props.get("claim_token") or {}
                if token.get("minLength") != 1 or token.get("maxLength") != 256:
                    raise AssertionError(f"{name} must publish the 1..256 token length bound")
            if name == "list_window_claims":
                cursor = schema_props.get("cursor") or {}
                if cursor.get("minLength") != 64 or cursor.get("maxLength") != 64:
                    raise AssertionError(f"{name} must publish the exact 64-character cursor bound")
                output_schema = json.dumps(tool.get("outputSchema") or {}, sort_keys=True)
                if "claim_token" in output_schema:
                    raise AssertionError(f"{name} output schema exposes claim tokens")
                for field in [
                    "window_id",
                    "stable_window_id",
                    "owned_by_caller",
                    "owned_by_caller_on_page",
                    "next_action",
                ]:
                    if field not in output_schema:
                        raise AssertionError(f"{name} output schema is missing {field!r}")
            if name in {"claim_window", "renew_window_claim", "release_window_claim"}:
                output_schema = json.dumps(tool.get("outputSchema") or {}, sort_keys=True)
                for field in ["claim_token", "expires_at_ms", "next_action"]:
                    if field not in output_schema:
                        raise AssertionError(f"{name} output schema is missing {field!r}")
            if name == "run_action_batch_and_observe" and props != {
                "window_id",
                "actions",
                "observation",
                "owner_thread_id",
                "claim_token",
            }:
                raise AssertionError(f"{name} exposes unexpected parameters: {sorted(props)}")
            if name in {"perform_action", "set_value"} and not OBJECT_REF_SELECTORS <= props:
                raise AssertionError(f"{name} is missing object/semantic element selectors: {sorted(OBJECT_REF_SELECTORS - props)}")

        invalid_batch = client.request(
            "tools/call",
            {
                "name": "run_action_batch",
                "arguments": {
                    "window_id": 0,
                    "actions": [{"type": "press_key", "key": "Enter"}],
                },
            },
        )["result"]
        invalid_batch_content = invalid_batch.get("content") or []
        invalid_batch_output = json.loads(invalid_batch_content[0].get("text") or "{}")
        if (
            invalid_batch_output.get("ok") is not False
            or invalid_batch_output.get("completed") != 0
            or invalid_batch_output.get("results") != []
        ):
            raise AssertionError(f"invalid action batch was not rejected before execution: {invalid_batch!r}")

        doctor = client.request("tools/call", {"name": "doctor", "arguments": {}})["result"]
        content = doctor.get("content") or []
        if not content or content[0].get("type") != "text":
            raise AssertionError(f"doctor did not return text content: {doctor!r}")
        report = json.loads(content[0].get("text") or "{}")
        for section in ["platform", "accessibility", "windowing", "input", "portals", "readiness"]:
            if section not in report:
                raise AssertionError(f"doctor report missing {section!r}: {report.keys()}")
        claim_readiness = (report.get("readiness") or {}).get("window_claims") or {}
        if claim_readiness.get("owner_source") != "host_task_metadata":
            raise AssertionError(f"doctor claim readiness has the wrong owner source: {claim_readiness!r}")
        if "supports_shared_lifecycle" not in claim_readiness:
            raise AssertionError(f"doctor claim readiness is missing backend support: {claim_readiness!r}")
        if (
            claim_readiness.get("min_lease_seconds"),
            claim_readiness.get("default_lease_seconds"),
            claim_readiness.get("max_lease_seconds"),
        ) != (5, 60, 300):
            raise AssertionError(f"doctor claim readiness has incorrect lease bounds: {claim_readiness!r}")
    finally:
        client.close()

    print(f"MCP safety check passed: {len(EXPECTED_TOOLS)} tools, version {version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"mcp_safety_check.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
