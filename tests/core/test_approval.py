"""The tool-approval gate: what is gated, who may answer, and how the answer is routed."""

from __future__ import annotations

import asyncio

import pytest

from aimu.aio.channels.base import Channel, ChannelMessage

from kokua.config import ConfigError
from kokua.config.schema import AgentConfig, AssistantConfig, ReviewerConfig
from kokua.core.agents import build_registry
from kokua.core.assistant import Assistant
from tests.channels import AlertCapturingChannel, FakeChannel, _config
from tests.fakes import _RequestsToolOnce
from tests.helpers import MockAsyncModelClient


async def test_assistant_wires_approval_policy(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    gate = assistant._agent.tool_approval
    assert gate.func == assistant._human.approve and gate.args == ("assistant",)


async def test_approve_allows_ungated_tool_without_prompting(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, default_confirm=["skills.add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    await assistant.start()
    assert await assistant._gate_for("assistant")("get_weather", {}) is True
    assert channel.sent == []  # no prompt for an ungated tool


async def _noop_prompt() -> None:
    """A prompt that sends nothing: these tests exercise the pending-request slot, not the channel."""


async def test_approve_gated_tool_waits_for_routed_answer(tmp_path):
    from kokua.channels.web import streaming_conversation

    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, default_confirm=["skills.add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    await assistant.start()
    await assistant._agent.load_tool_group("skills")
    # Foreground: the calling turn's conversation is the one currently viewed.
    token = streaming_conversation.set(assistant._active_id)
    try:
        task = asyncio.create_task(assistant._gate_for("assistant")("add_skill_script", {"skill_name": "x"}))
        await asyncio.sleep(0)  # let the policy register the pending approval and prompt
        assert assistant._human.approval.pending
        assert channel.sent  # a prompt was sent to the user
        assistant._human.approval.resolve(True)
        assert await task is True
    finally:
        streaming_conversation.reset(token)


async def test_approve_backgrounded_auto_denies_without_prompting(tmp_path):
    """The deny is immediate: the card that reports it is not a question, and nothing waits on a reply.

    A channel with no card surface (this one) prints the sentence instead, which is why the alert's
    text has to stand on its own.
    """
    from kokua.channels.web import streaming_conversation

    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, default_confirm=["skills.add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    await assistant.start()
    await assistant._agent.load_tool_group("skills")
    # Background: the calling turn's conversation ("elsewhere") isn't the one being viewed.
    token = streaming_conversation.set("elsewhere")
    try:
        assert await assistant._gate_for("assistant")("add_skill_script", {}) is False
        assert not assistant._human.approval.pending  # nothing is waiting on an answer
        assert [s for s in channel.sent if "[approve]" in s] == []  # and nothing asked for one
        assert any("add_skill_script" in s and "denied" in s for s in channel.sent)
    finally:
        streaming_conversation.reset(token)


async def test_serve_loop_routes_message_to_pending_approval(tmp_path):
    class _OneMsg(Channel):
        name = "fake"

        async def receive(self):
            yield ChannelMessage(text="y", channel="fake")

        async def send(self, content, *, reply_to=None):
            pass

    assistant = await Assistant.create(_config(tmp_path), _OneMsg(), client=MockAsyncModelClient([]))
    asking = asyncio.create_task(assistant._human.approval.ask(_noop_prompt))
    await asyncio.sleep(0)  # let it register as pending before the loop reads the "y"

    await assistant._serve_channel()

    assert await asking is True
    assert assistant._tracker.get(assistant._active_id) is None  # the answer did not start a new turn


async def test_denied_gated_tool_does_not_run(tmp_path):
    cfg = _config(tmp_path, default_confirm=["skills.add_skill_script"])
    client = _RequestsToolOnce("add_skill_script", {"skill_name": "disk", "filename": "u.py", "content": "print(1)\n"})
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    await assistant.start()
    await assistant._agent.load_tool_group("skills")
    # No streaming_conversation is set around this call, so it defaults to None -- not the viewed
    # conversation -- making the gate auto-deny without an interactive prompt. That exercises the real
    # dispatch path (the Agent's tool-loop engine + approval gate) via a normal run.

    await assistant._agent.run("go")

    denied = [m for m in client.messages if m.get("role") == "tool"]
    assert denied and denied[-1]["content"] == "Tool 'add_skill_script' was not approved."
    assert not (cfg.skills_dir / "disk" / "scripts" / "u.py").exists()


async def test_approve_serializes_concurrent_gated_calls(tmp_path):
    """Two concurrent gated approvals must not clobber each other's pending future.

    Without the lock the interleaved coroutines both call asyncio.gather concurrently. The first
    call takes the slot and yields at the sleep; without the lock the second then overwrites it with
    a fresh future before the first has resolved. The first call then calls set_result on the
    already-cleared (None) reference, raising AttributeError ('NoneType' has no attribute
    'set_result'). With the lock the second call waits until the first has fully completed (future
    resolved, pending_approval cleared) before it acquires the lock, creates its own future, and
    resolves it safely.
    """
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, default_confirm=["compute.execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    prompts: list[str] = []
    order: list[str] = []

    async def fake_prompt(name, arguments, *, agent=None):
        prompts.append(name)
        # Yield to the event loop before resolving so the two gathered coroutines can interleave.
        # Without the lock the second call overwrites the pending slot here, causing the
        # first call to resolve the wrong future and the second to deadlock (or raise
        # InvalidStateError if its future is resolved twice).
        await asyncio.sleep(0)
        assistant._human.approval.resolve(True)

    assistant._ui.ask_approval = fake_prompt

    async def call(tag):
        result = await assistant._gate_for("assistant")("execute_python", {"code": tag})
        order.append(tag)
        return result

    # Foreground: both concurrent calls belong to the viewed conversation.
    token = streaming_conversation.set(assistant._active_id)
    try:
        results = await asyncio.wait_for(asyncio.gather(call("a"), call("b")), timeout=2.0)
    finally:
        streaming_conversation.reset(token)

    assert results == [True, True]
    assert prompts == ["execute_python", "execute_python"]  # both prompted, one at a time
    assert set(order) == {"a", "b"}


async def test_background_turn_auto_denies_gated_tool(tmp_path):
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, default_confirm=["compute.execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    viewed = assistant._active_id
    await assistant.new_conversation()  # _active_id now the new (background) conversation
    background = assistant._active_id
    await assistant.select_conversation(viewed)  # make `viewed` active again
    # A turn running in `background` is not the viewed conversation -> auto-deny.
    token = streaming_conversation.set(background)
    try:
        assert await assistant._gate_for("assistant")("execute_python", {}) is False
    finally:
        streaming_conversation.reset(token)


async def test_foreground_turn_prompts_for_approval(tmp_path):
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, default_confirm=["compute.execute_python"])
    channel = FakeChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    viewed = assistant._active_id
    token = streaming_conversation.set(viewed)
    try:
        approve_task = asyncio.create_task(assistant._gate_for("assistant")("execute_python", {}))
        await asyncio.sleep(0.01)
        assert assistant._human.approval.pending
        assistant._human.approval.resolve(True)
        assert await approve_task is True
    finally:
        streaming_conversation.reset(token)


async def test_switch_away_resolves_pending_approval_as_denied(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    asking = asyncio.create_task(assistant._human.approval.ask(_noop_prompt))
    await asyncio.sleep(0)
    await assistant.new_conversation()  # switching away
    assert await asking is False


async def test_switch_away_resolves_a_pending_decision_with_its_default(tmp_path):
    cfg = _config(tmp_path)
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    asking = asyncio.create_task(assistant._human.decision.ask(_noop_prompt, context="the plan", default=None))
    await asyncio.sleep(0)
    await assistant.select_conversation(assistant._active_id)  # switching (even to the same id)
    assert await asking is None


# --- what a gate entry is allowed to name -------------------------------------------------------


async def test_the_shipped_gates_all_name_tools_that_exist(tmp_path):
    """The six entries config.example.toml ships have to pass the check that rejects a gate holding
    nothing back, or the default install would not start.

    Pinned as an exact resolved set, not just "it started", because the entries are a mix of forms and
    what a reader wants to know is which calls actually stop. `fs_write` is the bare one: it contributes
    both writers from one entry, which is the property that makes a writer added by a later AIMU gated
    on arrival, and a regression to naming its two tools would still start and still pass a
    smoke test."""
    config = _config(tmp_path)
    assert config.default_confirm == AssistantConfig().default_confirm
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    # Two of the entries name toolsets the shipped entry agent defers, so they resolve as it loads them.
    for toolset in ("skills", "mcp"):
        await assistant._agent.load_tool_group(toolset)
    assert assistant._human.policies.default.gated == {
        "add_skill_script",
        "add_mcp_server",
        "execute_python",
        "run_command",
        "write_file",
        "edit_file",
        "update_config",
    }
    # And the benign member of a toolset one of those entries names is deliberately not gated.
    assert "calculate" not in assistant._human.policies.default.gated


async def test_an_empty_gate_list_is_still_valid(tmp_path):
    """[] is the documented way to turn approval off, so it has to stay a legal value."""
    assistant = await Assistant.create(
        _config(tmp_path, default_confirm=[]), FakeChannel(), client=MockAsyncModelClient([])
    )
    await assistant.start()
    assert assistant._human.policies.default.gated == frozenset()


async def _gated(tmp_path, entries) -> frozenset:
    """The tool names ``entries`` resolves to on an otherwise default config."""
    assistant = await Assistant.create(
        _config(tmp_path, default_confirm=entries), FakeChannel(), client=MockAsyncModelClient([])
    )
    await assistant.start()
    return assistant._human.policies.default.gated


def _write_skill(config) -> None:
    """A one-script skill on disk, which registers a toolset named for the skill."""
    scripts = config.skills_dir / "weekly-digest" / "scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "SKILL.md").write_text(
        "---\nname: weekly-digest\ndescription: Digest the week.\n---\n\nDo the thing.\n", encoding="utf-8"
    )
    (scripts / "collect_notes.py").write_text("print('notes')\n", encoding="utf-8")


async def test_a_bare_toolset_gates_every_tool_it_provides(tmp_path):
    """The point of the prefix: a gate can name a capability, not one call into it. Note it gates
    `calculate` too, which the shipped default leaves ungated -- naming the capability is a broader
    statement than naming its two dangerous members, and that is the entry's whole meaning."""
    assert await _gated(tmp_path, ["compute"]) == {"calculate", "execute_python", "run_command"}


async def test_a_toolset_wildcard_means_exactly_what_the_bare_toolset_means(tmp_path):
    """`compute.*` exists because a reader may expect a glob to be required, not because it says
    anything the bare name does not."""
    assert await _gated(tmp_path, ["compute.*"]) == await _gated(tmp_path, ["compute"])


async def test_a_prefixed_tool_gates_only_that_tool(tmp_path):
    assert await _gated(tmp_path, ["compute.execute_python"]) == {"execute_python"}


async def test_a_bare_tool_name_fails_startup_and_names_the_prefix_to_write(tmp_path):
    """The migration error. A config written before the prefix was required stops at startup rather
    than resolving to something plausible, and the message is the edit to make."""
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["execute_python"])
    message = str(error.value)
    assert "execute_python" in message
    assert "compute.execute_python" in message


async def test_a_bare_tool_offered_under_two_prefixes_names_both(tmp_path):
    """activate_skill belongs to the reserved namespace, and every skill toolset carries it too, so a
    bare entry for it has more than one prefixed form and the user is the one who picks."""
    config = _config(tmp_path, default_confirm=["activate_skill"])
    _write_skill(config)
    with pytest.raises(ConfigError) as error:
        assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
        await assistant.start()
    assert "core.activate_skill" in str(error.value)


async def test_a_misspelled_tool_under_a_real_toolset_suggests_the_real_one(tmp_path):
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["compute.execute_pythn"])
    message = str(error.value)
    assert "compute.execute_pythn" in message
    assert "execute_python" in message  # the close match, offered so the fix does not need a hunt
    assert "add_mcp_server" in message  # and the reason a not-yet-connected MCP tool cannot be pre-gated


async def test_a_real_tool_under_the_wrong_toolset_points_at_the_right_one(tmp_path):
    """The likeliest new mistake: the tool is spelled correctly and filed under the wrong capability."""
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["web.execute_python"])
    assert "compute.execute_python" in str(error.value)


async def test_an_unknown_toolset_is_named_with_a_close_match(tmp_path):
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["computr.execute_python"])
    message = str(error.value)
    assert "computr" in message and "compute" in message


async def test_a_toolset_no_agent_declares_is_rejected(tmp_path):
    """github_backup is a real toolset the default config gives to nobody, so gating it holds back no
    call that can happen: the silent no-op this check exists to refuse."""
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["github_backup"])
    assert "github_backup" in str(error.value)


async def test_a_toolset_that_provides_no_tools_is_rejected(tmp_path):
    """planning is declared by the entry agent and contributes a workflow rather than tools, so there
    is nothing under it for a gate to hold back."""
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["planning"])
    assert "planning" in str(error.value)


@pytest.mark.parametrize("entry", ["compute.exec*", "comp*.execute_python", "*", "*.run_command"])
async def test_a_partial_glob_is_rejected(tmp_path, entry):
    """`*` is legal as the whole tool segment and nowhere else. A pattern quietly matching fewer tools
    than its author expected is the same failure the rest of this check exists to prevent."""
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, [entry])
    assert entry in str(error.value)


async def test_an_entry_with_two_dots_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["compute.sub.execute_python"])
    assert "compute.sub.execute_python" in str(error.value)


async def test_every_unmatched_gate_is_named_not_only_the_first(tmp_path):
    """One restart per typo is the failure mode this avoids: a user fixing a list of four wants all the
    bad entries in the first error."""
    with pytest.raises(ConfigError) as error:
        await _gated(tmp_path, ["compute.execute_pythn", "totally_made_up_toolset"])
    message = str(error.value)
    assert "compute.execute_pythn" in message and "totally_made_up_toolset" in message


async def test_a_tool_only_a_worker_holds_is_gateable(tmp_path):
    """execute_python is one of the shipped gates and the entry agent does not hold it: it comes from
    [agents.coder]. A check over the entry agent's own tools alone would reject the default config."""
    assistant = await Assistant.create(
        _config(tmp_path, default_confirm=["compute.execute_python"]), FakeChannel(), client=MockAsyncModelClient([])
    )
    assert "execute_python" not in {fn.__name__ for fn in assistant._agent.tools}


async def test_the_delegation_tool_is_gateable(tmp_path):
    """spawn_subagent is attached to the entry agent after its toolsets are built, so no toolset owns
    it and the reserved `core` namespace is the only prefix it can have."""
    assert await _gated(tmp_path, ["core.spawn_subagent"]) == {"spawn_subagent"}


async def test_the_reserved_namespace_also_gates_the_loaders_when_something_is_deferred(tmp_path):
    config = _config(tmp_path, default_confirm=["core"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    # AIMU adds the two loaders to an agent with a deferred toolset, and they belong to no toolset either.
    assert assistant._human.policies.default.gated == {
        "spawn_subagent",
        "activate_skill",
        "available_tools",
        "load_tools",
    }
    assistant._state.close()


async def test_a_skill_script_is_gateable(tmp_path):
    """A skill script is an unsandboxed subprocess, so it is worth gating, and an AIMU SkillAgent
    attaches its script tools on first run rather than at startup. The vocabulary is therefore derived
    from the skills on disk, not read off the built agent."""
    config = _config(tmp_path, default_confirm=["weekly-digest.weekly_digest__collect_notes", "core.activate_skill"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.policies.default.gated == {"weekly_digest__collect_notes", "activate_skill"}
    assistant._state.close()


async def test_a_bare_skill_gates_its_scripts_and_not_activate_skill(tmp_path):
    """Every skill's tool list carries activate_skill, so attributing it per skill would let one
    skill's gate stop every skill from being loaded. It belongs to `core`, and only there."""
    config = _config(tmp_path, default_confirm=["weekly-digest"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.policies.default.gated == {"weekly_digest__collect_notes"}
    assistant._state.close()


async def test_the_conversation_gates_the_reference_suggests_all_resolve(tmp_path):
    """docs/reference/configuration.md offers these four by name as the ones worth adding, so a reader
    should be able to paste them. They are the entries most likely to go stale, since the toolset that
    provides them is not the one a reader would guess from the tool name."""
    entries = [
        "conversations.read_conversation",
        "conversations.search_conversations",
        "conversations.rename_conversation",
        "conversations.export_conversation",
    ]
    assert await _gated(tmp_path, entries) == {entry.split(".", 1)[1] for entry in entries}


async def test_the_confirm_tools_flag_is_checked_by_the_same_rule(tmp_path):
    """--confirm-tools writes the same field, so a typo there has to fail the same way rather than
    reaching the gate as a name nothing matches."""
    from kokua.cli import build_arg_parser, resolve_config

    config = resolve_config(build_arg_parser().parse_args(["--confirm-tools", "config.update_confg"]))
    with pytest.raises(ConfigError) as error:
        assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
        await assistant.start()
    assert "config.update_confg" in str(error.value) and "update_config" in str(error.value)


async def test_the_gate_cannot_run_before_startup_resolves_it(tmp_path):
    """A gate whose resolved set is missing must not read as "nothing is gated": that is precisely the
    silent no-op the startup check exists to prevent."""
    from kokua.core.interaction import HumanGate

    gate = HumanGate(
        None,
        active_id=lambda: "a",
        is_proactive=lambda: False,
        turn_conversation=lambda: "a",
    )
    with pytest.raises(RuntimeError, match="before startup resolved"):
        await gate.approve("assistant", "execute_python", {})


async def test_background_auto_deny_raises_an_alert(tmp_path):
    """The deny is silent from the user's side: the tool result saying so is on a transcript they are
    not reading, and the turn carries on without the tool. The card is what makes it visible while
    there is still something to do about it."""
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, default_confirm=["compute.execute_python"])
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    viewed = assistant._active_id
    await assistant.new_conversation()
    background = assistant._active_id
    await assistant.select_conversation(viewed)

    token = streaming_conversation.set(background)
    try:
        assert await assistant._gate_for("assistant")("execute_python", {}) is False
        assert await assistant._gate_for("assistant")("execute_python", {}) is False  # same tool, same card
    finally:
        streaming_conversation.reset(token)

    (text, conversation_id, url, group) = channel.alerts[-1]
    assert "execute_python" in text and url is None
    assert conversation_id == background
    assert [g for _, _, _, g in channel.alerts] == [group, group]  # one group, so one card, not two


async def test_proactive_auto_deny_stays_silent(tmp_path):
    """A scheduled firing denies without a card: the run has its own report at the end, and a task
    that calls a gated tool every firing would otherwise raise one every firing."""
    from kokua.channels.web import proactive_turn

    cfg = _config(tmp_path, default_confirm=["compute.execute_python"])
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    token = proactive_turn.set(True)
    try:
        assert await assistant._gate_for("assistant")("execute_python", {}) is False
    finally:
        proactive_turn.reset(token)
    assert channel.alerts == []


async def test_assistant_wires_the_auto_approval_gate(tmp_path):
    """Startup resolves `[security.auto_approval]` onto the gate, which is what makes it reachable.

    Asserted here beside `test_assistant_wires_approval_policy` for the reason that one exists: every
    other test of the review layer either assigns `HumanGate.auto_approval` by hand or reaches the
    resolver through a helper, so the line in `start` that connects the two is the kind of thing a
    refactor drops with the suite still green and no symptom but a feature that never runs.

    A review entry is itself a gate, so the tool the reviewer may be asked about is in the policy's
    ``review`` half and nowhere else.
    """
    cfg = _config(
        tmp_path,
        auto_approval_enabled=True,
        auto_approval_reviewers=["approval"],
        default_confirm=["compute.execute_python"],
        default_review=["compute.run_command"],
        reviewers={"approval": ReviewerConfig(model="ollama:qwen3:8b", system_message="judge it")},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    auto = assistant._human.auto_approval
    assert auto is not None
    assert auto.toolset_of["run_command"] == "compute"
    assert assistant._human.policies.default.review == frozenset({"run_command"})


async def test_an_assistant_holds_no_gate_when_the_feature_is_off(tmp_path):
    """The shipped default: a started assistant gates tools and reviews none of them."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    assert assistant._human.policies is not None
    assert assistant._human.auto_approval is None


# --- Resolving one approval policy per agent -------------------------------------------------------

import dataclasses  # noqa: E402

from kokua.core.approval import DEFAULT_POLICY, resolve_approval_policies  # noqa: E402


async def _wired(tmp_path, **overrides):
    """A real wired assistant, so ``tools_by_toolset`` holds the tool names the shipped toolsets build."""
    config = _config(tmp_path, default_confirm=["fs_write"], default_review=[], **overrides)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    return config, assistant._state, assistant._agent


def _declaring(config: AssistantConfig, **pairs) -> AssistantConfig:
    """``config`` with ``{agent: (confirm, review)}`` declared on those agents."""
    agents = dict(config.agents)
    for name, (confirm, review) in pairs.items():
        agents[name] = dataclasses.replace(agents[name], confirm=confirm, review=review)
    return dataclasses.replace(config, agents=agents)


async def test_default_tier_resolves_against_every_built_tool(tmp_path):
    config, state, entry = await _wired(tmp_path)
    config = dataclasses.replace(config, default_review=["compute.run_command"])
    policies = resolve_approval_policies(config, state, entry)
    assert DEFAULT_POLICY == "[assistant.tools]"
    assert policies.default.source == DEFAULT_POLICY
    assert policies.default.confirm == {"write_file", "edit_file"}
    assert policies.default.review == {"run_command"}
    assert policies.default.gated == {"write_file", "edit_file", "run_command"}


async def test_an_agent_without_a_pair_shares_the_default_under_its_own_name(tmp_path):
    config, state, entry = await _wired(tmp_path)
    policies = resolve_approval_policies(config, state, entry)
    researcher = policies.for_agent("researcher")
    assert researcher.confirm == policies.default.confirm
    assert researcher.agent == "researcher"
    assert researcher.source == DEFAULT_POLICY


async def test_a_declared_pair_replaces_the_default_whole(tmp_path):
    config, state, entry = await _wired(tmp_path)
    config = _declaring(config, coder=(["compute.run_command"], []))
    coder = resolve_approval_policies(config, state, entry).for_agent("coder")
    assert coder.source == "[agents.coder.tools]"
    assert coder.confirm == {"run_command"}
    assert coder.review == frozenset()


async def test_an_unknown_name_gets_the_default_relabelled(tmp_path):
    config, state, entry = await _wired(tmp_path)
    policies = resolve_approval_policies(config, state, entry)
    composed = policies.for_agent("composed:quote-checker")
    assert composed.agent == "composed:quote-checker"
    assert composed.gated == policies.default.gated


async def test_a_declared_entry_must_name_a_toolset_the_agent_can_reach(tmp_path):
    config, state, entry = await _wired(tmp_path)
    config = _declaring(config, researcher=(["compute.run_command"], []))
    with pytest.raises(
        ConfigError,
        match=r"\[agents\.researcher\.tools\]\.confirm names 'compute\.run_command', "
        r"but researcher cannot reach the 'compute' toolset: it is not in \[agents\.researcher\.tools\]\.available\.",
    ):
        resolve_approval_policies(config, state, entry)


async def test_a_declared_entry_matching_no_tool_is_a_startup_error(tmp_path):
    config, state, entry = await _wired(tmp_path)
    assert "compute" in config.agents["coder"].available
    config = _declaring(config, coder=(["compute.run_comand"], []))
    with pytest.raises(ConfigError) as error:
        resolve_approval_policies(config, state, entry)
    message = str(error.value)
    assert "[agents.coder.tools].confirm has 1 entry that would gate nothing" in message
    assert "compute.run_comand" in message and "run_command" in message
    assert "cannot reach" not in message


async def test_a_declared_review_entry_must_name_a_toolset_the_agent_can_reach(tmp_path):
    config, state, entry = await _wired(tmp_path)
    config = _declaring(config, researcher=([], ["compute.run_command"]))
    with pytest.raises(
        ConfigError,
        match=r"\[agents\.researcher\.tools\]\.review names 'compute\.run_command', "
        r"but researcher cannot reach the 'compute' toolset",
    ):
        resolve_approval_policies(config, state, entry)


async def test_a_declared_review_entry_resolves_into_that_agents_review_set(tmp_path):
    config, state, entry = await _wired(tmp_path)
    config = _declaring(config, coder=(["compute.execute_python"], ["compute.run_command"]))
    coder = resolve_approval_policies(config, state, entry).for_agent("coder")
    assert coder.review == {"run_command"}
    assert coder.confirm == {"execute_python"}


async def test_confirm_and_review_overlapping_is_a_startup_error(tmp_path):
    config, state, entry = await _wired(tmp_path)
    config = dataclasses.replace(config, default_confirm=["compute"], default_review=["compute.run_command"])
    with pytest.raises(ConfigError, match=r"\[assistant\.tools\] gates run_command in both confirm and review"):
        resolve_approval_policies(config, state, entry)


async def test_a_declared_pair_overlapping_names_its_own_table(tmp_path):
    config, state, entry = await _wired(tmp_path)
    config = _declaring(config, coder=(["compute"], ["compute.run_command"]))
    with pytest.raises(ConfigError, match=r"\[agents\.coder\.tools\] gates run_command in both"):
        resolve_approval_policies(config, state, entry)


async def test_the_confirm_tools_flag_overlapping_the_files_review_fails(tmp_path):
    """``--confirm-tools`` replaces ``default_confirm`` but not the file's review list, so naming one
    tool in both is caught here rather than silently picking a winner."""
    import re
    from importlib.resources import files

    from kokua.cli import build_arg_parser, resolve_config

    text = files("kokua").joinpath("config.example.toml").read_text()
    text = re.sub(r"^review = \[.*?\]", 'review = ["compute.run_command"]', text, count=1, flags=re.M | re.S)
    path = tmp_path / "config.toml"
    path.write_text(text)
    args = build_arg_parser().parse_args(["--config", str(path), "--confirm-tools", "compute.run_command"])
    flagged = resolve_config(args)
    assert flagged.default_confirm == ["compute.run_command"]
    assert flagged.default_review == ["compute.run_command"]

    _, state, entry = await _wired(tmp_path)
    with pytest.raises(ConfigError, match="in both confirm and review"):
        resolve_approval_policies(flagged, state, entry)


async def test_the_shipped_example_resolves_to_todays_gate(tmp_path):
    import shutil
    from importlib.resources import files

    from kokua.config.file import load
    from tests.helpers import core_table

    path = tmp_path / "config.toml"
    shutil.copyfile(str(files("kokua").joinpath("config.example.toml")), path)
    loaded = load(str(path), table=core_table())
    # A scaffolded config writes its lock list out in full, so the default list's entry alone would
    # leave [assistant.tools] writable by update_config.
    assert "assistant.tools.*" in loaded["locked_config_keys"]
    config = _config(tmp_path, default_confirm=loaded["default_confirm"], default_review=loaded["default_review"])
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    # `skills` and `mcp` are deferred by the shipped entry agent, so the two entries naming their tools
    # wait for the build instead of resolving at startup.
    at_startup = assistant._human.policies.default
    assert at_startup.confirm == {"update_config"}
    assert at_startup.review == {"execute_python", "run_command", "write_file", "edit_file"}
    assert at_startup.pending_confirm == {"skills.add_skill_script", "mcp.add_mcp_server"}
    assert at_startup.pending_review == frozenset()
    assert len(at_startup.gated) == 5

    for toolset in ("skills", "mcp"):
        await assistant._agent.load_tool_group(toolset)

    # The union is what the old six-entry gate held, so neither the split nor residency changed which
    # calls are held: two of them resolve on load instead of at startup.
    after_load = assistant._human.policies.default
    assert after_load.pending_confirm == frozenset()
    # A pending confirm entry must land in confirm, not review: review is the half a model can approve.
    assert after_load.confirm == {"add_skill_script", "add_mcp_server", "update_config"}
    assert after_load.review == {"execute_python", "run_command", "write_file", "edit_file"}
    assert after_load.gated == {
        "add_skill_script",
        "add_mcp_server",
        "execute_python",
        "run_command",
        "write_file",
        "edit_file",
        "update_config",
    }
    assistant._state.close()


async def test_the_reserved_namespace_has_no_loaders_without_a_deferred_toolset(tmp_path):
    agents = {
        "assistant": AgentConfig(available=["time", "weekly-digest"], delegates_to=["helper"]),
        "helper": AgentConfig(description="Helps.", available=["time"]),
    }
    config = _config(tmp_path, agents=agents, default_confirm=["core"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.policies.default.gated == {"spawn_subagent", "activate_skill"}
    assistant._state.close()


# A skill on the entry agent is never deferred: its SkillAgent advertises every skill, and wiring holds
# skill names back before the residency split. So a gate naming one must resolve at startup, not wait
# for a build that never comes while the skill's scripts run ungated.


def _skill_tool_names(assistant) -> set[str]:
    names = set(assistant._state.skill_manager.skills["weekly-digest"].script_tool_names())
    assert names, "the skill must provide a tool for the gate to hold back"
    return names


def _with_skill_on_entry_agent(config: AssistantConfig) -> AssistantConfig:
    entry = config.agents["assistant"]
    assert entry.resident is not None, "the shipped entry agent declares resident, which is what exposes this"
    config.agents["assistant"] = dataclasses.replace(entry, available=[*entry.available, "weekly-digest"])
    _write_skill(config)
    return config


async def test_a_gate_on_an_entry_agent_skill_resolves_at_startup_with_resident_declared(tmp_path):
    config = _with_skill_on_entry_agent(_config(tmp_path, default_confirm=["weekly-digest"]))
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        policy = assistant._human.policies.default
        assert policy.pending_confirm == frozenset()
        assert _skill_tool_names(assistant) <= policy.confirm
    finally:
        assistant._state.close()


async def test_a_skill_alone_outside_resident_earns_no_loaders(tmp_path):
    """An entry agent whose only non-resident name is a skill defers nothing, so it holds no loaders and
    a bare `core` gates exactly what it did before residency."""
    agents = {
        "assistant": AgentConfig(available=["time", "weekly-digest"], resident=["time"], delegates_to=["helper"]),
        "helper": AgentConfig(description="Helps.", available=["time"]),
    }
    config = _config(tmp_path, agents=agents, default_confirm=["core"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        assert "load_tools" not in {fn.__name__ for fn in assistant._agent._effective_tools(None)}
        assert assistant._human.policies.default.gated == {"spawn_subagent", "activate_skill"}
    finally:
        assistant._state.close()


# A worker deferring a skill must not re-park a gate on that skill when the entry agent's SkillAgent
# already holds it: those tools exist from the first turn, whoever else could load them later.


def _worker_defers_skill(config: AssistantConfig) -> AssistantConfig:
    worker = config.agents["researcher"]
    config.agents["researcher"] = dataclasses.replace(
        worker, available=[*worker.available, "weekly-digest"], resident=list(worker.available)
    )
    return config


@pytest.mark.parametrize("entry_names_skill", [False, True], ids=["entry-does-not-name", "entry-names"])
async def test_a_worker_deferring_a_skill_the_entry_agent_holds_does_not_park_its_gate(tmp_path, entry_names_skill):
    config = _worker_defers_skill(_config(tmp_path, default_confirm=["weekly-digest"]))
    if entry_names_skill:
        entry = config.agents["assistant"]
        config.agents["assistant"] = dataclasses.replace(entry, available=[*entry.available, "weekly-digest"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        assert "weekly-digest" in {skill.name for skill in assistant._state.skill_manager.skills.values()}
        policy = assistant._human.policies.default
        assert policy.pending_confirm == frozenset()
        assert _skill_tool_names(assistant) <= policy.confirm
    finally:
        assistant._state.close()


async def test_approval_entries_resolve_against_the_expansion(tmp_path):
    """An "all" worker reaches every toolset it could load, so its own entry on a deferred one is
    pending rather than refused as out of reach, and resolves when the worker builds it."""
    from kokua.config.schema import ALL_TOOLSETS
    from kokua.registry.context import ToolsetContext
    from kokua.registry.registry import build_tools

    agents = {
        "assistant": AgentConfig(available=["time"], delegates_to=["explorer"]),
        "explorer": AgentConfig(
            description="Explores.",
            available=ALL_TOOLSETS,
            resident=["time"],
            confirm=["compute.run_command"],
            review=[],
        ),
    }
    config = _config(tmp_path, agents=agents, default_confirm=[], default_review=[])
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        state = assistant._state
        assert assistant._human.policies.by_agent["explorer"].pending_confirm == {"compute.run_command"}
        build_tools([state.registry["compute"]], ToolsetContext(state=state, agent=None, agent_name="explorer"))
        explorer = assistant._human.policies.by_agent["explorer"]
        assert explorer.pending_confirm == frozenset()
        assert explorer.confirm == {"run_command"}
    finally:
        assistant._state.close()


async def test_a_workers_own_pair_on_a_skill_it_defers_waits_for_its_build(tmp_path):
    """Nothing holds the skill at startup here (the entry agent neither names it nor authors skills), so
    the worker's own entry waits, and resolves when the worker's build of the skill reports its tools."""
    from kokua.registry.context import ToolsetContext
    from kokua.registry.registry import build_tools

    agents = {
        "assistant": AgentConfig(available=["time"], delegates_to=["helper"]),
        "helper": AgentConfig(
            description="Helps.",
            available=["time", "weekly-digest"],
            resident=["time"],
            confirm=["weekly-digest"],
            review=[],
        ),
    }
    config = _config(tmp_path, agents=agents, default_confirm=[], default_review=[])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        state = assistant._state
        assert assistant._human.policies.by_agent["helper"].pending_confirm == {"weekly-digest"}
        build_tools([state.registry["weekly-digest"]], ToolsetContext(state=state, agent=None, agent_name="helper"))
        helper = assistant._human.policies.by_agent["helper"]
        assert helper.pending_confirm == frozenset()
        assert set(state.all_skills.skills["weekly-digest"].script_tool_names()) <= helper.confirm
    finally:
        assistant._state.close()


# --- every agent's calls are judged by its own policy -------------------------------------------------


async def test_a_worker_spec_carries_its_own_gate(tmp_path):
    from kokua.core.agents import build_agent_specs

    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    specs = build_agent_specs(assistant._config, assistant._state, "assistant")
    gate = specs["coder"]["tool_approval"]
    assert gate.func == assistant._human.approve and gate.args == ("coder",)
    assert specs["researcher"]["tool_approval"].args == ("researcher",)
    assistant._state.close()


async def test_a_workers_declared_policy_decides_its_calls(tmp_path):
    """coder declares run_command as its only confirm entry, while the default tier gates fs_write only."""
    from kokua.channels.web import streaming_conversation

    config = _declaring(_config(tmp_path), coder=(["compute.run_command"], []))
    config = dataclasses.replace(config, default_confirm=["fs_write"], default_review=[])
    channel = FakeChannel()
    assistant = await Assistant.create(config, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()

    # write_file is in the default tier but not in coder's own policy, so coder runs it unprompted.
    assert await assistant._human.approve("coder", "write_file", {}) is True
    assert channel.sent == []

    token = streaming_conversation.set(assistant._active_id)
    try:
        task = asyncio.create_task(assistant._human.approve("coder", "run_command", {"command": "ls"}))
        await asyncio.sleep(0.01)
        assert assistant._human.approval.pending
        assistant._human.approval.resolve(False)
        assert await task is False
        # The entry agent has the default tier, which does not gate run_command.
        assert await assistant._human.approve("assistant", "run_command", {}) is True
    finally:
        streaming_conversation.reset(token)
    assistant._state.close()


async def test_an_unattended_turn_denies_a_workers_gated_call(tmp_path):
    from kokua.channels.web import proactive_turn

    config = _declaring(_config(tmp_path), coder=(["compute.run_command"], []))
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(config, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    token = proactive_turn.set(True)
    try:
        assert await assistant._human.approve("coder", "run_command", {}) is False
    finally:
        proactive_turn.reset(token)
    assert channel.alerts == []
    assistant._state.close()


async def test_a_rebuilt_agent_keeps_its_gate(tmp_path):
    assistant = await Assistant.create(
        _config(tmp_path), FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([])
    )
    await assistant.start()
    conversation = assistant._active_id
    first = assistant._registry.get(conversation)
    assistant._registry.drop_agent(conversation)
    rebuilt = assistant._registry.get(conversation)
    assert rebuilt is not first
    assert rebuilt.tool_approval.func == assistant._human.approve
    assert rebuilt.tool_approval.args == ("assistant",)
    assistant._state.close()


async def test_review_entries_prompt_when_auto_approval_is_off(tmp_path):
    """A review entry is a gate even with no reviewer to ask: the prompt the user would have got stays."""
    from kokua.channels.web import streaming_conversation

    config = _config(tmp_path, default_confirm=[], default_review=["compute.run_command"])
    channel = FakeChannel()
    assistant = await Assistant.create(config, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.auto_approval is None
    token = streaming_conversation.set(assistant._active_id)
    try:
        task = asyncio.create_task(assistant._gate_for("assistant")("run_command", {"command": "ls"}))
        await asyncio.sleep(0.01)
        assert assistant._human.approval.pending
        assistant._human.approval.resolve(True)
        assert await task is True
    finally:
        streaming_conversation.reset(token)
    assistant._state.close()


async def test_the_entry_agents_pair_may_name_a_skills_script_tool(tmp_path):
    """The entry agent's SkillAgent holds every skill, so its pair resolves against their toolsets too."""
    from tests.core.test_skill_toolsets import write_skill

    config = _config(tmp_path, default_confirm=["fs_write"], default_review=[])
    write_skill(config, "citation-check", "Verify a claim.", script="print('checked')\n")
    config = _declaring(config, assistant=(["citation-check.citation_check__run"], []))
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        policy = resolve_approval_policies(config, assistant._state, assistant._agent).for_agent("assistant")
        assert policy.confirm == {"citation_check__run"}
    finally:
        assistant._state.close()


async def test_an_unattended_turn_runs_a_tool_the_workers_own_pair_does_not_gate(tmp_path):
    """The inverse of the denial above: the default tier gates fs_write, but coder declared only
    run_command, so coder's write goes through even when nobody is watching."""
    from kokua.channels.web import proactive_turn

    config = _declaring(_config(tmp_path), coder=(["compute.run_command"], []))
    config = dataclasses.replace(config, default_confirm=["fs_write"], default_review=[])
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(config, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    token = proactive_turn.set(True)
    try:
        assert await assistant._human.approve("coder", "write_file", {}) is True
    finally:
        proactive_turn.reset(token)
    assistant._state.close()


# Residency: an approval entry naming a toolset nobody has built yet waits, and resolves the moment a
# build produces its tools. These tests hand-build the state, because nothing defers at wiring yet.


def _deferred_setup(tmp_path, **config_overrides):
    from types import SimpleNamespace

    from kokua.config.schema import AgentConfig
    from kokua.core.approval import resolve_approval_policies
    from kokua.registry.context import LiveState

    agents = {"assistant": AgentConfig(available=["memory", "mcp", "compute"], resident=["memory"])}
    config_overrides = {"default_confirm": [], "default_review": [], **config_overrides}
    config = AssistantConfig(data_dir=tmp_path, agents=agents, entry_agent="assistant", **config_overrides)
    # A real registry: which toolsets wait for a build depends on provenance (an entry agent's skills never do).
    state = LiveState(config=config, registry=build_registry(config))
    state.tools_by_toolset = {"memory": {"remember"}}
    policies = resolve_approval_policies(config, state, SimpleNamespace(tools=[]))
    return config, state, policies


def test_an_entry_for_a_deferred_toolset_is_pending_at_startup(tmp_path):
    _, _, policies = _deferred_setup(tmp_path, default_confirm=["mcp.add_mcp_server"])
    assert "mcp.add_mcp_server" in policies.default.pending_confirm
    assert "add_mcp_server" not in policies.default.gated


def test_building_the_toolset_resolves_its_pending_entries(tmp_path):
    _, _, policies = _deferred_setup(tmp_path, default_confirm=["mcp.add_mcp_server"])
    updated = policies.with_built("mcp", frozenset({"add_mcp_server", "remove_mcp_server"}))
    assert "add_mcp_server" in updated.default.confirm
    assert "remove_mcp_server" not in updated.default.confirm
    assert updated.default.pending_confirm == frozenset()
    assert policies.default.pending_confirm  # the original object is unchanged
    assert "add_mcp_server" not in policies.default.confirm


def test_a_bare_toolset_entry_resolves_to_every_built_tool(tmp_path):
    _, _, policies = _deferred_setup(tmp_path, default_review=["compute"])
    updated = policies.with_built("compute", frozenset({"calculate", "run_command"}))
    assert updated.default.review >= {"calculate", "run_command"}
    assert updated.default.pending_review == frozenset()


def test_building_one_toolset_leaves_another_toolsets_entries_pending(tmp_path):
    _, _, policies = _deferred_setup(tmp_path, default_confirm=["mcp.add_mcp_server", "compute.run_command"])
    updated = policies.with_built("mcp", frozenset({"add_mcp_server"}))
    assert updated.default.pending_confirm == frozenset({"compute.run_command"})


def test_a_declared_pair_waits_on_its_own_agents_deferred_toolsets(tmp_path):
    from types import SimpleNamespace

    from kokua.config.schema import AgentConfig
    from kokua.core.approval import resolve_approval_policies
    from kokua.registry.context import LiveState

    agents = {
        "assistant": AgentConfig(
            available=["memory", "mcp"], resident=["memory"], confirm=["mcp.add_mcp_server"], review=[]
        )
    }
    config = AssistantConfig(
        data_dir=tmp_path, agents=agents, entry_agent="assistant", default_confirm=[], default_review=[]
    )
    # A real registry: which toolsets wait for a build depends on provenance (an entry agent's skills never do).
    state = LiveState(config=config, registry=build_registry(config))
    state.tools_by_toolset = {"memory": {"remember"}}
    policies = resolve_approval_policies(config, state, SimpleNamespace(tools=[]))
    assert policies.by_agent["assistant"].pending_confirm == frozenset({"mcp.add_mcp_server"})
    updated = policies.with_built("mcp", frozenset({"add_mcp_server"}))
    assert updated.by_agent["assistant"].confirm == frozenset({"add_mcp_server"})


def test_a_misnamed_tool_entry_refuses_the_build(tmp_path):
    _, _, policies = _deferred_setup(tmp_path, default_confirm=["mcp.add_mcp_servr"])
    with pytest.raises(
        ConfigError,
        match=r"\[assistant\.tools\]\.confirm names 'mcp\.add_mcp_servr', but the 'mcp' toolset, now built, "
        r"provides no such tool",
    ):
        policies.with_built("mcp", frozenset({"add_mcp_server"}))


def test_a_built_tool_in_both_halves_is_refused(tmp_path):
    """The backstop for an overlap no entry's text reveals: a review already holding the tool's name
    through another toolset, while the confirm entry naming it waits on ``mcp``."""
    import dataclasses

    _, _, policies = _deferred_setup(tmp_path, default_confirm=["mcp.add_mcp_server"])
    default = dataclasses.replace(policies.default, review=frozenset({"add_mcp_server"}))
    policies = dataclasses.replace(policies, default=default)
    with pytest.raises(ConfigError, match="in both confirm and review"):
        policies.with_built("mcp", frozenset({"add_mcp_server"}))


@pytest.mark.parametrize(
    ("confirm", "review"),
    [
        ("mcp.add_mcp_server", "mcp"),
        ("mcp.add_mcp_server", "mcp.*"),
        ("mcp", "mcp.add_mcp_server"),
        ("mcp.add_mcp_server", "mcp.add_mcp_server"),
    ],
)
def test_pending_entries_certain_to_overlap_fail_startup(tmp_path, confirm, review):
    """Decidable from the text alone: if the named tool exists, both halves hold it; if not, the tool
    entry is dead and the build fails anyway. Either way waiting for the build only refuses every load."""
    with pytest.raises(ConfigError, match=r"\[assistant\.tools\] gates mcp\.add_mcp_server in both confirm and review"):
        _deferred_setup(tmp_path, default_confirm=[confirm], default_review=[review])


def test_whole_toolset_entries_pending_in_both_halves_fail_startup(tmp_path):
    with pytest.raises(ConfigError, match=r"gates mcp in both confirm and review"):
        _deferred_setup(tmp_path, default_confirm=["mcp"], default_review=["mcp.*"])


def test_a_declared_pairs_pending_overlap_fails_startup_naming_its_table(tmp_path):
    from types import SimpleNamespace

    from kokua.config.schema import AgentConfig
    from kokua.core.approval import resolve_approval_policies
    from kokua.registry.context import LiveState

    agents = {
        "assistant": AgentConfig(
            available=["memory", "mcp"], resident=["memory"], confirm=["mcp"], review=["mcp.add_mcp_server"]
        )
    }
    config = AssistantConfig(
        data_dir=tmp_path, agents=agents, entry_agent="assistant", default_confirm=[], default_review=[]
    )
    state = LiveState(config=config, registry=build_registry(config))
    state.tools_by_toolset = {"memory": {"remember"}}
    with pytest.raises(ConfigError, match=r"\[agents\.assistant\.tools\] gates mcp\.add_mcp_server in both"):
        resolve_approval_policies(config, state, SimpleNamespace(tools=[]))


@pytest.mark.parametrize(
    ("confirm", "review"),
    [("mcp.add_mcp_server", "mcp.remove_mcp_server"), ("mcp.add_mcp_server", "compute"), ("mcp", "compute.*")],
)
def test_pending_entries_that_may_not_overlap_still_start(tmp_path, confirm, review):
    _, _, policies = _deferred_setup(tmp_path, default_confirm=[confirm], default_review=[review])
    assert policies.default.pending_confirm == frozenset({confirm})


@pytest.mark.parametrize("entry", ["mcp", "mcp.*", "mcp.add_mcp_server"])
def test_a_build_through_build_tools_resolves_a_pending_entry(tmp_path, entry):
    """The mechanism a composed worker's late build relies on: the hook set on the state reaches a
    policies holder, so the tool is gated from the moment the build returns. Two tools, because a hook
    called once per tool would let a bare entry be drained by the first call and leave the second ungated."""
    from kokua.registry.context import ToolsetContext
    from kokua.registry.registry import Toolset, build_tools

    _, state, policies = _deferred_setup(tmp_path, default_confirm=[entry])
    holder = [policies]
    state.toolset_built = lambda name, names: holder.__setitem__(0, holder[0].with_built(name, names))

    def add_mcp_server():
        pass

    def remove_mcp_server():
        pass

    toolset = Toolset(name="mcp", description="mcp", build=lambda ctx: [add_mcp_server, remove_mcp_server])
    build_tools([toolset], ToolsetContext(state=state, agent=None, agent_name="composed:x"))
    confirmed = holder[0].for_agent("composed:x").confirm
    assert "add_mcp_server" in confirmed
    assert ("remove_mcp_server" in confirmed) == (entry != "mcp.add_mcp_server")


@pytest.mark.parametrize("entry", ["mcp.add*", "mcp.a.b", "mc*p.add_mcp_server"])
def test_a_malformed_entry_for_a_deferred_toolset_still_fails_startup(tmp_path, entry):
    with pytest.raises(ConfigError, match="would gate nothing"):
        _deferred_setup(tmp_path, default_confirm=[entry])


def test_a_refused_build_names_the_resolvers_reason(tmp_path):
    _, _, policies = _deferred_setup(tmp_path, default_confirm=["mcp.add_mcp_servr"])
    with pytest.raises(ConfigError, match=r"did you mean 'add_mcp_server'"):
        policies.with_built("mcp", frozenset({"add_mcp_server"}))


# A build re-resolves every entry naming its toolset, not only the pending ones: a toolset can offer more
# tools than it did when an entry first resolved (a skill gains a script mid-session), and an entry naming
# the whole toolset has to cover what it offers now.


def test_a_later_build_grows_a_resolved_whole_toolset_entry(tmp_path):
    _, _, policies = _deferred_setup(tmp_path, default_review=["memory"])
    assert policies.default.review == frozenset({"remember"})
    updated = policies.with_built("memory", frozenset({"remember", "recall"}))
    assert updated.default.review == frozenset({"remember", "recall"})


def test_a_resolved_tool_entry_missing_from_a_later_build_neither_refuses_nor_shrinks(tmp_path):
    """Its earlier build already proved it names a real tool, and a gate never loses a name it holds."""
    _, _, policies = _deferred_setup(tmp_path, default_confirm=["memory.remember"])
    updated = policies.with_built("memory", frozenset({"recall"}))
    assert updated.default.confirm == frozenset({"remember"})


def test_a_build_never_gates_activate_skill_through_a_toolset_entry(tmp_path):
    """``activate_skill`` arrives in every skill's build, and gating one skill must not gate them all."""
    _, _, policies = _deferred_setup(tmp_path, default_review=["memory"])
    updated = policies.with_built("memory", frozenset({"activate_skill", "remember"}))
    assert "activate_skill" not in updated.default.review


def _second_script_agents(available, resident=None, **pair) -> dict:
    return {
        "assistant": AgentConfig(available=["skills", "time"], delegates_to=["explorer"]),
        "explorer": AgentConfig(description="Explores.", available=available, resident=resident, **pair),
    }


async def _add_second_script(assistant) -> None:
    add_skill_script = next(fn for fn in assistant._agent.tools if fn.__name__ == "add_skill_script")
    result = await add_skill_script(skill_name="weekly-digest", filename="second.py", content="print('b')\n")
    assert "is now callable" in result


async def _gated_unattended(assistant, agent: str, tool: str) -> bool:
    """Whether ``agent`` calling ``tool`` is gated: an unattended turn denies a gated call and runs any other."""
    from kokua.channels.web import proactive_turn

    token = proactive_turn.set(True)
    try:
        return await assistant._human.approve(agent, tool, {}) is False
    finally:
        proactive_turn.reset(token)


@pytest.mark.parametrize("declaration", ["list", "all"])
async def test_a_bare_skill_entry_gates_a_script_added_mid_session_for_a_worker(tmp_path, declaration):
    """The worker receives the new script on its next spawn, so the gate on the skill has to cover it by
    then. The list worker inherits the default tier; the "all" worker declares its own pair."""
    from kokua.config.schema import ALL_TOOLSETS

    if declaration == "list":
        agents = _second_script_agents(["time", "weekly-digest"])
    else:
        agents = _second_script_agents(ALL_TOOLSETS, ["time", "weekly-digest"], confirm=[], review=["weekly-digest"])
    config = _config(
        tmp_path, agents=agents, default_confirm=["skills.add_skill_script"], default_review=["weekly-digest"]
    )
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        await _add_second_script(assistant)
        review = assistant._human.policies.for_agent("explorer").review
        assert {"weekly_digest__collect_notes", "weekly_digest__second"} <= review
        assert "activate_skill" not in review
        assert await _gated_unattended(assistant, "explorer", "weekly_digest__second")
    finally:
        assistant._state.close()


async def test_a_bare_skill_entry_gates_a_script_the_entry_agent_adds_to_itself(tmp_path):
    """No worker builds the skill here, so only the refresh itself can grow the policy, and the entry
    agent's ``SkillAgent`` can call the new script in its very next round."""
    agents = {"assistant": AgentConfig(available=["skills", "time"])}
    config = _config(
        tmp_path, agents=agents, default_confirm=["skills.add_skill_script"], default_review=["weekly-digest"]
    )
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    try:
        await assistant.start()
        await _add_second_script(assistant)
        assert await _gated_unattended(assistant, "assistant", "weekly_digest__second")
    finally:
        assistant._state.close()


def test_core_namespace_gains_the_group_tools_when_something_is_deferred(tmp_path):
    from types import SimpleNamespace

    from kokua.config.schema import AgentConfig
    from kokua.core.agents import gateable_tools
    from kokua.registry.context import LiveState

    def core_tools(resident):
        agents = {"assistant": AgentConfig(available=["memory", "mcp"], resident=resident)}
        config = AssistantConfig(data_dir=tmp_path, agents=agents, entry_agent="assistant")
        state = LiveState(config=config, registry=build_registry(config))
        return gateable_tools(state, SimpleNamespace(tools=[]))["core"]

    assert core_tools(["memory"]) >= {"available_tools", "load_tools"}
    assert not core_tools(["memory", "mcp"]) & {"available_tools", "load_tools"}


@pytest.mark.parametrize(
    ("tool_name", "mode"),
    [
        ("available_tools", "deferred"),
        ("load_tools", "deferred"),
        ("spawn_subagent", "delegator"),
        ("activate_skill", "skill"),
    ],
)
def test_a_resident_core_name_collision_is_gateable_only_under_core(tmp_path, tool_name, mode):
    from types import SimpleNamespace

    from kokua.config import MCPServerConfig
    from kokua.config.schema import AgentConfig
    from kokua.core.approval import _vocabulary_for
    from kokua.core.agents import build_registry, gateable_tools
    from kokua.registry.context import LiveState
    from tests.core.test_skill_toolsets import write_skill

    assistant_config = AgentConfig(available=["stocks", "time"], resident=["stocks", "time"])
    agents = {"assistant": assistant_config}
    if mode == "deferred":
        assistant_config.available = ["stocks", "time", "web"]
    elif mode == "delegator":
        assistant_config.delegates_to = ["worker"]
        agents["worker"] = AgentConfig(available=["time"])
    elif mode == "skill":
        assistant_config.available.append("weekly-digest")

    config = _config(
        tmp_path,
        agents=agents,
        mcp_servers=[MCPServerConfig(url="https://broker/mcp", name="stocks")],
    )
    if mode == "skill":
        write_skill(config, "weekly-digest", "Digest the week.")
    state = LiveState(config=config, registry=build_registry(config))
    state.tools_by_toolset["stocks"] = {tool_name, "get_quote"}

    entry_tools = []
    if mode == "delegator":

        def spawn_subagent():
            pass

        entry_tools.append(spawn_subagent)
    full = gateable_tools(state, SimpleNamespace(tools=entry_tools))
    vocabulary = _vocabulary_for("assistant", config, state, full)
    assert tool_name not in vocabulary["stocks"]
    assert tool_name in vocabulary["core"]
    assert "get_quote" in vocabulary["stocks"]


def test_core_gate_vocabulary_is_scoped_to_each_agent(tmp_path):
    from types import SimpleNamespace

    from kokua.config.schema import AgentConfig
    from kokua.core.approval import _vocabulary_for
    from kokua.core.agents import build_registry, gateable_tools
    from kokua.registry.context import LiveState

    agents = {
        "assistant": AgentConfig(available=["time"]),
        "worker": AgentConfig(available=["time"], delegates_to=["nested"]),
        "nested": AgentConfig(available=["time"]),
    }
    config = _config(tmp_path, agents=agents)
    state = LiveState(config=config, registry=build_registry(config))
    full = gateable_tools(state, SimpleNamespace(tools=[]))

    assert "spawn_subagent" not in _vocabulary_for("assistant", config, state, full)["core"]
    assert "spawn_subagent" in _vocabulary_for("worker", config, state, full)["core"]


async def test_assistant_hook_is_a_no_op_before_policies_exist_and_replaces_them_after(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assistant._human.policies = None
    assistant._on_toolset_built("mcp", frozenset({"add_mcp_server"}))
    assert assistant._human.policies is None

    await assistant.start()
    before = assistant._human.policies
    assistant._on_toolset_built("mcp", frozenset({"add_mcp_server"}))
    assert assistant._human.policies is not before  # replaced, never mutated
    assistant._state.close()


async def test_a_worker_composed_with_a_deferred_toolset_is_gated_by_the_pending_entry(tmp_path, monkeypatch):
    """The only declared holder of ``mcp`` defers it, so it is unbuilt at startup and its approval entry is
    pending. Composing a worker with ``mcp`` builds it through ``build_tools``, which resolves the entry
    before any tool reaches the worker, so the worker's own policy gates ``add_mcp_server``."""
    import kokua.core.agents  # noqa: F401  (bind the real spawn factory before the patch below)
    from kokua.config.schema import AgentConfig
    from kokua.registry.context import ToolsetContext
    from kokua.toolsets.capabilities import make_capability_tools

    class _Spawn:
        def __call__(self, model, **kwargs):
            async def spawn_subagent(agent_type: str, task: str, ctx=None) -> str:
                return "ran"

            return spawn_subagent

    monkeypatch.setattr("aimu.aio.tools.builtin.make_async_subagent_tool", _Spawn())
    config = _config(
        tmp_path,
        agents={"assistant": AgentConfig(available=["time", "mcp", "capabilities"], resident=["time", "capabilities"])},
        default_confirm=["mcp.add_mcp_server"],
    )
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert "mcp" not in assistant._state.tools_by_toolset or not assistant._state.tools_by_toolset["mcp"]

    compose = next(
        fn
        for fn in make_capability_tools(ToolsetContext(state=assistant._state, agent=object(), agent_name="assistant"))
        if fn.__name__ == "compose_subagent"
    )
    label = "composed:registrar"
    assert "add_mcp_server" not in assistant._human.policies.for_agent(label).confirm  # still pending

    await compose("registrar", "Add a server.", ["mcp"], "Manage servers.")

    assert "add_mcp_server" in assistant._human.policies.for_agent(label).confirm


async def test_a_real_mcp_tool_shadowed_by_fs_is_reported_at_startup(tmp_path, monkeypatch):
    """No stubs between the config and the alert: a connected server's tool names reach
    ``tools_by_toolset`` through the startup builds, and trust order puts Kokua's own ``fs`` first."""
    import kokua.core.agents as agents_mod
    from aimu import aio
    from kokua.config.schema import ALL_TOOLSETS, MCPServerConfig
    from tests.fakes import _FakeMCP, _fake_mcp_tool

    async def fake_connect(*, url=None, auth=None, **kw):
        return _FakeMCP([_fake_mcp_tool("read_file")])

    monkeypatch.setattr(aio.MCPClient, "connect", fake_connect)
    captured: list = []

    def fake_make(model, *, agent_types, tool_approval, **kwargs):
        captured.append(agent_types)

        async def spawn_subagent(agent_type: str, task: str) -> str:
            """menu"""
            return "ok"

        spawn_subagent.__tool_is_async__ = True
        spawn_subagent.__tool_is_streaming__ = False
        spawn_subagent.__tool_spec__ = {"function": {"name": "spawn_subagent"}}
        return spawn_subagent

    monkeypatch.setattr(agents_mod, "make_async_subagent_tool", fake_make)
    agents = {
        "assistant": AgentConfig(available=["time"], delegates_to=["explorer"]),
        "explorer": AgentConfig(description="Explores.", available=ALL_TOOLSETS, resident=["fs", "files"]),
    }
    config = _config(
        tmp_path,
        agents=agents,
        mcp_servers=[MCPServerConfig(url="https://files.example/mcp", name="files")],
        default_confirm=[],
        default_review=[],
    )
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(config, channel, client=MockAsyncModelClient([]))
    await assistant.start()

    assert [text for text, *_ in channel.alerts] == [
        "explorer: read_file from 'files' is shadowed by 'fs' (the earlier resident toolset keeps the name)."
    ]
    kept = [fn for fn in captured[-1]["explorer"]["tools"] if fn.__name__ == "read_file"]
    assert len(kept) == 1 and kept[0].__module__ != "tests.fakes"
    assistant._state.close()


async def test_an_agent_whose_toolsets_were_never_built_is_skipped_not_reported(tmp_path, caplog):
    """An agent nothing delegates to is never built at startup, so its toolsets' names are unknown and
    "no shadows" would be a claim nobody checked."""
    import logging

    from kokua.config.schema import ALL_TOOLSETS

    agents = {
        "assistant": AgentConfig(available=["time"]),
        "orphan": AgentConfig(description="Unreached.", available=ALL_TOOLSETS, resident=["fs", "time"]),
    }
    config = _config(tmp_path, agents=agents, default_confirm=[], default_review=[])
    assistant = await Assistant.create(config, AlertCapturingChannel(), client=MockAsyncModelClient([]))
    with caplog.at_level(logging.INFO, logger="kokua.core.agents"):
        await assistant.start()
    assert any("orphan" in record.getMessage() for record in caplog.records)
    assistant._state.close()


async def test_a_failing_shadow_alert_does_not_stop_startup(tmp_path, monkeypatch):
    from kokua.core import agents
    from kokua.core.agents import ShadowedTool

    monkeypatch.setattr(
        agents, "shadowed_tools", lambda config, registry, tools: [ShadowedTool("explorer", "read_file", "fs", "files")]
    )
    assistant = await Assistant.create(_config(tmp_path), AlertCapturingChannel(), client=MockAsyncModelClient([]))

    async def broken_alert(text, **kwargs):
        raise RuntimeError("channel went away")

    armed: list[bool] = []
    monkeypatch.setattr(assistant._ui, "alert", broken_alert)
    monkeypatch.setattr(type(assistant._state.tasks), "arm_all", lambda self: armed.append(True))
    await assistant.start()

    assert armed == [True]
    assistant._state.close()


async def test_startup_notices_each_shadowed_resident_tool(tmp_path, monkeypatch):
    from kokua.core import agents
    from kokua.core.agents import ShadowedTool

    shadow = ShadowedTool("explorer", "read_file", "fs", "files")
    monkeypatch.setattr(agents, "shadowed_tools", lambda config, registry, tools: [shadow])
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(_config(tmp_path), channel, client=MockAsyncModelClient([]))
    await assistant.start()
    await assistant.start()  # idempotent: the notice is raised once

    assert [text for text, *_ in channel.alerts] == [
        "explorer: read_file from 'files' is shadowed by 'fs' (the earlier resident toolset keeps the name)."
    ]
    assistant._state.close()
