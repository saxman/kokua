"""Tests for sub-agent role defaults and config fields."""

from __future__ import annotations

from kokua.config import DEFAULT_SUBAGENT_ROLES, AssistantConfig


def test_default_roles_present_and_shaped():
    assert set(DEFAULT_SUBAGENT_ROLES) == {"researcher", "coder", "generalist"}
    for role in DEFAULT_SUBAGENT_ROLES.values():
        assert isinstance(role["description"], str) and role["description"]
        assert isinstance(role["groups"], list) and role["groups"]
        assert isinstance(role["system_message"], str)


def test_role_groups_are_known_builtin_groups():
    from kokua.build import _TOOL_GROUPS

    for role in DEFAULT_SUBAGENT_ROLES.values():
        assert set(role["groups"]) <= set(_TOOL_GROUPS)


def test_config_subagent_defaults():
    cfg = AssistantConfig()
    assert cfg.subagent_roles == {}
    assert cfg.subagents_concurrent is True
