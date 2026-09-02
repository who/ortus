"""Tests for core/local_backend.py — the `[local]` table and `LocalConfig`."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from ortus.core.config import Config
from ortus.core.local_backend import (
    DEFAULT_LOCAL_BASE_URL,
    LOCAL_PROVIDER_ID,
    LOCAL_WIRE_API,
    MIN_RECOMMENDED_CONTEXT,
    LocalConfig,
    load_local_config,
    parse_local_table,
)
from ortus.core.profiles import SUPPORTED_EFFORTS, ProfileError


def test_constants_pin_the_serving_contract() -> None:
    assert DEFAULT_LOCAL_BASE_URL == "http://127.0.0.1:8080/v1"
    assert LOCAL_PROVIDER_ID == "ortus_local"
    assert LOCAL_WIRE_API == "responses"
    assert MIN_RECOMMENDED_CONTEXT == 32768


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


def test_local_efforts_are_the_codex_set_but_not_the_same_object() -> None:
    assert SUPPORTED_EFFORTS["local"] == SUPPORTED_EFFORTS["codex"]
    assert SUPPORTED_EFFORTS["local"] is not SUPPORTED_EFFORTS["codex"]
    assert "none" not in SUPPORTED_EFFORTS["local"]
