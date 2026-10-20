"""An agent's prompt is assembled from what it declares, so no sentence in it can go stale."""

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.core.agents import assemble_system_message, build_registry
from kokua.registry.registry import Toolset, select


def _assemble(agents, name="assistant", **config_kwargs):
    config = AssistantConfig(agents=agents, entry_agent="assistant", **config_kwargs)
    registry = build_registry(config)
    toolsets = select(agents[name].available, registry, agent=name, entry_point="assistant")
    return assemble_system_message(config, name, toolsets)


def test_the_declared_message_comes_first():
    agents = {"assistant": AgentConfig(system_message="Custom opener.", available=["memory"])}
    assert _assemble(agents).startswith("Custom opener.")


def test_an_agent_with_no_message_of_its_own_falls_back_to_the_global_one():
    """[assistant].system_message is the opener for an agent that declares none of its own, so setting it
    is not silently ignored."""
    agents = {"assistant": AgentConfig(available=["time"])}
    assert _assemble(agents, system_message="Global opener.").startswith("Global opener.")


def test_an_agents_own_message_wins_over_the_global_one():
    agents = {"assistant": AgentConfig(system_message="Mine.", available=["time"])}
    assert _assemble(agents, system_message="Global opener.").startswith("Mine.")


def test_system_override_wins_over_the_entry_agents_declared_opener():
    """--system (system_message_override) beats even a declared system_message for the entry agent. That
    is the exact case config.example.toml's shipped [agents.assistant] puts every default install in,
    which is what made --system a silent no-op before this."""
    agents = {"assistant": AgentConfig(system_message="Declared opener.", available=["time"])}
    assert _assemble(agents, system_message_override="Be terse.").startswith("Be terse.")


def test_system_override_beats_the_global_fallback_too():
    agents = {"assistant": AgentConfig(available=["time"])}
    message = _assemble(agents, system_message="Global opener.", system_message_override="Be terse.")
    assert message.startswith("Be terse.")
    assert "Global opener." not in message


def test_an_unset_system_override_leaves_the_declaration_in_charge():
    """The default (None) must not outrank a declared opener: system_message_override is None exactly
    when --system was never passed, which is a different question from system_message merely sitting at
    its own built-in default -- confusing the two is the bug this field exists to avoid repeating."""
    agents = {"assistant": AgentConfig(system_message="Declared opener.", available=["time"])}
    assert _assemble(agents).startswith("Declared opener.")
    assert _assemble(agents, system_message_override=None).startswith("Declared opener.")


def test_system_override_does_not_reach_a_worker():
    """The flag documents itself as overriding the entry agent's message, the one the user is talking to,
    not every agent Kokua builds; a worker's own declared opener must survive it untouched."""
    agents = {
        "assistant": AgentConfig(available=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(system_message="Researcher opener.", available=["web"]),
    }
    message = _assemble(agents, name="researcher", system_message_override="Be terse.")
    assert message.startswith("Researcher opener.")
    assert "Be terse." not in message


def test_a_toolsets_guidance_is_appended():
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["memory"])}
    assert "store_memory" in _assemble(agents)


def test_guidance_follows_declared_order():
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["documents", "memory"])}
    message = _assemble(agents)
    assert message.index("save_document") < message.index("store_memory")


def test_a_toolset_declared_twice_contributes_guidance_once():
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["memory", "memory"])}
    assert _assemble(agents).count("store_memory") == 1


def test_an_undeclared_toolsets_guidance_is_absent():
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["time"])}
    assert "store_memory" not in _assemble(agents)


def test_a_non_delegating_agent_gets_no_delegation_guidance():
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["time"])}
    assert "spawn_subagent" not in _assemble(agents)


def test_a_delegating_agent_gets_the_mechanism():
    """The full call shape, not just the bare name: the parameter names are what tell the model how to
    call the tool, so a weaker substring check could pass while the call shape had silently changed."""
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time"], delegates_to=["researcher"]),
        "researcher": AgentConfig(available=["web"]),
    }
    assert "spawn_subagent(agent_type, task)" in _assemble(agents)


def test_a_lean_delegator_is_told_it_has_almost_no_direct_tools():
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "memory"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    assert "almost no direct tools" in _assemble(agents)


def test_a_lean_delegator_is_told_it_is_a_lean_supervisor():
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "memory"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    assert "lean supervisor" in _assemble(agents)


def test_a_delegator_holding_a_domain_toolset_is_not():
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "fs"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    message = _assemble(agents)
    assert "spawn_subagent" in message
    assert "almost no direct tools" not in message


def test_a_delegator_holding_a_domain_toolset_is_not_called_a_lean_supervisor():
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "fs"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    assert "lean supervisor" not in _assemble(agents)


def test_any_delegating_agent_is_told_to_answer_trivia_itself():
    """Unconditional: over-delegating a greeting is the mirror problem the lean clause exists to
    prevent for domain work, so the instruction cannot be gated on cross_cutting the way that clause is."""
    lean = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "memory"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    tool_heavy = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "fs"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    assert "directly with your own tools" in _assemble(lean)
    assert "directly with your own tools" in _assemble(tool_heavy)


def test_the_stale_tool_enumeration_never_appears():
    """The old hardcoded list of the entry agent's cross-cutting tools ("date/time, memory, skills,
    config, scheduling, MCP management, reading past conversations") is gone for good: it is a
    hand-maintained copy of the declared toolset, exactly the kind of sentence this design exists to
    stop writing. A future edit that "restores" it for symmetry with the old prompt would be a
    regression, not a fix. Two distinct phrases from that list are checked (not just one) so a
    reintroduction that rewords the enumeration, rather than copying it verbatim, is still caught."""
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "memory"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    message = _assemble(agents)
    assert "MCP management" not in message
    assert "reading past conversations" not in message


def test_the_default_opener_is_used_when_an_agent_declares_none():
    agents = {"assistant": AgentConfig(available=["time"])}
    assert "personal assistant" in _assemble(agents)


def test_a_third_party_toolsets_guidance_reaches_the_prompt(monkeypatch):
    from kokua.core import agents as agents_module

    weather = Toolset(
        name="weather",
        description="Forecasts.",
        build=lambda ctx: [],
        guidance=" Call `forecast` for weather.",
    )
    monkeypatch.setattr(agents_module, "discover_toolsets", lambda: {"weather": weather})
    config = AssistantConfig(
        agents={"assistant": AgentConfig(system_message="Opener.", available=["weather"])},
        entry_agent="assistant",
    )
    registry = build_registry(config)
    toolsets = select(["weather"], registry, agent="assistant", entry_point="assistant")
    assert "forecast" in assemble_system_message(config, "assistant", toolsets)


def test_any_delegating_agent_is_told_what_it_cannot_answer_from_memory():
    """The lean clause names activities ("web research"), which only helps once the model has already
    decided the question needs the web. A question it believes it knows the answer to never gets that
    far, so the trigger has to be epistemic (could the answer have changed, could the user check it)
    rather than an activity. Unconditional for the same reason the trivia clause is: a tool-heavy
    delegator answers a stale question from memory exactly as readily as a lean one."""
    lean = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "memory"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    tool_heavy = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "fs"], delegates_to=["r"]),
        "r": AgentConfig(available=["web"]),
    }
    for message in (_assemble(lean), _assemble(tool_heavy)):
        assert "could have changed since you were trained" in message
        assert "even when you think you know" in message


def test_an_agent_holding_web_itself_is_told_to_retrieve_rather_than_recall():
    """The two halves partition: the delegation clause reaches an agent that must hand the work to a
    worker, and the `web` toolset's own guidance reaches whoever holds the tools, worker or not. An
    agent with web and no delegates is the case only the second half covers."""
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["web"])}
    message = _assemble(agents)
    assert "look it up" in message
    assert "spawn_subagent" not in message


def test_an_agent_holding_compute_is_told_to_calculate_rather_than_estimate():
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["compute"])}
    assert "rather than in prose" in _assemble(agents)


def test_an_agent_holding_the_clock_is_told_not_to_trust_its_own():
    agents = {"assistant": AgentConfig(system_message="Opener.", available=["time"])}
    message = _assemble(agents)
    assert "`get_current_date_and_time` before anything" in message
    assert "rather than adding or subtracting an offset" in message


def test_a_lean_delegator_is_told_to_delegate_arithmetic():
    """The delegating half of `compute`'s guidance: "running code" does not reach a model writing a
    conversion into its reply, since it does not think of that as running code."""
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "memory"], delegates_to=["c"]),
        "c": AgentConfig(available=["compute"]),
    }
    assert "That includes arithmetic" in _assemble(agents)


def test_a_delegator_holding_compute_is_not_told_to_delegate_arithmetic():
    """It holds the tools, so its own `compute` guidance applies, and the two must not contradict."""
    agents = {
        "assistant": AgentConfig(system_message="Opener.", available=["time", "compute"], delegates_to=["c"]),
        "c": AgentConfig(available=["compute"]),
    }
    message = _assemble(agents)
    assert "rather than in prose" in message
    assert "That includes arithmetic" not in message
