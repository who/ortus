"""Tests for core/local_backend.py — the `[local]` table and `LocalConfig`."""

from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ortus.core.config import Config
from ortus.core.local_backend import (
    DEFAULT_LOCAL_BASE_URL,
    LOCAL_TABLE_BACKENDS,
    MIN_RECOMMENDED_CONTEXT,
    OPENCODE_BINARY,
    OPENCODE_CONFIG_FILE,
    OPENCODE_INSTALL_DIR,
    OPENCODE_PROVIDER_ID,
    OPENCODE_PROVIDER_NPM,
    OPENCODE_SCHEMA_URL,
    LocalConfig,
    LocalServerError,
    OpenCodeBinaryError,
    list_served_models,
    load_local_config,
    opencode_provider_block,
    parse_local_table,
    probe_context_size,
    probe_models,
    resolve_opencode_binary,
    serving_hint,
)
from ortus.core.profiles import SUPPORTED_EFFORTS, ProfileError


def test_constants_pin_the_serving_contract() -> None:
    assert DEFAULT_LOCAL_BASE_URL == "http://127.0.0.1:8080/v1"
    assert MIN_RECOMMENDED_CONTEXT == 32768
    assert OPENCODE_PROVIDER_ID == "ortuslocal"
    assert OPENCODE_PROVIDER_NPM == "@ai-sdk/openai-compatible"
    assert OPENCODE_CONFIG_FILE == "opencode.json"
    assert OPENCODE_SCHEMA_URL == "https://opencode.ai/config.json"
    assert LOCAL_TABLE_BACKENDS == ("local", "opencode")


def test_origin_strips_v1() -> None:
    local = LocalConfig("http://127.0.0.1:8080/v1", "m")
    assert local.origin == "http://127.0.0.1:8080"
    bare = LocalConfig("http://gpu-box:11434", "m")
    assert bare.origin == "http://gpu-box:11434"


def test_display_has_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLAMA_API_KEY", "sk-secret-value")
    local = LocalConfig(
        "http://127.0.0.1:8080/v1", "qwen3:4b", api_key_env="LLAMA_API_KEY"
    )
    assert local.display == "local (127.0.0.1:8080) model=qwen3:4b"
    assert "sk-secret-value" not in local.display
    assert "sk-secret-value" not in repr(local)


def test_local_config_is_immutable() -> None:
    local = LocalConfig(DEFAULT_LOCAL_BASE_URL, "m")
    with pytest.raises(FrozenInstanceError):
        local.model = "other"  # type: ignore[misc]


# --- the opencode executable -------------------------------------------------
#
# The installer puts opencode in `~/.opencode/bin`, off a non-login PATH, so
# the resolver looks there after PATH and nowhere else. Every test fakes the
# home directory: the developer's own install must not answer for it.


def _install(home: Path, *, mode: int = 0o755) -> Path:
    """A stand-in opencode where the installer puts it."""
    binary = home / OPENCODE_INSTALL_DIR / OPENCODE_BINARY
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(mode)
    return binary


def _path_holds(monkeypatch: pytest.MonkeyPatch, **found: str) -> list[str]:
    """`shutil.which` answering from `found` only; returns the names it was asked."""
    asked: list[str] = []

    def which(name: str, *args: object, **kwargs: object) -> str | None:
        asked.append(name)
        return found.get(name)

    monkeypatch.setattr(shutil, "which", which)
    return asked


def test_opencode_install_dir_is_the_installer_default() -> None:
    assert OPENCODE_BINARY == "opencode"
    assert OPENCODE_INSTALL_DIR == Path(".opencode") / "bin"
    assert issubclass(OpenCodeBinaryError, RuntimeError)


def test_resolve_opencode_binary_path_wins_over_the_install_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An on-PATH opencode is the answer even with an install beside it."""
    home = tmp_path / "home"
    _install(home)
    asked = _path_holds(monkeypatch, opencode="/usr/bin/opencode")
    assert resolve_opencode_binary(home=home) == Path("/usr/bin/opencode")
    assert asked == ["opencode"]


def test_resolve_opencode_binary_returns_the_real_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative PATH entry or a symlink resolves to the executable's real path."""
    real = tmp_path / "tools" / "opencode-real"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    (tmp_path / "tools" / "opencode").symlink_to(real)
    monkeypatch.chdir(tmp_path)
    _path_holds(monkeypatch, opencode="tools/opencode")
    resolved = resolve_opencode_binary(home=tmp_path / "home")
    assert resolved.is_absolute()
    assert resolved == real.resolve()


def test_resolve_opencode_binary_falls_back_to_the_install_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off PATH, `~/.opencode/bin/opencode` is found and handed back absolute."""
    home = tmp_path / "home"
    installed = _install(home)
    _path_holds(monkeypatch)
    assert resolve_opencode_binary(home=home) == installed.resolve()
    # `home` defaults to the home directory of the process.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert resolve_opencode_binary() == installed.resolve()


def test_resolve_opencode_binary_not_executable_names_the_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file at the install path that cannot run is a miss with the chmod as the fix."""
    home = tmp_path / "home"
    installed = _install(home, mode=0o644)
    _path_holds(monkeypatch)
    with pytest.raises(OpenCodeBinaryError) as info:
        resolve_opencode_binary(home=home)
    assert str(installed) in str(info.value)
    assert "not executable" in str(info.value)
    assert info.value.remediation == f"chmod +x {installed}"


def test_resolve_opencode_binary_missing_names_both_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nowhere at all: the message says where it looked; the fix is PATH or install."""
    home = tmp_path / "home"
    _path_holds(monkeypatch)
    with pytest.raises(OpenCodeBinaryError) as info:
        resolve_opencode_binary(home=home)
    install_dir = home / OPENCODE_INSTALL_DIR
    assert str(info.value) == (
        f"opencode CLI not on PATH and not at {install_dir / 'opencode'}"
    )
    assert info.value.remediation == (
        f"add {install_dir} to PATH, or install opencode (https://opencode.ai)"
    )


def test_resolve_opencode_binary_explicit_path_is_taken_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path rather than a name skips the search: it is checked and made absolute."""
    asked = _path_holds(monkeypatch, opencode="/usr/bin/opencode")
    explicit = tmp_path / "bin" / "opencode"
    explicit.parent.mkdir()
    explicit.write_text("#!/bin/sh\nexit 0\n")
    explicit.chmod(0o755)
    home = tmp_path / "home"
    assert resolve_opencode_binary(str(explicit), home=home) == explicit.resolve()
    assert asked == []
    explicit.chmod(0o644)
    with pytest.raises(OpenCodeBinaryError, match="not executable") as info:
        resolve_opencode_binary(str(explicit), home=home)
    assert info.value.remediation == f"chmod +x {explicit}"
    with pytest.raises(OpenCodeBinaryError) as info:
        resolve_opencode_binary(str(tmp_path / "nowhere" / "opencode"), home=home)
    assert "not found at" in str(info.value)
    assert "install opencode" in info.value.remediation
    assert asked == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080"),
        ("https://gpu-box:8443/v1", "https://gpu-box:8443/v1"),
    ],
)
def test_parse_local_table_normalises_base_url(raw: str, expected: str) -> None:
    assert parse_local_table({"base_url": raw, "model": "m"}).base_url == expected


def test_parse_local_table_fills_the_default_base_url() -> None:
    local = parse_local_table({"model": "m"})
    assert local == LocalConfig(DEFAULT_LOCAL_BASE_URL, "m", None)


@pytest.mark.parametrize(
    "table, key",
    [
        (None, "local.model"),
        ({}, "local.model"),
        ({"model": ""}, "local.model"),
        ({"model": "a b"}, "local.model"),
        ({"model": 3}, "local.model"),
        ({"model": "m", "base_url": "127.0.0.1:8080/v1"}, "local.base_url"),
        ({"model": "m", "base_url": "http://"}, "local.base_url"),
        ({"model": "m", "api_key_env": "not a name"}, "local.api_key_env"),
        ({"model": "m", "api_key_env": "1KEY"}, "local.api_key_env"),
        ({"model": "m", "wire_api": "chat"}, "expected base_url, model, or api_key_env"),
        ("http://127.0.0.1:8080/v1", "expected a TOML table"),
    ],
)
def test_parse_local_table_names_the_key_at_fault(table: object, key: str) -> None:
    with pytest.raises(ProfileError, match=key):
        parse_local_table(table)


def test_load_local_config_without_a_pinned_backend() -> None:
    cfg = Config(values={"backend": "claude", "local": {"model": "m"}})
    assert load_local_config(cfg).model == "m"


def test_load_local_config_without_a_table_names_local_model() -> None:
    with pytest.raises(ProfileError, match="local.model"):
        load_local_config(Config(values={"backend": "claude"}))


def test_local_efforts_are_the_opencode_variant_names() -> None:
    """`local` is opencode under its older name, so its efforts are variant names."""
    assert SUPPORTED_EFFORTS["local"] == SUPPORTED_EFFORTS["opencode"]
    assert SUPPORTED_EFFORTS["local"] != SUPPORTED_EFFORTS["codex"]
    assert "none" in SUPPORTED_EFFORTS["local"]


# --- the opencode provider block --------------------------------------------
#
# `opencode.json` registers the served model as `provider.ortuslocal`; the
# block is the exact shape the opencode 1.18.27 spike drove a keyless
# llama-server with, so these pin it field for field.


def test_opencode_provider_block_names_the_base_url_and_model() -> None:
    """Keyless: the spike's shape, with no auth option at all."""
    local = LocalConfig("http://127.0.0.1:8080/v1", "org/model:Q4_K_M")
    block = opencode_provider_block(local)
    assert block == {
        "npm": OPENCODE_PROVIDER_NPM,
        "name": "Ortus local model",
        "options": {"baseURL": "http://127.0.0.1:8080/v1"},
        "models": {"org/model:Q4_K_M": {}},
    }
    # The block is what opencode.json carries, so it must round-trip as JSON.
    assert json.loads(json.dumps(block)) == block


def test_opencode_provider_block_keys_by_env_reference_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`api_key_env` becomes opencode's `{env:NAME}` reference; the value never lands."""
    monkeypatch.setenv("LLAMA_API_KEY", "sk-secret-value")
    local = LocalConfig(
        "http://127.0.0.1:8080/v1", "m", api_key_env="LLAMA_API_KEY"
    )
    block = opencode_provider_block(local)
    assert block["options"] == {
        "baseURL": "http://127.0.0.1:8080/v1",
        "apiKey": "{env:LLAMA_API_KEY}",
    }
    assert "sk-secret-value" not in json.dumps(block)


def test_opencode_provider_block_uses_the_normalised_base_url() -> None:
    """The table rules run first, so a trailing slash never reaches baseURL."""
    local = parse_local_table({"base_url": "http://gpu-box:11434/v1/", "model": "m"})
    assert opencode_provider_block(local)["options"]["baseURL"] == (
        "http://gpu-box:11434/v1"
    )


# --- probes: a canned OpenAI-compatible server on 127.0.0.1 -----------------


@dataclass
class Received:
    """One request the fake server saw, with header names lower-cased."""

    method: str
    path: str
    headers: dict[str, str]
    body: Any


@dataclass
class FakeServer:
    """A per-test route table plus everything the probes sent.

    A route value is `(status, payload)`; `payload` is JSON-encoded unless it
    is already `bytes`. A callable route receives the `Received` request and
    returns that pair, which is how a test makes the server stall.
    """

    routes: dict[tuple[str, str], Any] = field(default_factory=dict)
    received: list[Received] = field(default_factory=list)
    port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def config(
        self, model: str = "qwen3:4b", api_key_env: str | None = None
    ) -> LocalConfig:
        return LocalConfig(self.base_url, model, api_key_env)

    def serve_models(self, *ids: str) -> None:
        self.routes[("GET", "/v1/models")] = (
            200,
            {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]},
        )


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Keep the test log free of access lines."""

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        received = Received(
            self.command,
            self.path,
            {name.lower(): value for name, value in self.headers.items()},
            json.loads(raw) if raw else None,
        )
        fake = self.server.fake
        fake.received.append(received)
        route = fake.routes.get((self.command, self.path))
        if route is None:
            status, payload = 404, {"error": f"no route for {self.command} {self.path}"}
        elif callable(route):
            status, payload = route(received)
        else:
            status, payload = route
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # A handler stalled on purpose must not hold up fixture teardown.
    block_on_close = False

    def __init__(self, fake: FakeServer) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fake = fake

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A probe that timed out has hung up, so the stalled handler's write
        # fails; that is the test working, not something to print.
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


@pytest.fixture(autouse=True)
def _direct_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep loopback traffic off any HTTP proxy the host environment names."""
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def fake_server() -> Iterator[FakeServer]:
    """A live fake server on an ephemeral loopback port."""
    fake = FakeServer()
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


def _closed_port() -> int:
    """A loopback port nothing listens on, so a connection is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_serving_hint_names_the_port_and_jinja() -> None:
    hint = serving_hint(LocalConfig("http://127.0.0.1:9000/v1", "qwen3:4b"))
    llama, ollama = hint.splitlines()
    assert llama.startswith("llama-server ")
    for flag in (
        "--alias qwen3:4b",
        "--jinja",
        f"--ctx-size {MIN_RECOMMENDED_CONTEXT}",
        "--port 9000",
    ):
        assert flag in llama
    assert "ollama pull qwen3:4b" in ollama
    assert "http://127.0.0.1:11434/v1" in ollama


def test_serving_hint_defaults_the_port_from_the_scheme() -> None:
    assert "--port 443" in serving_hint(LocalConfig("https://gpu-box/v1", "m"))
    assert "--port 80" in serving_hint(LocalConfig("http://gpu-box/v1", "m"))


def test_local_server_error_carries_kind_and_remediation() -> None:
    error = LocalServerError("unreachable", "down", "serve it")
    assert isinstance(error, RuntimeError)
    assert (str(error), error.kind, error.remediation) == (
        "down",
        "unreachable",
        "serve it",
    )
    with pytest.raises(ValueError, match="unknown probe kind"):
        LocalServerError("bogus", "m", "r")


def test_probe_models_lists_served_ids(fake_server: FakeServer) -> None:
    fake_server.serve_models("qwen3:4b", "gemma4:26b")
    assert probe_models(fake_server.config("qwen3:4b"), timeout=1.0) == (
        "qwen3:4b",
        "gemma4:26b",
    )
    [seen] = fake_server.received
    assert (seen.method, seen.path) == ("GET", "/v1/models")
    assert "authorization" not in seen.headers


def test_probe_models_unreachable_names_serving_command() -> None:
    config = LocalConfig(f"http://127.0.0.1:{_closed_port()}/v1", "qwen3:4b")
    with pytest.raises(LocalServerError) as info:
        probe_models(config, timeout=1.0)
    assert info.value.kind == "unreachable"
    assert str(info.value).startswith(
        f"local server unreachable at {config.base_url}: "
    )
    assert "refused" in str(info.value).lower()
    assert "llama-server" in info.value.remediation
    assert "--jinja" in info.value.remediation
    assert info.value.remediation == serving_hint(config)


def test_probe_models_missing_model_lists_served(fake_server: FakeServer) -> None:
    fake_server.serve_models("gemma4:26b", "qwen3-coder:30b")
    with pytest.raises(LocalServerError) as info:
        probe_models(fake_server.config("qwen3:4b"), timeout=1.0)
    assert info.value.kind == "model-missing"
    assert (
        str(info.value)
        == "model 'qwen3:4b' is not served; served: gemma4:26b, qwen3-coder:30b"
    )
    assert "local.model" in info.value.remediation


def test_probe_models_empty_list_is_model_missing(fake_server: FakeServer) -> None:
    fake_server.serve_models()
    with pytest.raises(LocalServerError) as info:
        probe_models(fake_server.config(), timeout=1.0)
    assert info.value.kind == "model-missing"
    assert str(info.value).endswith("served: none")


@pytest.mark.parametrize(
    "route, reason",
    [
        ((200, b"<html>not json</html>"), "unparseable JSON"),
        ((503, {"error": "model loading"}), "HTTP 503 from /models"),
    ],
)
def test_probe_models_bad_answers_are_unreachable(
    fake_server: FakeServer, route: tuple[int, Any], reason: str
) -> None:
    fake_server.routes[("GET", "/v1/models")] = route
    with pytest.raises(LocalServerError) as info:
        probe_models(fake_server.config(), timeout=1.0)
    assert info.value.kind == "unreachable"
    assert reason in str(info.value)
    assert info.value.remediation == serving_hint(fake_server.config())


def test_probe_models_timeout_is_unreachable(fake_server: FakeServer) -> None:
    def stall(received: Received) -> tuple[int, Any]:
        time.sleep(1.0)
        return 200, {"data": []}

    fake_server.routes[("GET", "/v1/models")] = stall
    with pytest.raises(LocalServerError) as info:
        probe_models(fake_server.config(), timeout=0.2)
    assert info.value.kind == "unreachable"
    assert "timed out" in str(info.value)


def test_probe_models_401_is_auth_demanded(fake_server: FakeServer) -> None:
    fake_server.routes[("GET", "/v1/models")] = (401, {"error": "unauthorized"})
    with pytest.raises(LocalServerError) as info:
        probe_models(fake_server.config(), timeout=1.0)
    assert info.value.kind == "auth-demanded"
    assert "local.api_key_env" in info.value.remediation


def test_probe_models_hits_v1_models_after_normalisation(
    fake_server: FakeServer,
) -> None:
    config = parse_local_table({"base_url": fake_server.base_url + "/", "model": "m"})
    fake_server.serve_models("m")
    assert probe_models(config, timeout=1.0) == ("m",)
    assert fake_server.received[0].path == "/v1/models"


def test_list_served_models_needs_no_model(fake_server: FakeServer) -> None:
    """The unpinned listing hits the same endpoint, with the slash normalised."""
    fake_server.serve_models("qwen3:4b", "gemma4:26b")
    assert list_served_models(fake_server.base_url + "/", timeout=1.0) == (
        "qwen3:4b",
        "gemma4:26b",
    )
    [seen] = fake_server.received
    assert (seen.method, seen.path) == ("GET", "/v1/models")
    assert "authorization" not in seen.headers


def test_list_served_models_empty_list_is_an_answer(fake_server: FakeServer) -> None:
    """Nothing loaded is a listing, not a verdict; only the pinned probe objects."""
    fake_server.serve_models()
    assert list_served_models(fake_server.base_url, timeout=1.0) == ()


def test_list_served_models_sends_the_named_key(
    fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLAMA_API_KEY", "sk-secret-value")
    fake_server.serve_models("m")
    assert list_served_models(
        fake_server.base_url, api_key_env="LLAMA_API_KEY", timeout=1.0
    ) == ("m",)
    assert fake_server.received[0].headers["authorization"] == "Bearer sk-secret-value"


def test_list_served_models_unreachable_hints_an_unpinned_alias() -> None:
    base_url = f"http://127.0.0.1:{_closed_port()}/v1"
    with pytest.raises(LocalServerError) as info:
        list_served_models(base_url, timeout=1.0)
    assert info.value.kind == "unreachable"
    assert str(info.value).startswith(f"local server unreachable at {base_url}: ")
    assert "refused" in str(info.value).lower()
    assert "--alias <model>" in info.value.remediation


def test_list_served_models_401_is_auth_demanded(fake_server: FakeServer) -> None:
    fake_server.routes[("GET", "/v1/models")] = (401, {"error": "unauthorized"})
    with pytest.raises(LocalServerError) as info:
        list_served_models(fake_server.base_url, timeout=1.0)
    assert info.value.kind == "auth-demanded"
    assert "local.api_key_env" in info.value.remediation


_NOT_A_BASE_URL = [
    "127.0.0.1:8080/v1",
    "http:///v1",
    "ftp://gpu-box/v1",
    "http://[::1",
    "http://127.0.0.1:abc/v1",
]


@pytest.mark.parametrize("base_url", _NOT_A_BASE_URL)
def test_list_served_models_rejects_a_non_url_as_unreachable(base_url: str) -> None:
    """A base_url the request could not even address is unreachable, not a crash."""
    with pytest.raises(LocalServerError) as info:
        list_served_models(base_url, timeout=1.0)
    assert info.value.kind == "unreachable"
    assert "http:// or https://" in str(info.value)
    assert "local.base_url" in info.value.remediation


@pytest.mark.parametrize("base_url", _NOT_A_BASE_URL)
def test_parse_local_table_rejects_a_url_the_parser_cannot_read(
    base_url: str,
) -> None:
    """The same shapes fail the table rules, so no later reader has to parse them."""
    with pytest.raises(ProfileError, match="invalid local.base_url"):
        parse_local_table({"base_url": base_url, "model": "m"})


def test_probe_context_size_reads_props(fake_server: FakeServer) -> None:
    fake_server.routes[("GET", "/props")] = (
        200,
        {"default_generation_settings": {"n_ctx": 32768, "n_predict": -1}},
    )
    assert probe_context_size(fake_server.config(), timeout=1.0) == 32768
    [seen] = fake_server.received
    assert (seen.method, seen.path) == ("GET", "/props")


def test_probe_context_size_none_when_unexposed(fake_server: FakeServer) -> None:
    # Ollama and vLLM answer 404; a server that is down is probe_models's verdict.
    assert probe_context_size(fake_server.config(), timeout=1.0) is None
    assert fake_server.received[0].path == "/props"
    down = LocalConfig(f"http://127.0.0.1:{_closed_port()}/v1", "m")
    assert probe_context_size(down, timeout=1.0) is None


@pytest.mark.parametrize(
    "settings, expected",
    [
        ({"n_ctx": "4096"}, 4096),
        ({"n_ctx": 4096.0}, 4096),
        ({"n_ctx": "lots"}, None),
        ({"n_ctx": True}, None),
        ({}, None),
        ("not a table", None),
    ],
)
def test_probe_context_size_coerces_n_ctx(
    fake_server: FakeServer, settings: Any, expected: int | None
) -> None:
    fake_server.routes[("GET", "/props")] = (
        200,
        {"default_generation_settings": settings},
    )
    assert probe_context_size(fake_server.config(), timeout=1.0) == expected


def test_probes_send_bearer_only_from_env(
    fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLAMA_API_KEY", "sk-secret-value")
    fake_server.serve_models("qwen3:4b")
    fake_server.routes[("GET", "/props")] = (
        200,
        {"default_generation_settings": {"n_ctx": 8192}},
    )
    config = fake_server.config("qwen3:4b", api_key_env="LLAMA_API_KEY")

    assert probe_models(config, timeout=1.0) == ("qwen3:4b",)
    assert probe_context_size(config, timeout=1.0) == 8192
    assert [seen.headers.get("authorization") for seen in fake_server.received] == [
        "Bearer sk-secret-value"
    ] * 2

    # A server that demands a key names the variable, never its value.
    fake_server.routes[("GET", "/v1/models")] = (403, {"error": "forbidden"})
    with pytest.raises(LocalServerError) as info:
        probe_models(config, timeout=1.0)
    assert info.value.kind == "auth-demanded"
    assert "LLAMA_API_KEY" in info.value.remediation
    for text in (str(info.value), info.value.remediation, repr(info.value)):
        assert "sk-secret-value" not in text

    # The value is read at call time, so a variable that is not exported
    # sends nothing rather than a stale header.
    monkeypatch.delenv("LLAMA_API_KEY")
    fake_server.serve_models("qwen3:4b")
    fake_server.received.clear()
    probe_models(config, timeout=1.0)
    assert "authorization" not in fake_server.received[0].headers


def test_unreachable_message_never_carries_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLAMA_API_KEY", "sk-secret-value")
    config = LocalConfig(
        f"http://127.0.0.1:{_closed_port()}/v1", "m", api_key_env="LLAMA_API_KEY"
    )
    with pytest.raises(LocalServerError) as info:
        probe_models(config, timeout=1.0)
    assert "sk-secret-value" not in str(info.value) + info.value.remediation
