"""The skills toolset: AIMU's authoring tools, guarded against toolset names and followed by a registry refresh."""

from __future__ import annotations

import logging

from aimu.skills import make_skill_authoring_tool, make_skill_script_tool, make_skill_update_tool

from kokua.config import ConfigError, MCPServerConfig
from kokua.config.schema import AgentConfig
from kokua.core.agents import build_registry
from kokua.mcp import servers
from kokua.mcp.auth import OAuthSettings
from kokua.registry.context import LiveState, ToolsetContext
from kokua.registry.registry import ToolsetError
from kokua.toolsets.skills import TOOLSET
from tests.channels import _config
from tests.fakes import _FakeMCP, _fake_mcp_tool
from tests.helpers import allow_all_gates


class _ReloadingAgent:
    """The one thing ``add_skill_script`` asks of its agent."""

    async def reload_skills(self) -> None:
        return None


def _tools(config, *, agent=None, **state_fields) -> dict:
    state = LiveState(gate_for=allow_all_gates, config=config, registry=build_registry(config), **state_fields)
    tools = TOOLSET.build(ToolsetContext(state=state, agent=agent, agent_name="assistant"))
    return {fn.__name__: fn for fn in tools}


def _skills_config(tmp_path, **overrides):
    agents = {"assistant": AgentConfig(available=["skills", "web"])}
    return _config(
        tmp_path,
        agents=agents,
        default_confirm=[],
        default_review=[],
        config_path=tmp_path / "config.toml",
        **overrides,
    )


def _recording_refresh(calls: list):
    def refresh(*, tools_changed: bool) -> None:
        calls.append(tools_changed)

    return refresh


def test_the_wrapped_tools_keep_aimus_names_and_schemas(tmp_path):
    config = _skills_config(tmp_path)
    originals = [
        make_skill_authoring_tool(None, config.skills_dir),
        make_skill_update_tool(None, config.skills_dir),
        make_skill_script_tool(None, None, config.skills_dir),
    ]

    tools = _tools(config)

    assert list(tools) == ["author_skill", "update_skill", "add_skill_script"]
    for original in originals:
        assert tools[original.__name__].__tool_spec__ == original.__tool_spec__


async def test_author_skill_refuses_a_toolset_name_and_writes_nothing(tmp_path):
    server = MCPServerConfig(url="https://files.example/mcp", name="files")
    config = _skills_config(tmp_path, mcp_servers=[server])
    author_skill = _tools(config)["author_skill"]

    for name in ("web", "files"):  # a Kokua toolset, a configured MCP server
        result = await author_skill(name=name, description="d", body="b")
        assert result.startswith(f"Nothing was written: {name!r} is already a toolset name")
        assert not (config.skills_dir / name).exists()


async def test_author_skill_refuses_a_reserved_name_and_writes_nothing(tmp_path):
    config = _skills_config(tmp_path)
    author_skill = _tools(config)["author_skill"]

    for name in ("core", "all"):
        result = await author_skill(name=name, description="d", body="b")
        assert result.startswith(f"Nothing was written: {name!r} is already a toolset name (reserved)")
        assert not (config.skills_dir / name).exists()


async def test_author_skill_refuses_the_name_of_a_server_added_this_session(tmp_path, monkeypatch):
    """The live registry never learns a server ``add_mcp_server`` persisted, but the next start registers
    it, so the name is read from the file, disambiguated suffix and all."""

    async def fake_connect(url, **kwargs):
        return _FakeMCP([_fake_mcp_tool("get_quote")]), "none"

    async def notify(message: str) -> None:
        return None

    monkeypatch.setattr(servers, "connect_mcp", fake_connect)
    config = _skills_config(tmp_path)
    config.config_path.write_text(
        '[[mcp.server]]\nurl = "https://broker.example/other"\nname = "broker-example"\n', encoding="utf-8"
    )
    await servers.add_server(
        [],
        "https://broker.example/mcp",
        notify=notify,
        oauth=OAuthSettings(storage_dir=tmp_path / "oauth"),
        config_path=config.config_path,
        for_each_agent=lambda fn: None,
    )
    author_skill = _tools(config)["author_skill"]

    result = await author_skill(name="broker-example-2", description="d", body="b")

    assert result.startswith("Nothing was written: 'broker-example-2' is already a toolset name (MCP server)")
    assert not (config.skills_dir / "broker-example-2").exists()


async def test_an_unreadable_config_file_does_not_stop_author_skill(tmp_path, caplog):
    config = _skills_config(tmp_path)
    config.config_path.write_text("[mcp\nnot toml", encoding="utf-8")
    author_skill = _tools(config)["author_skill"]

    with caplog.at_level(logging.WARNING, logger="kokua.toolsets.skills"):
        result = await author_skill(name="weekly-digest", description="Digest the week.", body="Do it.")

    assert result.startswith("Created skill 'weekly-digest'")
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_a_config_file_with_invalid_utf8_does_not_stop_author_skill(tmp_path, caplog):
    config = _skills_config(tmp_path)
    config.config_path.write_bytes(b"[mcp]\n\xff\xfe not utf-8")
    author_skill = _tools(config)["author_skill"]

    with caplog.at_level(logging.WARNING, logger="kokua.toolsets.skills"):
        result = await author_skill(name="weekly-digest", description="Digest the week.", body="Do it.")

    assert result.startswith("Created skill 'weekly-digest'")
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_a_refused_author_skill_does_not_refresh(tmp_path):
    calls: list = []
    author_skill = _tools(_skills_config(tmp_path), refresh_skills=_recording_refresh(calls))["author_skill"]

    result = await author_skill(name="web", description="d", body="b")

    assert result.startswith("Nothing was written")
    assert calls == []


async def test_each_tool_refreshes_and_only_prose_edits_keep_the_skill_tools(tmp_path):
    calls: list = []
    tools = _tools(_skills_config(tmp_path), agent=_ReloadingAgent(), refresh_skills=_recording_refresh(calls))

    result = await tools["author_skill"](name="weekly-digest", description="Digest the week.", body="Do it.")
    assert result.startswith("Created skill 'weekly-digest'")
    await tools["update_skill"](skill_name="weekly-digest", body="Do it better.")
    result = await tools["add_skill_script"](skill_name="weekly-digest", filename="run.py", content="print(1)\n")
    assert "is now callable" in result

    assert calls == [True, False, True]


async def test_a_refresh_collision_reaches_the_model_instead_of_raising(tmp_path):
    def colliding(*, tools_changed: bool) -> None:
        raise ToolsetError("toolset name 'x' is claimed by two providers.")

    tools = _tools(_skills_config(tmp_path), refresh_skills=colliding)

    result = await tools["author_skill"](name="weekly-digest", description="Digest the week.", body="Do it.")

    assert result.startswith("Created skill 'weekly-digest'")
    assert "toolset name 'x' is claimed by two providers." in result


async def test_any_failed_refresh_reaches_the_model_after_the_write(tmp_path, caplog):
    """An approval entry refusing the new script, or anything unexpected: the script is on disk either way,
    and an exception in place of the result would tell the model it is not."""

    def refused(*, tools_changed: bool) -> None:
        raise ConfigError("[assistant.tools].review names 'weekly-digest.x', which matches nothing.")

    tools = _tools(_skills_config(tmp_path), agent=_ReloadingAgent(), refresh_skills=refused)
    await tools["author_skill"](name="weekly-digest", description="Digest the week.", body="Do it.")

    with caplog.at_level(logging.WARNING, logger="kokua.toolsets.skills"):
        result = await tools["add_skill_script"](skill_name="weekly-digest", filename="run.py", content="print(1)\n")

    assert "is now callable" in result
    assert "The write happened, but workers were not given the change" in result
    assert "matches nothing" in result
    assert any(record.exc_info for record in caplog.records)
