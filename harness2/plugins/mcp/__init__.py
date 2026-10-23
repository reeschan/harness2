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

"""A minimal MCP client built only on the standard library.

An optional plugin: harness2's core, CLI and benchmarks do not import it. Code outside harness2
(for example a task connector that talks to an MCP server) imports it when it needs one.

Two transports share one surface (`McpClient`): `HttpMcpClient` speaks Streamable HTTP to a
server on the local machine only (127.0.0.1, localhost or ::1), and `StdioMcpClient` runs the
server as a child process and exchanges one JSON-RPC message per line. Both send `initialize`
and the `notifications/initialized` notification once, before the first `call_tool`.

The HTTP transport ignores proxy settings in the environment and does not follow redirects, so
a request never leaves the loopback address it was given; a redirect answer raises `McpError`.
Each client serializes its calls with a lock, so one client may be shared between threads: the
calls run one after another, each getting its own response.

`call_tool` returns the tool's structured result: `structuredContent` when it is a dict or a
list, or else the first text content parsed as a JSON object or array. A result marked `isError`
raises `McpToolError`; transport and protocol failures (no connection, not JSON, a JSON-RPC
error, a response id that does not match, a redirect, a timeout) raise `McpError`. `client_from`
picks the transport from either a url or a command.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

__all__ = [
    "CLOSE_WAIT_SECONDS",
    "LOCAL_HOSTS",
    "PROTOCOL_VERSION",
    "HttpMcpClient",
    "McpClient",
    "McpError",
    "McpToolError",
    "StdioMcpClient",
    "client_from",
    "tool_result",
]

PROTOCOL_VERSION = "2025-06-18"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
CLOSE_WAIT_SECONDS = 5.0


class McpError(RuntimeError):
    """A transport or protocol failure while talking to an MCP server."""


class McpToolError(McpError):
    """A tools/call result marked `isError: true`; the message is the content text."""


@runtime_checkable
class McpClient(Protocol):
    """Calls tools on one MCP server."""

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict | list: ...

    def close(self) -> None: ...


def _client_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - always present on supported Pythons
        return "0"
    try:
        return version("harness2")
    except PackageNotFoundError:
        return "0"


def _content_text(result: Mapping[str, Any]) -> list[str]:
    content = result.get("content")
    if not isinstance(content, list):
        return []
    return [
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]


def tool_result(result: Any) -> dict | list:
    """The structured result a tools/call result carries: a dict or a list.

    `structuredContent` is returned when it is a dict or a list; otherwise the first text content
    is parsed as JSON and returned when it is a dict or a list. A result marked `isError` raises
    `McpToolError`; anything else that carries no such value raises `McpError`.
    """
    if not isinstance(result, dict):
        raise McpError(f"tools/call result is not an object: {result!r}")
    if result.get("isError") is True:
        reason = "\n".join(_content_text(result)) or "tool call failed"
        raise McpToolError(reason)
    structured = result.get("structuredContent")
    if isinstance(structured, (dict, list)):
        return structured
    texts = _content_text(result)
    if texts:
        try:
            value = json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise McpError(f"tool result text is not JSON: {texts[0][:200]!r}") from exc
        if isinstance(value, (dict, list)):
            return value
    raise McpError("tool result has neither structuredContent nor a JSON object or array text")


def _response_result(message: Any, request_id: int) -> Any:
    if not isinstance(message, dict):
        raise McpError(f"response is not a JSON object: {message!r}")
    if message.get("id") != request_id:
        raise McpError(f"response id {message.get('id')!r} does not match request {request_id}")
    error = message.get("error")
    if error is not None:
        if isinstance(error, dict):
            raise McpError(f"JSON-RPC error {error.get('code')}: {error.get('message')}")
        raise McpError(f"JSON-RPC error: {error!r}")
    if "result" not in message:
        raise McpError("response has neither result nor error")
    return message["result"]


class _BaseClient:
    """The JSON-RPC sequencing both transports share.

    One lock per client makes `call_tool` (with the initialization it may do first) and `close`
    run one at a time, so threads sharing a client never interleave requests on the transport.
    """

    def __init__(self) -> None:
        self._next_id = 1
        self._initialized = False
        self._lock = threading.Lock()

    def _send_request(self, message: dict) -> Any:
        raise NotImplementedError

    def _send_notification(self, message: dict) -> None:
        raise NotImplementedError

    def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        return _response_result(self._send_request(message), request_id)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "harness2", "version": _client_version()},
            },
        )
        self._send_notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict | list:
        with self._lock:
            self._ensure_initialized()
            result = self._request("tools/call", {"name": name, "arguments": dict(arguments)})
        return tool_result(result)

    def _close(self) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            self._close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _parse_event_stream(body: str, request_id: int) -> Any:
    """The first message with `request_id` among the `data:` events of an SSE body."""
    events: list[list[str]] = [[]]
    for line in body.splitlines():
        if not line.strip():
            events.append([])
        elif line.startswith("data:"):
            data = line[5:]
            events[-1].append(data[1:] if data.startswith(" ") else data)
    for lines in events:
        if not lines:
            continue
        try:
            message = json.loads("\n".join(lines))
        except json.JSONDecodeError as exc:
            raise McpError(f"event-stream data is not JSON: {lines[0][:200]!r}") from exc
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise McpError(f"event stream has no response for request {request_id}")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so the 3xx answer surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_opener() -> urllib.request.OpenerDirector:
    """An opener that ignores proxy settings in the environment and follows no redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


class HttpMcpClient(_BaseClient):
    """An MCP client over Streamable HTTP to a server on this machine.

    Requests go straight to the given loopback url: proxy settings are ignored and a redirect
    answer raises `McpError` without being followed.
    """

    def __init__(self, url: str, *, timeout: float = 60) -> None:
        super().__init__()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "http":
            raise ValueError(f"MCP url must use http: {url!r}")
        if parts.hostname not in LOCAL_HOSTS:
            raise ValueError(f"MCP url host must be one of {LOCAL_HOSTS}: {url!r}")
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self._opener = _local_opener()

    def _post(self, message: dict):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        data = json.dumps(message).encode("utf-8")
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            if 300 <= exc.code < 400:
                raise McpError(
                    f"MCP server answered a redirect (HTTP {exc.code}) to "
                    f"{message.get('method')}; redirects are not followed"
                )
            raise McpError(f"MCP server answered HTTP {exc.code} to {message.get('method')}")
        except (urllib.error.URLError, OSError) as exc:
            raise McpError(f"cannot reach MCP server at {self.url}: {exc}") from exc
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self.session_id = session_id
        return response

    def _send_request(self, message: dict) -> Any:
        with self._post(message) as response:
            content_type = response.headers.get("Content-Type", "")
            try:
                body = response.read().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise McpError(f"cannot read MCP response: {exc}") from exc
        if content_type.split(";")[0].strip().lower() == "text/event-stream":
            return _parse_event_stream(body, message["id"])
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise McpError(f"MCP response is not JSON: {body[:200]!r}") from exc

    def _send_notification(self, message: dict) -> None:
        response = self._post(message)
        response.close()
        if response.status not in (200, 202):
            raise McpError(f"MCP server answered HTTP {response.status} to a notification")


class StdioMcpClient(_BaseClient):
    """An MCP client that runs the server as a child process speaking line-delimited JSON."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 60,
    ) -> None:
        super().__init__()
        self.timeout = timeout
        self._buffer = b""
        try:
            self._proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                env=None if env is None else dict(env),
            )
        except OSError as exc:
            raise McpError(f"cannot start MCP server {list(argv)!r}: {exc}") from exc
        self._closed = False

    def _write(self, message: dict) -> None:
        if self._closed:
            raise McpError("MCP client is closed")
        line = json.dumps(message).encode("utf-8") + b"\n"
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise McpError(f"MCP server stdin is closed: {exc}") from exc

    def _readline(self, deadline: float) -> bytes:
        assert self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise McpError(f"MCP server did not answer within {self.timeout} seconds")
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise McpError("MCP server exited before answering")
                self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    def _send_request(self, message: dict) -> Any:
        self._write(message)
        deadline = time.monotonic() + self.timeout
        while True:
            line = self._readline(deadline).strip()
            if not line:
                continue
            try:
                reply = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise McpError(f"MCP server wrote a line that is not JSON: {line[:200]!r}") from exc
            if isinstance(reply, dict) and reply.get("id") == message["id"]:
                return reply

    def _send_notification(self, message: dict) -> None:
        self._write(message)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=CLOSE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=CLOSE_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def client_from(
    *,
    url: str | None = None,
    command: Sequence[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> McpClient:
    """An HTTP client for `url` or a stdio client for `command`; exactly one must be given."""
    if (url is None) == (command is None):
        raise ValueError("give exactly one of url and command")
    if url is not None:
        return HttpMcpClient(url, timeout=timeout)
    assert command is not None
    if not command:
        raise ValueError("command must not be empty")
    return StdioMcpClient(command, cwd=cwd, env=env, timeout=timeout)
