"""Document collections, searched, read, and written through AIMU's collection tools.

Defines no tools of its own: an agent declaring ``documents`` gets AIMU's ``make_collection_tools`` bound to
every collection's store, and one naming a collection gets the same tools bound to that collection alone.
A collection is a folder: Kokua's own ``kokua`` (``documents_path``), and each ``[[documents.collection]]``
the user declares, such as an Obsidian vault. The stores are shared and lazy for the same reasons as
``memory``: two agents holding one collection share one store and one index, and no agent declaring the
toolset means no store opened (see ``LiveState.document_stores``).

Each collection is also a *part* of this toolset (see ``Toolset.parts``), so an agent can be granted less
than every collection: ``work`` reads one vault, ``work_write`` reads and writes it, and only a collection
declared ``writable`` has a ``_write`` part. However it is granted, it is one toolset under one name, so a
gate written as ``documents.edit_document`` holds for an agent that holds only ``work_write``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aimu.tools.builtin import make_collection_tools

from kokua.config.schema import BUILTIN_COLLECTION, AssistantConfig
from kokua.registry import Toolset
from kokua.registry.registry import Setting

if TYPE_CHECKING:
    from kokua.registry.context import ToolsetContext

TOOLSET_NAME = "documents"

#: What the model reads about Kokua's own collection, which has no ``[[documents.collection]]`` entry to
#: carry a description.
BUILTIN_DESCRIPTION = "Documents you saved, and files the user put in Kokua's own documents folder."

DEFAULT_EMBEDDING_MODEL = "hf:BAAI/bge-base-en-v1.5"

# Why this text says what it does, since each clause is there for a failure it prevents.
#
# Collections are folders the user adds files to directly, so documents arrive by two routes, not one.
# Text naming only `save_document` and `search_documents` described the route where the model puts
# documents in and left the other invisible: asked about papers the user had just copied into the folder,
# the model had no reason to list, and reported an empty store while the files sat there. So it lists
# first, and the listing is also where it learns which collections it holds, what each is for, and which
# it may change, because those differ by agent and this text is the same for all of them.
#
# Search before read, because search finds passages across every collection at once and a read needs a
# collection and a path to start from. Read after search, because a search result is an excerpt of one
# passage, and synthesizing from search results means synthesizing from fragments. The result's collection
# is passed on to `read_document`, which cannot tell two collections holding the same path apart without
# it. Citing collection, path, and page is what lets the user check a claim against the paper it came from.
#
# The edit sentences are about one failure AIMU 0.31.0 closed and this text has to stop the model
# attempting. `read_document` returns a 2,000-line window rather than a whole document, and `save_document`
# replaces a whole one, so read-then-save on anything longer deleted everything past the window (a
# 3,000-line document came back 51 lines long, with the truncation marker saved into it as content). AIMU
# refuses that save now, naming `edit_document` in the refusal. Kokua still names `edit_document` here,
# because the refusal arrives after the model has already decided what to do and spent a turn on it, and
# because the model cannot page a document it does not know is windowed. PDFs are read as extracted text
# and can never be written, which the model would otherwise discover by trying.
GUIDANCE = (
    " Documents live in named collections: folders of the user's notes and papers, and `kokua`, Kokua's "
    "own folder. The user adds files to these folders directly, so call `list_documents` with no "
    "collection to see which collections you hold, what each is for, whether it is writable, and what is "
    "in it, rather than assuming a document is missing because you did not put it there. To answer from "
    "documents, search first: `search_documents` finds passages across your collections, each citing its "
    "collection, path, and heading or page. A result is an excerpt, so before analyzing, summarizing, or "
    "synthesizing, call `read_document` with its path and pass the result's collection as `collection`. "
    "When you use a document, cite its collection, path, and page. `read_document` returns one window of a "
    "long document and says so, naming the offset that continues the read: page through it before "
    "summarizing, and say which part you read if you stop early. Only a writable collection can be "
    "changed. To change a document, call `edit_document` with the exact text to replace, never "
    "`save_document`: saving replaces the whole document with what you pass, so saving back a document you "
    "read only part of would destroy the rest, and it is refused for that reason. Save longer reference "
    "material the user gives you with `save_document` under a descriptive path ending in `.md`. Documents "
    "are Markdown, plain text, or PDF files, and PDFs are read-only: you can search and read one, never "
    "save or edit it. If `list_documents` reports a file it could not read, tell the user why; for one "
    "that is not text, ask them to export it to Markdown or plain text."
)

#: Cold: the embedding model decides how every collection's index is built, so a change needs a restart.
#: An empty ``embedding_model`` means keyword-only search: no model is built, and nothing downloads.
DOCUMENTS_SETTINGS: tuple[Setting, ...] = (
    Setting("embedding_model", str, DEFAULT_EMBEDDING_MODEL),
    Setting("rerank", bool, False),
)


def _collections(config: AssistantConfig) -> list[tuple[str, bool]]:
    """Each collection's name and whether it may be written, in ``LiveState.document_stores`` order."""
    return [(BUILTIN_COLLECTION, True)] + [
        (collection.name, collection.writable) for collection in config.document_collections
    ]


def _parts(config: AssistantConfig) -> dict[str, tuple[str, bool]]:
    """``<name>`` grants a collection read-only, and ``<name>_write`` read and write where it is writable."""
    parts: dict[str, tuple[str, bool]] = {}
    for name, writable in _collections(config):
        parts[name] = (name, False)
        if writable:
            parts[f"{name}_write"] = (name, True)
    return parts


def _build(ctx: "ToolsetContext") -> list:
    """Every collection, each writable where it is declared so: the parts a full grant would name."""
    return _build_parts(ctx, frozenset(_collections(ctx.config)))


def _build_parts(ctx: "ToolsetContext", parts: frozenset[tuple[str, bool]]) -> list:
    """The collections ``parts`` names, writable only where a writable part was named.

    With no collection writable, AIMU builds no write tool at all, so a read-only grant holds nothing that
    could write rather than tools that refuse.
    """
    held = {name for name, _ in parts}
    writable = {name for name, can_write in parts if can_write}
    stores = {name: store for name, store in ctx.state.document_stores().items() if name in held}
    rerank = ctx.config.toolset_settings.get(TOOLSET_NAME, {}).get("rerank", False)
    # What a collection is for is the user's to say, and `list_documents` shows it beside the collection's
    # name, which is where the model tells which collection a question belongs to. It rides on the listing
    # rather than the guidance because the guidance is the same text for every agent, while the collections
    # an agent holds differ by grant. Only held collections are passed: AIMU refuses any other.
    described = {BUILTIN_COLLECTION: BUILTIN_DESCRIPTION}
    described |= {c.name: c.description for c in ctx.config.document_collections if c.description}
    descriptions = {name: text for name, text in described.items() if name in held}
    return make_collection_tools(stores, writable=writable, rerank=rerank, descriptions=descriptions)


TOOLSET = Toolset(
    name=TOOLSET_NAME,
    description="The user's document collections (notes, papers) and Kokua's own documents, searchable and readable.",
    build=_build,
    guidance=GUIDANCE,
    cross_cutting=True,
    settings=DOCUMENTS_SETTINGS,
    parts=_parts,
    build_parts=_build_parts,
    writable_part_remedy="set writable = true on the [[documents.collection]] named {part!r}",
)
