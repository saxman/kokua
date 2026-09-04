"""Kokua owns the delegation topology: each agent's menu is its own targets, and a target that
delegates carries its own delegate rather than inheriting the parent's menu."""

from aimu.tools import builtin

from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.core.agents import build_agent_specs, build_registry
from kokua.registry.context import LiveState

SPAWN = "spawn_subagent"


def _state(tmp_path, agents, entry="assistant") -> tuple[AssistantConfig, LiveState]:
    config = AssistantConfig(data_dir=tmp_path, agents=agents, entry_agent=entry)
    state = LiveState(config=config, registry=build_registry(config))
    return config, state


def test_a_spec_is_produced_for_each_target(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher", "coder"]),
        "researcher": AgentConfig(tools=["web"]),
        "coder": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    specs = build_agent_specs(config, state, "assistant")
    assert sorted(specs) == ["coder", "researcher"]


def test_a_spec_carries_only_its_own_declared_tools(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["researcher"]["tools"]}
    assert names == {fn.__name__ for fn in builtin.web}
    assert SPAWN not in names


def test_a_spec_description_opens_its_system_message(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(description="Research specialist.", tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    spec = build_agent_specs(config, state, "assistant")["researcher"]
    assert spec["system_message"].startswith("Research specialist.")


def test_a_target_that_delegates_gets_its_own_delegate(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["lead"]),
        "lead": AgentConfig(tools=["time"], delegates_to=["helper"]),
        "helper": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["lead"]["tools"]}
    assert SPAWN in names


def test_a_leaf_target_gets_no_delegate(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["helper"]),
        "helper": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["helper"]["tools"]}
    assert SPAWN not in names


def test_an_agent_with_no_targets_produces_no_specs(tmp_path):
    config, state = _state(tmp_path, {"assistant": AgentConfig(tools=["time"])})
    assert build_agent_specs(config, state, "assistant") == {}


def test_a_nested_spawn_tool_offers_only_the_childs_own_targets(tmp_path):
    """The whole point of Kokua owning the topology: a nested spawn tool's menu is the child's own
    delegates_to, never the parent's menu it was built under."""
    agents = {
        "assistant": AgentConfig(delegates_to=["lead"]),
        "lead": AgentConfig(tools=["time"], delegates_to=["helper", "other"]),
        "helper": AgentConfig(tools=["fs"]),
        "other": AgentConfig(tools=["fs"]),
    }
    config, state = _state(tmp_path, agents)
    lead_tools = build_agent_specs(config, state, "assistant")["lead"]["tools"]
    spawn = next(fn for fn in lead_tools if fn.__name__ == SPAWN)
    assert "helper" in spawn.__doc__
    assert "other" in spawn.__doc__
    assert "lead" not in spawn.__doc__


def test_a_spec_carries_the_model_its_agent_declares(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], model="ollama:qwen3:32b"),
    }
    config, state = _state(tmp_path, agents)
    assert build_agent_specs(config, state, "assistant")["researcher"]["model"] == "ollama:qwen3:32b"


def test_a_spec_declaring_no_model_carries_none_so_the_spawn_default_applies(tmp_path):
    """AIMU reads a missing spec ``model`` as "use the tool's own model", which is the default one the
    delegate was built with. A worker therefore inherits the default rather than its delegator's pin."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"], model="ollama:qwen3:32b"),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    assert "model" not in build_agent_specs(config, state, "assistant")["researcher"]


class _EnumLikeModel:
    """What a live client actually reports: a catalogued id and nothing else.

    The fake client here used to hold a plain ``provider:model_id`` string, which is why the endpoint
    bug survived a test suite that covered this path. A real ``AsyncModelClient.model`` is a ``Model``
    enum member, and no ``@base_url`` reaches it. Shaped like one so a fallback that reads it cannot
    look correct here while dropping the endpoint in production.
    """

    def __init__(self, model_id):
        self.value = model_id

    def __str__(self):
        return f"OllamaModel.{self.value}"


class _FakeClient:
    def __init__(self, model):
        self.model = model


class _FakeAgent:
    """Enough of a live agent for ``make_delegation_tool``: a name and a client carrying a model."""

    def __init__(self, name, model):
        self.name = name
        self.model_client = _FakeClient(model)


def _captured_spawn_call(monkeypatch, config, state, agent) -> tuple[object, dict]:
    """The positional model and keyword arguments ``make_delegation_tool`` builds its spawn tool with.

    Shared by ``_captured_spawn_model`` and ``_captured_spawn_kwargs``: what a delegate is built with
    is not observable from the tool it returns, so capturing has to happen inside the patched factory.
    """
    from kokua.core import agents as agents_mod

    captured: dict = {"model": None, "kwargs": {}}

    def fake_make(model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """menu"""
            return "ok"

        spawn_subagent.__name__ = "spawn_subagent"
        return spawn_subagent

    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", fake_make)
    agents_mod.make_delegation_tool(agent, config, state)
    return captured["model"], captured["kwargs"]


def _captured_spawn_model(monkeypatch, config, state, agent) -> object:
    return _captured_spawn_call(monkeypatch, config, state, agent)[0]


def _captured_spawn_kwargs(monkeypatch, config, state, agent) -> dict:
    """The keyword arguments `make_delegation_tool` builds its spawn tool with.

    A sibling of ``_captured_spawn_model``, which captures the positional model. Both exist because
    what a delegate is built with is not observable from the tool it returns.
    """
    return _captured_spawn_call(monkeypatch, config, state, agent)[1]


def test_the_delegate_is_built_with_the_default_model_not_the_delegators_pin(tmp_path, monkeypatch):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"], model="ollama:qwen3:32b"),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    config.model = "ollama:qwen3:8b"
    agent = _FakeAgent("assistant", "ollama:qwen3:32b")
    assert _captured_spawn_model(monkeypatch, config, state, agent) == "ollama:qwen3:8b"


def test_an_unset_default_is_the_resolved_string_not_the_delegators_client(tmp_path, monkeypatch):
    """With no [assistant].model, the delegate is built with the string AIMU's default resolved to.

    The regression this pins: it used to be built with ``agent.model_client.model``, and a live
    client answers with a resolved ``Model`` enum. An enum names a catalogued id and carries nothing
    else, so a default like ``ollama:qwen3.8:27b@http://gpu-box:11434`` reached the spawn tool as
    ``qwen3.8:27b`` and every sub-agent was rebuilt against the provider default. The fake here keeps
    an enum-shaped model for that reason: nothing may read the endpoint back off a client, because on
    a real one it is not there to read.
    """
    from tests.conftest import TEST_DEFAULT_MODEL

    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    agent = _FakeAgent("assistant", _EnumLikeModel("qwen3.8:27b"))
    assert _captured_spawn_model(monkeypatch, config, state, agent) == TEST_DEFAULT_MODEL


def test_a_spec_carries_the_thinking_level_its_agent_declares(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], thinking="high"),
    }
    config, state = _state(tmp_path, agents)
    config.thinking = "low"
    assert build_agent_specs(config, state, "assistant")["researcher"]["thinking"] == "high"


def test_a_spec_carries_the_resolved_default_when_its_agent_declares_nothing(tmp_path):
    """Unlike ``model``, an omitted spec ``thinking`` is not a fallback AIMU can resolve: the spawn tool
    has no thinking tier, so a worker inheriting the default requires the resolved value written in."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    config.thinking = "medium"
    assert build_agent_specs(config, state, "assistant")["researcher"]["thinking"] == "medium"


def test_a_spec_carries_thinking_off_rather_than_dropping_it(tmp_path):
    """``False`` must survive into the spec, or a worker declaring "do not reason" would silently
    inherit the default instead."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], thinking=False),
    }
    config, state = _state(tmp_path, agents)
    config.thinking = "high"
    assert build_agent_specs(config, state, "assistant")["researcher"]["thinking"] is False


def test_a_spec_omits_thinking_when_nothing_is_declared_anywhere(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    assert "thinking" not in build_agent_specs(config, state, "assistant")["researcher"]


def test_a_spec_carries_the_generation_parameters_its_agent_resolves(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], generation={"temperature": 0.2}),
    }
    config, state = _state(tmp_path, agents)
    config.generation = {"temperature": 0.7, "context_length": 32768}
    assert build_agent_specs(config, state, "assistant")["researcher"]["generate_kwargs"] == {
        "temperature": 0.2,
        "context_length": 32768,
    }


def test_a_spec_carries_the_resolved_generation_default_when_its_agent_declares_nothing(tmp_path):
    """AIMU reads a missing spec key as "none" rather than falling back, so the resolved value has to
    be written in or an undeclared worker would skip the default instead of inheriting it."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    config.generation = {"context_length": 32768}
    assert build_agent_specs(config, state, "assistant")["researcher"]["generate_kwargs"] == {"context_length": 32768}


def test_a_spec_omits_generate_kwargs_when_nothing_is_declared_anywhere(tmp_path):
    """An empty dict is still a written tier, and this tier sits above the model card's own profile."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    assert "generate_kwargs" not in build_agent_specs(config, state, "assistant")["researcher"]


def test_every_spec_key_kokua_writes_is_one_aimu_accepts():
    """AIMU validates a spec when the spawn tool is built, so a typo here would fail at agent build."""
    from aimu.tools.builtin import SUBAGENT_SPEC_KEYS

    assert {
        "system_message",
        "tools",
        "model",
        "thinking",
        "generate_kwargs",
        "max_iterations",
    } <= SUBAGENT_SPEC_KEYS


def test_a_nested_delegate_is_built_with_the_resolved_default(tmp_path, monkeypatch):
    """A worker that itself delegates gets a spawn tool, and it needs a model like any other.

    This path passed ``config.model`` straight through, so with no ``[assistant].model`` it handed
    AIMU None. Nothing failed at build time: the spec's own model covers a worker that declares one,
    and the tool is only constructed per spawn, so an undeclared worker two levels down raised
    ``ValueError: No available async client for model type 'NoneType'`` at the moment it was asked to
    do something, and only then.

    ``max_iterations`` is the same trap one field over, which is why ``lead`` pins its own cap here.
    AIMU reads a spec without the key as "the cap this tool was built with", so building ``lead``'s
    spawn tool with ``max_iterations_for("lead")`` would hand every worker below it a cap it never
    declared. Nothing else in the suite watches this construction site.
    """
    from kokua.core import agents as agents_mod

    from tests.conftest import TEST_DEFAULT_MODEL

    captured: list[tuple[object, dict]] = []

    def fake_make(model, **kwargs):
        captured.append((model, kwargs))

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """menu"""
            return "ok"

        spawn_subagent.__name__ = "spawn_subagent"
        return spawn_subagent

    agents = {
        "assistant": AgentConfig(delegates_to=["lead"]),
        "lead": AgentConfig(tools=["web"], delegates_to=["helper"], max_iterations=25),
        "helper": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", fake_make)
    agents_mod.build_agent_specs(config, state, "assistant")
    assert [model for model, _ in captured] == [TEST_DEFAULT_MODEL]
    assert [kwargs["max_iterations"] for _, kwargs in captured] == [config.max_iterations]
    assert config.max_iterations_for("lead") == 25  # so the assertion above could have failed


def test_the_delegate_is_built_with_the_metrics_forwarder(tmp_path, monkeypatch):
    """A spawn builds its own client, so without an explicit sink the delegated work is never
    measured and a heavily delegating turn reads as cheap.

    The forwarder, not a turn's own accumulator: it is built once here and reads the running turn
    off a contextvar when an event actually fires, so one tool serves every later turn correctly.

    Coverage of the delegation seam splits into three links, none proven by a single test. This one
    pins the first: Kokua wires the spawn tool to the forwarder, by calling ``make_delegation_tool``
    for real and asserting the captured ``events=`` kwarg is ``record_event``; severing the wiring
    fails this assertion. The second link, AIMU delivering a sink passed as
    ``make_async_subagent_tool(events=...)`` to a spawned child's model turns, is pinned by AIMU's own
    suite, not this one, since Kokua has no reason to re-prove a library capability it depends on. The
    third, ``record_event`` routing to whichever turn opened the accumulator and attributing by the
    event's ``agent`` field, is pinned by
    ``test_the_forwarder_attributes_a_delegates_cost_to_the_delegate`` below, which calls
    ``record_event`` directly with hand-built events and so says nothing about the first two links.
    ``make_delegation_tool`` offers no seam to inject a fake spawned child, so a single test spanning
    all three would mean contorting it for injectability to re-cover ground these three already cover
    between them. What none of the three catches: someone changing this wiring and this test in the
    same change would not be caught by the third test, since that one never touches
    ``make_delegation_tool`` at all. That composition is accepted, not closed, deliberately.
    """
    from kokua.core.metrics import record_event

    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(),
    }
    config, state = _state(tmp_path, agents)
    captured = _captured_spawn_kwargs(monkeypatch, config, state, _FakeAgent("assistant", "ollama:qwen3:32b"))
    assert captured["events"] is record_event


def test_the_forwarder_attributes_a_delegates_cost_to_the_delegate():
    """``record_event`` routes to whichever accumulator the running turn opened, and ``TurnMetrics``
    attributes by the event's ``agent`` field rather than folding it into the entry agent's total.

    This calls ``record_event`` directly with a hand-built event; it does not go through
    ``make_delegation_tool`` or a real model call, so it proves the routing and attribution half of the
    delegation seam, not the wiring (see the previous test's docstring for where each link is pinned).

    Two calls, not one: ``TurnMetrics.record`` only publishes a ``by_agent`` breakdown once more than
    one distinct agent contributed (see ``test_omits_by_agent_when_only_the_entry_agent_ran`` in
    ``test_metrics.py``), so a lone delegated call would settle into the top-level totals with nothing
    to distinguish it. Including the entry agent's own call is also the realistic shape: a turn that
    delegates still made at least one call of its own before or after doing so.
    """
    from aimu.events import ModelTurnFinished

    from kokua.core.metrics import TurnMetrics, current_metrics, record_event

    metrics = TurnMetrics()
    token = current_metrics.set(metrics)
    try:
        record_event(
            ModelTurnFinished(model="m1", usage={"input_tokens": 50, "output_tokens": 5}, duration_s=1.0, agent=None)
        )
        # A stand-in for what a delegate's own model turn would carry, once AIMU delivers the spawn
        # factory's events=record_event to it.
        record_event(
            ModelTurnFinished(
                model="m2",
                usage={"input_tokens": 900, "output_tokens": 90},
                duration_s=2.0,
                agent="subagent-research",
            )
        )
    finally:
        current_metrics.reset(token)
    record = metrics.record(wall_seconds=3.0)
    assert record["by_agent"]["subagent-research"]["input_tokens"] == 900


def test_a_spec_carries_the_cap_its_agent_declares(tmp_path):
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"], max_iterations=25),
    }
    config, state = _state(tmp_path, agents)
    assert build_agent_specs(config, state, "assistant")["researcher"]["max_iterations"] == 25


def test_a_spec_omits_the_cap_when_its_agent_declares_none(tmp_path):
    """Unlike thinking and generate_kwargs, this key needs no resolved value written in: AIMU reads a
    missing max_iterations as "the cap the spawn tool was built with", which is the [assistant] default."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"]),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    config.max_iterations = 15
    assert "max_iterations" not in build_agent_specs(config, state, "assistant")["researcher"]


def test_the_spawn_tool_is_built_with_the_global_cap_not_the_delegators(tmp_path, monkeypatch):
    """The inheritance bug this guards against: since a spec without the key falls back to the tool's
    value, passing max_iterations_for(delegator) would make an undeclared worker inherit its delegator's
    pin, which is the one thing max_iterations_for promises not to do."""
    agents = {
        "assistant": AgentConfig(delegates_to=["researcher"], max_iterations=40),
        "researcher": AgentConfig(tools=["web"]),
    }
    config, state = _state(tmp_path, agents)
    config.max_iterations = 10
    agent = _FakeAgent("assistant", "ollama:qwen3:8b")
    assert _captured_spawn_kwargs(monkeypatch, config, state, agent)["max_iterations"] == 10


def test_the_shipped_analyst_can_read_the_export_the_assistant_hands_it(tmp_path):
    """`export_conversation` answers with a path, and a path is a dead end unless something the
    assistant can reach reads files. Pins that pairing over the shipped example rather than the
    example's prose: the delegate has to exist, be reachable from the entry agent, and actually
    resolve to an agent holding `read_file`."""
    from tests.channels import example_agents

    config, state = _state(tmp_path, example_agents())
    spec = build_agent_specs(config, state, "assistant")["analyst"]

    assert "read_file" in {fn.__name__ for fn in spec["tools"]}
