# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The HTTP and stdio transports of the harness2.plugins.mcp plugin against small fake servers."""

import ast
import http.server
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from harness2.plugins.mcp import (
    HttpMcpClient,
    McpClient,
    McpError,
    McpToolError,
    StdioMcpClient,
    client_from,
)

SESSION_ID = "session-abc"
ROOT = Path(__file__).resolve().parents[1]


def _tool_result(name, arguments):
    """The (result, error) a fake server gives for one tools/call."""
    if name == "echo":
        payload = {"echo": arguments}
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload,
            "isError": False,
        }, None
    if name == "text_only":
        return {"content": [{"type": "text", "text": json.dumps({"from": "text"})}]}, None
    if name == "fail":
        return {
            "content": [{"type": "text", "text": "task PBI-9 is already claimed"}],
            "isError": True,
        }, None
    if name == "rpc_error":
        return None, {"code": -32601, "message": "method not found"}
    raise AssertionError(name)


class FakeHttpHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length))
        headers = {key.lower(): value for key, value in self.headers.items()}
        self.server.requests.append((message.get("method"), headers))
        if self.server.redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if "id" not in message:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if message["method"] == "initialize":
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
            }
        else:
            with self.server.state_lock:
                self.server.active += 1
                self.server.max_active = max(self.server.max_active, self.server.active)
            time.sleep(self.server.delay)
            with self.server.state_lock:
                self.server.active -= 1
            params = message["params"]
            result, error = _tool_result(params["name"], params["arguments"])
            reply = {"jsonrpc": "2.0", "id": message["id"]}
            if error is not None:
                reply["error"] = error
            else:
                reply["result"] = result
        if self.server.sse and message["method"] == "tools/call":
            other = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            body = (
                f"event: message\ndata: {json.dumps(other)}\n\n"
                f"event: message\ndata: {json.dumps(reply)}\n\n"
            ).encode("utf-8")
            content_type = "text/event-stream"
        else:
            body = json.dumps(reply).encode("utf-8")
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if message["method"] == "initialize":
            self.send_header("Mcp-Session-Id", SESSION_ID)
        self.end_headers()
        self.wfile.write(body)


def _start_server():
    """A fake MCP server on 127.0.0.1 and the thread serving it."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHttpHandler)
    server.requests = []
    server.sse = False
    server.redirect_to = None
    server.delay = 0.0
    server.state_lock = threading.Lock()
    server.active = 0
    server.max_active = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _call_from_two_threads(client):
    """Call echo from two threads released together; the results and errors by thread."""
    barrier = threading.Barrier(2)
    results, errors = {}, {}

    def run(n):
        barrier.wait()
        try:
            results[n] = client.call_tool("echo", {"thread": n})
        except Exception as exc:
            errors[n] = exc

    threads = [threading.Thread(target=run, args=(n,)) for n in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


class HttpClientTest(unittest.TestCase):
    def setUp(self):
        self.server, self.thread = _start_server()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def tearDown(self):
        _stop_server(self.server, self.thread)

    def methods(self):
        return [method for method, _ in self.server.requests]

    def test_initialize_is_sent_once(self):
        with HttpMcpClient(self.url, timeout=5) as client:
            self.assertIsInstance(client, McpClient)
            client.call_tool("echo", {"n": 1})
            client.call_tool("echo", {"n": 2})
        self.assertEqual(
            self.methods(),
            ["initialize", "notifications/initialized", "tools/call", "tools/call"],
        )

    def test_structured_content_is_returned(self):
        with HttpMcpClient(self.url, timeout=5) as client:
            self.assertEqual(client.call_tool("echo", {"repo": "/r"}), {"echo": {"repo": "/r"}})

    def test_text_json_without_structured_content(self):
        with HttpMcpClient(self.url, timeout=5) as client:
            self.assertEqual(client.call_tool("text_only", {}), {"from": "text"})

    def test_is_error_raises_tool_error(self):
        with HttpMcpClient(self.url, timeout=5) as client:
            with self.assertRaises(McpToolError) as caught:
                client.call_tool("fail", {})
        self.assertIn("already claimed", str(caught.exception))

    def test_json_rpc_error_raises_mcp_error(self):
        with HttpMcpClient(self.url, timeout=5) as client:
            with self.assertRaises(McpError) as caught:
                client.call_tool("rpc_error", {})
        self.assertNotIsInstance(caught.exception, McpToolError)
        self.assertIn("method not found", str(caught.exception))

    def test_session_id_is_sent_on_later_requests(self):
        with HttpMcpClient(self.url, timeout=5) as client:
            client.call_tool("echo", {})
        sessions = [headers.get("mcp-session-id") for _, headers in self.server.requests]
        self.assertEqual(sessions, [None, SESSION_ID, SESSION_ID])

    def test_accept_and_content_type_headers(self):
        with HttpMcpClient(self.url, timeout=5) as client:
            client.call_tool("echo", {})
        for _, headers in self.server.requests:
            self.assertEqual(headers.get("content-type"), "application/json")
            self.assertEqual(headers.get("accept"), "application/json, text/event-stream")

    def test_event_stream_response(self):
        self.server.sse = True
        with HttpMcpClient(self.url, timeout=5) as client:
            self.assertEqual(client.call_tool("echo", {"a": 1}), {"echo": {"a": 1}})

    def test_unreachable_server_raises_mcp_error(self):
        port = self.server.server_address[1]
        self.server.shutdown()
        self.server.server_close()
        with HttpMcpClient(f"http://127.0.0.1:{port}/mcp", timeout=2) as client:
            with self.assertRaises(McpError):
                client.call_tool("echo", {})

    def test_non_local_url_is_rejected(self):
        for url in (
            "http://example.com:7830/mcp",
            "http://10.0.0.1/mcp",
            "https://127.0.0.1/mcp",
            "ftp://localhost/mcp",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    HttpMcpClient(url)
        for url in ("http://localhost:7830/mcp", "http://[::1]:7830/mcp"):
            HttpMcpClient(url)

    def test_proxy_environment_is_ignored(self):
        dead_proxy = f"http://127.0.0.1:{_unused_port()}"
        env = {key: value for key, value in os.environ.items()}
        for key in ("NO_PROXY", "no_proxy", "REQUEST_METHOD"):
            env.pop(key, None)
        env["HTTP_PROXY"] = dead_proxy
        env["http_proxy"] = dead_proxy
        with mock.patch.dict(os.environ, env, clear=True):
            # The default opener would send this request to the dead proxy.
            with self.assertRaises(urllib.error.URLError) as caught:
                urllib.request.build_opener().open(self.url, timeout=2)
            self.assertNotIsInstance(caught.exception, urllib.error.HTTPError)
            with HttpMcpClient(self.url, timeout=5) as client:
                self.assertEqual(client.call_tool("echo", {"p": 1}), {"echo": {"p": 1}})
        self.assertIn("tools/call", self.methods())

    def test_redirect_raises_mcp_error_and_is_not_followed(self):
        target, target_thread = _start_server()
        try:
            self.server.redirect_to = f"http://127.0.0.1:{target.server_address[1]}/mcp"
            with HttpMcpClient(self.url, timeout=5) as client:
                with self.assertRaises(McpError) as caught:
                    client.call_tool("echo", {})
            self.assertIn("redirect", str(caught.exception))
            self.assertEqual(self.methods(), ["initialize"])
            self.assertEqual(target.requests, [])
        finally:
            _stop_server(target, target_thread)

    def test_concurrent_calls_do_not_overlap(self):
        self.server.delay = 0.3
        with HttpMcpClient(self.url, timeout=10) as client:
            results, errors = _call_from_two_threads(client)
        self.assertEqual(errors, {})
        self.assertEqual(results, {0: {"echo": {"thread": 0}}, 1: {"echo": {"thread": 1}}})
        self.assertEqual(self.server.max_active, 1)
        self.assertEqual(
            self.methods(),
            ["initialize", "notifications/initialized", "tools/call", "tools/call"],
        )


FAKE_STDIO_SERVER = r"""
import json
import os
import select
import sys
import time

mode = sys.argv[1]
out = sys.stdout
fd = sys.stdin.fileno()
buffer = b""


def send(message):
    out.write(json.dumps(message) + "\n")
    out.flush()


def read_line():
    global buffer
    while b"\n" not in buffer:
        chunk = os.read(fd, 65536)
        if not chunk:
            return None
        buffer += chunk
    line, _, buffer = buffer.partition(b"\n")
    return line


def pending():
    return b"\n" in buffer or bool(select.select([fd], [], [], 0)[0])


while True:
    line = read_line()
    if line is None:
        break
    if not line.strip():
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-06-18"}})
        continue
    if mode == "exit":
        sys.exit(0)
    if mode == "hang":
        continue
    overlap = False
    if mode == "slow":
        time.sleep(0.3)
        overlap = pending()
    send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
    send({"jsonrpc": "2.0", "id": message["id"] + 100, "result": {"structuredContent": {}}})
    name = message["params"]["name"]
    if overlap:
        result = {"content": [{"type": "text", "text": "overlapping request"}], "isError": True}
    elif name == "fail":
        result = {"content": [{"type": "text", "text": "report is unknown"}], "isError": True}
    else:
        payload = {"echo": message["params"]["arguments"], "id": message["id"]}
        result = {"content": [{"type": "text", "text": json.dumps(payload)}],
                  "structuredContent": payload, "isError": False}
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
"""


def _stdio(mode, timeout=5):
    return StdioMcpClient([sys.executable, "-c", FAKE_STDIO_SERVER, mode], timeout=timeout)


class StdioClientTest(unittest.TestCase):
    def test_skips_notifications_and_other_ids(self):
        with _stdio("normal") as client:
            self.assertIsInstance(client, McpClient)
            self.assertEqual(client.call_tool("echo", {"x": 1}), {"echo": {"x": 1}, "id": 2})
            self.assertEqual(client.call_tool("echo", {"x": 2}), {"echo": {"x": 2}, "id": 3})

    def test_is_error_raises_tool_error(self):
        with _stdio("normal") as client:
            with self.assertRaises(McpToolError) as caught:
                client.call_tool("fail", {})
        self.assertIn("report is unknown", str(caught.exception))

    def test_child_exit_raises_mcp_error(self):
        with _stdio("exit") as client:
            with self.assertRaises(McpError):
                client.call_tool("echo", {})

    def test_timeout_raises_mcp_error(self):
        with _stdio("hang", timeout=0.5) as client:
            with self.assertRaises(McpError) as caught:
                client.call_tool("echo", {})
        self.assertIn("did not answer", str(caught.exception))

    def test_close_leaves_no_child(self):
        client = _stdio("normal")
        client.call_tool("echo", {})
        proc = client._proc
        client.close()
        self.assertIsNotNone(proc.poll())
        client.close()
        with self.assertRaises(McpError):
            client.call_tool("echo", {})

    def test_concurrent_calls_do_not_overlap(self):
        with _stdio("slow", timeout=10) as client:
            results, errors = _call_from_two_threads(client)
        self.assertEqual(errors, {})
        self.assertEqual(sorted(results), [0, 1])
        for n, result in results.items():
            self.assertEqual(result["echo"], {"thread": n})
        self.assertEqual(sorted(result["id"] for result in results.values()), [2, 3])


class ClientFromTest(unittest.TestCase):
    def test_needs_exactly_one_transport(self):
        with self.assertRaises(ValueError):
            client_from(url="http://127.0.0.1:7830/mcp", command=[sys.executable])
        with self.assertRaises(ValueError):
            client_from()

    def test_picks_transport(self):
        self.assertIsInstance(client_from(url="http://127.0.0.1:7830/mcp"), HttpMcpClient)
        with client_from(command=[sys.executable, "-c", FAKE_STDIO_SERVER, "normal"]) as client:
            self.assertIsInstance(client, StdioMcpClient)


def _imported_modules(path, package):
    """The absolute names a module imports, including string arguments naming a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("harness2.plugins"):
                names.append(node.value)
    return names


class PluginIsOptionalTest(unittest.TestCase):
    def test_core_cli_and_benchmarks_do_not_import_plugins(self):
        offenders = []
        checked = 0
        for top in ("core", "cli", "benchmarks"):
            for path in sorted((ROOT / "harness2" / top).rglob("*.py")):
                package = ".".join(path.relative_to(ROOT).parent.parts)
                checked += 1
                for name in _imported_modules(path, package):
                    if name == "harness2.plugins" or name.startswith("harness2.plugins."):
                        offenders.append(f"{path.relative_to(ROOT)}: {name}")
        self.assertGreater(checked, 0)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
