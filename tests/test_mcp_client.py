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

"""The HTTP and stdio transports of core.mcp_client against small fake MCP servers."""

import http.server
import json
import sys
import threading
import unittest

from harness2.core.mcp_client import (
    HttpMcpClient,
    McpClient,
    McpError,
    McpToolError,
    StdioMcpClient,
    client_from,
)

SESSION_ID = "session-abc"


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


class HttpClientTest(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHttpHandler)
        self.server.requests = []
        self.server.sse = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

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


FAKE_STDIO_SERVER = r"""
import json
import sys

mode = sys.argv[1]
out = sys.stdout


def send(message):
    out.write(json.dumps(message) + "\n")
    out.flush()


for line in sys.stdin:
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
    send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
    send({"jsonrpc": "2.0", "id": message["id"] + 100, "result": {"structuredContent": {}}})
    name = message["params"]["name"]
    if name == "fail":
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


if __name__ == "__main__":
    unittest.main()
