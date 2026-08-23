"""The ``config`` toolset: what read_config / update_config report back to the model.

The policy and the apply-then-persist ordering underneath are covered in ``tests/config/test_store.py``.
"""

from __future__ import annotations

import tomllib

import pytest

from kokua.config import file as settings
from kokua.config import schema
from kokua.config.schema import AgentConfig, AssistantConfig
from kokua.toolsets import config as config_tools
from tests.helpers import core_table


def _read(path):
    with path.open("rb") as file:
        return tomllib.load(file)


def _tools(tmp_path, apply_hot=None, config=None, registry=None):
    async def _noop(section, key, value):
        return None

    path = tmp_path / "config.toml"
    read_config, update_config = config_tools.make_config_tools(
        path,
        apply_hot or _noop,
        core_table(),
        config=config or AssistantConfig(),
        registry=registry if registry is not None else {},
    )
    return path, read_config, update_config


async def test_read_config_returns_file_text(tmp_path):
    path, read_config, _ = _tools(tmp_path)
    path.write_text('# my config\n[assistant]\nmodel = "m"\n', encoding="utf-8")
    text = await read_config()
    assert "# my config" in text and "model" in text


async def test_read_config_when_absent_notes_defaults(tmp_path):
    _, read_config, _ = _tools(tmp_path)
    assert "default" in (await read_config()).lower()


async def test_update_config_writes_scalar_and_reports_restart(tmp_path):
    path, _, update_config = _tools(tmp_path)
    result = await update_config("logging", "level", "DEBUG")
    assert _read(path)["logging"]["level"] == "DEBUG"
    assert "restart" in result.lower()


async def test_update_config_hot_key_applies_live(tmp_path):
    applied = []

    async def apply_hot(section, key, value):
        applied.append((section, key, value))

    path, _, update_config = _tools(tmp_path, apply_hot)
    result = await update_config("display", "show_tools", "false")
    assert _read(path)["display"]["show_tools"] is False
    assert applied == [("display", "show_tools", False)]
    assert "restart" not in result.lower()


async def test_update_config_restart_key_does_not_apply_live(tmp_path):
    applied = []

    async def apply_hot(section, key, value):
        applied.append((section, key, value))

    path, _, update_config = _tools(tmp_path, apply_hot)
    await update_config("web", "port", "9100")
    assert applied == []


async def test_update_config_hot_key_not_persisted_when_apply_fails(tmp_path):
    async def apply_hot(section, key, value):
        raise RuntimeError("bad flag")

    path, _, update_config = _tools(tmp_path, apply_hot)
    result = await update_config("display", "show_tools", "false")
    assert not path.exists()  # apply failed, so nothing was written
    assert "could not be applied" in result.lower()


async def test_update_config_sets_a_cold_toolset_key(tmp_path):
    """The round trip a toolset's *cold* key takes through this tool, which the table alone cannot answer:
    it holds hot settings only, so ``make_config_tools`` resolves the rest from ``startup_schema()``.
    Uses the shipped ``[planning].review_rounds`` deliberately -- the assistant refusing a key that is
    plainly in the user's config file is the failure this pins."""
    path, _, update_config = _tools(tmp_path)

    result = await update_config("planning", "review_rounds", "3")

    assert _read(path)["planning"]["review_rounds"] == 3
    assert "restart" in result.lower()  # cold: saved, effective next startup


async def test_update_config_type_checks_a_cold_toolset_key(tmp_path):
    path, _, update_config = _tools(tmp_path)

    result = await update_config("planning", "review_rounds", "several")

    assert "must be an integer" in result
    assert not path.exists()


async def test_update_config_still_refuses_an_undeclared_key_in_a_toolset_section(tmp_path):
    """Widening the schema with the cold half must not turn a toolset's section into a free-for-all."""
    path, _, update_config = _tools(tmp_path)

    result = await update_config("planning", "made_up", "1")

    assert "unknown config key" in result
    assert not path.exists()


@pytest.mark.parametrize(
    "section,key,value",
    [("security", "confirm_tools", "[]"), ("email", "to", "attacker@x.com"), ("paths", "data_dir", "/tmp/x")],
)
async def test_update_config_refuses_blocklisted_keys(tmp_path, section, key, value):
    path, _, update_config = _tools(tmp_path)
    result = await update_config(section, key, value)
    assert not path.exists()  # nothing written
    assert "hand-edit" in result.lower() or "cannot" in result.lower()


async def test_update_config_rejects_invalid_value_without_writing(tmp_path):
    path, _, update_config = _tools(tmp_path)
    result = await update_config("web", "port", "not-a-number")
    assert not path.exists()
    assert "port" in result


async def test_update_config_points_a_misplaced_key_at_its_real_section(tmp_path):
    """`thinking` is an [assistant] key and [assistant.generation] is the sub-table directly beneath it,
    so this is the miss to expect. The error has to carry the fix: the assistant retries from it alone.

    `thinking` is also an agent key (``AGENT_SCHEMA``'s wildcarded ``agents.*``), so the hint now names
    both places the key lives; this only checks the one this test is about.
    """
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant.generation", "thinking", "medium")

    assert "did you mean" in result
    assert "[assistant].thinking" in result
    assert not path.exists()


async def test_update_config_lists_the_keys_a_known_section_accepts(tmp_path):
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant.generation", "warmth", "0.7")

    assert "Accepted in [assistant.generation]: context_length" in result
    assert "temperature" in result
    assert not path.exists()


def _stub_model_resolution(monkeypatch):
    """Let any model string resolve, so a test about something else does not depend on which provider
    extras are installed. `update_config` validates `[assistant].model` by building a throwaway client."""
    from aimu import aio

    monkeypatch.setattr(aio, "client", lambda model, system=None: object())


async def test_update_config_refuses_a_model_string_this_process_cannot_build(tmp_path):
    """`[assistant].model` is startup-only, so a bad value is not caught by a failed hot apply: without
    this check it persists and the failure surfaces at the next startup, with Kokua unable to start."""
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant", "model", "bogus-provider:whatever")

    assert result.startswith("Rejected:") and "bogus-provider" in result
    assert not path.exists()


async def test_update_config_writes_a_model_string_that_resolves(tmp_path, monkeypatch):
    _stub_model_resolution(monkeypatch)
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant", "model", "ollama:qwen3.8:27b")

    assert _read(path)["assistant"]["model"] == "ollama:qwen3.8:27b"
    assert "takes effect the next time Kokua restarts" in result


@pytest.mark.parametrize("key,value", [("model", "ollama:qwen3.8:27b"), ("thinking", "medium")])
async def test_update_config_says_a_startup_only_assistant_key_waits_for_a_restart(tmp_path, monkeypatch, key, value):
    """Neither is rebindable live -- no client is ever pointed at another model, and an agent's reasoning
    effort is fixed when it is built -- so the tool must not let the assistant report either as in force."""
    _stub_model_resolution(monkeypatch)
    path, _, update_config = _tools(tmp_path)

    result = await update_config("assistant", key, value)

    assert _read(path)["assistant"][key] == value
    assert "takes effect the next time Kokua restarts" in result


async def test_update_config_points_a_task_edit_at_the_scheduling_tools(tmp_path):
    path, _, update_config = _tools(tmp_path)
    result = await update_config("scheduling.task.morning-brief", "prompt", "new text")
    assert not path.exists()  # nothing written
    assert "update_scheduled_task" in result
    assert "security-critical" not in result


async def test_read_config_states_the_write_policy_in_force(tmp_path):
    config = AssistantConfig(locked_config_keys=["email.to"])
    path, read_config, _ = _tools(tmp_path, config=config)
    path.write_text('[assistant]\nmodel = "m"\n', encoding="utf-8")
    text = await read_config()
    assert "email.to" in text
    # The user's list, not the shipped one.
    assert "paths.data_dir" not in text.split("config.toml follows")[0]
    assert 'model = "m"' in text


async def test_read_config_states_the_policy_even_with_no_file(tmp_path):
    _, read_config, _ = _tools(tmp_path, config=AssistantConfig(locked_config_keys=["email.to"]))
    assert "email.to" in await read_config()


async def test_update_config_refusal_names_the_pattern_that_matched(tmp_path):
    _, _, update_config = _tools(tmp_path, config=AssistantConfig(locked_config_keys=["email.*"]))
    result = await update_config("email", "to", "someone@example.com")
    assert "email.*" in result
    assert "locked_config_keys" in result


async def test_update_config_refuses_the_lock_list_itself(tmp_path):
    _, _, update_config = _tools(tmp_path, config=AssistantConfig(locked_config_keys=[]))
    result = await update_config("security", "locked_config_keys", "email.to")
    assert "locked_config_keys" in result


def test_the_example_config_ships_the_default_lock_list():
    """The scaffolded file is what a user reads the policy off, so it must be the policy."""
    example = tomllib.loads(settings.example_text())
    assert example["security"]["locked_config_keys"] == list(schema.DEFAULT_LOCKED_CONFIG_KEYS)


def _stub_toolset(name):
    from kokua.toolsets.registry import Toolset

    return Toolset(name=name, description=f"{name} for a test", build=lambda ctx: [])


def _unlocked(**kwargs):
    """A config whose agents.* lock is off, which is the only state these writes are reachable in."""
    return AssistantConfig(locked_config_keys=[], **kwargs)


async def test_update_config_writes_an_agent_tools_list_when_unlocked(tmp_path):
    config = _unlocked(agents={"researcher": AgentConfig(tools=["time"])}, entry_agent="researcher")
    path, _, update_config = _tools(
        tmp_path, config=config, registry={"time": _stub_toolset("time"), "memory": _stub_toolset("memory")}
    )
    result = await update_config("agents.researcher", "tools", "time,memory")
    assert _read(path)["agents"]["researcher"]["tools"] == ["time", "memory"]
    assert "restart" in result.lower()


async def test_update_config_refuses_an_agent_tools_list_naming_an_unknown_toolset(tmp_path):
    config = _unlocked(agents={"researcher": AgentConfig(tools=["time"])}, entry_agent="researcher")
    path, _, update_config = _tools(tmp_path, config=config, registry={"time": _stub_toolset("time")})
    result = await update_config("agents.researcher", "tools", "time,nosuch")
    assert "nosuch" in result
    assert not path.exists()


async def test_update_config_refuses_a_delegation_cycle(tmp_path):
    config = _unlocked(
        agents={"a": AgentConfig(delegates_to=["b"]), "b": AgentConfig()},
        entry_agent="a",
    )
    path, _, update_config = _tools(tmp_path, config=config)
    result = await update_config("agents.b", "delegates_to", "a")
    assert "delegation cycle" in result
    assert not path.exists()


async def test_update_config_refuses_an_agent_model_this_process_cannot_build(tmp_path):
    config = _unlocked(agents={"researcher": AgentConfig()}, entry_agent="researcher")
    path, _, update_config = _tools(tmp_path, config=config)
    result = await update_config("agents.researcher", "model", "nosuchprovider:nosuchmodel")
    assert "nosuchmodel" in result
    assert not path.exists()


async def test_update_config_creates_an_agent_table_that_does_not_exist_yet(tmp_path):
    config = _unlocked(agents={"assistant": AgentConfig()}, entry_agent="assistant")
    path, _, update_config = _tools(tmp_path, config=config, registry={"time": _stub_toolset("time")})
    await update_config("agents.newbie", "tools", "time")
    assert _read(path)["agents"]["newbie"]["tools"] == ["time"]


async def test_update_config_still_refuses_an_agent_write_by_default(tmp_path):
    """The shipped policy locks agents.*, so none of the above is reachable without a hand-edit."""
    config = AssistantConfig(agents={"researcher": AgentConfig()}, entry_agent="researcher")
    path, _, update_config = _tools(tmp_path, config=config)
    result = await update_config("agents.researcher", "tools", "time")
    assert "agents.*" in result
    assert not path.exists()


async def test_update_config_dry_run_sees_a_prior_write_from_the_same_session(tmp_path):
    """Reproduces the cycle the dry run exists to prevent, across two writes rather than one.

    Each write, taken alone, validates fine against what was on disk when it landed: `a -> b` is
    acyclic, and so is `b -> a` if checked against the agents this *session* started with, which never
    learned about the first write, since an agent table is a cold key nothing re-applies live. Checked
    against the file instead (what the next startup actually reads), the second write closes `a -> b -> a`
    and must be refused.
    """
    config = _unlocked(agents={"a": AgentConfig(), "b": AgentConfig()}, entry_agent="a")
    path, _, update_config = _tools(tmp_path, config=config)
    # Pre-seed the file with exactly these two agents. Left to create itself on the first write below,
    # `config_store.set_value` scaffolds a brand-new file from the shipped example config (see
    # `config/store.py`'s `_load`), which carries its own default agents, a distraction this test
    # does not want in the graph the second write's dry run sees.
    path.write_text("[agents.a]\n\n[agents.b]\n", encoding="utf-8")

    first = await update_config("agents.a", "delegates_to", "b")
    assert "restart" in first.lower()
    assert _read(path)["agents"]["a"]["delegates_to"] == ["b"]

    second = await update_config("agents.b", "delegates_to", "a")
    assert "delegation cycle" in second
    assert _read(path)["agents"].get("b", {}).get("delegates_to") is None


async def test_update_config_dry_run_sees_a_prior_entry_agent_write(tmp_path):
    """The other half of the same staleness, and the one the file-backed baseline first missed:
    `[assistant].agent` is unlocked by default, so the assistant can point the entry agent at a table
    that does not exist. That write is cold, so the session snapshot still names the old entry agent,
    and an agent write checked against the snapshot is reported as safe while the next startup refuses
    the file for naming an agent it has no table for."""
    config = _unlocked(agents={"a": AgentConfig()}, entry_agent="a")
    path, _, update_config = _tools(tmp_path, config=config)
    path.write_text("[agents.a]\n", encoding="utf-8")

    first = await update_config("assistant", "agent", "ghost")
    assert "restart" in first.lower()
    assert _read(path)["assistant"]["agent"] == "ghost"

    second = await update_config("agents.a", "description", "hi")

    assert "ghost" in second
    assert _read(path)["agents"]["a"].get("description") is None


async def test_update_config_refusal_names_which_agent_the_fault_is_actually_in(tmp_path):
    """A graph already broken elsewhere must not make the refusal read as though the agent just written
    is the broken one: the write to `r` is fine on its own, the fault is in `q`'s tools."""
    config = _unlocked(agents={"q": AgentConfig(tools=["nosuch"]), "r": AgentConfig()}, entry_agent="r")
    path, _, update_config = _tools(tmp_path, config=config, registry={})

    result = await update_config("agents.r", "description", "the researcher")

    assert "agents.r" in result
    assert "'q'" in result
    assert "different agent" in result
    assert not path.exists()


async def test_update_config_validates_against_the_live_config_when_no_file_exists_yet(tmp_path):
    """Nothing is on disk yet, so the dry run has no file to be checked against; it must fall back to the
    live snapshot rather than treating an absent file as a parse failure."""
    config = _unlocked(agents={"researcher": AgentConfig()}, entry_agent="researcher")
    path, _, update_config = _tools(tmp_path, config=config, registry={"time": _stub_toolset("time")})
    assert not path.exists()

    result = await update_config("agents.researcher", "tools", "time")

    assert _read(path)["agents"]["researcher"]["tools"] == ["time"]
    assert "restart" in result.lower()


async def test_update_config_refuses_an_agent_write_when_the_file_does_not_currently_parse(tmp_path):
    """A file that fails to parse for a reason having nothing to do with [agents.*] (a concurrent
    hand-edit adding a bad key while Kokua runs, say) must refuse the write rather than silently falling
    back to the live config: the next startup will fail on that same broken file regardless of what this
    dry run decides, so validating against something else would be answering the wrong question."""
    config = _unlocked(agents={"researcher": AgentConfig()}, entry_agent="researcher")
    path, _, update_config = _tools(tmp_path, config=config)
    path.write_text('[email]\nnosuchkey = "x"\n', encoding="utf-8")

    result = await update_config("agents.researcher", "description", "the researcher")

    assert "does not currently parse" in result
    assert "nosuchkey" in result
    assert _read(path) == {"email": {"nosuchkey": "x"}}
