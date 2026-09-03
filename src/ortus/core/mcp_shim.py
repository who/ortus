"""Loopback shim that flattens Responses namespace tools for the local backend.

codex 0.147.0 advertises every MCP server to the model as one Responses tool
of ``type = "namespace"`` whose ``tools[]`` nest the server's functions. The
OpenAI-compatible servers Ortus targets (llama-server, Ollama) accept that
entry with HTTP 200 and silently drop it from the rendered prompt, so the
model never sees CodeGraph and codex observes no tool call. The same servers
handle a plain function named ``<namespace>__<tool>`` correctly, and echo the
name back exactly.

The shim sits on 127.0.0.1 between codex and the server. On the request leg
it flattens each namespace entry into plain functions carrying that joined
name; on the response leg it splits a returned flat name back into the
``namespace`` plus ``name`` pair codex routes on, for streaming SSE and plain
JSON bodies alike. Everything else, on both legs, passes through untouched.
It starts whenever the local backend launches a worker, with or without
CodeGraph, and it stops when that worker exits.

The request leg also normalizes two shapes llama-server cannot execute.
codex opens every turn with a ``developer`` message in ``input[]`` after
``instructions``; llama-server renders that role as a second system message,
and a chat template that requires the system message first raises, so the
server answers 500 before the model runs. Such messages, and ``system`` ones,
become ``user`` messages in place, with ``instructions`` left as the one
system message. A chunked request body is decoded here and forwarded with an
accurate ``Content-Length``, so it never reaches the server as zero bytes.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from ortus.core.output import warn

#: Joins a namespace and a tool name into the flat name the server sees.
SEPARATOR = "__"
#: The path prefix the shim serves; codex appends ``/responses`` to it.
SHIM_PATH_PREFIX = "/v1"
#: The SSE events whose ``item`` is a function call codex acts on. Every
#: other event line is forwarded byte-for-byte.
RESTORED_EVENTS = frozenset({"response.output_item.added", "response.output_item.done"})
#: ``input[]`` message roles the server renders as a second system message.
#: Each becomes ``user``; ``instructions`` stays the single system message.
DEMOTED_ROLES = frozenset({"developer", "system"})
#: Longest chunk-size line or trailer accepted while decoding a chunked body.
_MAX_LINE = 8192

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
#: Request headers the shim owns on the upstream leg.
_REQUEST_HEADERS_DROPPED = _HOP_BY_HOP | {"host", "content-length", "accept-encoding"}


# --- rewriters ---------------------------------------------------------------


def flatten_tools(tools: Any) -> tuple[Any, tuple[str, ...]]:
    """Expand each namespace entry into ``<namespace>__<tool>`` functions.

    Returns the new list and the names of the namespaces expanded, in order.
    A list without namespace entries comes back as the same object, so the
    caller can tell nothing changed and forward the original bytes.
    """
    if not isinstance(tools, list):
        return tools, ()
    namespaces: list[str] = []
    flat: list[Any] = []
    taken = {
        entry.get("name")
        for entry in tools
        if isinstance(entry, dict) and entry.get("type") == "function"
    }
    for entry in tools:
        if not (
            isinstance(entry, dict)
            and entry.get("type") == "namespace"
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("tools"), list)
        ):
            flat.append(entry)
            continue
        namespace = entry["name"]
        namespaces.append(namespace)
        for tool in entry["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                flat.append(tool)
                continue
            name = f"{namespace}{SEPARATOR}{tool['name']}"
            if name in taken:
                # Both are forwarded; which one the server keeps is its call,
                # and codex's router answers for whatever comes back.
                warn(f"mcp shim: flattened tool {name} collides with an existing tool")
            taken.add(name)
            flat.append({**tool, "type": "function", "name": name})
    if not namespaces:
        return tools, ()
    return flat, tuple(namespaces)


def _flatten_input(items: Any) -> tuple[Any, bool]:
    """Rewrite the ``input[]`` items the server cannot take as codex sends them.

    Replayed ``function_call`` history items carry the ``namespace`` plus
    ``name`` shape the response leg restored; the server only ever knew the
    flat name. Messages in a demoted role become ``user`` messages in place,
    text and order untouched. Every other item passes through as the same
    object, so a list that needs nothing comes back as the same list.
    """
    if not isinstance(items, list):
        return items, False
    changed = False
    out: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        # A Responses input item without ``type`` is a message.
        kind = item.get("type", "message")
        if (
            kind == "function_call"
            and isinstance(item.get("namespace"), str)
            and isinstance(item.get("name"), str)
        ):
            rest = {key: value for key, value in item.items() if key != "namespace"}
            rest["name"] = f"{item['namespace']}{SEPARATOR}{item['name']}"
            out.append(rest)
            changed = True
        elif (
            kind == "message"
            and isinstance(item.get("role"), str)
            and item["role"] in DEMOTED_ROLES
        ):
            out.append({**item, "role": "user"})
            changed = True
        else:
            out.append(item)
    return (out if changed else items), changed


def flatten_request(body: Any) -> tuple[Any, tuple[str, ...]]:
    """Rewrite one Responses request body; the same object back when untouched."""
    if not isinstance(body, dict):
        return body, ()
    tools, namespaces = flatten_tools(body.get("tools"))
    items, items_changed = _flatten_input(body.get("input"))
    if not namespaces and not items_changed:
        return body, ()
    rewritten = dict(body)
    if namespaces:
        rewritten["tools"] = tools
    if items_changed:
        rewritten["input"] = items
    return rewritten, namespaces


def restore_item(item: Any, namespaces: tuple[str, ...]) -> dict[str, Any] | None:
    """Split a flat ``function_call`` name back into namespace plus name.

    Only names under a namespace this request flattened are restored; any
    other name passes through, so a model inventing a tool still meets
    codex's normal unsupported-call error. Returns None when untouched.
    """
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return None
    name = item.get("name")
    if not isinstance(name, str):
        return None
    for namespace in sorted(namespaces, key=len, reverse=True):
        prefix = namespace + SEPARATOR
        if name.startswith(prefix) and len(name) > len(prefix):
            restored = dict(item)
            restored["namespace"] = namespace
            restored["name"] = name[len(prefix) :]
            return restored
    return None


def restore_event_line(line: bytes, namespaces: tuple[str, ...]) -> bytes:
    """Rewrite one SSE ``data:`` line when it carries a restorable item."""
    if not namespaces or not line.startswith(b"data:"):
        return line
    try:
        event = json.loads(line[len(b"data:") :])
    except ValueError:
        return line
    if not isinstance(event, dict) or event.get("type") not in RESTORED_EVENTS:
        return line
    restored = restore_item(event.get("item"), namespaces)
    if restored is None:
        return line
    ending = line[len(line.rstrip(b"\r\n")) :]
    payload = json.dumps({**event, "item": restored}, separators=(",", ":"))
    return b"data: " + payload.encode() + ending


def restore_response(body: Any, namespaces: tuple[str, ...]) -> Any:
    """Rewrite the ``function_call`` items of a non-streaming ``output[]``."""
    if not namespaces or not isinstance(body, dict):
        return body
    output = body.get("output")
    if not isinstance(output, list):
        return body
    changed = False
    items: list[Any] = []
    for item in output:
        restored = restore_item(item, namespaces)
        items.append(item if restored is None else restored)
        changed = changed or restored is not None
    return {**body, "output": items} if changed else body


# --- forwarder ---------------------------------------------------------------


@dataclass(frozen=True)
class _Upstream:
    """The configured server, split once so every request can dial it."""

    scheme: str
    host: str
    port: int
    path: str

    @classmethod
    def from_base_url(cls, base_url: str) -> _Upstream:
        parts = urlsplit(base_url)
        secure = parts.scheme == "https"
        return cls(
            scheme=parts.scheme,
            host=parts.hostname or "127.0.0.1",
            port=parts.port or (443 if secure else 80),
            path=parts.path.rstrip("/"),
        )

    def connect(self) -> HTTPConnection:
        connection = HTTPSConnection if self.scheme == "https" else HTTPConnection
        return connection(self.host, self.port)

    def path_for(self, request_path: str) -> str:
        """Map the shim's ``/v1`` onto the configured base path."""
        prefix = SHIM_PATH_PREFIX
        if request_path == prefix or request_path.startswith(
            (prefix + "/", prefix + "?")
        ):
            return self.path + request_path[len(prefix) :]
        return request_path


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _ShimServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """The worker log is the record; access lines would only add noise."""

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        try:
            raw = self._read_body()
        except ValueError as exc:
            self._send_bad_request(str(exc))
            return
        body, namespaces = _rewrite_request_bytes(raw)
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _REQUEST_HEADERS_DROPPED
        }
        # Compressed bodies cannot be rewritten line by line.
        headers["Accept-Encoding"] = "identity"
        headers["Content-Length"] = str(len(body))
        api_key_env = self.server.api_key_env
        if api_key_env is not None and not any(
            name.lower() == "authorization" for name in headers
        ):
            # The key rides the upstream leg only; codex never held it.
            key = os.environ.get(api_key_env)
            if key:
                headers["Authorization"] = f"Bearer {key}"
        upstream = self.server.upstream
        connection = upstream.connect()
        try:
            connection.request(
                self.command, upstream.path_for(self.path), body=body, headers=headers
            )
            response = connection.getresponse()
        except OSError as exc:
            connection.close()
            self._send_gateway_error(exc)
            return
        try:
            self._relay(response, namespaces)
        finally:
            connection.close()

    def _read_body(self) -> bytes:
        """The request body with any chunked framing removed.

        codex sends ``Content-Length`` today, but a chunked body read by its
        declared length is zero bytes, and forwarding that would hand the
        server an empty request. Decoding here keeps the upstream leg on an
        accurate ``Content-Length`` whichever framing the client chose.
        """
        if _is_chunked(self.headers.get("Transfer-Encoding")):
            return _decode_chunked(self.rfile)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _relay(self, response: HTTPResponse, namespaces: tuple[str, ...]) -> None:
        content_type = response.getheader("Content-Type") or ""
        passthrough = [
            (name, value)
            for name, value in response.getheaders()
            if name.lower() not in _HOP_BY_HOP and name.lower() != "content-length"
        ]
        if content_type.startswith("text/event-stream"):
            # Unbuffered: each upstream line goes out as its own chunk, so a
            # slow decode still reaches codex event by event.
            self.send_response(response.status, response.reason)
            for name, value in passthrough:
                self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                line = response.readline()
                if not line:
                    break
                self._write_chunk(restore_event_line(line, namespaces))
            self._write_chunk(b"")
            return
        data = response.read()
        if namespaces and content_type.startswith("application/json"):
            try:
                parsed = json.loads(data)
            except ValueError:
                pass
            else:
                restored = restore_response(parsed, namespaces)
                if restored is not parsed:
                    data = json.dumps(restored).encode()
        self.send_response(response.status, response.reason)
        for name, value in passthrough:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _send_gateway_error(self, exc: OSError) -> None:
        """Report an unreachable server as a 502 naming it; never mask it."""
        upstream = self.server.upstream
        self._send_json_error(
            502,
            "upstream_unreachable",
            f"ortus mcp shim: {upstream.scheme}://{upstream.host}:{upstream.port}"
            f" unreachable: {exc}",
        )

    def _send_bad_request(self, detail: str) -> None:
        """Refuse a body the shim could not decode.

        Forwarding whatever did decode would hand the server a truncated
        request with a length that vouches for it; a 400 that names the
        defect is the honest answer.
        """
        self._send_json_error(400, "malformed_request", f"ortus mcp shim: {detail}")

    def _send_json_error(self, status: int, kind: str, message: str) -> None:
        data = json.dumps({"error": {"type": kind, "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


def _is_chunked(transfer_encoding: str | None) -> bool:
    """Whether the body carries chunked framing; ``chunked`` is always last."""
    if not transfer_encoding:
        return False
    codings = [part.strip().lower() for part in transfer_encoding.split(",")]
    return codings[-1] == "chunked"


def _read_line(stream: BinaryIO) -> bytes:
    """One line of chunk framing without its ending; EOF or overlong raises."""
    line = stream.readline(_MAX_LINE + 1)
    if len(line) > _MAX_LINE:
        raise ValueError("chunked request body: framing line too long")
    if not line.endswith(b"\n"):
        raise ValueError("chunked request body ended before its final chunk")
    return line.rstrip(b"\r\n")


def _decode_chunked(stream: BinaryIO) -> bytes:
    """Strip chunked framing: size lines, data, and a terminating zero chunk.

    Chunk extensions and trailers are read and dropped. A body that ends
    mid-stream or carries an unreadable size raises ``ValueError`` so the
    caller refuses it instead of forwarding a short body under an accurate
    length.
    """
    chunks: list[bytes] = []
    while True:
        size_line = _read_line(stream)
        try:
            size = int(size_line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise ValueError("chunked request body: unreadable chunk size") from None
        if size < 0:
            raise ValueError("chunked request body: negative chunk size")
        if size == 0:
            break
        data = stream.read(size)
        if len(data) != size:
            raise ValueError("chunked request body ended inside a chunk")
        if _read_line(stream) != b"":
            raise ValueError("chunked request body: chunk data not followed by CRLF")
        chunks.append(data)
    while _read_line(stream) != b"":
        pass  # trailers: nothing in them reaches the upstream leg
    return b"".join(chunks)


def _rewrite_request_bytes(raw: bytes) -> tuple[bytes, tuple[str, ...]]:
    """Rewrite a JSON request body; one that needs no rewrite is forwarded as is."""
    if not raw:
        return raw, ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw, ()
    body, namespaces = flatten_request(parsed)
    if body is parsed:
        return raw, ()
    return json.dumps(body).encode(), namespaces


class _ShimServer(ThreadingHTTPServer):
    daemon_threads = True
    # A handler mid-stream when the worker dies must not hold up teardown.
    block_on_close = False

    def __init__(self, upstream: _Upstream, api_key_env: str | None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.upstream = upstream
        self.api_key_env = api_key_env

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The worker hung up mid-stream (killed, reaped, or timed out); the
        # write failing is the shim noticing, not something to print.
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


class McpShim:
    """A running shim: its loopback address, and how to stop it."""

    def __init__(
        self, upstream_base_url: str, server: _ShimServer, thread: threading.Thread
    ) -> None:
        self.upstream_base_url = upstream_base_url
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        """The provider ``base_url`` codex is pointed at while the shim runs."""
        return f"http://127.0.0.1:{self.port}{SHIM_PATH_PREFIX}"

    def close(self) -> None:
        """Stop accepting, release the port, and join the serving thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> McpShim:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def start_shim(upstream_base_url: str, *, api_key_env: str | None = None) -> McpShim:
    """Serve on an ephemeral loopback port, forwarding to ``upstream_base_url``.

    ``api_key_env`` names the variable whose value, when set, becomes the
    ``Authorization`` header on the upstream leg. The shim reads it per
    request and never records it.
    """
    server = _ShimServer(_Upstream.from_base_url(upstream_base_url), api_key_env)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        name="ortus-mcp-shim",
        daemon=True,
    )
    thread.start()
    return McpShim(upstream_base_url, server, thread)
