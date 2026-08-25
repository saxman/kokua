"""Tests for the ``compute`` toolset: the shell tool it now carries, and the one setting that decides
what that tool's child can see in the environment."""

from __future__ import annotations

from aimu.tools import builtin

from kokua.config.schema import AssistantConfig
from kokua.registry import LiveState, ToolsetContext
from kokua.toolsets import compute


def _ctx(**settings):
    """The real ``ToolsetContext`` `build` actually receives, carrying ``compute``'s settings the way
    ``config/settings_sources.py`` would seed them."""
    config = AssistantConfig(toolset_settings={"compute": settings} if settings else {})
    return ToolsetContext(state=LiveState(config=config), agent=None, agent_name="coder")


def test_the_toolset_carries_the_shell_tool():
    """The docstring has advertised shell commands since before one existed. This is the assertion that
    keeps that sentence true. Pinned against AIMU's own group rather than a literal set, so a fourth
    member added there is either carried here too or the mismatch is caught, in either direction."""
    names = {fn.__name__ for fn in compute.TOOLSET.build(_ctx())}
    assert names == {fn.__name__ for fn in builtin.compute}


def test_the_passthrough_setting_reaches_the_built_tool(monkeypatch):
    """The wiring this toolset exists to do. The setting is only real if the name in config.toml arrives
    in the child's environment, and nothing else about the tool would fail if it did not."""
    monkeypatch.setenv("GH_TOKEN", "ghp-from-config")
    tools = compute.TOOLSET.build(_ctx(command_env_passthrough="GH_TOKEN"))
    run_command = next(fn for fn in tools if fn.__name__ == "run_command")
    assert "ghp-from-config" in run_command("printenv GH_TOKEN")


def test_an_unset_passthrough_admits_nothing(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp-not-asked-for")
    tools = compute.TOOLSET.build(_ctx())
    run_command = next(fn for fn in tools if fn.__name__ == "run_command")
    assert "ghp-not-asked-for" not in run_command("printenv GH_TOKEN")


def test_the_passthrough_string_splits_forgivingly():
    """A comma-separated string is the shape available, not the shape a reader expects, so the parsing has
    to tolerate what someone writing a list in prose would type: spaces after commas, a trailing comma."""
    assert compute._env_passthrough(_ctx(command_env_passthrough="A, B,")) == ("A", "B")


def test_an_empty_passthrough_yields_no_names():
    """The default must not produce a single empty name: passing "" through to the child would set a
    variable called "" rather than admitting nothing."""
    assert compute._env_passthrough(_ctx()) == ()
    assert compute._env_passthrough(_ctx(command_env_passthrough="")) == ()


def test_the_setting_is_declared_on_the_toolset():
    """A setting reaches the schema, the sanitizer, and the persist path by being declared here, so this
    is what makes [compute] a real section rather than an unknown-key startup error."""
    keys = {s.key: s for s in compute.TOOLSET.settings}
    assert keys["command_env_passthrough"].kind is str
    assert keys["command_env_passthrough"].default == ""
    assert not keys["command_env_passthrough"].hot  # baked into the closure at build time


def test_the_docstring_names_both_gated_tools():
    """The module docstring is where a reader learns that declaring this toolset is what grants the
    reach, and it named only execute_python when only execute_python needed naming."""
    assert "execute_python" in compute.__doc__
    assert "run_command" in compute.__doc__
