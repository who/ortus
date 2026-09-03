"""The MCP namespace shim between codex and a local Responses server.

Request leg: namespace tools flatten to ``<namespace>__<tool>`` functions.
Response leg: flat names under a flattened namespace come back as
``namespace`` plus ``name``. Everything else is byte-for-byte passthrough.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from ortus.core.mcp_shim import (
    SEPARATOR,
    McpShim,
    flatten_request,
    flatten_tools,
    restore_event_line,
    start_shim,
)

NAMESPACE = "mcp__codegraph"
TOOL = "codegraph_explore"
FLAT = f"{NAMESPACE}{SEPARATOR}{TOOL}"
SHIM_THREAD = "ortus-mcp-shim"


# --- wire shapes -------------------------------------------------------------


def function_tool(name: str = "exec_command") -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": f"{name} description",
        "strict": False,
        "parameters": {"type": "object", "properties": {}},
    }


def namespace_tool(namespace: str = NAMESPACE, *tools: str) -> dict[str, Any]:
    """The entry codex 0.147.0 sends for one MCP server."""
    return {
        "type": "namespace",
        "name": namespace,
        "description": "server instructions",
        "tools": [
            {
                "type": "function",
                "name": name,
                "description": f"{name} description",
                "strict": False,
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
            for name in (tools or (TOOL,))
        ],
    }


def call_item(name: str = FLAT) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": name,
        "arguments": '{"query": "grind"}',
        "status": "completed",
    }


def message_item(text: str = "done") -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def request_body(
    *tools: dict[str, Any], input_items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if input_items is None:
        input_items = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ]
    return {
        "model": "m",
        "stream": True,
        "instructions": "be brief",
        "input": input_items,
        "tools": list(tools),
        "tool_choice": "auto",
    }


def sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()
        for name, payload in events
    )


def stream_with(*items: dict[str, Any]) -> bytes:
    """The stream a server sends for ``items``: created, added/done each, completed."""
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "response.created",
            {
                "type": "response.created",
                "response": {"id": "resp_1", "status": "in_progress", "output": []},
            },
        )
    ]
    for index, item in enumerate(items):
        events.append(
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**item, "status": "in_progress"},
                },
            )
        )
        events.append(
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": item,
                },
            )
        )
    events.append(
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": list(items),
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            },
        )
    )
    return sse(*events)


def parse_sse(raw: bytes) -> list[dict[str, Any]]:
    return [
        json.loads(line[len(b"data:") :])
        for line in raw.splitlines()
        if line.startswith(b"data:")
    ]


# --- a recording fake of the local server -----------------------------------


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    raw: bytes

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.raw)


@dataclass
class Upstream:
    """Records every request and answers each with one canned body."""

    port: int = 0
    status: int = 200
    content_type: str = "text/event-stream"
    body: bytes = b""
    chunked: bool = False
    received: list[Received] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def last(self) -> Received:
        return self.received[-1]

    def answer_stream(self, *items: dict[str, Any], chunked: bool = False) -> None:
        self.content_type = "text/event-stream"
        self.body = stream_with(*items)
        self.chunked = chunked

    def answer_json(self, payload: dict[str, Any]) -> None:
        self.content_type = "application/json"
        self.body = json.dumps(payload).encode()
        self.chunked = False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Keep the test log free of access lines."""

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        upstream = self.server.upstream
        upstream.received.append(
            Received(
                self.path,
                {name.lower(): value for name, value in self.headers.items()},
                raw,
            )
        )
        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.content_type)
        self.send_header("Connection", "close")
        if upstream.chunked:
            # One chunk per line, the way a server streams a slow decode.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for line in upstream.body.splitlines(keepends=True):
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(upstream.body)))
            self.end_headers()
            self.wfile.write(upstream.body)
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, upstream: Upstream) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.upstream = upstream


@contextmanager
def serving() -> Iterator[Upstream]:
    """A live fake server on an ephemeral loopback port."""
    fake = Upstream()
    server = _Server(fake)
    fake.port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    with serving() as fake:
        yield fake


@pytest.fixture
def shim(upstream: Upstream) -> Iterator[McpShim]:
    with start_shim(upstream.base_url) as running:
        yield running


# --- the codex side of the shim ---------------------------------------------


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    raw: bytes

    @property
    def events(self) -> list[dict[str, Any]]:
        return parse_sse(self.raw)

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.raw)


def send(
    shim: McpShim,
    body: bytes | dict[str, Any],
    *,
    method: str = "POST",
    path: str = "/v1/responses",
    headers: dict[str, str] | None = None,
) -> Reply:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    connection = http.client.HTTPConnection("127.0.0.1", shim.port, timeout=10)
    connection.request(
        method,
        path,
        body=data if method == "POST" else None,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **(headers or {}),
        },
    )
    response = connection.getresponse()
    reply = Reply(
        response.status,
        {name.lower(): value for name, value in response.getheaders()},
        response.read(),
    )
    connection.close()
    return reply


def raw_exchange(port: int, body: bytes) -> tuple[bytes, list[bytes]]:
    """POST over a bare socket and return the head plus the decoded chunks."""
    request = (
        b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        pieces = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            pieces.append(data)
    head, _, payload = b"".join(pieces).partition(b"\r\n\r\n")
    chunks: list[bytes] = []
    while payload:
        size_line, _, rest = payload.partition(b"\r\n")
        size = int(size_line, 16)
        if size == 0:
            break
        chunks.append(rest[:size])
        payload = rest[size + 2 :]
    return head, chunks


def _shim_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == SHIM_THREAD]


# --- request leg -------------------------------------------------------------


def test_flatten_tools_flattens_namespace_tools_in_order() -> None:
    plain = function_tool()
    tools, namespaces = flatten_tools(
        [
            plain,
            namespace_tool(),
            namespace_tool("multi_agent_v1", "spawn_agent", "wait_agent"),
        ]
    )
    assert namespaces == (NAMESPACE, "multi_agent_v1")
    assert [tool["name"] for tool in tools] == [
        "exec_command",
        FLAT,
        "multi_agent_v1__spawn_agent",
        "multi_agent_v1__wait_agent",
    ]
    assert all(tool["type"] == "function" for tool in tools)
    assert tools[0] is plain
    # Description, strictness, and the schema ride along untouched.
    assert tools[1] == {**namespace_tool()["tools"][0], "name": FLAT}
    # Nothing to flatten: the same objects come back, so bytes can be reused.
    untouched = [plain]
    assert flatten_tools(untouched)[0] is untouched
    body = request_body(plain)
    assert flatten_request(body) == (body, ())
    assert flatten_request(body)[0] is body


def test_shim_flattens_namespace_tools_on_the_upstream_leg(
    upstream: Upstream, shim: McpShim
) -> None:
    upstream.answer_stream(message_item())
    reply = send(
        shim,
        request_body(
            function_tool(),
            namespace_tool(),
            namespace_tool("multi_agent_v1", "spawn_agent"),
        ),
    )
    assert reply.status == 200
    sent = upstream.last
    assert sent.path == "/v1/responses"
    tools = sent.body["tools"]
    assert [tool["type"] for tool in tools] == ["function"] * 3
    assert [tool["name"] for tool in tools] == [
        "exec_command",
        FLAT,
        "multi_agent_v1__spawn_agent",
    ]
    assert tools[1]["parameters"] == namespace_tool()["tools"][0]["parameters"]
    # The shim owns the transport headers and forwards the rest.
    assert sent.headers["accept-encoding"] == "identity"
    assert sent.headers["content-length"] == str(len(sent.raw))
    assert sent.headers["accept"] == "text/event-stream"
    assert sent.body["input"] == request_body()["input"]
    assert sent.body["instructions"] == "be brief"


def test_shim_flattens_replayed_namespaced_calls(
    upstream: Upstream, shim: McpShim
) -> None:
    """codex replays a restored call as namespace plus name on the next turn."""
    upstream.answer_stream(message_item())
    replayed = {
        "type": "function_call",
        "namespace": NAMESPACE,
        "name": TOOL,
        "call_id": "call_1",
        "arguments": "{}",
    }
    output = {"type": "function_call_output", "call_id": "call_1", "output": "ok"}
    send(shim, request_body(namespace_tool(), input_items=[replayed, output]))
    sent = upstream.last.body["input"]
    assert sent[0] == {
        "type": "function_call",
        "name": FLAT,
        "call_id": "call_1",
        "arguments": "{}",
    }
    assert sent[1] == output


def test_flattened_name_collision_is_warned_and_forwarded(
    upstream: Upstream, shim: McpShim, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream.answer_stream(message_item())
    send(shim, request_body(function_tool(FLAT), namespace_tool()))
    assert [tool["name"] for tool in upstream.last.body["tools"]] == [FLAT, FLAT]
    assert "collides" in capsys.readouterr().err


def test_shim_passes_requests_without_namespaces_byte_identical(
    upstream: Upstream, shim: McpShim
) -> None:
    upstream.answer_stream(message_item())
    raw = (
        b'{"model": "m",\n  "stream": true, "input": [], "tools": ['
        + json.dumps(function_tool()).encode()
        + b'] , "note": "\xc3\xa9"}'
    )
    reply = send(shim, raw)
    assert upstream.last.raw == raw
    assert reply.status == 200
    assert reply.raw == upstream.body
    assert reply.headers["content-type"] == "text/event-stream"
    # A body the shim cannot parse is forwarded as is, too.
    reply = send(shim, b"not json")
    assert upstream.last.raw == b"not json"
    assert reply.status == 200


# --- response leg ------------------------------------------------------------


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_shim_restores_function_call_names_when_streaming(
    upstream: Upstream, shim: McpShim, chunked: bool
) -> None:
    upstream.answer_stream(call_item(), chunked=chunked)
    reply = send(shim, request_body(namespace_tool()))
    assert reply.status == 200
    assert reply.headers["content-type"] == "text/event-stream"
    assert reply.headers["transfer-encoding"] == "chunked"
    events = reply.events
    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.output_item.done",
        "response.completed",
    ]
    for event in events[1:3]:
        item = event["item"]
        assert item["namespace"] == NAMESPACE
        assert item["name"] == TOOL
        assert item["call_id"] == "call_1"
        assert item["arguments"] == '{"query": "grind"}'
    # Only the two item payloads change; every other line, including the
    # completed event that still carries the flat name, is forwarded as is.
    theirs = upstream.body.splitlines(keepends=True)
    ours = reply.raw.splitlines(keepends=True)
    assert len(ours) == len(theirs)
    changed = [i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b]
    assert changed == [4, 7]
    assert events[3]["response"]["output"][0]["name"] == FLAT


def test_shim_restores_function_call_names_without_streaming(
    upstream: Upstream, shim: McpShim
) -> None:
    upstream.answer_json(
        {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [call_item(), message_item()],
        }
    )
    reply = send(shim, request_body(namespace_tool()))
    assert reply.status == 200
    assert reply.headers["content-type"] == "application/json"
    assert reply.headers["content-length"] == str(len(reply.raw))
    output = reply.json["output"]
    assert output[0] == {**call_item(), "namespace": NAMESPACE, "name": TOOL}
    assert output[1] == message_item()


def test_shim_restores_only_names_under_a_flattened_namespace(
    upstream: Upstream, shim: McpShim
) -> None:
    upstream.answer_stream(
        call_item(FLAT),
        call_item("exec_command"),
        call_item("mcp__other__explore"),
        call_item(f"{NAMESPACE}{SEPARATOR}"),
    )
    events = send(shim, request_body(namespace_tool())).events
    done = [e["item"] for e in events if e["type"] == "response.output_item.done"]
    assert [(item.get("namespace"), item["name"]) for item in done] == [
        (NAMESPACE, TOOL),
        (None, "exec_command"),
        (None, "mcp__other__explore"),
        (None, f"{NAMESPACE}{SEPARATOR}"),
    ]
    # A request that flattened nothing restores nothing, even a matching name.
    upstream.answer_stream(call_item(FLAT))
    events = send(shim, request_body(function_tool())).events
    assert "namespace" not in events[2]["item"]
    assert events[2]["item"]["name"] == FLAT


def test_shim_streams_each_line_as_its_own_chunk(
    upstream: Upstream, shim: McpShim
) -> None:
    upstream.answer_stream(call_item(), chunked=True)
    head, chunks = raw_exchange(
        shim.port, json.dumps(request_body(namespace_tool())).encode()
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert b"transfer-encoding: chunked" in head.lower()
    assert chunks == [
        restore_event_line(line, (NAMESPACE,))
        for line in upstream.body.splitlines(keepends=True)
    ]


# --- forwarding --------------------------------------------------------------


def test_shim_attaches_the_named_key_upstream_only(
    upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream.answer_stream(message_item())
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    with start_shim(upstream.base_url, api_key_env="LLAMA_API_KEY") as shim:
        send(shim, request_body(namespace_tool()))
        assert "authorization" not in upstream.last.headers
        monkeypatch.setenv("LLAMA_API_KEY", "sk-live-secret")
        send(shim, request_body(namespace_tool()))
        assert upstream.last.headers["authorization"] == "Bearer sk-live-secret"
        # A caller that authenticates itself is left alone.
        send(shim, request_body(), headers={"Authorization": "Bearer theirs"})
        assert upstream.last.headers["authorization"] == "Bearer theirs"


def test_shim_maps_v1_onto_the_configured_base_path(upstream: Upstream) -> None:
    upstream.answer_json({"ok": True})
    with start_shim(f"http://127.0.0.1:{upstream.port}/api") as shim:
        assert shim.base_url == f"http://127.0.0.1:{shim.port}/v1"
        send(shim, request_body())
        assert upstream.last.path == "/api/responses"
        send(shim, b"", method="GET", path="/v1/models?x=1")
        assert upstream.last.path == "/api/models?x=1"
        send(shim, b"", method="GET", path="/props")
        assert upstream.last.path == "/props"


def test_shim_reports_an_unreachable_server_as_502() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with start_shim(f"http://127.0.0.1:{port}/v1") as shim:
        reply = send(shim, request_body(namespace_tool()))
    assert reply.status == 502
    error = reply.json["error"]
    assert error["type"] == "upstream_unreachable"
    assert f"127.0.0.1:{port}" in error["message"]
    assert "unreachable" in error["message"]


def test_shim_close_releases_the_port_and_thread(upstream: Upstream) -> None:
    shim = start_shim(upstream.base_url)
    port = shim.port
    assert len(_shim_threads()) == 1
    shim.close()
    assert _shim_threads() == []
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
