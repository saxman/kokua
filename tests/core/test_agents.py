"""The registry assembled from every provider, and one agent's tools resolved from it."""

import pytest

from kokua.config.file import ConfigError
from kokua.config.schema import ALL_TOOLSETS, AgentConfig, AssistantConfig
from kokua.core.agents import build_registry, validate_agents
from kokua.registry.registry import ToolsetError, register


def test_registry_contains_every_provider():
    registry = build_registry(AssistantConfig())
    for name in (
        "web",
        "time",
        "memory",
        "documents",
        "skills",
        "config",
        "mcp",
        "scheduling",
        "conversations",
        "aimu_agents",
        "github_backup",
        "image",
    ):
        assert name in registry, name


def test_kokuas_own_plugins_are_labeled_built_in_not_plugin():
    """The toolsets Kokua's own distribution registers under `kokua.toolsets` are told apart from a
    third party's by provenance (which distribution registered the entry point), not a hand-maintained
    name list, so `TRUST_ORDER` can rank them ahead of a third party's without knowing their names."""
    registry = build_registry(AssistantConfig())
    for name in ("aimu_agents", "image"):
        assert registry.providers[name] == "built-in toolset", name


def test_a_third_party_plugin_is_still_labeled_plugin(monkeypatch):
    from kokua.core import agents
    from kokua.registry.registry import Toolset

    weather = Toolset(name="weather", description="Forecasts.", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"weather": weather})
    registry = build_registry(AssistantConfig())
    assert registry.providers["weather"] == "plugin"


def test_a_third_party_shadowing_a_shipped_name_is_rejected(monkeypatch):
    """Every toolset arrives through one entry-point group now, so a third party's package registering a
    name Kokua ships is a collision inside that group. The two are still told apart by which
    distribution registered them, which is what lets the message name each side, and it is the only
    thing that split is for."""
    from kokua.core import agents
    from kokua.registry.registry import Toolset

    shipped = Toolset(name="memory", description="Kokua's own.", build=lambda ctx: [])
    # Registered under a different entry-point key, which is not what collides: `register` keys on
    # TOOLSET.name, so this is a clash even though nothing in either table is spelled the same.
    theirs = Toolset(name="memory", description="A third party's.", build=lambda ctx: [])
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"memory": shipped, "their-key": theirs})
    monkeypatch.setattr(agents, "own_distribution_toolset_names", lambda: {"memory"})

    with pytest.raises(ToolsetError) as excinfo:
        build_registry(AssistantConfig())

    message = str(excinfo.value)
    assert "memory" in message
    assert "built-in toolset" in message
    assert "plugin" in message


def test_a_toolset_that_fails_to_build_raises(monkeypatch):
    """No source is tolerated, because there is no source this codebase is willing to be quiet about.
    A build failure is a bug in whoever wrote the toolset, and a warning in a log file is not how a
    person finds out an agent silently lost a capability."""
    from kokua.core import agents
    from kokua.registry.context import LiveState, ToolsetContext
    from kokua.registry.registry import Toolset

    def _boom(ctx):
        raise RuntimeError("boom")

    broken = Toolset(name="broken", description="broken", build=_boom)
    monkeypatch.setattr(agents, "discover_toolsets", lambda: {"broken": broken})

    registry = build_registry(AssistantConfig())
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig()), agent=None, agent_name="assistant")
    with pytest.raises(RuntimeError, match="boom"):
        registry["broken"].build(ctx)


def test_no_toolset_is_wrapped_in_the_registry():
    """Every toolset in the registry carries the ``build`` its author wrote, whatever route it arrived
    by. The registry used to substitute a swallowing wrapper for entry-point toolsets, which meant a bug
    in one of Kokua's own became a log line while the same bug elsewhere took startup down."""
    from kokua.plugins import discover_toolsets

    registry = build_registry(AssistantConfig())
    for source in discover_toolsets().values():
        assert registry[source.name].build is source.build, source.name


def _config(agents, entry="assistant") -> AssistantConfig:
    return AssistantConfig(agents=agents, entry_agent=entry)


def _valid() -> dict:
    return {
        "assistant": AgentConfig(available=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(available=["web"]),
    }


def test_a_valid_graph_passes():
    config = _config(_valid())
    validate_agents(config, build_registry(config))


def test_no_agents_at_all_is_rejected():
    """Reachable only when config.toml already exists (config/file.py's own missing-file error fires
    first otherwise), so plain `kokua config init` would refuse to overwrite it -- the message must name
    a remedy that actually works from that state, not the one that requires no file to be there yet."""
    config = _config({})
    with pytest.raises(ConfigError, match="at least one") as excinfo:
        validate_agents(config, build_registry(config))
    message = str(excinfo.value)
    assert "config.example.toml" in message
    assert "kokua config init --force" in message


def test_an_agent_named_for_a_bus_address_is_rejected():
    """An agent's declared name is the address a mid-turn message is sent to, and these two are
    already spoken for. ``user`` is the sharper one: an entry agent declared under it mints the user's
    own address, and ``MessageBus.close`` would then re-run that agent's messages as the user's words,
    which is impersonation by configuration. ``everyone`` can never be addressed alone, because
    ``matches`` answers the broadcast before it looks at an address. Both fail at startup rather than
    silently at reader-open, since the collision is in the config and invisible everywhere else.
    """
    for reserved in ("user", "everyone"):
        agents = _valid()
        agents[reserved] = AgentConfig(available=["time"])
        config = _config(agents)
        with pytest.raises(ConfigError, match="reserved") as excinfo:
            validate_agents(config, build_registry(config))
        assert reserved in str(excinfo.value)


def test_an_agent_name_cannot_collide_with_a_gate_name():
    """A composed worker is gated as 'composed:<label>' and the default tier as '[assistant.tools]'; a
    hand-written quoted table with either shape would share that name, so both characters are refused."""
    for reserved in ("composed:coder", "[assistant.tools]"):
        agents = _valid()
        agents[reserved] = AgentConfig(available=["time"])
        config = _config(agents)
        with pytest.raises(ConfigError, match="reserved for the approval gate") as excinfo:
            validate_agents(config, build_registry(config))
        assert reserved in str(excinfo.value)


def test_other_quoted_agent_names_stay_legal():
    agents = _valid()
    agents["my-agent.v2"] = AgentConfig(available=["time"])
    config = _config(agents)
    validate_agents(config, build_registry(config))


def test_a_missing_entry_agent_is_rejected():
    config = _config(_valid(), entry="supervisor")
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "supervisor" in str(excinfo.value)


def test_an_unknown_delegation_target_is_rejected():
    agents = _valid()
    agents["assistant"] = AgentConfig(available=["time"], delegates_to=["reasercher"])
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "reasercher" in str(excinfo.value)


def test_a_delegation_cycle_is_rejected_and_named():
    agents = {
        "assistant": AgentConfig(delegates_to=["a"]),
        "a": AgentConfig(delegates_to=["b"]),
        "b": AgentConfig(delegates_to=["a"]),
    }
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    message = str(excinfo.value)
    assert "a" in message and "b" in message
    assert "cycle" in message.lower()


def test_an_agent_delegating_to_itself_is_rejected():
    config = _config({"assistant": AgentConfig(delegates_to=["assistant"])})
    with pytest.raises(ConfigError, match="cycle"):
        validate_agents(config, build_registry(config))


def test_an_unknown_toolset_name_is_rejected_during_validation():
    config = _config({"assistant": AgentConfig(available=["memry"])})
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "memry" in str(excinfo.value)


def test_skills_on_a_worker_is_rejected_during_validation():
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(available=["skills"]),
    }
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "skills" in str(excinfo.value)


def test_a_worker_model_may_carry_an_endpoint_override():
    """A worker's model accepts the same extended string ``[assistant].model`` does.

    The two are validated by different code (this one parses, ``[assistant].model`` builds a
    throwaway client), so they can drift apart, and a validator that understood only
    ``provider:model_id`` would refuse an endpoint the entry agent runs on happily: pinning a
    worker to the same host the assistant uses would fail at startup.
    """
    agents = {
        "assistant": AgentConfig(available=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(available=["web"], model="ollama:qwen3.5:9b@http://example.local:11434"),
    }
    config = _config(agents)
    validate_agents(config, build_registry(config))


def test_an_unresolvable_worker_model_is_still_rejected():
    """The endpoint grammar widens what parses, not what passes: a bad id fails as before."""
    agents = {
        "assistant": AgentConfig(available=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(available=["web"], model="ollama:no-such-model@http://example.local:11434"),
    }
    config = _config(agents)
    with pytest.raises(ConfigError) as excinfo:
        validate_agents(config, build_registry(config))
    assert "researcher" in str(excinfo.value)


def test_a_configured_section_for_an_undeclared_toolset_is_reported():
    from tests.channels import example_agents
    from kokua.core.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].available = ["time"]
    config = AssistantConfig(agents=agents, entry_agent="assistant", configured_sections=("planning",))

    assert configured_but_undeclared(config, build_registry(config)) == ["planning"]


def test_a_configured_section_for_a_declared_toolset_is_not_reported():
    from tests.channels import example_agents
    from kokua.core.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].available = ["planning"]
    config = AssistantConfig(agents=agents, entry_agent="assistant", configured_sections=("planning",))

    assert configured_but_undeclared(config, build_registry(config)) == []


def test_a_defaulted_section_the_file_never_had_is_not_reported():
    """`toolset_settings` carries a bucket for every declared setting once seeding has run, so a
    section that was only ever defaulted (never in configured_sections) must not be reported even
    though it appears here."""
    from tests.channels import example_agents
    from kokua.core.agents import configured_but_undeclared

    agents = example_agents()
    agents["assistant"].available = ["time"]
    config = AssistantConfig(
        agents=agents, entry_agent="assistant", toolset_settings={"planning": {"plan_review": False}}
    )

    assert configured_but_undeclared(config, build_registry(config)) == []


def test_a_diamond_shaped_graph_passes():
    """Two agents delegating to a shared target is not a cycle: the DFS must mark a finished node
    ``done`` so revisiting it through a second path does not re-walk it or read as a cycle."""
    agents = {
        "assistant": AgentConfig(delegates_to=["a", "b"]),
        "a": AgentConfig(delegates_to=["shared"]),
        "b": AgentConfig(delegates_to=["shared"]),
        "shared": AgentConfig(),
    }
    config = _config(agents)
    validate_agents(config, build_registry(config))


def test_approval_note_wording():
    from kokua.core.agents import approval_note

    assert approval_note(["compute.run_command"], ["compute.execute_python"], "compute") == (
        "Asks first: execute_python, run_command."
    )
    assert approval_note(["fs_write"], [], "fs_write") == "Asks first: every tool."
    assert approval_note(["fs_write.*"], [], "fs_write") == "Asks first: every tool."
    assert approval_note(["fs_write"], [], "web") == ""
    assert approval_note([], [], "web") == ""


def test_lean_delegation_hint_considers_deferred_toolsets():
    from kokua.core.agents import LEAN_DELEGATION_GUIDANCE, assemble_system_message

    registry = build_registry(AssistantConfig())
    config = AssistantConfig(
        agents={"lead": AgentConfig(available=["time", "web"], resident=["time"], delegates_to=["helper"])},
        entry_agent="lead",
    )
    resident, deferred = [registry["time"]], [registry["web"]]
    assert LEAN_DELEGATION_GUIDANCE not in assemble_system_message(config, "lead", resident, deferred)
    # Judged over the resident toolsets alone it would read as a lean supervisor, which is the mistake
    # a deferred domain toolset must not allow.
    assert LEAN_DELEGATION_GUIDANCE in assemble_system_message(config, "lead", resident)


# --- available = "all": every installed toolset, expanded in trust order ------------------------------


def _all_config(monkeypatch, agents) -> tuple[AssistantConfig, object]:
    """``agents`` over a registry holding all four providers: Kokua's own toolsets, a third-party plugin
    ``stocks_plugin``, a skill ``weekly-digest``, and an MCP server ``files``."""
    from kokua.config import MCPServerConfig
    from kokua.core import agents as core_agents
    from kokua.plugins import discover_toolsets
    from kokua.registry.registry import Toolset

    installed = discover_toolsets()
    stocks = Toolset(name="stocks_plugin", description="Quotes.", build=lambda ctx: [])
    monkeypatch.setattr(core_agents, "discover_toolsets", lambda: {**installed, "stocks_plugin": stocks})
    config = AssistantConfig(
        agents=agents,
        entry_agent="assistant",
        mcp_servers=[MCPServerConfig(url="https://files.example/mcp", name="files")],
    )
    skill = config.skills_dir / "weekly-digest"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: weekly-digest\ndescription: Digest the week.\n---\n\nDo the thing.\n", encoding="utf-8"
    )
    return config, build_registry(config)


def _explorer(**overrides) -> dict:
    explorer = {"available": ALL_TOOLSETS, "resident": ["time"], **overrides}
    return {
        "assistant": AgentConfig(available=["time"], delegates_to=["explorer"]),
        "explorer": AgentConfig(**explorer),
    }


def test_all_expands_in_trust_order(monkeypatch):
    from kokua.core.agents import TRUST_ORDER, available_for

    config, registry = _all_config(monkeypatch, _explorer())
    expanded = available_for(config, registry, "explorer")

    assert registry.providers[expanded[0]] == "built-in toolset"
    assert expanded[-3:] == ["stocks_plugin", "weekly-digest", "files"]
    ranks = [TRUST_ORDER.index(registry.providers[name]) for name in expanded]
    assert ranks == sorted(ranks)
    # Within a rank, registration order is kept rather than re-sorted.
    own = [name for name in registry if registry.providers[name] == "built-in toolset" and name != "skills"]
    assert [name for name in expanded if registry.providers[name] == "built-in toolset"] == own


def test_every_provider_label_build_registry_registers_has_a_trust_rank(monkeypatch):
    """A new provider label must be ranked on purpose; without this it would silently sort last."""
    from kokua.core import agents as core_agents
    from kokua.core.agents import TRUST_ORDER

    labels: list[str] = []

    def recording_register(sources):
        labels.extend(label for label, _ in sources)
        return register(sources)

    monkeypatch.setattr(core_agents, "register", recording_register)
    build_registry(AssistantConfig())
    assert set(labels) == set(TRUST_ORDER)


def test_all_skips_skills_on_a_worker_and_keeps_it_on_the_entry_agent(monkeypatch):
    from kokua.core.agents import available_for

    agents = _explorer()
    agents["assistant"] = AgentConfig(available=ALL_TOOLSETS, resident=["time"], delegates_to=["explorer"])
    config, registry = _all_config(monkeypatch, agents)
    assert "skills" not in available_for(config, registry, "explorer")
    assert "skills" in available_for(config, registry, config.entry_agent)


def test_a_list_declaration_is_returned_as_written(monkeypatch):
    from kokua.core.agents import available_for

    config, registry = _all_config(monkeypatch, _explorer(available=["files", "time"], resident=None))
    assert available_for(config, registry, "explorer") == ["files", "time"]


def test_resident_and_deferred_follow_the_expansion_order(monkeypatch):
    from kokua.core.agents import available_for, deferred_for, resident_for

    config, registry = _all_config(monkeypatch, _explorer(resident=["files", "time"]))
    expanded = available_for(config, registry, "explorer")
    assert resident_for(config, registry, "explorer") == ["time", "files"]
    assert deferred_for(config, registry, "explorer") == [name for name in expanded if name not in ("time", "files")]


def test_resident_absent_means_everything_resident():
    from kokua.core.agents import deferred_for, resident_for

    config = AssistantConfig(agents={"a": AgentConfig(available=["memory", "web"])})
    registry = build_registry(config)
    assert resident_for(config, registry, "a") == ["memory", "web"]
    assert deferred_for(config, registry, "a") == []


def test_resident_order_never_reorders_tools():
    from kokua.core.agents import deferred_for, resident_for

    config = AssistantConfig(
        agents={"a": AgentConfig(available=["memory", "web", "time"], resident=["time", "memory"])}
    )
    registry = build_registry(config)
    assert resident_for(config, registry, "a") == ["memory", "time"]
    assert deferred_for(config, registry, "a") == ["web"]


def test_a_resident_name_outside_the_expansion_fails_startup(monkeypatch):
    config, registry = _all_config(monkeypatch, _explorer(resident=["skills"]))
    with pytest.raises(ConfigError, match=r"resident names 'skills'.*only works on the entry point"):
        validate_agents(config, registry)


def test_a_resident_name_nothing_installs_fails_startup(monkeypatch):
    config, registry = _all_config(monkeypatch, _explorer(resident=["nosuch"]))
    with pytest.raises(ConfigError, match=r"resident names 'nosuch', which is not an installed toolset"):
        validate_agents(config, registry)


def test_all_without_resident_fails_validation(monkeypatch):
    """The parser refuses this shape, but a config built without it (``update_config``'s dry run) has
    only validation to stop it."""
    config, registry = _all_config(monkeypatch, _explorer(resident=None))
    with pytest.raises(ConfigError, match='available = "all" needs a resident list'):
        validate_agents(config, registry)


def test_an_all_entry_agent_offers_and_declares_every_workflow_command(monkeypatch):
    from kokua.core.agents import build_command_map, undeclared_workflow_commands

    agents = {"assistant": AgentConfig(available=ALL_TOOLSETS, resident=["time"])}
    config, registry = _all_config(monkeypatch, agents)
    assert "plan" in build_command_map(config, registry)
    assert undeclared_workflow_commands(config, registry) == {}


def test_an_all_agent_declares_every_configured_section(monkeypatch):
    from kokua.core.agents import configured_but_undeclared

    config, registry = _all_config(monkeypatch, _explorer())
    config.configured_sections = ("planning",)
    assert configured_but_undeclared(config, registry) == []


def test_shadowed_tools_names_the_winner_and_the_loser(monkeypatch):
    from kokua.core.agents import ShadowedTool, shadowed_tools

    config, registry = _all_config(monkeypatch, _explorer(resident=["time", "files"]))
    tools = {"time": {"read_file"}, "files": {"read_file", "grep"}}

    assert shadowed_tools(config, registry, tools) == [ShadowedTool("explorer", "read_file", "time", "files")]


def test_shadowed_tools_ignores_a_skill_on_the_entry_agent(monkeypatch):
    from kokua.core.agents import shadowed_tools

    agents = _explorer()
    agents["assistant"] = AgentConfig(available=ALL_TOOLSETS, resident=["time", "weekly-digest"])
    config, registry = _all_config(monkeypatch, agents)

    assert shadowed_tools(config, registry, {"time": {"now"}, "weekly-digest": {"now"}}) == []


@pytest.mark.parametrize("declaration", ["list", "all"])
def test_shadowed_tools_ignores_the_activate_skill_every_skill_carries(tmp_path, declaration):
    """Every skill's build includes the one shared ``activate_skill``, so nothing is lost when a second
    resident skill offers it again."""
    from kokua.core.agents import shadowed_tools
    from tests.core.test_skill_toolsets import write_skill

    skills = ["weekly-digest", "citation-check"]
    explorer = (
        AgentConfig(available=skills) if declaration == "list" else AgentConfig(available=ALL_TOOLSETS, resident=skills)
    )
    agents = {"assistant": AgentConfig(available=["time"], delegates_to=["explorer"]), "explorer": explorer}
    config = AssistantConfig(data_dir=tmp_path, agents=agents, entry_agent="assistant")
    write_skill(config, "weekly-digest", "Digest the week.", script="print(1)\n")
    write_skill(config, "citation-check", "Check a citation.", script="print(2)\n")
    tools = {
        "weekly-digest": {"activate_skill", "weekly_digest__run"},
        "citation-check": {"activate_skill", "citation_check__run"},
    }

    assert shadowed_tools(config, build_registry(config), tools) == []


def _labelling_state(tmp_path, agents, tools_by_toolset):
    from kokua.registry.context import LiveState

    config = AssistantConfig(data_dir=tmp_path, agents=agents, entry_agent="assistant")
    state = LiveState(config=config, registry=build_registry(config))
    state.tools_by_toolset = tools_by_toolset
    return state


def test_toolset_of_answers_from_the_calling_agents_own_toolsets(tmp_path):
    """Two toolsets offering one name is the case a global lookup gets wrong: each agent is answered
    from what it declared, in the order its tools were built, so the toolset that won is the one named."""
    from kokua.core.agents import toolset_of

    agents = {
        "assistant": AgentConfig(available=["time", "compute"], delegates_to=["explorer"]),
        "explorer": AgentConfig(available=["compute"]),
    }
    state = _labelling_state(tmp_path, agents, {"time": {"now", "clash"}, "compute": {"calculate", "clash"}})

    assert toolset_of(state, "assistant", "calculate") == "compute"
    assert toolset_of(state, "assistant", "clash") == "time"
    assert toolset_of(state, "explorer", "clash") == "compute"


@pytest.mark.parametrize("tool", ["spawn_subagent", "activate_skill", "available_tools", "load_tools"])
def test_toolset_of_puts_the_tools_no_toolset_provides_under_core(tmp_path, tool):
    from kokua.core.agents import toolset_of

    state = _labelling_state(tmp_path, {"assistant": AgentConfig(available=["time"])}, {"time": {"now"}})
    assert toolset_of(state, "assistant", tool) == "core"


def test_toolset_of_names_a_skill_for_its_scripts(tmp_path):
    """The entry agent's SkillAgent holds a skill's scripts without any build recording them."""
    from kokua.core.agents import toolset_of
    from tests.core.test_skill_toolsets import write_skill

    state = _labelling_state(tmp_path, {"assistant": AgentConfig(available=["time"])}, {"time": {"now"}})
    write_skill(state.config, "weekly-digest", "Digest the week.", script="print(1)\n")
    assert toolset_of(state, "assistant", "weekly_digest__run") == "weekly-digest"


def test_toolset_of_falls_back_to_every_toolset_for_an_agent_the_config_does_not_describe(tmp_path):
    """A composed worker or a critic has no declaration to search, so every built toolset is."""
    from kokua.core.agents import toolset_of

    state = _labelling_state(tmp_path, {"assistant": AgentConfig(available=["time"])}, {"web": {"get_web_content"}})
    assert toolset_of(state, "composed:reader", "get_web_content") == "web"
    assert toolset_of(state, None, "get_web_content") == "web"


def test_toolset_of_is_none_for_a_name_nothing_recorded(tmp_path):
    """Shown bare rather than under a guess."""
    from kokua.core.agents import toolset_of

    state = _labelling_state(tmp_path, {"assistant": AgentConfig(available=["time"])}, {"time": {"now"}})
    assert toolset_of(state, "assistant", "mystery") is None
