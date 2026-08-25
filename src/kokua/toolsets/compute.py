"""AIMU's Python, shell, and calculation tools, wrapped as a toolset.

Defines no tools of its own. Note what declaring this grants: ``execute_python`` and ``run_command`` run
with the privileges of the Kokua process, which is why the shipped ``[security].confirm_tools`` gates
both by name rather than leaving the agent's declaration as the only control. Neither offers
containment, and ``run_command`` offers one step less of it: a shell string reaches a credential sitting
in a file with no code for anyone to read first, and process signalling is unconfined, so Kokua's own
process is reachable from a command it ran.

The one setting here exists because the safe default is not the useful one. A command's child sees an
environment allowlist with no API keys in it, which is what stops ``run_command("env")`` lifting a
credential into the model's context, and which also makes ``gh``, ``ssh``, and ``git push`` over ssh
fail. Naming a variable in ``command_env_passthrough`` is how a user grants one of those back: the
capability stays real and the control stays theirs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aimu.tools import builtin

from kokua.registry import Setting, Toolset

if TYPE_CHECKING:
    from kokua.registry.context import ToolsetContext


#: The registry name this toolset is installed under. Defined once so `TOOLSET` and the section
#: `_env_passthrough` reads cannot drift apart if either is renamed.
TOOLSET_NAME = "compute"

#: The ``[compute]`` section of config.toml. A comma-separated string rather than a list because
#: ``Setting.kind`` carries one of ``str``, ``int``, or ``bool`` (see ``config/table.py``), and a list is
#: the shape a reader would expect here. Not hot: the value is baked into the tool's closure when an
#: agent is built, so a hot flag would silently do nothing until a restart, which is worse than a cold
#: one that says so.
COMPUTE_SETTINGS: tuple[Setting, ...] = (Setting("command_env_passthrough", str, ""),)


def _env_passthrough(ctx: "ToolsetContext") -> tuple[str, ...]:
    """The environment variable names a command may see, from ``[compute] command_env_passthrough``.

    Splitting tolerates surrounding whitespace and drops empty segments, so ``"SSH_AUTH_SOCK, GH_TOKEN"``
    and a trailing comma behave as written. Dropping the empties is not cosmetic: an empty name would
    reach AIMU as a request to pass through a variable called ``""``.
    """
    raw = ctx.config.toolset_settings.get(TOOLSET_NAME, {}).get("command_env_passthrough", "")
    return tuple(name.strip() for name in raw.split(",") if name.strip())


TOOLSET = Toolset(
    name=TOOLSET_NAME,
    description="Run Python, shell commands, and calculations.",
    build=lambda ctx: [
        builtin.calculate,
        builtin.execute_python,
        builtin.make_command_tool(env_passthrough=_env_passthrough(ctx)),
    ],
    settings=COMPUTE_SETTINGS,
)
