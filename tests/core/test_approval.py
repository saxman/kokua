"""The tool-approval gate: what is gated, who may answer, and how the answer is routed."""

from __future__ import annotations

import asyncio

import pytest

from aimu.aio.channels.base import Channel, ChannelMessage

from kokua.config import ConfigError
from kokua.config.schema import AssistantConfig, ReviewerConfig
from kokua.core.assistant import Assistant
from tests.channels import AlertCapturingChannel, FakeChannel, _config
from tests.fakes import _RequestsToolOnce
from tests.helpers import MockAsyncModelClient


async def test_assistant_wires_approval_policy(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant._agent.tool_approval == assistant._approve


async def test_approve_allows_ungated_tool_without_prompting(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["skills.add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    await assistant.start()
    assert await assistant._approve("get_weather", {}) is True
    assert channel.sent == []  # no prompt for an ungated tool


async def _noop_prompt() -> None:
    """A prompt that sends nothing: these tests exercise the pending-request slot, not the channel."""


async def test_approve_gated_tool_waits_for_routed_answer(tmp_path):
    from kokua.channels.web import streaming_conversation

    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["skills.add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    await assistant.start()
    # Foreground: the calling turn's conversation is the one currently viewed.
    token = streaming_conversation.set(assistant._active_id)
    try:
        task = asyncio.create_task(assistant._approve("add_skill_script", {"skill_name": "x"}))
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
        _config(tmp_path, confirm_tools=["skills.add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    await assistant.start()
    # Background: the calling turn's conversation ("elsewhere") isn't the one being viewed.
    token = streaming_conversation.set("elsewhere")
    try:
        assert await assistant._approve("add_skill_script", {}) is False
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
    cfg = _config(tmp_path, confirm_tools=["skills.add_skill_script"])
    client = _RequestsToolOnce("add_skill_script", {"skill_name": "disk", "filename": "u.py", "content": "print(1)\n"})
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
    await assistant.start()
    # No streaming_conversation is set around this call, so it defaults to None -- not the viewed
    # conversation -- making _approve auto-deny without an interactive prompt. That exercises the real
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

    cfg = _config(tmp_path, confirm_tools=["compute.execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    prompts: list[str] = []
    order: list[str] = []

    async def fake_prompt(name, arguments):
        prompts.append(name)
        # Yield to the event loop before resolving so the two gathered coroutines can interleave.
        # Without the lock the second call overwrites the pending slot here, causing the
        # first call to resolve the wrong future and the second to deadlock (or raise
        # InvalidStateError if its future is resolved twice).
        await asyncio.sleep(0)
        assistant._human.approval.resolve(True)

    assistant._ui.ask_approval = fake_prompt

    async def call(tag):
        result = await assistant._approve("execute_python", {"code": tag})
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

    cfg = _config(tmp_path, confirm_tools=["compute.execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    viewed = assistant._active_id
    await assistant.new_conversation()  # _active_id now the new (background) conversation
    background = assistant._active_id
    await assistant.select_conversation(viewed)  # make `viewed` active again
    # A turn running in `background` is not the viewed conversation -> auto-deny.
    token = streaming_conversation.set(background)
    try:
        assert await assistant._approve("execute_python", {}) is False
    finally:
        streaming_conversation.reset(token)


async def test_foreground_turn_prompts_for_approval(tmp_path):
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, confirm_tools=["compute.execute_python"])
    channel = FakeChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    viewed = assistant._active_id
    token = streaming_conversation.set(viewed)
    try:
        approve_task = asyncio.create_task(assistant._approve("execute_python", {}))
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
    assert config.confirm_tools == AssistantConfig().confirm_tools
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.gated_tools == {
        "add_skill_script",
        "add_mcp_server",
        "execute_python",
        "run_command",
        "write_file",
        "edit_file",
        "update_config",
    }
    # And the benign member of a toolset one of those entries names is deliberately not gated.
    assert "calculate" not in assistant._human.gated_tools


async def test_an_empty_gate_list_is_still_valid(tmp_path):
    """[] is the documented way to turn approval off, so it has to stay a legal value."""
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=[]), FakeChannel(), client=MockAsyncModelClient([])
    )
    await assistant.start()
    assert assistant._human.gated_tools == frozenset()


async def _gated(tmp_path, entries) -> frozenset:
    """The tool names ``entries`` resolves to on an otherwise default config."""
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=entries), FakeChannel(), client=MockAsyncModelClient([])
    )
    await assistant.start()
    return assistant._human.gated_tools


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
    config = _config(tmp_path, confirm_tools=["activate_skill"])
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
        _config(tmp_path, confirm_tools=["compute.execute_python"]), FakeChannel(), client=MockAsyncModelClient([])
    )
    assert "execute_python" not in {fn.__name__ for fn in assistant._agent.tools}


async def test_the_delegation_tool_is_gateable(tmp_path):
    """spawn_subagent is attached to the entry agent after its toolsets are built, so no toolset owns
    it and the reserved `core` namespace is the only prefix it can have."""
    assert await _gated(tmp_path, ["core.spawn_subagent"]) == {"spawn_subagent"}


async def test_the_reserved_namespace_gates_both_of_its_members(tmp_path):
    config = _config(tmp_path, confirm_tools=["core"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.gated_tools == {"spawn_subagent", "activate_skill"}
    assistant._state.close()


async def test_a_skill_script_is_gateable(tmp_path):
    """A skill script is an unsandboxed subprocess, so it is worth gating, and an AIMU SkillAgent
    attaches its script tools on first run rather than at startup. The vocabulary is therefore derived
    from the skills on disk, not read off the built agent."""
    config = _config(tmp_path, confirm_tools=["weekly-digest.weekly_digest__collect_notes", "core.activate_skill"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.gated_tools == {"weekly_digest__collect_notes", "activate_skill"}
    assistant._state.close()


async def test_a_bare_skill_gates_its_scripts_and_not_activate_skill(tmp_path):
    """Every skill's tool list carries activate_skill, so attributing it per skill would let one
    skill's gate stop every skill from being loaded. It belongs to `core`, and only there."""
    config = _config(tmp_path, confirm_tools=["weekly-digest"])
    _write_skill(config)
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()
    assert assistant._human.gated_tools == {"weekly_digest__collect_notes"}
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
    with pytest.raises(RuntimeError):
        await gate.approve("execute_python", {})


async def test_background_auto_deny_raises_an_alert(tmp_path):
    """The deny is silent from the user's side: the tool result saying so is on a transcript they are
    not reading, and the turn carries on without the tool. The card is what makes it visible while
    there is still something to do about it."""
    from kokua.channels.web import streaming_conversation

    cfg = _config(tmp_path, confirm_tools=["compute.execute_python"])
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    viewed = assistant._active_id
    await assistant.new_conversation()
    background = assistant._active_id
    await assistant.select_conversation(viewed)

    token = streaming_conversation.set(background)
    try:
        assert await assistant._approve("execute_python", {}) is False
        assert await assistant._approve("execute_python", {}) is False  # same tool, same card
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

    cfg = _config(tmp_path, confirm_tools=["compute.execute_python"])
    channel = AlertCapturingChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
    await assistant.start()
    token = proactive_turn.set(True)
    try:
        assert await assistant._approve("execute_python", {}) is False
    finally:
        proactive_turn.reset(token)
    assert channel.alerts == []


async def test_assistant_wires_the_auto_approval_gate(tmp_path):
    """Startup resolves `[security.auto_approval]` onto the gate, which is what makes it reachable.

    Asserted here beside `test_assistant_wires_approval_policy` for the reason that one exists: every
    other test of the review layer either assigns `HumanGate.auto_approval` by hand or reaches the
    resolver through a helper, so the line in `start` that connects the two is the kind of thing a
    refactor drops with the suite still green and no symptom but a feature that never runs.

    The subset assertion is the pairing the resolver enforces: an auto-approval set reaching a tool
    nothing gates would describe a prompt that never existed.
    """
    cfg = _config(
        tmp_path,
        auto_approval_enabled=True,
        auto_approval_reviewers=["approval"],
        auto_approval_tools=["compute.run_command"],
        reviewers={"approval": ReviewerConfig(model="ollama:b", system_message="judge it")},
    )
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    auto = assistant._human.auto_approval
    assert auto is not None
    assert auto.tools == frozenset({"run_command"})
    assert auto.tools <= assistant._human.gated_tools


async def test_an_assistant_holds_no_gate_when_the_feature_is_off(tmp_path):
    """The shipped default: a started assistant gates tools and reviews none of them."""
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    await assistant.start()

    assert assistant._human.gated_tools is not None
    assert assistant._human.auto_approval is None
