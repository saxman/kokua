"""Command-line entry point.

Resolves an :class:`~kokua.config.AssistantConfig` from (in increasing precedence) built-in
defaults, an optional TOML config file, and command-line flags, then runs the selected front end
(default ``cli``; ``web`` and any installed plugin are also selectable). ``--list-frontends`` lists the
front-end plugins; ``--list-toolsets`` lists the whole toolset registry, which is the discovery command
for the single namespace an ``[agents.*]`` table's ``tools`` list draws on.

Flag defaults are the ``None`` sentinel rather than the real default value, so an unspecified flag
defers to the config file (and then the built-in default) instead of overriding it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from . import plugins
from .aimu_compat import AimuVersionError, require_aimu
from kokua.config import file as settings
from kokua.config import AssistantConfig, ConfigError, MCPServerConfig
from kokua.config import settings_sources
from kokua.config.store import disambiguate_name, name_from_url

# Safe at module level, unlike kokua.toolsets.agents below: `from . import plugins` above already imports
# kokua.toolsets, and registry.py itself imports no AIMU surface the preflight checks.
from kokua.toolsets.registry import ToolsetError
from .logging_setup import configure_logging


def build_arg_parser(prog: str = "kokua") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Kokua: a personal AI assistant.")

    # Plugin selection / introspection.
    parser.add_argument(
        "--frontend",
        default=None,
        help="Front end to run: 'cli' (terminal), 'web' (browser), or any installed plugin. Default: cli.",
    )
    parser.add_argument("--list-frontends", action="store_true", help="List available front ends and exit.")
    parser.add_argument(
        "--list-toolsets",
        action="store_true",
        help="List every toolset name an [agents.<name>].tools list may use, grouped by provider, and exit.",
    )
    parser.add_argument(
        "--plugins",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Discover toolset plugins via the 'kokua.toolsets' entry-point group. Default: on "
        "(use --no-plugins to disable for this run).",
    )

    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Path to a TOML config file. Default: $KOKUA_CONFIG or $KOKUA_HOME/config.toml if present.",
    )

    # Model + behaviour.
    parser.add_argument(
        "--model",
        default=None,
        help="Model string (e.g. 'ollama:qwen3:8b'). Unset: AIMU_LANGUAGE_MODEL, else a running "
        "local model (never a cloud model); startup fails if none is found.",
    )
    parser.add_argument(
        "--system",
        default=None,
        help="Override the entry agent's system message for this run (its [agents.<name>].system_message "
        "or the [assistant].system_message fallback, whichever it would otherwise use). Does not affect "
        "a delegated worker's own declared message.",
    )
    parser.add_argument(
        "--show-thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Show the model's reasoning as it streams. Default: on (use --no-show-thinking to hide).",
    )
    parser.add_argument(
        "--show-tools",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Show tool calls as they happen. Default: on (use --no-show-tools to hide).",
    )
    parser.add_argument(
        "--mcp",
        action="append",
        default=None,
        metavar="URL",
        help="Remote MCP server URL whose tools the assistant should use (repeatable). Connects "
        "unauthenticated (or via OAuth on an auth challenge); for a server needing a bearer token, "
        "configure it in config.toml under [[mcp.server]] with token_env. The assistant can also "
        "connect more servers mid-session via the add_mcp_server tool.",
    )
    parser.add_argument(
        "--confirm-tools",
        default=None,
        metavar="NAMES",
        help="Comma-separated tool names that require interactive confirmation before each call. "
        "Default: add_skill_script,add_mcp_server,execute_python,update_config. Pass an empty string "
        "to disable.",
    )

    # Web front-end binding (ignored by other front ends).
    parser.add_argument("--host", default=None, help="Web front end bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="Web front end bind port. Default: 8000")

    # Subcommands. Optional: with none given, the flags above drive a normal assistant run.
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Inspect or scaffold the configuration file.")
    config_sub = config_parser.add_subparsers(dest="config_command")
    init_parser = config_sub.add_parser(
        "init", help="Write a starter config.toml (every key at its default) to the config location."
    )
    init_parser.add_argument(
        "--path",
        default=None,
        metavar="PATH",
        help="Write here instead of the default ($KOKUA_CONFIG or $KOKUA_HOME/config.toml).",
    )
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config file.")

    skills_parser = subparsers.add_parser("skills", help="List or install the skills this repository ships.")
    skills_sub = skills_parser.add_subparsers(dest="skills_command")
    skills_sub.add_parser("list", help="List the skills bundled in this repository's skills/ directory.")
    install_parser = skills_sub.add_parser(
        "install", help="Copy bundled skills into the skills folder, where the assistant discovers them."
    )
    install_parser.add_argument(
        "name",
        nargs="*",
        help="Skills to install. Installs every bundled skill when omitted.",
    )
    install_parser.add_argument(
        "--force", action="store_true", help="Overwrite a skill of the same name that is already installed."
    )
    return parser


def _mcp_servers_from_urls(urls: list[str]) -> list[MCPServerConfig]:
    """One ``MCPServerConfig`` per ``--mcp`` URL, named by host and disambiguated against each other.

    Two URLs on one host (a service exposing several MCP endpoints under one domain) derive the same
    base name from ``name_from_url``; without disambiguating here, the second would collide with the
    first in the registry at startup with no ``--mcp``-only way to rename either. Uses the same
    ``disambiguate_name`` the ``add_mcp_server`` tool's config write applies against names already on
    file, so a name derived from one URL never depends on which of the two disambiguation call sites ran.
    """
    servers: list[MCPServerConfig] = []
    used: set[str] = set()
    for url in urls:
        name = disambiguate_name(name_from_url(url), used)
        used.add(name)
        servers.append(MCPServerConfig(url=url, name=name))
    return servers


def _cli_overrides(args: argparse.Namespace) -> dict:
    """Collect the flags the user actually passed (non-sentinel), keyed by AssistantConfig field."""
    overrides: dict = {}

    def take(field: str, value, transform=None):
        if value is not None:
            overrides[field] = transform(value) if transform else value

    take("model", args.model)
    take("system_message_override", args.system)
    take("show_thinking", args.show_thinking)
    take("show_tools", args.show_tools)
    take("mcp_servers", args.mcp, _mcp_servers_from_urls)
    take("load_plugins", args.plugins)
    take("confirm_tools", args.confirm_tools, lambda v: [name.strip() for name in v.split(",") if name.strip()])
    take("frontend", args.frontend)
    take("host", args.host)
    take("port", args.port)
    return overrides


def resolve_config(args: argparse.Namespace) -> AssistantConfig:
    """Merge built-in defaults < config file < CLI flags into the final config.

    The settings table is built before the file is parsed, because the installed toolsets are what say
    which sections exist. Toolset discovery is side-effect-free and deliberately not gated on
    ``load_plugins``: gating it would make a config file naming a third party's section unparseable
    whenever plugins are switched off, which is a worse failure than the capability simply being absent.
    """
    config_path, _ = settings.resolve_path(args.config)
    table = settings_sources.build_settings_table()
    # Every name a section header in the file could belong to, so `load` can report one even when the
    # section sets no keys (every key commented out, as the shipped example's [planning] ships) -- a
    # toolset with no settings can never own a section, so it is excluded here rather than left for
    # `load` to always find an empty intersection against. Called through the module (not a name bound
    # at import time) for the same reason `build_settings_table` and `startup_schema` are below: a test
    # that monkeypatches `settings_sources.declaring_toolsets` has to reach every call site here, and a
    # name captured by `from ... import` at module load would keep pointing at the original.
    section_owners = tuple(
        sorted(toolset.name for toolset in settings_sources.declaring_toolsets() if toolset.settings)
    )
    from_file = settings.load(
        args.config,
        table=table,
        extra_schema=settings_sources.startup_schema(),
        declaring_names=section_owners,
    )
    # Popped rather than left in `from_file`, which is about to be spread into `overrides`: `load` and
    # this function would otherwise both hand `configured_sections` to `AssistantConfig`, one via
    # **overrides and one by name, which raises "multiple values for keyword argument".
    configured = from_file.pop("configured_sections", ())
    overrides = {"config_path": config_path, **from_file, **_cli_overrides(args)}
    config = AssistantConfig(**overrides, configured_sections=configured)
    settings_sources.seed_toolset_defaults(config)
    return config


def _init_config(args: argparse.Namespace) -> int:
    """Write the shipped example to the config location. Refuses to clobber unless --force."""
    path, _ = settings.resolve_path(args.path)
    if path.exists() and not args.force:
        print(f"config file already exists: {path} (use --force to overwrite)")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(settings.example_text(), encoding="utf-8")
    print(f"wrote starter config: {path}")
    return 0


def _bundled_skills_dir() -> Optional[Path]:
    """This repository's ``skills/`` directory, or None when Kokua was not installed from a checkout.

    Deliberately outside the package and therefore outside the wheel: these skills are content, not
    Python, and shipping them inside ``src/kokua`` would put them back in the source tree they were
    moved out of. The cost is that an installed-from-PyPI Kokua has no copy, which is why the caller
    says where to get one rather than failing with a bare path.
    """
    candidate = Path(__file__).resolve().parents[2] / "skills"
    return candidate if candidate.is_dir() else None


def _bundled_skill_names(bundled: Path) -> list[str]:
    return sorted(d.name for d in bundled.iterdir() if (d / "SKILL.md").is_file())


def _no_bundled_skills_message() -> str:
    return (
        "no bundled skills found: this Kokua was not installed from a source checkout, and the skills/ "
        "directory ships with the repository rather than the package. Clone or download "
        "https://github.com/saxman/kokua and run this from there, or copy skills/<name> into your skills "
        "folder yourself."
    )


def _list_skills() -> int:
    """Print the skills this repository bundles, with the description each declares."""
    bundled = _bundled_skills_dir()
    if bundled is None:
        print(_no_bundled_skills_message())
        return 1
    names = _bundled_skill_names(bundled)
    if not names:
        print(f"no skills in {bundled}")
        return 1
    print(f"Skills bundled in {bundled}. Install with `kokua skills install <name>`.\n")
    for name in names:
        # Read the description straight out of the frontmatter rather than parsing the whole file with
        # AIMU's loader: this runs before the preflight, so it must not import the AIMU surface.
        description = ""
        for line in (bundled / name / "SKILL.md").read_text(encoding="utf-8").splitlines():
            if line.startswith("description:"):
                description = line.partition(":")[2].strip()
                break
        print(f"  {name}: {description}")
    return 0


def _install_skills(args: argparse.Namespace) -> int:
    """Copy bundled skills into the configured skills folder. Refuses to clobber without --force."""
    import shutil

    bundled = _bundled_skills_dir()
    if bundled is None:
        print(_no_bundled_skills_message())
        return 1

    available = _bundled_skill_names(bundled)
    wanted = args.name or available
    unknown = [name for name in wanted if name not in available]
    if unknown:
        print(f"unknown skill(s): {', '.join(unknown)}. Available: {', '.join(available) or '(none)'}")
        return 1

    # The destination comes from the resolved config, so [paths].data_dir and $KOKUA_HOME are honoured
    # the same way the running assistant honours them, rather than being guessed at here.
    target = resolve_config(args).skills_dir
    target.mkdir(parents=True, exist_ok=True)

    installed, skipped = [], []
    for name in wanted:
        destination = target / name
        if destination.exists() and not args.force:
            skipped.append(name)
            continue
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(bundled / name, destination)
        installed.append(name)

    if installed:
        print(f"installed into {target}: {', '.join(installed)}")
    if skipped:
        print(f"already installed (use --force to overwrite): {', '.join(skipped)}")
    print("\nDeclare a skill by name in an [agents.<name>].tools list to give that agent access to it.")
    return 0


def _print_toolsets(config: AssistantConfig) -> None:
    """Print every name an ``[agents.*]`` table may put in ``tools``, grouped by what provides it.

    Grouped by provider because the single namespace deliberately hides provenance from the agent: a
    config says ``web``, not ``aimu:web``, so this command is the one place a user can learn that ``web``
    comes from AIMU, ``scheduling`` from Kokua, ``pdf`` from an installed plugin, and their own server
    from ``[[mcp.server]]``. Reads the whole registry rather than only the plugin entry points, because a
    list that omitted the built-in groups would read as "those are unavailable to you".
    """
    # Imported here, not at module level: kokua.toolsets.agents reaches kokua.core, which imports the
    # AIMU surface `preflight` exists to check, and this module must be importable before that check runs.
    from kokua.toolsets.agents import build_registry

    try:
        registry = build_registry(config)
    except ToolsetError as e:
        # Same reason Assistant.create translates this: a name two providers claim is a config mistake,
        # and this command is exactly where a user comes to diagnose one.
        print(e, file=sys.stderr)
        raise SystemExit(2) from None

    by_provider: dict[str, list[str]] = {}
    for name in registry:
        by_provider.setdefault(registry.providers[name], []).append(name)

    print("Toolsets available to this install. Name any of these in an [agents.<name>].tools list.\n")
    for provider, names in by_provider.items():
        print(f"{provider}:")
        for name in sorted(names):
            print(f"  {name}: {registry[name].description}")
        print()


def preflight() -> None:
    """Fail with an instruction, not a traceback, when the installed AIMU is too old.

    Shared by both console scripts. Runs before anything imports a front end, since that is what pulls
    in the AIMU surface whose absence would otherwise surface as a bare ``ImportError``.
    """
    try:
        require_aimu()
    except AimuVersionError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from None


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.command == "config":
        if args.config_command == "init":
            raise SystemExit(_init_config(args))
        parser.parse_args(["config", "--help"])  # no/unknown subcommand: show config usage and exit.
        return

    # Before the preflight, like `config`: installing a skill copies files and needs no AIMU surface, so
    # it should work even when the sibling checkout is out of date.
    if args.command == "skills":
        if args.skills_command == "list":
            raise SystemExit(_list_skills())
        if args.skills_command == "install":
            raise SystemExit(_install_skills(args))
        parser.parse_args(["skills", "--help"])  # no/unknown subcommand: show usage and exit.
        return

    preflight()

    if args.list_frontends:
        for name, frontend in sorted(plugins.discover_frontends().items()):
            print(f"{name}: {frontend.description}")
        return

    # A ConfigError is a user mistake with a known fix (a missing config.toml, no [agents.*] tables, a
    # bad key), so it prints as an instruction. A traceback here would bury the one line that matters. A
    # ToolsetError joins it because resolve_config now reads the installed toolsets' settings declarations,
    # and a bad one (a reserved section name, an unsupported type) is the same kind of fixable mistake --
    # made by whoever wrote the toolset, who needs the sentence rather than a stack.
    try:
        config = resolve_config(args)
    except (ConfigError, ToolsetError) as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from None

    # After resolve_config, not before it: the registry it lists depends on the file (load_plugins, and
    # every [[mcp.server]] name), so this command cannot answer honestly without one.
    if args.list_toolsets:
        _print_toolsets(config)
        return

    configure_logging(config)  # rotating file log + faulthandler, before the assistant starts
    frontend = plugins.get_frontend(config.frontend)
    try:
        asyncio.run(frontend.run(config, args))
    except KeyboardInterrupt:
        pass
    except ConfigError as e:  # raised at boot, e.g. a config defining no agents or an unknown toolset
        print(e, file=sys.stderr)
        raise SystemExit(2) from None


def main_web() -> None:
    """The `kokua-web` console script: preflight, then hand off to the web front end's own `main`.

    A thin wrapper rather than pointing the script straight at `kokua.frontends.web:main`, because
    importing that module pulls in the AIMU surface `preflight` checks, so the check has to happen
    before the import rather than inside it.
    """
    preflight()
    from .frontends.web import main as web_main

    # The same one-line report `main` gives a ConfigError, because the mistake and its fix do not depend
    # on which of the two console scripts the user typed. This route reaches the front end without
    # passing through `main`, so it needs its own handler rather than inheriting that one.
    try:
        web_main()
    except ConfigError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
