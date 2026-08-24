"""The tool-approval gate: what is gated, who may answer, and how the answer is routed."""

from __future__ import annotations

import asyncio

import pytest

from aimu.aio.channels.base import Channel, ChannelMessage

from kokua.config import ConfigError
from kokua.config.schema import AssistantConfig
from kokua.core.assistant import Assistant
from tests.channels import FakeChannel, _config
from tests.fakes import _RequestsToolOnce
from tests.helpers import MockAsyncModelClient


async def test_assistant_wires_approval_policy(tmp_path):
    assistant = await Assistant.create(_config(tmp_path), FakeChannel(), client=MockAsyncModelClient([]))
    assert assistant._agent.tool_approval == assistant._approve


async def test_approve_allows_ungated_tool_without_prompting(tmp_path):
    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    assert await assistant._approve("get_weather", {}) is True
    assert channel.sent == []  # no prompt for an ungated tool


async def _noop_prompt() -> None:
    """A prompt that sends nothing: these tests exercise the pending-request slot, not the channel."""


async def test_approve_gated_tool_waits_for_routed_answer(tmp_path):
    from kokua.channels.web import streaming_conversation

    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
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


async def test_approve_proactive_auto_denies_gated_tool(tmp_path):
    from kokua.channels.web import streaming_conversation

    channel = FakeChannel()
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["add_skill_script"]), channel, client=MockAsyncModelClient([])
    )
    # Background: the calling turn's conversation ("elsewhere") isn't the one being viewed.
    token = streaming_conversation.set("elsewhere")
    try:
        assert await assistant._approve("add_skill_script", {}) is False
        assert channel.sent == []  # auto-deny: no prompt, no waiting
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
    cfg = _config(tmp_path, confirm_tools=["add_skill_script"])
    client = _RequestsToolOnce("add_skill_script", {"skill_name": "disk", "filename": "u.py", "content": "print(1)\n"})
    assistant = await Assistant.create(cfg, FakeChannel(), client=client)
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

    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client=MockAsyncModelClient([]))

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

    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    assistant = await Assistant.create(cfg, FakeChannel(), client_factory=lambda cid: MockAsyncModelClient([]))
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

    cfg = _config(tmp_path, confirm_tools=["execute_python"])
    channel = FakeChannel()
    assistant = await Assistant.create(cfg, channel, client_factory=lambda cid: MockAsyncModelClient([]))
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
    """The four names config.example.toml ships have to pass the check that rejects a gate naming
    nothing, or the default install would not start."""
    config = _config(tmp_path)
    assert config.confirm_tools == AssistantConfig().confirm_tools
    await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))


async def test_an_empty_gate_list_is_still_valid(tmp_path):
    """[] is the documented way to turn approval off, so it has to stay a legal value."""
    await Assistant.create(_config(tmp_path, confirm_tools=[]), FakeChannel(), client=MockAsyncModelClient([]))


async def test_a_misspelled_gate_fails_startup_and_suggests_the_real_name(tmp_path):
    with pytest.raises(ConfigError) as error:
        await Assistant.create(
            _config(tmp_path, confirm_tools=["execute_pythn"]), FakeChannel(), client=MockAsyncModelClient([])
        )
    message = str(error.value)
    assert "execute_pythn" in message
    assert "execute_python" in message  # the close match, offered so the fix does not need a hunt
    assert "add_mcp_server" in message  # and the reason a not-yet-connected MCP tool cannot be pre-gated


async def test_every_unmatched_gate_is_named_not_only_the_first(tmp_path):
    """One restart per typo is the failure mode this avoids: a user fixing a list of four wants all the
    bad entries in the first error."""
    with pytest.raises(ConfigError) as error:
        await Assistant.create(
            _config(tmp_path, confirm_tools=["execute_pythn", "totally_made_up_tool"]),
            FakeChannel(),
            client=MockAsyncModelClient([]),
        )
    message = str(error.value)
    assert "execute_pythn" in message and "totally_made_up_tool" in message


async def test_a_tool_only_a_worker_holds_is_gateable(tmp_path):
    """execute_python is one of the shipped gates and the entry agent does not hold it: it comes from
    [agents.coder]. A check over the entry agent's own tools alone would reject the default config."""
    assistant = await Assistant.create(
        _config(tmp_path, confirm_tools=["execute_python"]), FakeChannel(), client=MockAsyncModelClient([])
    )
    assert "execute_python" not in {fn.__name__ for fn in assistant._agent.tools}


async def test_the_delegation_tool_is_gateable(tmp_path):
    """spawn_subagent is attached to the entry agent after its toolsets are built, so it is the tool the
    collection point in build_tools cannot see on its own."""
    await Assistant.create(
        _config(tmp_path, confirm_tools=["spawn_subagent"]), FakeChannel(), client=MockAsyncModelClient([])
    )


async def test_a_skill_script_is_gateable(tmp_path):
    """A skill script is an unsandboxed subprocess, so it is worth gating, and an AIMU SkillAgent
    attaches its script tools on first run rather than at startup. The vocabulary is therefore derived
    from the skills on disk, not read off the built agent."""
    config = _config(tmp_path, confirm_tools=["weekly_digest__collect_notes", "activate_skill"])
    scripts = config.skills_dir / "weekly-digest" / "scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "SKILL.md").write_text(
        "---\nname: weekly-digest\ndescription: Digest the week.\n---\n\nDo the thing.\n", encoding="utf-8"
    )
    (scripts / "collect_notes.py").write_text("print('notes')\n", encoding="utf-8")
    assistant = await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    assistant._state.close()


async def test_the_confirm_tools_flag_is_checked_by_the_same_rule(tmp_path):
    """--confirm-tools writes the same field, so a typo there has to fail the same way rather than
    reaching the gate as a name nothing matches."""
    from kokua.cli import build_arg_parser, resolve_config

    config = resolve_config(build_arg_parser().parse_args(["--confirm-tools", "update_confg"]))
    with pytest.raises(ConfigError) as error:
        await Assistant.create(config, FakeChannel(), client=MockAsyncModelClient([]))
    assert "update_confg" in str(error.value) and "update_config" in str(error.value)
