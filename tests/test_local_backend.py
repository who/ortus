"""``LocalRunner.run`` composes the MCP shim with a real child process.

The child is a stand-in for ``codex exec`` that reads the provider
``base_url`` from its own argv and sends one namespace-tool request there,
so these tests prove the wiring end to end: the override names the shim, the
server sees flat tools, the child sees restored names, and the shim is gone
once the child exits.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ortus.core.agent import LocalRunner
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.local_backend import LocalConfig
from tests.test_mcp_shim import (
    FLAT,
    NAMESPACE,
    SHIM_THREAD,
    TOOL,
    Upstream,
    call_item,
    namespace_tool,
    parse_sse,
    serving,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    with serving() as fake:
        yield fake


FAKE_CODEX = '''#!@PYTHON@
"""Stand-in for codex exec: one namespace-tool request to the provider."""
import http.client
import json
import os
import sys
from urllib.parse import urlsplit

argv = sys.argv[1:]
print(json.dumps({"argv": argv}), flush=True)
if os.environ.get("FAKE_CODEX_EXIT"):
    sys.exit(int(os.environ["FAKE_CODEX_EXIT"]))
prefix = "model_providers.ortus_local.base_url="
base_url = json.loads(next(a for a in argv if a.startswith(prefix))[len(prefix):])
parts = urlsplit(base_url)
tool = json.loads(\'\'\'@TOOL@\'\'\')
body = json.dumps({"model": "m", "stream": True, "input": [], "tools": [tool]})
connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
connection.request(
    "POST",
    parts.path + "/responses",
    body=body,
    headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
)
response = connection.getresponse()
print(json.dumps({"status": response.status, "body": response.read().decode()}), flush=True)
'''


def _fake_codex(tmp_path: Path) -> str:
    script = tmp_path / "codex"
    script.write_text(
        FAKE_CODEX.replace("@PYTHON@", sys.executable).replace(
            "@TOOL@", json.dumps(namespace_tool())
        )
    )
    script.chmod(0o755)
    return str(script)


def _records(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines() if line]


def _override(argv: list[str], key: str) -> str | None:
    prefix = f"model_providers.ortus_local.{key}="
    for value in argv:
        if value.startswith(prefix):
            return json.loads(value[len(prefix) :])
    return None


def _done_item(reply: dict[str, Any]) -> dict[str, Any]:
    events = parse_sse(reply["body"].encode())
    assert events[2]["type"] == "response.output_item.done"
    return events[2]["item"]


def _shim_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == SHIM_THREAD]


def test_local_worker_round_trips_through_the_shim(
    upstream: Upstream, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLAMA_API_KEY", "sk-live-secret")
    upstream.answer_stream(call_item())
    local = LocalConfig(upstream.base_url, "m", api_key_env="LLAMA_API_KEY")
    runner = LocalRunner(
        local, _fake_codex(tmp_path), codegraph=CodeGraphCapability("codegraph")
    )
    log = tmp_path / "worker.jsonl"
    assert runner.run("work", repo=tmp_path, log_path=log) == 0
    launch, reply = _records(log)
    argv = launch["argv"]
    # The override named the shim, not the server, and carried no key pair.
    base_url = _override(argv, "base_url")
    assert base_url != upstream.base_url
    assert base_url.startswith("http://127.0.0.1:")
    assert base_url.endswith("/v1")
    assert _override(argv, "env_key") is None
    assert "sk-live-secret" not in " ".join(argv)
    # The server saw flat tools and the key; the child saw the restored call.
    sent = upstream.last
    assert [tool["name"] for tool in sent.body["tools"]] == [FLAT]
    assert sent.headers["authorization"] == "Bearer sk-live-secret"
    assert reply["status"] == 200
    item = _done_item(reply)
    assert item["namespace"] == NAMESPACE
    assert item["name"] == TOOL
    # Gone with the child: no runner reference, no thread, no listener.
    assert runner.shim is None
    assert _shim_threads() == []
    port = int(base_url.removeprefix("http://127.0.0.1:").removesuffix("/v1"))
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_local_worker_without_codegraph_talks_to_the_server_directly(
    upstream: Upstream, tmp_path: Path
) -> None:
    upstream.answer_stream(call_item())
    local = LocalConfig(upstream.base_url, "m", api_key_env="LLAMA_API_KEY")
    runner = LocalRunner(local, _fake_codex(tmp_path))
    log = tmp_path / "worker.jsonl"
    assert runner.run("work", repo=tmp_path, log_path=log) == 0
    launch, reply = _records(log)
    assert _override(launch["argv"], "base_url") == upstream.base_url
    assert _override(launch["argv"], "env_key") == "LLAMA_API_KEY"
    # No shim in the path: the namespace entry reaches the server as sent,
    # and the flat name comes back as the server wrote it.
    assert upstream.last.body["tools"][0]["type"] == "namespace"
    assert "authorization" not in upstream.last.headers
    item = _done_item(reply)
    assert item["name"] == FLAT
    assert "namespace" not in item
    assert runner.shim is None
    assert _shim_threads() == []


def test_shim_stops_when_the_child_fails(upstream: Upstream, tmp_path: Path) -> None:
    runner = LocalRunner(
        LocalConfig(upstream.base_url, "m"),
        _fake_codex(tmp_path),
        codegraph=CodeGraphCapability("codegraph"),
        extra_env={"FAKE_CODEX_EXIT": "3"},
    )
    assert runner.run("work", repo=tmp_path, log_path=tmp_path / "worker.jsonl") == 3
    assert runner.shim is None
    assert _shim_threads() == []
    assert upstream.received == []
