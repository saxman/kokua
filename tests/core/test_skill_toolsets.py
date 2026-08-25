"""Individual skills as entries in the one toolset namespace.

A skill name sits in an agent's ``tools`` list beside a built-in group or an MCP server, so an agent
declares "citation-check" the way it declares "web". Two paths deliver it, split on who authors: the
entry agent keeps AIMU's ``SkillAgent`` (whose catalogue is scoped to its declaration) because it is
the only agent that can write a skill and needs to see one the moment it does; a worker, which never
authors, gets the skill's script tools through the registry like any other toolset.
"""

from __future__ import annotations

import pytest

from kokua.config.schema import AgentConfig
from kokua.core.agents import build_agent_specs, build_registry, without_skill_names
from kokua.registry.context import LiveState
from kokua.registry.registry import ToolsetError, select
from tests.channels import _config


def write_skill(config, name: str, description: str, script: str | None = None) -> None:
    """Create a spec-valid skill under the config's skills dir, optionally with one script."""
    skill_dir = config.skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nDo the thing.\n",
        encoding="utf-8",
    )
    if script is not None:
        scripts = skill_dir / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "run.py").write_text(script, encoding="utf-8")


def _state(config) -> LiveState:
    return LiveState(config=config, registry=build_registry(config))


def test_a_skill_on_disk_is_registered_under_its_name(tmp_path):
    config = _config(tmp_path)
    write_skill(config, "citation-check", "Verify a claim against its source.")

    assert "citation-check" in build_registry(config)


def test_a_skills_toolset_contributes_its_scripts_and_activate_skill(tmp_path):
    config = _config(tmp_path)
    write_skill(config, "citation-check", "Verify a claim.", script="print('checked')\n")
    state = _state(config)

    tools = state.registry["citation-check"].build(_ctx(state))

    names = {fn.__name__ for fn in tools}
    assert "activate_skill" in names
    assert "citation_check__run" in names


def test_a_skills_guidance_names_the_skill_and_how_to_load_it(tmp_path):
    config = _config(tmp_path)
    write_skill(config, "citation-check", "Verify a claim against its source.")

    guidance = build_registry(config)["citation-check"].guidance

    assert "citation-check" in guidance
    assert "Verify a claim against its source." in guidance
    assert "activate_skill" in guidance


def test_a_skill_named_like_a_toolset_is_rejected(tmp_path):
    """The namespace is one namespace, so a skill cannot quietly shadow a built-in group."""
    config = _config(tmp_path)
    write_skill(config, "web", "Shadows the built-in web group.")

    with pytest.raises(ToolsetError) as excinfo:
        build_registry(config)

    assert "web" in str(excinfo.value)


def test_a_worker_declaring_a_skill_receives_its_tools(tmp_path):
    """A worker is a plain agent, so the registry is the only way a skill reaches it."""
    config = _config(tmp_path)
    write_skill(config, "citation-check", "Verify a claim.", script="print('checked')\n")
    config.agents["assistant"] = AgentConfig(tools=["time"], delegates_to=["checker"])
    config.agents["checker"] = AgentConfig(description="Checks claims.", tools=["citation-check", "time"])
    state = _state(config)

    spec = build_agent_specs(config, state, "assistant")["checker"]

    names = {fn.__name__ for fn in spec["tools"]}
    assert "citation_check__run" in names
    assert "activate_skill" in names


def test_a_workers_prompt_carries_the_declared_skills_description(tmp_path):
    config = _config(tmp_path)
    write_skill(config, "citation-check", "Verify a claim against its source.")
    config.agents["assistant"] = AgentConfig(tools=["time"], delegates_to=["checker"])
    config.agents["checker"] = AgentConfig(description="Checks claims.", tools=["citation-check"])
    state = _state(config)

    spec = build_agent_specs(config, state, "assistant")["checker"]

    assert "Verify a claim against its source." in spec["system_message"]


def test_a_worker_does_not_receive_a_skill_it_did_not_declare(tmp_path):
    config = _config(tmp_path)
    write_skill(config, "wanted", "Keep me.", script="print('wanted')\n")
    write_skill(config, "unwanted", "Not for this worker.", script="print('unwanted')\n")
    config.agents["assistant"] = AgentConfig(tools=["time"], delegates_to=["checker"])
    config.agents["checker"] = AgentConfig(description="Checks.", tools=["wanted"])
    state = _state(config)

    names = {fn.__name__ for fn in build_agent_specs(config, state, "assistant")["checker"]["tools"]}

    assert "wanted__run" in names
    assert "unwanted__run" not in names


def test_the_entry_agents_catalogue_is_scoped_to_what_it_declares(tmp_path):
    """Without the authoring toolset, the entry agent sees the skills it named and no others."""
    config = _config(tmp_path)
    write_skill(config, "declared", "Named by the entry agent.")
    write_skill(config, "undeclared", "Present on disk, named by nobody.")
    config.agents["assistant"] = AgentConfig(tools=["time", "declared"])
    state = _state(config)

    assert sorted(state.skill_manager.skills) == ["declared"]


def test_an_authoring_entry_agent_sees_every_skill(tmp_path):
    """Scoping an author's catalogue would hide the skill it just wrote, so authoring wins.

    `add_skill_script` promises the script is callable in the same turn, which cannot be true if a
    newly authored skill falls outside the manager's include set.
    """
    config = _config(tmp_path)
    write_skill(config, "declared", "Named by the entry agent.")
    write_skill(config, "undeclared", "Present on disk, named by nobody.")
    config.agents["assistant"] = AgentConfig(tools=["time", "skills", "declared"])
    state = _state(config)

    assert sorted(state.skill_manager.skills) == ["declared", "undeclared"]


def test_a_skill_name_is_dropped_from_what_a_skill_agent_resolves(tmp_path):
    """The one thing the provider map still decides. AIMU already hands a ``SkillAgent`` the catalogue
    and script tools of every skill in its manager, so resolving those names as toolsets as well would
    put the catalogue in its prompt twice. Everything else in the declaration is untouched, which is why
    this strips by provider rather than by guessing from the name.
    """
    config = _config(tmp_path)
    write_skill(config, "citation-check", "Verify a claim.")
    config.agents["assistant"] = AgentConfig(tools=["time", "skills", "citation-check"])
    registry = build_registry(config)

    assert without_skill_names(config.agents["assistant"].tools, registry) == ["time", "skills"]


def test_a_skill_resolves_through_select_so_a_typo_is_still_caught(tmp_path):
    config = _config(tmp_path)
    write_skill(config, "citation-check", "Verify a claim.")
    registry = build_registry(config)

    select(["citation-check"], registry, agent="checker", entry_point="assistant")

    with pytest.raises(ToolsetError, match="citation-chek"):
        select(["citation-chek"], registry, agent="checker", entry_point="assistant")


def _ctx(state: LiveState):
    from kokua.registry.context import ToolsetContext

    return ToolsetContext(state=state, agent=None, agent_name="assistant")
