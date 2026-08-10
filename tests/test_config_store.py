"""Tests for the comment-preserving config.toml write layer."""

from __future__ import annotations

import tomllib

from kokua.config import store as config_store
from kokua.config import file as settings


def _read(path):
    with path.open("rb") as file:
        return tomllib.load(file)


def test_set_value_seeds_from_example_when_file_absent(tmp_path):
    path = tmp_path / "config.toml"
    config_store.set_value(path, "assistant", "model", "anthropic:claude-opus-4-8")
    text = path.read_text(encoding="utf-8")
    # Seeded from the shipped example, so its documentation comments are present.
    assert "# Kokua configuration" in text
    assert _read(path)["assistant"]["model"] == "anthropic:claude-opus-4-8"


def test_set_value_preserves_existing_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# keep me\n[assistant]\nmodel = "old"\n', encoding="utf-8")
    config_store.set_value(path, "assistant", "model", "new")
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert _read(path)["assistant"]["model"] == "new"


def test_set_value_creates_missing_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[assistant]\nmodel = "m"\n', encoding="utf-8")
    config_store.set_value(path, "logging", "level", "DEBUG")
    assert _read(path)["logging"]["level"] == "DEBUG"
    assert _read(path)["assistant"]["model"] == "m"


def test_set_value_writes_nested_generation_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.set_value(path, "generation", "temperature", 0.3)
    assert _read(path)["generation"]["temperature"] == 0.3


def test_unset_value_removes_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[assistant]\nmodel = "m"\nmemory = true\n', encoding="utf-8")
    config_store.unset_value(path, "assistant", "model")
    data = _read(path)
    assert "model" not in data["assistant"]
    assert data["assistant"]["memory"] is True


def test_unset_value_missing_key_is_no_op(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[assistant]\nmemory = true\n", encoding="utf-8")
    config_store.unset_value(path, "assistant", "model")
    assert _read(path)["assistant"]["memory"] is True


def test_add_mcp_server_appends_and_is_readable_by_settings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://plain/mcp")
    config_store.add_mcp_server(path, "https://auth/mcp", token_env="TOK")
    servers = settings.load(str(path))["mcp_servers"]
    assert [(s.url, s.token_env) for s in servers] == [
        ("https://plain/mcp", None),
        ("https://auth/mcp", "TOK"),
    ]


def test_add_mcp_server_updates_existing_url(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://x/mcp")
    config_store.add_mcp_server(path, "https://x/mcp", token_env="TOK")
    servers = settings.load(str(path))["mcp_servers"]
    assert [(s.url, s.token_env) for s in servers] == [("https://x/mcp", "TOK")]


def test_remove_mcp_server(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://a/mcp")
    config_store.add_mcp_server(path, "https://b/mcp")
    assert config_store.remove_mcp_server(path, "https://a/mcp") is True
    servers = settings.load(str(path))["mcp_servers"]
    assert [s.url for s in servers] == ["https://b/mcp"]


def test_remove_mcp_server_absent_returns_false(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\nurl = "https://a/mcp"\n', encoding="utf-8")
    assert config_store.remove_mcp_server(path, "https://gone/mcp") is False
