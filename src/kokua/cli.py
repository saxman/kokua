"""Command-line entry point.

Resolves an :class:`~kokua.config.AssistantConfig` from (in increasing precedence) built-in
defaults, an optional TOML config file, and command-line flags, then runs the selected front end
(default ``cli``; ``web`` and any installed plugin are also selectable). ``--list-frontends`` /
``--list-toolsets`` introspect the plugin registry.

Flag defaults are the ``None`` sentinel rather than the real default value, so an unspecified flag
defers to the config file (and then the built-in default) instead of overriding it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import plugins
from .aimu_compat import AimuVersionError, require_aimu
from kokua.config import file as settings
from kokua.config import AssistantConfig, ConfigError, MCPServerConfig
from kokua.mcp.servers import name_from_url
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
    parser.add_argument("--list-toolsets", action="store_true", help="List installed toolsets and exit.")
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
    parser.add_argument("--system", default=None, help="Override the assistant's system message.")
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
        "Default: add_skill_script,add_mcp_server,execute_python. Pass an empty string to disable.",
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
    return parser


def _cli_overrides(args: argparse.Namespace) -> dict:
    """Collect the flags the user actually passed (non-sentinel), keyed by AssistantConfig field."""
    overrides: dict = {}

    def take(field: str, value, transform=None):
        if value is not None:
            overrides[field] = transform(value) if transform else value

    take("model", args.model)
    take("system_message", args.system)
    take("show_thinking", args.show_thinking)
    take("show_tools", args.show_tools)
    take("mcp_servers", args.mcp, lambda urls: [MCPServerConfig(url=url, name=name_from_url(url)) for url in urls])
    take("load_plugins", args.plugins)
    take("confirm_tools", args.confirm_tools, lambda v: [name.strip() for name in v.split(",") if name.strip()])
    take("frontend", args.frontend)
    take("host", args.host)
    take("port", args.port)
    return overrides


def resolve_config(args: argparse.Namespace) -> AssistantConfig:
    """Merge built-in defaults < config file < CLI flags into the final config."""
    config_path, _ = settings.resolve_path(args.config)
    overrides = {"config_path": config_path, **settings.load(args.config), **_cli_overrides(args)}
    return AssistantConfig(**overrides)


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

    preflight()

    if args.list_frontends:
        for name, frontend in sorted(plugins.discover_frontends().items()):
            print(f"{name}: {frontend.description}")
        return
    if args.list_toolsets:
        toolsets = plugins.discover_toolsets()
        if not toolsets:
            print("No toolset plugins installed.")
        for name, toolset in sorted(toolsets.items()):
            print(f"{name}: {toolset.description}")
        return

    # A ConfigError is a user mistake with a known fix (a missing config.toml, no sub-agent roles, a
    # bad key), so it prints as an instruction. A traceback here would bury the one line that matters.
    try:
        config = resolve_config(args)
    except ConfigError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from None

    configure_logging(config)  # rotating file log + faulthandler, before the assistant starts
    frontend = plugins.get_frontend(config.frontend)
    try:
        asyncio.run(frontend.run(config, args))
    except KeyboardInterrupt:
        pass
    except ConfigError as e:  # raised at boot, e.g. a config that defines no sub-agent roles
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

    web_main()


if __name__ == "__main__":
    main()
