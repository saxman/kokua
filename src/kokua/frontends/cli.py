"""The terminal front end: Kokua's `CLIChannel` driving the assistant in the shell."""

from __future__ import annotations

import argparse
import sys

from ..assistant import Assistant
from ..channels.cli import CLIChannel
from ..config import AssistantConfig
from ..plugins import FrontEnd

_STARTUP_NOTICE = (
    "[notice] This assistant can author and run Python/shell scripts with full access to this "
    "machine (no sandbox), and can connect to remote MCP servers and run whatever tools they "
    "expose. Only use it with a model, inputs, and MCP servers you trust."
)


async def run(config: AssistantConfig, args: argparse.Namespace) -> None:
    print(_STARTUP_NOTICE, file=sys.stderr)
    channel = CLIChannel(show_thinking=config.show_thinking, show_tools=config.show_tools)
    assistant = await Assistant.create(config, channel)
    await assistant.run()


FRONTEND = FrontEnd(name="cli", description="Chat in the terminal (stdin/stdout).", run=run)
