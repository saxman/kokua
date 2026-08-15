"""The [agents.*] schema, and a named error for every key this release removed."""

import pytest

from kokua.config.file import ConfigError, load
from kokua.config.schema import AssistantConfig


def _load(tmp_path, body: str) -> AssistantConfig:
    """Write a config file and resolve it the way the CLI does: file dict into the dataclass."""
    path = tmp_path / "config.toml"
    path.write_text(body)
    return AssistantConfig(**load(str(path)))


MINIMAL = """
[assistant]
agent = "assistant"

[agents.assistant]
tools = ["memory", "time"]
delegates_to = ["researcher"]

[agents.researcher]
description = "Research specialist."
tools = ["web"]
"""

# The [assistant].memory case cannot be expressed by appending a second [assistant] table (TOML
# rejects a duplicate table before any Kokua code runs), so it sets the key inside MINIMAL's existing
# [assistant] table instead.
_MEMORY_STILL_SET = MINIMAL.replace('agent = "assistant"\n', 'agent = "assistant"\nmemory = true\n')


def test_agents_load_with_their_tools_and_delegates(tmp_path):
    config = _load(tmp_path, MINIMAL)
    assert config.entry_agent == "assistant"
    assert config.agents["assistant"].tools == ["memory", "time"]
    assert config.agents["assistant"].delegates_to == ["researcher"]
    assert config.agents["researcher"].description == "Research specialist."
    assert config.agents["researcher"].delegates_to == []


def test_entry_agent_defaults_to_assistant(tmp_path):
    body = MINIMAL.replace('agent = "assistant"\n', "")
    assert _load(tmp_path, body).entry_agent == "assistant"


def test_an_unknown_key_in_an_agent_table_is_named(tmp_path):
    body = MINIMAL + '\n[agents.coder]\ngroup = ["fs"]\n'
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, body)
    assert "group" in str(excinfo.value)


@pytest.mark.parametrize(
    "config_text, removed, replacement",
    [
        # `replacement` must not be a substring of `removed` (or vice versa): otherwise the second
        # assertion is satisfied for free by the first and proves nothing about the replacement
        # guidance specifically. "tools" is a substring of "[tools]" itself, so the replacement here
        # is the concrete path the message actually points at instead.
        (MINIMAL + '\n[tools]\ngroups = ["web"]\n', "[tools]", "[agents.<name>].tools"),
        (MINIMAL + "\n[subagents]\nconcurrent = true\n", "[subagents]", "concurrent_tools"),
        (MINIMAL + '\n[subagents.roles.x]\ndescription = "x"\n', "[subagents.roles", "[agents."),
    ],
)
def test_a_removed_key_fails_and_names_its_replacement(tmp_path, config_text, removed, replacement):
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, config_text)
    message = str(excinfo.value)
    assert removed in message
    assert replacement in message


def test_removed_memory_key_names_the_toolsets_that_replace_it(tmp_path):
    """A dedicated test, not a parametrize case: "memory" is both the removed key and part of any
    honest replacement guidance ("declare the memory toolset..."), so the two assertions cannot use
    the same word without one becoming free. The replacement is checked with "documents" instead,
    which appears only in the guidance and names the second toolset [assistant].memory used to gate,
    so the assertion fails if that guidance is ever dropped or reworded into uselessness."""
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, _MEMORY_STILL_SET)
    message = str(excinfo.value)
    assert "memory" in message
    assert "documents" in message


def test_a_removed_per_agent_key_fails(tmp_path):
    body = MINIMAL + '\n[agents.coder]\ntool_packs = ["pdf"]\n'
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, body)
    assert "tool_packs" in str(excinfo.value)
    assert "tools" in str(excinfo.value)
