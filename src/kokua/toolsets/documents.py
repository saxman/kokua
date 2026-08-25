"""AIMU's document store, wrapped as a toolset.

Defines no tools of its own: an agent declaring ``documents`` gets AIMU's ``make_document_tools`` bound
to the one store this process opened. Shared and lazy for the same reasons as ``memory``: two agents
declaring it share one store over one directory, and no agent declaring it means no store opened.
"""

from __future__ import annotations

from aimu.tools.builtin import make_document_tools

from kokua.registry import Toolset

GUIDANCE = (
    " For longer reference material the user provides (notes, documents), call `save_document` with a "
    "descriptive path and `search_documents` to find relevant passages later."
)

TOOLSET = Toolset(
    name="documents",
    description="Longer reference documents the user provides, searchable across conversations.",
    build=lambda ctx: make_document_tools(ctx.state.document_store),
    guidance=GUIDANCE,
    cross_cutting=True,
)
