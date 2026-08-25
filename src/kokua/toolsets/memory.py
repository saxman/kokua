"""AIMU's semantic memory store, wrapped as a toolset.

Defines no tools of its own: an agent declaring ``memory`` gets AIMU's ``make_memory_tools`` bound to the
one store this process opened.

That store is a lazy singleton on ``LiveState``, not something built here, and the reason is a bug the
split prevents: two agents declaring this toolset must share one store over one directory, so ``build``
creates only a closure over shared state and never state of its own. It is also why declaring this
toolset is what opens the store at all -- no agent naming ``memory`` means no store on disk, with no
flag able to disagree.
"""

from __future__ import annotations

from aimu.tools.builtin import make_memory_tools

from kokua.registry import Toolset

GUIDANCE = (
    " You have a persistent memory across conversations. When the user shares a durable fact about "
    "themselves or a preference worth remembering, call `store_memory` to save it, and call "
    "`search_memories` to recall such facts when they would help. Do not store transient chit-chat."
)

TOOLSET = Toolset(
    name="memory",
    description="Facts about the user, remembered across conversations.",
    build=lambda ctx: make_memory_tools(ctx.state.memory_store),
    guidance=GUIDANCE,
    cross_cutting=True,
)
