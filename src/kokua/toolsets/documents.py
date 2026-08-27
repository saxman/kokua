"""AIMU's document store, wrapped as a toolset.

Defines no tools of its own: an agent declaring ``documents`` gets AIMU's ``make_document_tools`` bound
to the one store this process opened. Shared and lazy for the same reasons as ``memory``: two agents
declaring it share one store over one directory, and no agent declaring it means no store opened.
"""

from __future__ import annotations

from aimu.tools.builtin import make_document_tools

from kokua.registry import Toolset

# Worth spelling out why this is four sentences rather than one. The store is a directory on disk
# that the user can add files to directly, so documents arrive by two routes, not one. The earlier
# text named only `save_document` and `search_documents`, which described the route where the model
# puts documents in and left the other invisible: asked about papers the user had just copied into
# the folder, the model had no reason to list, and reported an empty store while the files sat
# there. `read_document` is named for the same class of reason -- `search_documents` excerpts each
# match, so synthesizing from search results means synthesizing from the first page of each.
GUIDANCE = (
    " Documents are a folder on disk, not just a place you save things: the user can add files to it "
    "directly. When the user refers to documents, notes, papers, or files they have provided, call "
    "`list_documents` first to see what is actually there, rather than assuming the store is empty "
    "because you did not put anything in it. Call `read_document` for a document's full text when you "
    "need to analyze, summarize, or synthesize it, and `search_documents` only to locate which "
    "document mentions something, since it returns excerpts rather than whole documents. Save longer "
    "reference material the user gives you with `save_document` under a descriptive path. Documents "
    "must be UTF-8 text; if `list_documents` reports a file it could not read, tell the user to "
    "export that file to Markdown or plain text."
)

TOOLSET = Toolset(
    name="documents",
    description="Longer reference documents the user provides, searchable across conversations.",
    build=lambda ctx: make_document_tools(ctx.state.document_store),
    guidance=GUIDANCE,
    cross_cutting=True,
)
