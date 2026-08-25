"""What makes `pyproject.toml` trustworthy as the index of every shipped toolset.

There is no directory scan and no list in code: the `kokua.toolsets` entry-point table decides which
toolsets exist, which is what lets every toolset Kokua ships register exactly the way a third party's
does. The cost of a hand-maintained table is drift, and drift here is silent in both directions -- a
module nobody registered is simply not a toolset, and an entry naming the wrong module registers under a
name nobody wrote, because `register` keys on `TOOLSET.name` while the entry-point key feeds only the
provenance label. So the correspondence is asserted rather than remembered.

Read against `pyproject.toml` rather than `importlib.metadata.entry_points()`, deliberately. This is the
lesson `tests/test_package_data.py` was written for: an editable install resolves things a built wheel
would not, and stale installed metadata would let a table that no longer matches the source tree pass.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from kokua.plugins import discover_toolsets

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOOLSETS_DIR = REPO_ROOT / "src" / "kokua" / "toolsets"


def _declared_entry_points() -> dict[str, str]:
    """The `kokua.toolsets` table as `pyproject.toml` has it: name -> target."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["project"]["entry-points"]["kokua.toolsets"]


def _module_stems() -> set[str]:
    """Every module in the toolsets directory that should declare a toolset.

    `__init__.py` is the one file there that is not a toolset, and it exists only because Python needs
    the directory to be a package the wheel collects and `import_module` can resolve.
    """
    return {path.stem for path in TOOLSETS_DIR.glob("*.py") if path.stem != "__init__"}


def test_every_toolset_module_is_registered():
    """A module nobody registered is dead code: no name resolves to it, so no agent can declare it, and
    nothing anywhere says so."""
    unregistered = sorted(_module_stems() - set(_declared_entry_points()))

    assert not unregistered, f"toolset modules with no kokua.toolsets entry point: {unregistered}"


def test_every_registered_name_has_a_module():
    """The other direction: an entry naming a module that does not exist fails at import, but only on the
    run that happens to load it, and the error names an import path rather than the table that is wrong."""
    missing = sorted(
        name
        for name, target in _declared_entry_points().items()
        if target.startswith("kokua.toolsets.")
        and not (TOOLSETS_DIR / f"{target.split(':')[0].rsplit('.', 1)[1]}.py").is_file()
    )

    assert not missing, f"kokua.toolsets entries naming no module: {missing}"


@pytest.mark.parametrize("name", sorted(_declared_entry_points()))
def test_the_entry_point_key_the_module_name_and_the_toolset_name_all_agree(name):
    """The correspondence that is silent when broken. `register` keys on `TOOLSET.name`, and the
    entry-point key is read only for the provenance label, so `image = "kokua.toolsets.github_backup"`
    would load and register under `github_backup` with nothing objecting.
    """
    target = _declared_entry_points()[name]
    module_path, attribute = target.split(":")

    assert attribute == "TOOLSET", f"{name}: entry points must name TOOLSET, not {attribute!r}"
    assert module_path == f"kokua.toolsets.{name}", f"{name}: entry point targets {module_path!r}"
    assert discover_toolsets()[name].name == name, f"{name}: TOOLSET.name disagrees with its file name"


def test_skills_is_the_only_entry_point_only_toolset():
    """`entry_point_only` is a hard constraint where it is set, not a policy: a spawned worker is a plain
    AIMU agent, so `make_skill_script_tool` has no agent object to bind and the tool cannot be built at
    all. Anything else carrying the flag is worth a second look, since a toolset that merely *reads
    oddly* on a worker should say so in its own text instead of being made undeclarable."""
    flagged = sorted(name for name, toolset in discover_toolsets().items() if toolset.entry_point_only)

    assert flagged == ["skills"]


def test_no_toolset_registers_aimus_image_group(tmp_path):
    """AIMU's own `image` group is registered nowhere, and Kokua's `image` toolset is why (see its
    module docstring). Two toolsets sharing a *tool* name are deduplicated first-wins by declared order,
    unlike two sharing a *toolset* name, which `register` refuses -- so registering both would let
    declared order silently decide which `generate_image` an agent gets. Pinned here because the failure
    is invisible: the agent still has a working-looking tool.
    """
    from aimu.tools import builtin as aimu_builtin

    from kokua.config.schema import AssistantConfig
    from kokua.registry import LiveState, ToolsetContext

    aimu_image_names = {fn.__name__ for fn in aimu_builtin.image}
    ctx = ToolsetContext(state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=None, agent_name="assistant")

    for name, toolset in discover_toolsets().items():
        if toolset.entry_point_only:
            continue  # needs a real agent to build, and cannot collide on generate_image anyway
        built = {fn.__name__ for fn in toolset.build(ctx)}
        assert aimu_image_names.isdisjoint(built) or name == "image", name


def test_importing_a_toolset_module_does_not_pull_the_preflight_surface():
    """`aimu.aio.tools.builtin` is the AIMU surface `aimu_compat.require_aimu` probes for, and every
    toolset module is on the import path of `resolve_config`, which runs before that preflight on
    invocations such as `kokua skills install`. Importing the surface at module scope would turn an AIMU
    checkout missing it into a bare ImportError there, instead of the actionable message the preflight
    prints, so a toolset needing it must import it inside the function that calls it.

    Run in a child interpreter because this one has already imported the surface: the suite exercises the
    composition path directly, so an in-process `sys.modules` check would say nothing about the graph.
    """
    probe = (
        "import sys; from kokua.plugins import discover_toolsets; discover_toolsets(); "
        "print('aimu.aio.tools.builtin' in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "False", result.stdout


def test_the_cross_cutting_toolsets_are_exactly_these():
    """`cross_cutting` marks a capability an agent holds to manage *itself* rather than to do domain
    work, and the only thing reading it is the delegation guidance, which uses it to tell a lean
    supervisor from a tool-heavy worker. Pinned as an exact set because the flag is easy to set by
    reflex on a new toolset, and one wrong entry changes the prompt of every agent that declares it with
    nothing else to notice.

    The two that most repay a second look are `time`, an AIMU group like `fs` but flagged because an
    agent keeps a clock for its own scheduling, and `github_backup`, which backs up Kokua's own state
    rather than doing anything for a task.
    """
    cross_cutting = sorted(name for name, toolset in discover_toolsets().items() if toolset.cross_cutting)

    assert cross_cutting == [
        "benchmark",
        "capabilities",
        "config",
        "conversations",
        "documents",
        "github_backup",
        "mcp",
        "memory",
        "planning",
        "scheduling",
        "skills",
        "time",
    ]


def test_only_planning_carries_a_workflow():
    """A `Toolset` may carry a turn strategy instead of tools, which is how `/plan` is granted by naming
    "planning" in an agent's `tools` and by nothing else. Pinned so a second workflow-bearing toolset is
    a deliberate change: its command joins the same namespace, and two claiming one command would be
    resolved by declared order rather than refused."""
    carrying = sorted(name for name, toolset in discover_toolsets().items() if toolset.workflow is not None)

    assert carrying == ["planning"]
