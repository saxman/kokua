"""Command-line entry point.

Resolves an :class:`~kokua.config.AssistantConfig` from (in increasing precedence) built-in
defaults, an optional TOML config file, and command-line flags, then runs the selected front end
(default ``cli``; ``web`` and any installed plugin are also selectable). ``--list-frontends`` /
``--list-tool-packs`` introspect the plugin registry.

Flag defaults are the ``None`` sentinel rather than the real default value, so an unspecified flag
defers to the config file (and then the built-in default) instead of overriding it.
"""

from __future__ import annotations

import argparse
import asyncio

from . import plugins, settings
from .config import AssistantConfig


def build_arg_parser(prog: str = "kokua") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Kokua: a personal AI assistant.")

    # Plugin selection / introspection.
    parser.add_argument(
        "--frontend",
        default=None,
        help="Front end to run: 'cli' (terminal), 'web' (browser), or any installed plugin. Default: cli.",
    )
    parser.add_argument("--list-frontends", action="store_true", help="List available front ends and exit.")
    parser.add_argument("--list-tool-packs", action="store_true", help="List installed tool-pack plugins and exit.")
    parser.add_argument(
        "--plugins",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Discover tool-pack plugins via the 'kokua.tools' entry-point group. Default: on "
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
        help="Model string (e.g. 'anthropic:claude-sonnet-4-6'). Defaults to AIMU_LANGUAGE_MODEL "
        "/ a locally available model.",
    )
    parser.add_argument("--system", default=None, help="Override the assistant's system message.")
    parser.add_argument(
        "--reminder-seconds",
        type=float,
        default=None,
        help="If set, send a proactive reminder this many seconds after startup.",
    )
    parser.add_argument(
        "--reminder-text",
        default=None,
        help="Override the prompt used to generate the proactive reminder.",
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
        "--tools",
        default=None,
        help="Comma-separated AIMU built-in tool groups to expose: web, fs, compute, misc, image, "
        "audio, speech, transcription (or 'all' / 'none'). Default: web,fs,compute,misc. The "
        "generative groups (image/audio/speech/transcription) require their AIMU_*_MODEL env var.",
    )
    parser.add_argument(
        "--mcp",
        action="append",
        default=None,
        metavar="URL",
        help="Remote MCP server URL whose tools the assistant should use (repeatable). The "
        "assistant can also connect more servers mid-session via the add_mcp_server tool.",
    )
    parser.add_argument(
        "--mcp-bearer",
        default=None,
        help="Bearer token applied to all --mcp servers that require authentication.",
    )
    parser.add_argument(
        "--memory",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Persistent memory across conversations: facts about the user (semantic) plus "
        "user-provided documents. Default: on (use --no-memory to disable).",
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
    take("reminder_seconds", args.reminder_seconds)
    take("reminder_text", args.reminder_text)
    take("show_thinking", args.show_thinking)
    take("show_tools", args.show_tools)
    take("tools", args.tools, lambda v: [group.strip() for group in v.split(",") if group.strip()])
    take("mcp_servers", args.mcp)
    take("mcp_bearer", args.mcp_bearer)
    take("memory", args.memory)
    take("load_plugins", args.plugins)
    take("frontend", args.frontend)
    take("host", args.host)
    take("port", args.port)
    return overrides


def resolve_config(args: argparse.Namespace) -> AssistantConfig:
    """Merge built-in defaults < config file < CLI flags into the final config."""
    overrides = {**settings.load(args.config), **_cli_overrides(args)}
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


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.command == "config":
        if args.config_command == "init":
            raise SystemExit(_init_config(args))
        parser.parse_args(["config", "--help"])  # no/unknown subcommand: show config usage and exit.
        return

    if args.list_frontends:
        for name, frontend in sorted(plugins.discover_frontends().items()):
            print(f"{name}: {frontend.description}")
        return
    if args.list_tool_packs:
        packs = plugins.discover_tool_packs()
        if not packs:
            print("No tool-pack plugins installed.")
        for name, pack in sorted(packs.items()):
            print(f"{name}: {pack.description}")
        return

    config = resolve_config(args)
    frontend = plugins.get_frontend(config.frontend)
    try:
        asyncio.run(frontend.run(config, args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
