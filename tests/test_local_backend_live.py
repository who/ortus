"""Opt-in proof that a real local server answers the probes and a real
``LocalRunner`` child completes one bounded CodeGraph query.

Configuration comes from the environment, never from a repository
``.ortusrc``, so the module can point at whatever the operator is serving:

    ORTUS_RUN_LOCAL_BACKEND_SMOKE=1 ORTUS_LOCAL_BASE_URL=http://127.0.0.1:8080/v1 \\
    ORTUS_LOCAL_MODEL=<served id> uv run pytest tests/test_local_backend_live.py \\
    -m live_provider

Without the opt-in variable every test here skips, and the ``live_provider``
mark keeps the hermetic gate from ever collecting it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ortus.core.agent import LocalRunner
from ortus.core.codegraph import (
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    parse_transcript,
)
from ortus.core.local_backend import (
    DEFAULT_LOCAL_BASE_URL,
    LocalConfig,
    parse_local_table,
    probe_models,
    probe_tool_calling,
)

pytestmark = [pytest.mark.live_provider, pytest.mark.slow]

SMOKE_VAR = "ORTUS_RUN_LOCAL_BACKEND_SMOKE"
BASE_URL_VAR = "ORTUS_LOCAL_BASE_URL"
MODEL_VAR = "ORTUS_LOCAL_MODEL"
API_KEY_ENV_VAR = "ORTUS_LOCAL_API_KEY_ENV"

#: The same one-query prompt the codex live test sends, so a local model is
#: judged against exactly the behaviour the hosted harness already proves.
BOUNDED_QUERY_PROMPT = (
    "Call codegraph_explore exactly once with the bounded query "
    "'Orient to src/ortus/core/agent.py'. Do not call shell tools. Then stop."
)
#: Local decode is slow; a cold load plus one tool round-trip can take minutes.
WORKER_TIMEOUT = 900


def _local_config_from_env() -> LocalConfig:
    """Build the ``[local]`` table from the environment, or skip.

    Missing opt-in is a skip, and so is a missing model id: the skip message
    names the variable so the operator knows which one to set. The values run
    through the same validation as a ``.ortusrc`` table, so a malformed URL
    fails the way it would fail ``ortus check``.
    """
    if os.environ.get(SMOKE_VAR) != "1":
        pytest.skip(f"set {SMOKE_VAR}=1 to run")
    model = os.environ.get(MODEL_VAR)
    if not model:
        pytest.skip(f"set {MODEL_VAR} to the served model id to run")
    table: dict[str, str] = {
        "base_url": os.environ.get(BASE_URL_VAR) or DEFAULT_LOCAL_BASE_URL,
        "model": model,
    }
    api_key_env = os.environ.get(API_KEY_ENV_VAR)
    if api_key_env:
        table["api_key_env"] = api_key_env
    return parse_local_table(table)


def test_local_server_answers_tool_probe() -> None:
    """The served list carries the model and ``/responses`` calls a tool.

    A server that is up but serves a different model fails here with the
    served ids in the message; a server that narrates instead of calling the
    probe tool fails with ``tools-unsupported``. Both are findings, not skips.
    """
    config = _local_config_from_env()
    served = probe_models(config)
    assert config.model in served, served
    probe_tool_calling(config)


def test_real_local_worker_completes_bounded_codegraph_query(tmp_path: Path) -> None:
    """A fresh ``codex exec`` against the local provider reaches CodeGraph.

    Mirrors the codex live test: the CodeGraph probe takes the codex
    capability path for ``local``, the runner is read-only, and the
    transcript must show the MCP call. A model that answers in prose without
    calling ``codegraph_explore`` leaves ``capability_observed`` false, which
    is the intended signal.
    """
    config = _local_config_from_env()
    repo = Path.cwd()
    probe = CodeGraphAdapter().probe(repo, CodeGraphMode.REQUIRED, backend="local")
    runner = LocalRunner(config, codegraph=probe.capability, sandbox_mode="read-only")
    log = tmp_path / "local-codegraph.jsonl"
    rc = runner.run(
        BOUNDED_QUERY_PROMPT,
        repo=repo,
        log_path=log,
        timeout=WORKER_TIMEOUT,
    )
    assert rc == 0, f"codex exec exited {rc}; log at {log}"
    summary = parse_transcript(log, phase=CodeGraphPhase.VERIFICATION, probe=probe)
    assert summary.capability_observed, f"no CodeGraph call in {log}"
