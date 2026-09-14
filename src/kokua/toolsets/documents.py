"""AIMU's document store, wrapped as a toolset.

Defines no tools of its own: an agent declaring ``documents`` gets AIMU's ``make_document_tools`` bound
to the one store this process opened. Shared and lazy for the same reasons as ``memory``: two agents
declaring it share one store over one directory, and no agent declaring it means no store opened.
"""

from __future__ import annotations

from aimu.tools.builtin import make_document_tools

from kokua.registry import Toolset

# Worth spelling out why this is six sentences rather than one. The store is a directory on disk
# that the user can add files to directly, so documents arrive by two routes, not one. The earlier
# text named only `save_document` and `search_documents`, which described the route where the model
# puts documents in and left the other invisible: asked about papers the user had just copied into
# the folder, the model had no reason to list, and reported an empty store while the files sat
# there. `read_document` is named for the same class of reason -- `search_documents` excerpts each
# match, so synthesizing from search results means synthesizing from the first page of each.
#
# The last two sentences are about one failure AIMU 0.31.0 closed and this text has to stop the model
# attempting. `read_document` now returns a 2,000-line window rather than a whole document, and
# `save_document` replaces a whole one, so read-then-save on anything longer deleted everything past
# the window (a 3,000-line document came back 51 lines long, with the truncation marker saved into it
# as content). AIMU refuses that save now, naming `edit_document` in the refusal. Kokua still names
# `edit_document` here, because the refusal arrives after the model has already decided what to do and
# spent a turn on it, and because the model cannot page a document it does not know is windowed.
GUIDANCE = (
    " Documents are a folder on disk, not just a place you save things: the user can add files to it "
    "directly. When the user refers to documents, notes, papers, or files they have provided, call "
    "`list_documents` first to see what is actually there, rather than assuming the store is empty "
    "because you did not put anything in it. Call `read_document` to analyze, summarize, or synthesize "
    "a document, and `search_documents` only to locate which document mentions something, since it "
    "returns excerpts rather than whole documents. Save longer reference material the user gives you "
    "with `save_document` under a descriptive path. `read_document` returns one window of a long "
    "document and says so, naming the offset that continues the read: page through it before "
    "summarizing, and say which part you read if you stop early. To change a document, call "
    "`edit_document` with the exact text to replace, never `save_document`: saving replaces the whole "
    "document with what you pass, so saving back a document you only read a window of would destroy the "
    "rest, and it is refused for that reason. Documents must be UTF-8 text; if `list_documents` reports "
    "a file it could not read, tell the user to export that file to Markdown or plain text."
)

TOOLSET = Toolset(
    name="documents",
    description="Longer reference documents the user provides, searchable across conversations.",
    build=lambda ctx: make_document_tools(ctx.state.document_store),
    guidance=GUIDANCE,
    cross_cutting=True,
)
