"""The `[local]` table: an operator-served OpenAI-compatible model.

`local` is the Ortus backend for a model the operator serves themselves
(llama-server first; Ollama and vLLM through the same seam). The Codex CLI is
the harness and reaches the server through a custom model provider, so
everything Ortus has to know about the model is data in `.ortusrc`:

    [local]
    base_url = "http://127.0.0.1:8080/v1"   # optional; this is the default
    model = "qwen3:4b"                       # required; as GET {base_url}/models reports it
    api_key_env = "LLAMA_API_KEY"            # optional; a variable NAME, never a value

The wire API is not configuration. Codex 0.147.0 speaks only the Responses
API to a custom provider, so `LOCAL_WIRE_API` is a constant and the serving
contract is `POST {base_url}/responses`.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ortus.core.profiles import ProfileError

if TYPE_CHECKING:  # pragma: no cover - config.py imports this module
    from ortus.core.config import Config

#: Where llama-server listens by default. Ollama's own default is port 11434.
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
#: The codex `model_providers.<id>` entry Ortus registers at launch. Codex
#: reserves `openai`, `ollama`, and `lmstudio`, so the id is namespaced.
LOCAL_PROVIDER_ID = "ortus_local"
#: The only wire API codex 0.147.0 accepts for a custom provider.
LOCAL_WIRE_API = "responses"
#: Tokens. A worker prompt plus CodeGraph tool output does not fit a smaller
#: window; the context probe warns below this.
MIN_RECOMMENDED_CONTEXT = 32768

#: The keys a `[local]` table may carry, in the order an operator meets them.
LOCAL_KEYS: tuple[str, ...] = ("base_url", "model", "api_key_env")

#: One message for both ways of having no model: a table without the key, and
#: `backend = "local"` pinned without a table at all.
MISSING_MODEL_MESSAGE = (
    "missing local.model: backend local needs a [local] table in .ortusrc "
    'with model = "<id as GET {base_url}/models reports it>"'
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_URL_SCHEMES = ("http://", "https://")


@dataclass(frozen=True)
class LocalConfig:
    """Immutable, validated `[local]` table.

    `api_key_env` is the name of an environment variable. Codex reads its
    value at launch; Ortus never does, so nothing here can carry a secret.
    """

    base_url: str
    model: str
    api_key_env: str | None = None

    @property
    def origin(self) -> str:
        """`base_url` without its `/v1` suffix, where llama-server serves `/props`."""
        if self.base_url.endswith("/v1"):
            return self.base_url[: -len("/v1")]
        return self.base_url

    @property
    def display(self) -> str:
        """A credential-free description for operator logs and check rows."""
        return f"local ({urlsplit(self.base_url).netloc}) model={self.model}"


def parse_local_table(table: Any) -> LocalConfig:
    """Apply the table rules to a raw `[local]` value and build a `LocalConfig`.

    `base_url` defaults to `DEFAULT_LOCAL_BASE_URL`, must carry an http(s)
    scheme and a host, and loses any trailing slash so `/v1/` and `/v1` read
    the same. `model` is required and is a single token. `api_key_env` is an
    environment variable name. Anything else raises `ProfileError`, the type
    every `load_config` caller already catches, naming the `local.<key>` at
    fault.
    """
    if table is None:
        raise ProfileError(MISSING_MODEL_MESSAGE)
    if not isinstance(table, dict):
        raise ProfileError("invalid local configuration: expected a TOML table")
    unknown = set(table) - set(LOCAL_KEYS)
    if unknown:
        raise ProfileError(
            f"invalid [local] field(s): {', '.join(sorted(unknown))}; "
            f"expected {LOCAL_KEYS[0]}, {LOCAL_KEYS[1]}, or {LOCAL_KEYS[2]}"
        )
    if "model" not in table:
        raise ProfileError(MISSING_MODEL_MESSAGE)
    model = table["model"]
    if not isinstance(model, str) or not model or any(c.isspace() for c in model):
        raise ProfileError(
            "invalid local.model: expected a non-empty model id without whitespace"
        )
    base_url = table.get("base_url", DEFAULT_LOCAL_BASE_URL)
    if (
        not isinstance(base_url, str)
        or not base_url.startswith(_URL_SCHEMES)
        or not urlsplit(base_url).netloc
    ):
        raise ProfileError(
            "invalid local.base_url: expected an http:// or https:// URL "
            "with a host, such as http://127.0.0.1:8080/v1"
        )
    api_key_env = table.get("api_key_env")
    if api_key_env is not None and (
        not isinstance(api_key_env, str) or not _ENV_NAME.fullmatch(api_key_env)
    ):
        raise ProfileError(
            "invalid local.api_key_env: expected an environment variable name "
            "such as LLAMA_API_KEY, never the key itself"
        )
    return LocalConfig(
        base_url=base_url.rstrip("/"), model=model, api_key_env=api_key_env
    )


def load_local_config(cfg: Config) -> LocalConfig:
    """The `[local]` table of a loaded `Config`, validated.

    `load_config` already checks the table when `backend = "local"` is pinned,
    but `--backend local` and `ORTUS_BACKEND=local` reach here without the pin,
    so the rules run again and a missing table gets the same message either way.
    """
    return parse_local_table(cfg.get("local"))


# --- probes -----------------------------------------------------------------
#
# Four questions `ortus check` and the grind preflight share: is the server
# up, does it serve the configured model, does it call tools, and how big is
# its window. Each is one request over stdlib urllib, and each failure names
# the exact serving command so a mis-served model is caught here rather than
# as a hung worker.

#: The verdicts a probe can hand back, each with its own remediation.
PROBE_KINDS: tuple[str, ...] = (
    "unreachable",
    "model-missing",
    "tools-unsupported",
    "auth-demanded",
)
#: The function the tool-calling probe asks the model to call.
PROBE_TOOL_NAME = "ortus_ping"
#: Words in an error body from `/responses` that mean the server understood
#: the request and rejected the tool part of it.
_TOOL_REJECTION_WORDS = ("tool", "template", "jinja")
_TOOL_REJECTION_STATUSES = frozenset({400, 404, 422, 500})
_EXCERPT_CHARS = 200
_TOOLS_REMEDIATION = (
    "restart llama-server with --jinja (and a tool-capable chat template)"
)


class LocalServerError(RuntimeError):
    """A probe verdict the operator has to act on.

    `kind` names the failure for check rows and the preflight; `remediation`
    is the text to print under the message, usually the serving command.
    Neither carries key material: probes read the API key at call time and
    put it in a request header only.
    """

    def __init__(self, kind: str, message: str, remediation: str) -> None:
        if kind not in PROBE_KINDS:
            raise ValueError(
                f"unknown probe kind {kind!r}; expected one of {PROBE_KINDS}"
            )
        super().__init__(message)
        self.kind = kind
        self.remediation = remediation


class _Unreachable(Exception):
    """A transport failure inside `_request_json`; the probes add the verdict."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def serving_hint(config: LocalConfig) -> str:
    """The reference serving commands for `config`, one line per server.

    `<repo>:<quant>` stays a placeholder because which weights to serve is
    the operator's choice; the alias, the port, `--jinja`, and the context
    size are what the probes and the worker depend on.
    """
    parts = urlsplit(config.base_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return (
        f"llama-server -hf <repo>:<quant> --alias {config.model} --jinja "
        f"--ctx-size {MIN_RECOMMENDED_CONTEXT} --flash-attn on "
        f"--host 127.0.0.1 --port {port}\n"
        f"ollama serve && ollama pull {config.model}  "
        '(Ollama: set local.base_url = "http://127.0.0.1:11434/v1")'
    )


def probe_models(config: LocalConfig, *, timeout: float = 5.0) -> tuple[str, ...]:
    """The ids `GET {base_url}/models` serves, once `config.model` is among them.

    Raises `unreachable` when the server does not answer with a JSON body,
    `auth-demanded` when it wants a key, and `model-missing` when it answers
    but the configured model is not in the list.
    """
    try:
        status, payload = _request_json(
            "GET",
            f"{config.base_url}/models",
            headers=_auth_headers(config),
            timeout=timeout,
        )
    except _Unreachable as exc:
        raise _unreachable(config, exc.reason) from None
    if status in (401, 403):
        raise _auth_demanded(config, status)
    if not 200 <= status < 300:
        raise _unreachable(config, f"HTTP {status} from /models: {_excerpt(payload)}")
    served = _served_ids(payload)
    if config.model not in served:
        raise LocalServerError(
            "model-missing",
            f"model {config.model!r} is not served; served: {', '.join(served) or 'none'}",
            "set local.model to a served id or load the model",
        )
    return served


def probe_tool_calling(config: LocalConfig, *, timeout: float = 120.0) -> None:
    """Ask `POST {base_url}/responses` to call one tool and check that it did.

    This is the endpoint and the tool shape codex uses, so a server that
    narrates here instead of calling would hang a worker later. The default
    timeout leaves room for a cold model load.
    """
    body = {
        "model": config.model,
        "input": f"Call the {PROBE_TOOL_NAME} tool once.",
        "tools": [
            {
                "type": "function",
                "name": PROBE_TOOL_NAME,
                "description": "Readiness probe.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        ],
        "tool_choice": "required",
        "max_output_tokens": 64,
        "stream": False,
    }
    try:
        status, payload = _request_json(
            "POST",
            f"{config.base_url}/responses",
            body=body,
            headers=_auth_headers(config),
            timeout=timeout,
        )
    except _Unreachable as exc:
        raise _unreachable(config, exc.reason) from None
    if status in (401, 403):
        raise _auth_demanded(config, status)
    if status in _TOOL_REJECTION_STATUSES and _mentions_tools(payload):
        raise LocalServerError(
            "tools-unsupported",
            f"server rejected the tool call (HTTP {status}): {_excerpt(payload)}",
            _TOOLS_REMEDIATION,
        )
    if not 200 <= status < 300:
        raise _unreachable(
            config, f"HTTP {status} from /responses: {_excerpt(payload)}"
        )
    if not _called_probe_tool(payload):
        raise LocalServerError(
            "tools-unsupported",
            "server answered without calling the tool",
            _TOOLS_REMEDIATION,
        )


def probe_context_size(config: LocalConfig, *, timeout: float = 5.0) -> int | None:
    """`n_ctx` from llama-server's `GET /props`, or None when it is not exposed.

    Ollama and vLLM have no `/props`, and a server that is down is
    `probe_models`'s verdict rather than this one, so nothing here raises.
    """
    try:
        status, payload = _request_json(
            "GET",
            f"{config.origin}/props",
            headers=_auth_headers(config),
            timeout=timeout,
        )
    except _Unreachable:
        return None
    if not 200 <= status < 300 or not isinstance(payload, dict):
        return None
    settings = payload.get("default_generation_settings")
    if not isinstance(settings, dict):
        return None
    return _as_int(settings.get("n_ctx"))


def _request_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> tuple[int, Any]:
    """One HTTP round trip, returned as `(status, payload)`.

    A 2xx answer must be JSON and comes back decoded; any other status comes
    back with its body as text so the caller can classify it. A refused
    connection, a timeout, or a 2xx body that is not JSON raises
    `_Unreachable` with a reason that never quotes the request headers.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    # A fresh opener reads the proxy environment on every call rather than
    # once per process, so a `no_proxy` exported for the local server applies
    # to the next probe.
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise _Unreachable(str(exc.reason)) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise _Unreachable(str(exc) or type(exc).__name__) from exc
    try:
        return status, json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Unreachable("unparseable JSON") from exc


def _auth_headers(config: LocalConfig) -> dict[str, str]:
    """`Authorization` for the configured key, read from the environment now.

    No header when `api_key_env` is unset or the variable is absent or empty;
    check reports an absent variable on its own row.
    """
    if config.api_key_env is None:
        return {}
    value = os.environ.get(config.api_key_env)
    if not value:
        return {}
    return {"Authorization": f"Bearer {value}"}


def _unreachable(config: LocalConfig, reason: str) -> LocalServerError:
    return LocalServerError(
        "unreachable",
        f"local server unreachable at {config.base_url}: {reason}",
        serving_hint(config),
    )


def _auth_demanded(config: LocalConfig, status: int) -> LocalServerError:
    if config.api_key_env is None:
        remediation = (
            "set local.api_key_env to the name of an environment variable "
            "holding the server's API key, then export that variable"
        )
    else:
        remediation = (
            f"export {config.api_key_env} (local.api_key_env) with the server's API key"
        )
    return LocalServerError(
        "auth-demanded",
        f"local server at {config.base_url} demands authentication (HTTP {status})",
        remediation,
    )


def _served_ids(payload: Any) -> tuple[str, ...]:
    """`data[*].id` from a `/models` body; any other shape serves nothing."""
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return ()
    return tuple(
        str(entry["id"])
        for entry in entries
        if isinstance(entry, dict) and "id" in entry
    )


def _called_probe_tool(payload: Any) -> bool:
    """Whether a `/responses` body carries a call to `PROBE_TOOL_NAME`."""
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("name") == PROBE_TOOL_NAME
        for item in output
    )


def _mentions_tools(text: Any) -> bool:
    lowered = str(text).lower()
    return any(word in lowered for word in _TOOL_REJECTION_WORDS)


def _excerpt(text: Any) -> str:
    """The start of a response body on one line, for an error message."""
    flat = " ".join(str(text).split())
    if len(flat) > _EXCERPT_CHARS:
        return flat[:_EXCERPT_CHARS] + "..."
    return flat or "<empty body>"


def _as_int(value: Any) -> int | None:
    """`n_ctx` as an int; llama-server has sent it as both a number and a string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
