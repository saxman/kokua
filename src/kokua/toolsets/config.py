"""The ``config`` toolset: the assistant's own ``read_config`` / ``update_config``.

These let the assistant inspect and repair its configuration during a conversation (e.g. "you keep
failing to send email, check your config"). Reads are unrestricted; writes go through the same
validation as the config file and are refused for whatever the user's own
``[security].locked_config_keys`` list matches (``config.store.is_locked``).

The policy, the coercion, and the apply-then-persist ordering are all in ``config/store.py``. What is
here is the two tool schemas and the four sentences that report what happened -- including the one that
says a change waits for a restart, which is what keeps the assistant honest about a cold setting.

Two things this module has to supply rather than merely wrap, both because ``config/`` is the bottom
layer and can reach neither the installed toolsets nor AIMU: *which keys exist* (the cold half of the
toolsets' declarations, assembled here and handed down -- see :func:`make_config_tools`), and whether a
model string resolves (see :func:`_resolvable_model`).
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

from aimu.tools import tool

from kokua.config import file as settings
from kokua.config import store as config_store
from kokua.config.schema import AgentConfig
from kokua.config.settings_sources import startup_schema
from kokua.toolsets.registry import Toolset


def _policy_preamble(locked: Sequence[str]) -> str:
    """The write policy in force, as comment lines above the file text ``read_config`` returns.

    Generated from the same list the refusal checks rather than read out of the file, for two reasons: a
    hand-written config that never mentions ``[security].locked_config_keys`` is running on the shipped
    default with nothing in its text to say so, and a generated line cannot drift from the policy the way
    the three prose restatements this replaced could.

    Kept out of ``update_config``'s tool description on purpose. That text sits in the model's context
    every turn, and Kokua is run against locally served models with small context windows; this is what
    the assistant reads when it actually cares.
    """
    patterns = ", ".join(locked) if locked else "(none)"
    return (
        "# --- Write policy in force (generated, not part of the file) ---\n"
        "# update_config refuses any key matching these patterns:\n"
        f"#   {patterns}\n"
        "# [security].locked_config_keys sets that list, and is itself always locked.\n"
        "# [scheduling.task.*] is refused here but changes through the scheduling tools.\n"
        "# --- config.toml follows ---\n"
    )


def _resolvable_model(section: str, key: str, value: str) -> str:
    """Refuse a model string this process could not build a client for.

    A schema *converter* rather than a check in the tool body, so it runs wherever the schema is
    consulted and cannot be bypassed by a second write path. ``[assistant].model`` is startup-only, so
    the apply-before-persist ordering that catches a bad hot value never sees it: an unresolvable string
    would be saved and turn up as a Kokua that will not start, with the assistant that wrote it gone.

    The import is function-local because ``core.build`` reaches ``toolsets/`` and a module-level import
    here would close that cycle -- the same reason ``core.build`` imports ``toolsets.agents`` inside a
    function.
    """
    from kokua.core.build import ModelClientError, validate_model_string

    try:
        validate_model_string(value)
    except ModelClientError as error:
        raise settings.ConfigError(f"[{section}].{key}: {error}") from error
    return value


def _agents_on_disk(config_path: Path, table) -> Optional[dict]:
    """The persisted ``[agents.*]`` tables, read fresh, or ``None`` when there is nothing to read yet.

    Agent keys are cold: ``apply_setting`` persists a successful write to the file but never calls
    ``apply_hot`` for one, so the live ``AssistantConfig.agents`` a running session holds stays exactly
    what it was at process start until the next restart. A dry run against that frozen snapshot would
    validate each write against a config nothing has actually been checked against twice in a row: two
    sequential writes, each individually fine against what was on disk when it landed, could together
    reintroduce the exact fault (an unresolvable delegate, a cycle) this converter exists to catch,
    because the second dry run never saw the first write. Reading the file each time instead is what lets
    the second call see the first call's already-persisted change, which is what the next real startup
    will also see.

    ``startup_schema()``, not the module's own ``cold_schema``: the latter routes every ``agents.*`` key
    through this very converter, and handing it to ``load`` as ``extra_schema`` would consult it while
    resolving the write this call is in the middle of deciding. ``[agents]`` itself never actually reaches
    that schema (``load`` parses it via its own ``_parse_agent``, not through ``coerce_config_string``),
    so today this would not recurse, but nothing enforces that staying true, and there is no reason to
    depend on it.

    Returns ``None`` only for "the file does not exist yet", checked directly with
    :func:`kokua.config.store.read_text` rather than by catching ``load``'s ``ConfigError``: that
    exception also covers a malformed ``[[mcp.server]]`` entry, an unknown scheduling key, a removed key,
    and more, none of which mean "nothing to read yet". A file that exists but fails to parse is left to
    raise out of this function uncaught, so the caller must refuse the write rather than quietly falling
    back to the live snapshot, since a file that cannot parse will fail the next startup regardless of what
    this dry run decides, and validating against something else would be answering the wrong question.
    """
    if config_store.read_text(config_path) is None:
        return None
    return settings.load(str(config_path), table=table, extra_schema=startup_schema()).get("agents", {})


def _validated_agent_write(config, registry, config_path: Path, table) -> Callable[[str, str, Any], Any]:
    """A converter refusing an agent-table write that would produce a config the next startup rejects.

    A dry run of the real ``validate_agents`` rather than a set of per-key checks, so the tool cannot
    drift from what startup enforces: one call covers an unknown toolset, an unresolvable model, an
    unknown delegate, a delegation cycle, and a broken entry agent. That matters more here than for an
    ordinary key, because an agent write is startup-only and outlives the conversation that made it, so
    a bad value surfaces as a Kokua that will not start with the assistant that wrote it gone.

    Supplied by the caller rather than living in ``config/file.py`` because that layer may reach neither
    the toolset registry nor AIMU, which is the same seam :func:`_resolvable_model` uses.

    Dry-runs against :func:`_agents_on_disk`, not ``config.agents`` directly, since "would the next
    startup accept this" is a question about the file, not about a snapshot frozen at process start (see
    that function's docstring for why the two can disagree). ``copy.copy`` rather than
    ``dataclasses.replace`` on the whole config: only ``agents`` is rebound, ``validate_agents`` reads
    nothing else but ``entry_agent``, and a shallow copy re-runs no ``__post_init__``.

    The check runs against every configured agent, not only the one being written, so an agent already
    broken by some other change (an MCP server removed mid-session, say) blocks a write to any other
    agent's table too, exact parity with startup, which would refuse the whole file. The error is
    reworded rather than simply prefixed with the section being written, so that when the fault named
    inside it belongs to a different agent's table, a model reading the refusal is not misled into
    repairing the one it just tried to write.
    """
    from kokua.toolsets.agents import validate_agents

    def convert(section: str, key: str, value: Any) -> Any:
        name = section.split(".")[1]
        try:
            baseline = _agents_on_disk(config_path, table)
        except settings.ConfigError as error:
            raise settings.ConfigError(
                f"config.toml does not currently parse, so [{section}].{key} cannot be checked against it: {error}"
            ) from error
        if baseline is None:
            baseline = config.agents
        candidate = copy.copy(config)
        agent = baseline.get(name, AgentConfig())
        candidate.agents = {**baseline, name: replace(agent, **{key: value})}
        try:
            validate_agents(candidate, registry)
        except settings.ConfigError as error:
            raise settings.ConfigError(
                f"{error} (found while validating every agent for the write to [{section}].{key}; the "
                "fault may be in a different agent's table than this one)"
            ) from error
        return value

    return convert


def make_config_tools(
    config_path: Path,
    apply_hot: Callable[[str, str, object], Awaitable[None]],
    table,
    *,
    config,
    registry,
) -> list[Callable]:
    """Build ``read_config`` / ``update_config`` bound to the config file, the live-apply callback, and
    the live settings table, so ``update_config`` resolves a key against the same declarations the
    applier does.

    The table is only half of those declarations: it holds hot settings, so the *cold* keys a toolset
    declared are resolved from ``startup_schema()``, computed once here. This module is above ``config/``
    and so may read the installed toolsets, which is the reason the seam is here rather than inside
    ``config.store`` -- and it is the same route ``kokua.cli`` takes to build the parse schema, so the
    tool and the file agree about which keys exist by construction. Without it the assistant would answer
    "unknown config key" for a cold toolset key sitting in the user's own file.

    The one entry added on top is a stricter ``[assistant].model``: the file's own schema type-checks it
    as a string and leaves resolving it to startup, which is too late for a tool whose write outlives the
    conversation that made it (see :func:`_resolvable_model`).

    ``config`` is the live ``AssistantConfig``, read for its ``locked_config_keys`` at call time rather
    than bound to a snapshot, since a settings applier can mutate it between calls. ``registry`` is the
    live toolset registry, read here to dry-run an agent-table write against it.

    The agent entries are the third thing this module supplies rather than wraps: ``AGENT_SCHEMA`` in
    ``config/file.py`` types an agent key, and the converter layered over it here is what checks the
    resulting config would still start. ``build_schema`` merges ``extra_schema`` last, which is what lets
    that override land.
    """
    agent_write = _validated_agent_write(config, registry, config_path, table)
    cold_schema = {
        **startup_schema(),
        ("assistant", "model"): ("model", (str,), "a string", _resolvable_model),
        **{
            location: (target, types, label, agent_write)
            for location, (target, types, label, _) in settings.AGENT_SCHEMA.items()
            if location[0] == "agents.*"
        },
    }

    @tool
    async def read_config() -> str:
        """Return the current contents of config.toml so you can diagnose a configuration problem.

        Use this before update_config to see what is set, and to check keys and section names. The
        reply opens with the write policy in force: which keys update_config will refuse. If no config
        file exists yet, all settings are at their built-in defaults.
        """
        preamble = _policy_preamble(config.locked_config_keys)
        text = config_store.read_text(config_path)
        if text is None:
            return preamble + "No config file exists yet; all settings are at their built-in defaults."
        return preamble + text

    @tool
    async def update_config(section: str, key: str, value: str) -> str:
        """Change one setting in config.toml, e.g. section="email", key="host", value="smtp.gmail.com".

        Pass the value as a string; it is coerced to the setting's real type (numbers, true/false, and
        comma-separated lists are understood). A hot setting -- a display flag, a planning flag, or any
        other setting flagged to apply live -- takes effect immediately in this session. Every other
        setting, the model and the reasoning effort among them, is saved and takes effect only the next
        time Kokua restarts. The result says which of the two happened; pass that on, because a saved
        setting is not yet in force and only the user can restart. A model string is checked against the
        providers actually installed before it is saved, so a name that does not resolve is refused here
        rather than breaking the next startup. Which keys are refused is the user's own
        [security].locked_config_keys list, not a fixed set; read_config opens with the list in force.
        """
        try:
            applied = await config_store.apply_setting(
                config_path,
                section,
                key,
                value,
                apply_hot,
                table=table,
                locked=config.locked_config_keys,
                extra_schema=cold_schema,
            )
        except config_store.SettingLocked as locked:
            if locked.section.startswith("scheduling.task"):
                return (
                    f"[{locked.section}] is a scheduled task, not a setting. Use update_scheduled_task "
                    "to change it, so its schedule is re-armed to match."
                )
            if locked.pattern is None:
                return (
                    "[security].locked_config_keys is the list of keys I may not write, so I may not "
                    "write it either. Hand-edit config.toml to change it."
                )
            return (
                f"[{section}].{key} is refused by [security].locked_config_keys, which matches it as "
                f"{locked.pattern!r}. Hand-edit config.toml to change either."
            )
        except settings.ConfigError as error:
            return f"Rejected: {error}"
        except config_store.HotApplyFailed as error:
            return f"[{section}].{key} could not be applied: {error}. No change was saved."
        if applied.hot:
            return f"Set [{section}].{key} = {applied.value!r} and applied it to the current session."
        return (
            f"Set [{section}].{key} = {applied.value!r} in config.toml. It takes effect the next time Kokua restarts."
        )

    return [read_config, update_config]


TOOLSET = Toolset(
    name="config",
    description="Read config.toml and change a runtime setting, persisted back to the file.",
    build=lambda ctx: make_config_tools(
        ctx.config.config_path,
        ctx.state.reapply_config,
        ctx.state.settings_table,
        config=ctx.config,
        registry=ctx.state.registry,
    ),
    cross_cutting=True,
)
