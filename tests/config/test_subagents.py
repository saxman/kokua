"""Sub-agent roles come from config.toml, not from a default baked into Python.

The shipped `config.example.toml` is where `researcher` / `coder` / `generalist` are defined, so the
tests here read that file rather than a constant: it is the only place those roles exist, and a test
that parsed a Python copy of them would not notice the shipped file rotting.
"""

from __future__ import annotations

import tomllib

from kokua.config import file as settings
from kokua.config.schema import AssistantConfig


def _load_example(tmp_path) -> dict:
    """Field overrides produced by loading the config Kokua ships (what `config init` writes)."""
    path = tmp_path / "config.toml"
    path.write_text(settings.example_text(), encoding="utf-8")
    return settings.load(str(path))


def test_no_roles_are_defaulted_in_code():
    from kokua.config import schema

    assert not hasattr(schema, "DEFAULT_SUBAGENT_ROLES")
    assert AssistantConfig().subagent_roles == {}


def test_shipped_example_defines_the_built_in_roles(tmp_path):
    roles = _load_example(tmp_path)["subagent_roles"]
    assert set(roles) == {"researcher", "coder", "generalist"}
    for name, role in roles.items():
        assert isinstance(role["description"], str) and role["description"], name
        assert isinstance(role["groups"], list) and role["groups"], name
        assert isinstance(role["system_message"], str) and role["system_message"], name


def test_shipped_example_roles_name_known_tool_groups(tmp_path):
    from kokua.core.build import _TOOL_GROUPS

    for name, role in _load_example(tmp_path)["subagent_roles"].items():
        assert set(role["groups"]) <= set(_TOOL_GROUPS), name


def test_shipped_example_roles_stay_within_its_own_enabled_groups(tmp_path):
    """A shipped role naming a group the shipped [tools].groups omits would arrive tool-poorer than it
    reads, since a role is intersected with the global set."""
    overrides = _load_example(tmp_path)
    enabled = set(overrides["tools"])
    for name, role in overrides["subagent_roles"].items():
        assert set(role["groups"]) <= enabled, name


def test_shipped_example_roles_build_usable_toolsets(tmp_path):
    from kokua.core.build import _build_subagent_agent_types

    overrides = _load_example(tmp_path)
    cfg = AssistantConfig(data_dir=tmp_path, tools=overrides["tools"], subagent_roles=overrides["subagent_roles"])
    types = _build_subagent_agent_types(cfg)
    assert {fn.__name__ for fn in types["researcher"]["tools"]} >= {"web_search", "get_webpage"}
    assert {fn.__name__ for fn in types["coder"]["tools"]} >= {"read_file", "execute_python"}
    assert types["generalist"]["tools"]


def test_example_roles_are_uncommented_so_config_init_writes_them():
    """`config init` copies the example verbatim, so the roles must be live TOML, not comments."""
    data = tomllib.loads(settings.example_text())
    assert set(data["subagents"]["roles"]) == {"researcher", "coder", "generalist"}


def test_config_subagent_defaults():
    cfg = AssistantConfig()
    assert cfg.subagent_roles == {}
    assert cfg.subagents_concurrent is True
