#!/usr/bin/env python3
"""Exercise stock Codex against local MCP tools and a loopback Responses fixture.

No credentials, hosted inference, desktop capture, or input injection are used.
"""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
METADATA = {"width": 1, "height": 1, "coordinate_scale": 1, "probe": "stock-codex-image"}


def fixture_result(structured=False):
    result = {"content": [{"type": "text", "text": json.dumps(METADATA)},
                        {"type": "image", "mimeType": "image/png", "data": PNG}],
            "isError": False}
    if structured:
        result["structuredContent"] = METADATA
    return result


def serve_fixture(metadata_log=None):
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": request["params"]["protocolVersion"],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "stock-codex-fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "image_probe", "description": "Return a synthetic pixel and metadata; no desktop access.",
                                 "inputSchema": {"type": "object", "properties": {"structured": {"type": "boolean"}}, "additionalProperties": False}}]}
        elif method == "tools/call" and request["params"]["name"] == "image_probe":
            if metadata_log:
                with metadata_log.open("a") as stream:
                    stream.write(json.dumps(request["params"].get("_meta", {})) + "\n")
            result = fixture_result(request["params"].get("arguments", {}).get("structured", False))
        elif method in ("resources/list", "resources/templates/list"):
            result = {"resources" if method == "resources/list" else "resourceTemplates": []}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


class Rpc:
    def __init__(self, command, env, cwd):
        self.stderr = tempfile.TemporaryFile(mode="w+")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, text=True, env=env, cwd=cwd)
        self.messages = queue.Queue()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                self.messages.put({"invalid": line})
        self.messages.put({"eof": True})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params, timeout=90):
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if message.get("eof") or "invalid" in message:
                raise RuntimeError(f"app-server closed or emitted invalid JSON: {message}")
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Unexpected server request in compatibility test"}})
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]
            if time.monotonic() >= deadline:
                raise TimeoutError(method)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.stderr.close()


def assert_image_content(result):
    if result.get("isError"):
        raise AssertionError(f"MCP probe failed: {result}")
    images = [item for item in result["content"] if item.get("type") == "image"]
    assert len(images) == 1 and images[0]["data"] == PNG, "MCP image content lost or corrupted"
    assert not result.get("structuredContent"), "Media responses must omit structuredContent for stock Codex"
    assert json.loads(result["content"][0]["text"]) == METADATA, "MCP metadata lost"


def assert_model_image(request):
    outputs = [item for item in request["input"] if item.get("type") == "function_call_output" and item.get("call_id") == "compat-image"]
    assert len(outputs) == 1, "Model request has no successful image tool output"
    output = outputs[0]["output"]
    assert isinstance(output, list), f"Image content replaced by text: {str(output)[:200]}"
    images = [item for item in output if item.get("type") == "input_image"]
    assert len(images) == 1 and images[0]["image_url"] == "data:image/png;base64," + PNG, f"Model-visible image lost or corrupted: {output}"
    texts = [item["text"] for item in output if item.get("type") == "input_text"]
    assert any(text.startswith("{") and json.loads(text) == METADATA for text in texts), "Image coordinate metadata missing"


class ResponsesFixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        # Codex may negotiate zstd request compression; require plain JSON here.
        request = json.loads(body)
        self.server.requests.append(request)
        count = len(self.server.requests)
        response_id = f"compat-{count}"
        events = [{"type": "response.created", "response": {"id": response_id}}]
        if count <= 2:
            events.append({"type": "response.output_item.done", "item": {
                "type": "function_call", "call_id": "compat-structured" if count == 1 else "compat-image", "namespace": "mcp__compat",
                "name": "image_probe", "arguments": json.dumps({"structured": count == 1})}})
        events.append({"type": "response.completed", "response": {"id": response_id,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})
        payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def sha256_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install_marketplace(codex, env, home, engine_binary):
    """Install all five actual manifests/launchers, using the release bundle layout."""
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    stage = home / "marketplace"
    (stage / ".agents/plugins").mkdir(parents=True)
    (stage / ".agents/plugins/marketplace.json").write_text(json.dumps(marketplace))
    assert len(marketplace["plugins"]) == 5, "Expected all five desktop plugins"
    expected_servers = set()
    versions = set()
    for entry in marketplace["plugins"]:
        relative = Path(entry["source"]["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        destination = stage / relative
        shutil.copytree(ROOT / relative, destination,
                        ignore=shutil.ignore_patterns("target", "node_modules", "__pycache__"))
        manifest = json.loads((destination / ".codex-plugin/plugin.json").read_text())
        assert manifest["name"] == entry["name"], "Marketplace/manifest name mismatch"
        versions.add(manifest["version"])
        mcp = json.loads((destination / manifest["mcpServers"]).read_text())
        for name, server in mcp["mcpServers"].items():
            assert os.access(destination / server["command"], os.X_OK), "Plugin launcher missing or not executable"
            expected_servers.add(name)
    assert len(versions) == 1, "Plugin release versions differ"
    target = {"x86_64": "x86_64", "aarch64": "aarch64"}[platform.machine()] + "-unknown-linux-gnu"
    prebuilt = stage / "computer-use-linux/prebuilt" / target
    prebuilt.mkdir(parents=True, exist_ok=True)
    checksums = []
    for name in ("computer-use-linux", "computer-use-linux-cosmic"):
        source = engine_binary if name == "computer-use-linux" else engine_binary.with_name(name)
        shutil.copy2(source, prebuilt / name)
        checksums.append(sha256_file(source) + "  " + name)
    (prebuilt / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    (prebuilt.parent / "RELEASE_BUNDLE").write_text(next(iter(versions)) + "\n")
    commands = [["plugin", "marketplace", "add", str(stage), "--json"]]
    commands.extend(["plugin", "add", entry["name"] + "@" + marketplace["name"], "--json"] for entry in marketplace["plugins"])
    for args in commands:
        subprocess.run([codex, *args], env=env, cwd=home, check=True, capture_output=True, text=True, timeout=60)
    return expected_servers


def run(codex, engine_binary, expected_version=None):
    version = subprocess.check_output([codex, "--version"], text=True).strip()
    expected = expected_version or json.loads((ROOT / "scripts/stock-codex/package.json").read_text())["dependencies"]["@openai/codex"]
    assert version == f"codex-cli {expected}", f"Expected pinned official Codex {expected}, got {version}"
    with tempfile.TemporaryDirectory(prefix="stock-codex-compat-") as directory:
        home = Path(directory)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ResponsesFixture)
        server.requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Deliberately avoid inheriting credentials, user config, and desktop session.
        env = {key: value for key, value in os.environ.items() if key in ("PATH", "LD_LIBRARY_PATH", "CARGO_HOME", "RUSTUP_HOME", "CODEX_COMPUTER_USE_LINUX_TARGET_DIR")}
        env.update(HOME=str(home), CODEX_HOME=str(home), XDG_CONFIG_HOME=str(home / "config"), XDG_CACHE_HOME=str(home / "cache"))
        config = f'''model = "gpt-5.1-codex"
model_provider = "compat"
approval_policy = "never"
sandbox_mode = "danger-full-access"
[model_providers.compat]
name = "Local deterministic compatibility fixture"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
[features]
code_mode = false
[mcp_servers.compat]
command = {json.dumps(sys.executable)}
args = [{json.dumps(str(Path(__file__).resolve()))}, "--fixture", "--metadata-log", {json.dumps(str(home / "tool-metadata.jsonl"))}]
'''
        (home / "config.toml").write_text(config)
        expected_servers = install_marketplace(codex, env, home, engine_binary.resolve())
        rpc = Rpc([codex, "app-server"], env, home)
        try:
            rpc.request("initialize", {"clientInfo": {"name": "stock_codex_compat", "version": "1"}, "capabilities": {"experimentalApi": True}})
            rpc.send({"method": "initialized", "params": {}})
            thread = rpc.request("thread/start", {"cwd": str(home), "ephemeral": True})["thread"]["id"]
            inventory = rpc.request("mcpServerStatus/list", {"threadId": thread}, timeout=150)["data"]
            names = {item["name"] for item in inventory}
            for name in expected_servers:
                matching = [item for item in inventory if item["name"] == name or item["name"].endswith("_" + name)]
                assert len(matching) == 1 and matching[0]["tools"], f"Installed plugin server {name} missing or failed: {names}"
            desktop = next(item for item in inventory if any(tool["name"] == "doctor" for tool in item["tools"].values()))
            assert any(tool["name"] == "doctor" for tool in desktop["tools"].values()), "Real engine doctor tool not discovered"
            doctor = rpc.request("mcpServer/tool/call", {"threadId": thread, "server": desktop["name"], "tool": "doctor", "arguments": {}})
            assert not doctor.get("isError"), "Real engine doctor returned a tool error"
            report = doctor.get("structuredContent") or json.loads(doctor["content"][0]["text"])
            assert all(key in report for key in ("platform", "readiness", "input")), "Malformed doctor report"
            probe = rpc.request("mcpServer/tool/call", {"threadId": thread, "server": "compat", "tool": "image_probe", "arguments": {}})
            assert_image_content(probe)
            rpc.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "Run the synthetic compatibility image probe."}]})
            deadline = time.monotonic() + 45
            while len(server.requests) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(server.requests) == 3, f"Expected three local model requests, got {len(server.requests)}"
            legacy = next(item["output"] for item in server.requests[1]["input"] if item.get("call_id") == "compat-structured" and item.get("type") == "function_call_output")
            legacy_images = isinstance(legacy, list) and any(item.get("type") == "input_image" for item in legacy)
            assert_model_image(server.requests[2])
            tool_metadata = [json.loads(line) for line in (home / "tool-metadata.jsonl").read_text().splitlines()]
            assert len(tool_metadata) == 3 and all(item.get("threadId") == thread for item in tool_metadata), f"Host thread ownership metadata missing: {tool_metadata}"
            return {"codex": version, "installed_plugins": 5, "engine_sha256": sha256_file(engine_binary), "thread_ownership_metadata": "passed", "engine_mcp_handshake": "passed", "doctor": "passed", "app_server_image": "passed", "model_visible_image_and_metadata": "passed", "hosted_inference": False, "desktop_mutation": False, "structured_media_support": "supported" if legacy_images else "requires content-only workaround"}
        except Exception:
            rpc.stderr.seek(0)
            print(rpc.stderr.read()[-4000:], file=sys.stderr)
            raise
        finally:
            rpc.close()
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--metadata-log", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-version", help="Exact resolved candidate version; defaults to the package.json pin")
    parser.add_argument("--engine-binary", type=Path, default=Path(os.environ.get("CODEX_COMPUTER_USE_LINUX_TARGET_DIR", str(Path.home() / ".cache/codex-computer-use-linux/target"))) / "release/computer-use-linux", help="Built engine; sibling cosmic helper is also required for the release bundle")
    args = parser.parse_args()
    if args.fixture:
        serve_fixture(args.metadata_log)
    else:
        print(json.dumps(run(args.codex, args.engine_binary, args.expected_version), indent=2))


if __name__ == "__main__":
    main()
