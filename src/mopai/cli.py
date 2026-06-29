"""Command-line entry point.

Builds an :class:`~mopai.config.AssistantConfig` from flags, then runs the selected front end
(default ``cli``; ``web`` and any installed plugin are also selectable). ``--list-frontends`` /
``--list-tool-packs`` introspect the plugin registry.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from . import plugins
from .config import AssistantConfig


def build_arg_parser(prog: str = "mopai") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="Mopai: my own personal ai.")

    # Plugin selection / introspection.
    parser.add_argument(
        "--frontend",
        default="cli",
        help="Front end to run: 'cli' (terminal), 'web' (browser), or any installed plugin. Default: cli.",
    )
    parser.add_argument("--list-frontends", action="store_true", help="List available front ends and exit.")
    parser.add_argument("--list-tool-packs", action="store_true", help="List installed tool-pack plugins and exit.")
    parser.add_argument("--no-plugins", action="store_true", help="Disable tool-pack plugin discovery for this run.")

    # Model + behaviour.
    parser.add_argument(
        "--model",
        default=None,
        help="Model string (e.g. 'anthropic:claude-sonnet-4-6'). Defaults to AIMU_LANGUAGE_MODEL "
        "/ a locally available model.",
    )
    parser.add_argument("--system", default=None, help="Override the assistant's system message.")
    parser.add_argument(
        "--skills-dir",
        default=None,
        help="Directory where authored skills are written and discovered. Default: <state>/skills.",
    )
    parser.add_argument(
        "--history",
        default=None,
        help="Conversation history database path. Default: <state>/history.json.",
    )
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
        default=True,
        help="Show the model's reasoning as it streams. Default: on (use --no-show-thinking to hide).",
    )
    parser.add_argument(
        "--show-tools",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show tool calls as they happen. Default: on (use --no-show-tools to hide).",
    )
    parser.add_argument(
        "--tools",
        default="web,fs,compute,misc",
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
        default=True,
        help="Persistent memory across conversations: facts about the user (semantic) plus "
        "user-provided documents. Default: on (use --no-memory to disable).",
    )

    # Web front-end binding (ignored by other front ends).
    parser.add_argument("--host", default="127.0.0.1", help="Web front end bind host. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="Web front end bind port. Default: 8000")
    return parser


def config_from_args(args: argparse.Namespace) -> AssistantConfig:
    # Omitted path flags fall back to the AssistantConfig defaults (under the app state dir).
    kwargs = {
        "model": args.model,
        "reminder_seconds": args.reminder_seconds,
        "show_thinking": args.show_thinking,
        "show_tools": args.show_tools,
        "tools": [group.strip() for group in args.tools.split(",") if group.strip()],
        "mcp_servers": args.mcp or [],
        "mcp_bearer": args.mcp_bearer,
        "memory": args.memory,
        "load_plugins": not args.no_plugins,
    }
    if args.skills_dir is not None:
        kwargs["skills_dir"] = Path(args.skills_dir)
    if args.history is not None:
        kwargs["history_path"] = args.history
    if args.system is not None:
        kwargs["system_message"] = args.system
    if args.reminder_text is not None:
        kwargs["reminder_text"] = args.reminder_text
    return AssistantConfig(**kwargs)


def main() -> None:
    args = build_arg_parser().parse_args()

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

    config = config_from_args(args)
    frontend = plugins.get_frontend(args.frontend)
    try:
        asyncio.run(frontend.run(config, args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
