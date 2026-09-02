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

import re
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
