# Retrieval

An agent answering from your notes and papers has to find the right passage before it can read it, and
"find" is where most of the machinery is. A model cannot read a whole vault on every question, so
something has to pick the few hundred words worth putting in its context. That something is retrieval,
and this page walks through Kokua's: how documents are cut into passages, how two different rankings
score them, how those rankings are combined, how the index keeps up with files you edit elsewhere, and
how one set of tools reaches several folders at different levels of access.

The indexing and ranking live in AIMU, under `DocumentStore`
([Search document folders](https://saxman.info/aimu/how-to/search-document-folders/) is its how-to).
Kokua's part is configuration, access, and the words the model reads:
[`toolsets/documents.py`](https://github.com/saxman/kokua/blob/main/src/kokua/toolsets/documents.py) (the
toolset and its guidance),
[`registry/context.py`](https://github.com/saxman/kokua/blob/main/src/kokua/registry/context.py)
(`LiveState.document_stores`, one store per folder), and
[`registry/registry.py`](https://github.com/saxman/kokua/blob/main/src/kokua/registry/registry.py) (the
*parts* that let one agent hold less than every folder).

## Collections

A **collection** is a folder. Every install has one, `kokua`, the folder the assistant saves documents in
(`data/documents/`). You declare more in `config.toml`, such as an Obsidian vault or a folder of papers:

```toml
[[documents.collection]]
name = "work"
path = "~/Obsidian/Work"
writable = false
description = "Work projects: papers, meeting notes, and method write-ups."
```

The folder stays the source of truth. Kokua never moves your files, never writes into a folder you did
not mark `writable`, and keeps everything it derives from them somewhere else (see [The index is a
cache](#the-index-is-a-cache-and-never-lives-in-a-vault)). A file counts as a document when its suffix is
`.md`, `.markdown`, `.txt`, or `.pdf` (`DOCUMENT_EXTENSIONS` in `registry/context.py`). A vault holds more
than notes: Obsidian keeps canvases, attachments, and its own settings beside them, and a file that is
not a document, reported as unreadable on every search, would crowd out the ones that are. Dot-files and
dot-directories (`.obsidian/`, `.trash/`, `.git/`) are skipped, and so is a symlink leading out of the
folder.

The model reaches every collection it holds through five tools AIMU's `make_collection_tools` builds:
`list_documents`, `read_document`, `search_documents`, and, only when it may write somewhere,
`save_document` and `edit_document`. A search covers every held collection at once, and each result
names where it came from:

```text
[work] /papers/sampling-review.pdf (page 3)
Temperature sampling rescales the logits before ...

[personal] /Notes/Methods.md (Methods > Sampling)
...
also in: work:/archive/Methods.md
```

That is the shape of the output, built in AIMU's `search_documents`; a result longer than an excerpt
ends with the `read_document` call that returns the rest. The guidance in `toolsets/documents.py` asks
the model to search first, then read the document a result came from before summarizing it, because a
result is one passage and an answer built from passages alone is an answer built from fragments.

## Passages: cutting documents where their authors did

A whole document is the wrong unit to rank and the wrong unit to hand a model. Ranking a 40-page paper
as one item lets a single relevant paragraph drown in the rest, and returning it whole spends the
context on 39 pages nobody asked about. So a document is indexed as **passages**: chunks of at most
1,200 characters, with 150 repeated between neighbors so a sentence cut at a boundary is whole in one of
them (`chunk_document` in AIMU's `aimu/memory/_index.py`).

Where the cuts fall matters as much as their size. A Markdown file is first split at its headings, and
each section is chunked on its own, so a passage never straddles two sections and carries its heading
path (`Methods > Sampling`) as its location. A PDF is read as text with one `## Page N` marker per page
and split at those, so its passages cite a page. Two details keep the locations honest: YAML
frontmatter (an Obsidian note's `---` block) is metadata and is dropped, and a `#` line inside fenced
code is code, not a heading. A PDF's extracted text has no structure of its own, so in a PDF the page
marker is the only heading recognized, and a line beginning `# of samples` stays text.

## Two rankings, for two kinds of question

A research question usually needs both of these at once.

**Keyword ranking finds the exact terms.** A gene name, a method, an author: if the passage contains the
word, it should rank. The index is SQLite's FTS5 full-text engine, whose built-in ranking function is
BM25, the standard term-weighting formula: a word counts for more the rarer it is across all passages,
and for less in a long passage than a short one. It needs no model and nothing beyond Python's standard
library. Your query is not FTS5 syntax, so `CRISPR-Cas13` would otherwise parse as a column filter and a
`NOT`; the index keeps only the query's words, drops common English ones, quotes each, and joins them with
`OR`, so any query is accepted and a passage matching more of the words ranks higher
(`_match_expression`). FTS5 was chosen over AIMU's in-memory `aimu.rag.bm25_scores` because that function
rescans the corpus on every query, which does not survive tens of thousands of passages.

**Vector ranking finds the same idea in other words.** A note that says "the model picks less likely
tokens when it is hotter" shares no keyword with "temperature sampling". An **embedding** model turns a
passage into a list of numbers placed so that texts meaning similar things land near each other, and a
query is embedded the same way and compared by distance. Kokua's default is
`hf:BAAI/bge-base-en-v1.5`, which runs on your machine. Passages and queries are embedded differently
(`input_type="document"` and `"query"`), since retrieval models like it are trained to match a short
question to a longer answer rather than two texts of a kind. The vectors live in a Chroma collection
beside the keyword index. They are keyed by a hash of the document's text, not its path, so two copies of
one file in a folder are embedded once, and changing `[documents].embedding_model` re-embeds the folder
on its own rather than mixing two models' coordinates in one index.

Set `embedding_model = ""` and there is no vector half: search is keyword-only, nothing downloads, and
everything below still holds with one ranking instead of two.

## Fusing them: reciprocal rank fusion

Two rankings have to become one list, and their scores cannot be compared: a BM25 score and a cosine
distance are on scales with nothing in common, and any normalization of either is a guess. **Reciprocal
rank fusion** sidesteps the scores entirely and uses only positions. Each passage scores
`1 / (k + rank)` in every ranking that holds it, summed, with `k = 60`:

| Passage | Keyword rank | Vector rank | Fused score |
| --- | --- | --- | --- |
| A | 1 | 4 | 1/61 + 1/64 = 0.0320 |
| C | (absent) | 1 | 1/61 = 0.0164 |
| B | 2 | (absent) | 1/62 = 0.0161 |

A passage both rankings like beats one that only one ranking loves, which is usually what a
mixed-vocabulary question wants. `k` damps the advantage of the very top ranks; 60 is the value the
method's authors (Cormack, Clarke, and Buettcher, 2009) found robust. The whole function is a dozen lines,
`reciprocal_rank_fusion` in AIMU's `aimu/rag/fusion.py`, and it is worth reading.

Fusion runs twice. Within one collection it merges the keyword and vector rankings, each contributing a
pool of candidates several times larger than the answer, so a passage ranked modestly by both can still
surface. Across collections, `search_collections` (AIMU's `aimu/memory/document_store.py`) fuses each collection's own ranking the same way, which is why a small
folder and a large one need no common scale either. A passage held verbatim by two collections (one
paper filed in both vaults) comes back once, at its best rank, with the other copy in `also in`.
`[documents].rerank = true` adds a cross-encoder pass over the fused top results, a model that reads the
query and each passage together; it is slower, and off by default.

## Keeping up with edits made elsewhere

You edit a vault in Obsidian, not through Kokua, so the index has to notice. There is no file watcher.
Before each search the store walks the folder's metadata, comparing each file's modification time and
size with what it recorded, and re-chunks only the files that differ; a file that is gone has its
passages deleted. At the scale of a personal vault the walk is cheap, and it means a note saved a second
ago is searchable on the next query with no background process to keep alive. A file whose metadata
changed but whose text did not (a `touch`, a sync tool rewriting it) keeps its passages and vectors,
because the text's hash is compared before anything is redone. A file that could not be opened, such as a
cloud-synced one not downloaded yet, is tried again a minute later whether or not it changed.

## The first build, and saying when results are partial

The first search over a folder of several hundred papers has a lot to do: extract every PDF, cut tens of
thousands of passages, and embed them all. A search cannot wait minutes for that. So a search indexes
changed files itself for about two seconds, then hands the rest to one background thread per collection,
which also does all the embedding (`PassageIndex.refresh` in `aimu/memory/_index.py`). On a fresh install
that first embedding is also when the default model downloads, about 440 MB.

Meanwhile a search answers from what is indexed so far, which raises the design question this page cares
most about: a half-built index that silently returns a short list reads to the model exactly like a
search that found little. So the tool says so, in the result the model reads:

```text
Note: indexing in progress (412 of 1870 files embedded); results may be incomplete.
```

The same honesty covers failures. An embedding model that fails to load leaves keyword search working and
says `semantic search is unavailable (...); these are keyword matches only`; a file the index could not
process is named. A file that changed and has not been re-indexed yet is left out rather than returned
with text it no longer contains. And `list_documents` names every file it could not read and why, a
scanned PDF with no extractable text among them, so the model can tell you instead of reporting the file
missing. Nothing is skipped in silence, because a skipped file is invisible to everyone involved.

## The index is a cache, and never lives in a vault

Each collection's index is a SQLite database and a Chroma directory under
`$KOKUA_HOME/data/index/<name>/` (`AssistantConfig.index_dir`). It is derived from the folder and only
from the folder: delete it and you lose nothing but the time to rebuild it, which is also why an index
written by an older layout is dropped and rebuilt rather than migrated. Treating it as a cache is what lets
the folder stay the one source of truth.

It is kept out of the folder on purpose, enforced at both ends. AIMU refuses an index path inside the
folder it indexes, and Kokua refuses a collection whose folder is, is inside, or holds its data
directory, since indexing that would index the index and your conversations
(`validate_document_collections` in
[`core/agents.py`](https://github.com/saxman/kokua/blob/main/src/kokua/core/agents.py)). An index inside a
vault would be synced to your other devices, shown in Obsidian, and, if it were ever read as documents,
searched. For the same reason the [GitHub backup](../how-to/back-up-to-github.md) leaves `data/index/`
out: it is rebuilt from what the backup already holds.

The stores themselves are opened lazily and shared. `LiveState.document_stores` builds one per collection
the first time any agent's documents toolset is built, so two agents holding one collection share one
index, and an install where no agent declares `documents` opens nothing. Building a store embeds nothing;
that waits for the first search.

## Granting collections: parts of one toolset

An agent's capability is its `available` list ([Tool access](tool-access.md)), so a collection reaches an
agent only by being named there. Each collection adds names to that list:

| In `available` | The agent holds |
| --- | --- |
| `"documents"` | every collection, each writable where it is declared `writable` |
| `"work"` | the `work` collection, read-only, even when it is declared writable |
| `"work_write"` | the `work` collection, read and write; this name exists only when `work` is `writable = true` |

These names are **parts** of the one `documents` toolset, not toolsets of their own. A toolset that
declares `parts` (a function from the config to the names it can be granted as) and `build_parts` (how to
build the tools for a given set of them) can be granted narrower than whole, and `select` resolves part
names back into that single toolset, its build bound to the parts named. A researcher declaring
`available = ["work"]` therefore holds `list_documents`, `read_document`, and `search_documents` over
`work` alone, and no write tool at all: with no collection writable, AIMU builds none, so a read-only
grant holds nothing that could write rather than tools that refuse. An agent naming both `documents` and
a part gets the whole toolset, since the whole already holds every part.

Per-collection tools (`search_work`, `read_personal`) would have needed no new mechanism, and were rejected
for three reasons, each worth knowing for a toolset of your own:

- **First-wins deduplication.** `build_tools` keeps the first tool of a name among toolsets built
  together. Two toolsets each providing `search_documents` would silently lose the second one's, so a
  collection per toolset with shared tool names cannot work, and distinct names are the next problem.
- **Context size.** About five tool schemas per collection, sent on every request, against a local
  model's window that long conversations already fill. And a search across three vaults would become
  three calls merged by the model instead of one ranking fused by code built to do it.
- **One approval vocabulary.** However an agent was granted its collections, its tools were built by one
  toolset under one name, so `documents.edit_document` gates every document edit for every agent. An
  approval entry naming a part (`work_write.edit_document`) is refused at startup with the entry to write
  instead, because a part narrows what a toolset's tools reach, never which tools it has.

Residency follows the same rule: since every part builds as one toolset, the toolset is resident or
deferred whole. A `resident` list naming `work` while `available` also grants `personal`, or `documents`
whole, is a startup error (`_check_resident_parts` in `core/agents.py`) rather than a declaration that
silently made more resident than it said. `kokua --list-toolsets` and `list_capabilities` list each toolset's
parts beneath it, `kokua agents` shows the parts an agent was granted, and a part name shares the namespace every toolset name lives in, so a collection, a skill,
and an MCP server can never take one name.

## Writes ask first

The shipped `config.example.toml` puts `documents.save_document` and `documents.edit_document` in
`[assistant.tools].confirm`, so every document write asks you, in Kokua's own folder as much as in a
vault. That is coarser than it might be, and on purpose for now: the gate decides by tool name, so it
cannot tell a write to `kokua` from one to your personal vault, and the safe reading of "cannot tell" is
"ask". Loosen it in your own config if Kokua's folder is the only one you mark writable. A scheduled task
that tries to write a document is refused, and told that nobody is there to approve it, so it reports what
it would have written instead.

## Privacy

Everything above runs on your machine by default: the index is local files, and the default embedding
model runs locally once downloaded. But retrieval exists to put your documents in front of a model, so
what leaves the machine is decided by which models you configure.

- **The chat model reads every passage a search returns.** That is the point of searching. Run Kokua on
  a cloud model (`anthropic:`, `openai:`, or any remote endpoint) and each search sends excerpts of your
  vault to that provider, along with whatever the model then reads in full with `read_document`. A
  sub-agent granted a collection sends its excerpts to whichever model *it* runs on.
- **The embedding model reads every passage of every collection.** `hf:` models run locally. Naming a
  remote embedding model instead (AIMU accepts `openai:` ones) would send the whole of every collection
  to that provider during indexing, not only what a search turns up.
- **`update_config` can change `[documents].embedding_model`.** No toolset section is locked by default,
  and the change applies at the next restart. If your vaults must stay local, add
  `"documents.embedding_model"` to [`[security].locked_config_keys`](../reference/configuration.md#locked_config_keys).
  `[[documents.collection]]` itself is refused by `update_config` whatever the lock list says, since it
  names folders the assistant may read.

## What it does not do

No OCR, so a scanned PDF is reported as having no extractable text. No Obsidian structure: wikilinks,
backlinks, tags as a query language. PDF text comes from pypdf, which handles two-column layouts,
hyphenation, and equations imperfectly, and a passage from such a page can read out of order. And no file
watcher: the metadata walk runs when a search does, which is the right trade at the size of a personal
vault and would not be at the size of a company's.

## See also

- [Configuration reference](../reference/configuration.md#documents): every `[documents]` and
  `[[documents.collection]]` key, and what startup checks.
- [Tool access](tool-access.md): availability, residency, and approval, which parts narrow rather than
  replace.
- [Search document folders](https://saxman.info/aimu/how-to/search-document-folders/): the AIMU side,
  `DocumentStore.search_passages`, `index_status`, and `make_collection_tools`.
- [Architecture](architecture.md#how-an-agents-tools-resolve): where parts are resolved among the steps
  that build an agent's tools.
