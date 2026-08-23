"""The [agents.*] schema, and a named error for every key this release removed."""

import pytest

from kokua.config.file import ConfigError, load
from kokua.config.schema import AssistantConfig
from tests.helpers import core_table


def _load(tmp_path, body: str) -> AssistantConfig:
    """Write a config file and resolve it the way the CLI does: file dict into the dataclass."""
    path = tmp_path / "config.toml"
    path.write_text(body)
    return AssistantConfig(**load(str(path), table=core_table()))


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


def test_the_agents_section_cannot_be_written_by_the_assistant():
    from kokua.config import store as config_store

    defaults = config_store.DEFAULT_LOCKED_CONFIG_KEYS
    assert config_store.is_locked("agents.assistant", "tools", defaults)
    assert config_store.is_locked("agents", "assistant", defaults)
    assert config_store.is_locked("security", "confirm_tools", defaults)
    assert not config_store.is_locked("display", "show_tools", defaults)


async def test_update_config_refuses_an_agent_table_and_still_writes_a_runtime_setting(tmp_path):
    """The predicate is wired into the tool, not just defined: an agent table is refused and unchanged,
    while an ordinary runtime setting still goes through."""
    from kokua.toolsets.config import make_config_tools

    path = tmp_path / "config.toml"
    path.write_text('[agents.assistant]\ntools = ["time"]\n', encoding="utf-8")

    async def apply_hot(section, key, value):
        return None

    tools = make_config_tools(path, apply_hot, core_table(), config=AssistantConfig(), registry={})
    update = next(t for t in tools if t.__name__ == "update_config")

    refusal = await update(section="agents.assistant", key="tools", value="fs, compute")
    assert "hand-editing" in refusal
    assert 'tools = ["time"]' in path.read_text(encoding="utf-8")

    assert "9100" in await update(section="web", key="port", value="9100")
    assert "9100" in path.read_text(encoding="utf-8")


def test_the_shipped_example_config_loads_and_validates(tmp_path):
    import shutil
    from importlib.resources import files

    from kokua.config.file import load
    from kokua.toolsets.agents import build_registry, validate_agents

    source = files("kokua").joinpath("config.example.toml")
    path = tmp_path / "config.toml"
    shutil.copyfile(str(source), path)
    config = AssistantConfig(**load(str(path), table=core_table()))
    validate_agents(config, build_registry(config))
    assert config.entry_agent in config.agents


def test_a_removed_per_agent_key_fails(tmp_path):
    body = MINIMAL + '\n[agents.coder]\ntool_packs = ["pdf"]\n'
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, body)
    assert "tool_packs" in str(excinfo.value)
    assert "tools" in str(excinfo.value)


def test_an_agent_declares_its_own_model(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nmodel = "ollama:qwen3:32b"')
    config = _load(tmp_path, body)
    assert config.agents["researcher"].model == "ollama:qwen3:32b"
    assert config.agents["assistant"].model is None


def test_an_agents_model_must_be_a_string(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nmodel = 3')
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, body)
    assert "[agents.researcher].model must be a str" in str(excinfo.value)


def test_model_for_falls_back_to_the_default_when_an_agent_declares_none(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nmodel = "ollama:qwen3:32b"')
    config = _load(tmp_path, body)
    config.model = "ollama:qwen3:8b"
    assert config.model_for("researcher") == "ollama:qwen3:32b"
    assert config.model_for("assistant") == "ollama:qwen3:8b"


def test_an_agent_declares_its_own_thinking_level(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nthinking = "high"')
    config = _load(tmp_path, body)
    assert config.agents["researcher"].thinking == "high"
    assert config.agents["assistant"].thinking is None


def test_an_agent_can_declare_thinking_off(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nthinking = false')
    config = _load(tmp_path, body)
    assert config.agents["researcher"].thinking is False


def test_an_agents_thinking_must_be_a_known_level(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nthinking = "xhigh"')
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, body)
    assert "[agents.researcher].thinking" in str(excinfo.value)
    assert "xhigh" in str(excinfo.value)


def test_an_agents_thinking_rejects_a_number(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nthinking = 3')
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, body)
    assert "[agents.researcher].thinking" in str(excinfo.value)


def test_thinking_for_falls_back_to_the_default_when_an_agent_declares_none(tmp_path):
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nthinking = "low"')
    config = _load(tmp_path, body)
    config.thinking = "high"
    assert config.thinking_for("researcher") == "low"
    assert config.thinking_for("assistant") == "high"


def test_thinking_for_lets_an_agent_override_a_default_back_to_off(tmp_path):
    """``False`` is a real declaration, so resolution cannot be an ``or``-style truthiness test."""
    body = MINIMAL.replace('tools = ["web"]', 'tools = ["web"]\nthinking = false')
    config = _load(tmp_path, body)
    config.thinking = "high"
    assert config.thinking_for("researcher") is False
    assert config.thinking_for("assistant") == "high"


def test_thinking_for_is_none_when_nothing_is_declared_anywhere(tmp_path):
    config = _load(tmp_path, MINIMAL)
    assert config.thinking_for("assistant") is None
    assert config.thinking_for("researcher") is None


GENERATION = """
[assistant]
agent = "assistant"

[assistant.generation]
temperature = 0.7
context_length = 32768

[agents.assistant]
tools = ["memory"]
delegates_to = ["researcher"]

[agents.researcher]
tools = ["web"]

[agents.researcher.generation]
temperature = 0.2
"""


def test_an_agents_generation_table_loads_onto_its_agent(tmp_path):
    config = _load(tmp_path, GENERATION)
    assert config.generation == {"temperature": 0.7, "context_length": 32768}
    assert config.agents["researcher"].generation == {"temperature": 0.2}
    assert config.agents["assistant"].generation == {}


def test_an_out_of_range_value_in_an_agents_table_names_that_table(tmp_path):
    body = GENERATION.replace("temperature = 0.2", "temperature = 9.0")
    with pytest.raises(ConfigError, match=r"\[agents.researcher.generation\].temperature"):
        _load(tmp_path, body)


def test_an_unknown_key_in_an_agents_generation_table_is_refused(tmp_path):
    body = GENERATION.replace("temperature = 0.2", "tempurature = 0.2")
    with pytest.raises(ConfigError, match="tempurature"):
        _load(tmp_path, body)


def test_generation_for_merges_an_agents_table_over_the_default(tmp_path):
    config = _load(tmp_path, GENERATION)
    assert config.generation_for("researcher") == {"temperature": 0.2, "context_length": 32768}


def test_generation_for_gives_an_undeclared_agent_the_default(tmp_path):
    config = _load(tmp_path, GENERATION)
    assert config.generation_for("assistant") == {"temperature": 0.7, "context_length": 32768}


def test_generation_for_is_empty_when_nothing_is_declared_anywhere(tmp_path):
    """Empty is the normal case, and what leaves a model card's own profile in force."""
    config = _load(tmp_path, MINIMAL)
    assert config.generation_for("assistant") == {}


def test_generation_for_does_not_inherit_from_a_delegator(tmp_path):
    """Per agent, like the model and the effort: a delegator's tuning must not follow its workers."""
    body = GENERATION.replace(
        'delegates_to = ["researcher"]\n',
        'delegates_to = ["researcher"]\n\n[agents.assistant.generation]\ntemperature = 1.5\n',
    )
    config = _load(tmp_path, body)
    assert config.generation_for("assistant")["temperature"] == 1.5
    assert config.generation_for("researcher")["temperature"] == 0.2


def test_generation_for_returns_a_fresh_dict(tmp_path):
    """The caller assigns it to a live client, which may then mutate its own defaults."""
    config = _load(tmp_path, GENERATION)
    config.generation_for("researcher")["temperature"] = 1.9
    assert config.generation == {"temperature": 0.7, "context_length": 32768}
