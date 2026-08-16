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


def test_add_mcp_server_two_urls_on_one_host_get_distinct_names(tmp_path):
    """Two endpoints on one host derive the same base name; the write has to disambiguate them itself,
    since a successful add_mcp_server call must not be able to produce a config the registry's collision
    check would later reject at boot."""
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://broker.example.com/mcp/quotes")
    config_store.add_mcp_server(path, "https://broker.example.com/mcp/orders")
    servers = settings.load(str(path))["mcp_servers"]
    names = [s.name for s in servers]
    assert len(set(names)) == len(names)
    assert names[0] == "broker-example-com"
    assert names[1] == "broker-example-com-2"

    from kokua.toolsets.agents import build_registry
    from kokua.config.schema import AgentConfig, AssistantConfig

    config = AssistantConfig(
        agents={"assistant": AgentConfig(tools=names)},
        entry_agent="assistant",
        load_plugins=False,
        mcp_servers=servers,
    )
    registry = build_registry(config)  # must not raise a collision
    assert set(names) <= set(registry)


def test_add_mcp_server_replacing_an_existing_url_keeps_its_hand_edited_name(tmp_path):
    """Re-adding an already-recorded URL must not silently rename an entry a human named by hand: that
    name may be the one an [agents.*] table (hand-edit only) already references."""
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\nurl = "https://x/mcp"\nname = "custom-name"\n', encoding="utf-8")
    config_store.add_mcp_server(path, "https://x/mcp", token_env="TOK")
    servers = settings.load(str(path))["mcp_servers"]
    assert [(s.url, s.name, s.token_env) for s in servers] == [("https://x/mcp", "custom-name", "TOK")]


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
