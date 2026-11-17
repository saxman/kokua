"""The three toolsets over AIMU's persistent state: memory, documents, and skill authoring.

Together rather than one file each because what matters about them is shared and comparative: each binds
to a lazy singleton on ``LiveState`` rather than building state of its own, which is what makes two
agents declaring one share a store over one directory instead of opening two.
"""

from __future__ import annotations

import threading
import time

import pytest

from kokua.config.schema import AgentConfig, AssistantConfig, DocumentCollectionConfig
from kokua.registry import LiveState, ToolsetContext
from kokua.registry.registry import ToolsetError, register, select, with_parts
from kokua.toolsets.documents import TOOLSET as DOCUMENTS
from kokua.toolsets.memory import TOOLSET as MEMORY
from kokua.toolsets.skills import TOOLSET as SKILLS


def _ctx(tmp_path, agent=None) -> ToolsetContext:
    return ToolsetContext(
        state=LiveState(config=AssistantConfig(data_dir=tmp_path)), agent=agent, agent_name="assistant"
    )


def test_memory_builds_tools_over_the_shared_store(tmp_path):
    names = {fn.__name__ for fn in MEMORY.build(_ctx(tmp_path))}

    assert "store_memory" in names
    assert "search_memories" in names


def test_documents_builds_the_document_tools(tmp_path):
    names = {fn.__name__ for fn in DOCUMENTS.build(_ctx(tmp_path))}

    assert "save_document" in names
    assert "search_documents" in names


def test_memory_and_documents_are_separate_toolsets_with_their_own_guidance():
    """Split so an agent can hold one without the other, and so the prompt it carries names only the
    tools it actually has."""
    assert MEMORY.guidance != DOCUMENTS.guidance


def test_skills_builds_the_authoring_and_script_tools(tmp_path):
    """Only the three authoring tools. The catalogue, `activate_skill`, and one tool per skill script come
    from AIMU's `SkillAgent` regardless of whether this toolset is declared."""

    class FakeAgent:
        tools: list = []

    names = {fn.__name__ for fn in SKILLS.build(_ctx(tmp_path, agent=FakeAgent()))}

    assert names == {"author_skill", "update_skill", "add_skill_script"}


def test_skills_builds_against_a_none_agent_without_complaining(tmp_path):
    """Why `skills` is `entry_point_only`, and why the flag has to be checked at *declaration* rather
    than left to fail on its own. `make_skill_script_tool` uses its agent at CALL time
    (`await agent.reload_skills()`), not build time, so building against `ctx.agent = None` -- what every
    spawned worker gets -- succeeds quietly here and would fail mid-call, after the script was already
    written to disk. `select` refusing the declaration at startup is the only place that can catch it.
    """
    names = {fn.__name__ for fn in SKILLS.build(_ctx(tmp_path, agent=None))}

    assert names == {"author_skill", "update_skill", "add_skill_script"}


def test_documents_guidance_names_the_tools_that_discover_user_supplied_files():
    """The regression this guards: the guidance used to name only `save_document` and
    `search_documents`, framing the store as write-then-search (documents get in because the model
    put them there). But every collection is a real directory the user can drop files into, so a
    prompt that never mentions `list_documents` gives the model no reason to look, and it reports an
    empty store while the files sit on disk."""
    guidance = DOCUMENTS.guidance

    assert "list_documents" in guidance
    assert "read_document" in guidance


def test_documents_guidance_says_what_a_document_can_be():
    """Only Markdown, text, and PDF files are documents, and a PDF cannot be written, so the model has to
    know both to ask for an export rather than report a file missing, and to stop before a doomed save."""
    guidance = DOCUMENTS.guidance

    assert "text" in guidance.lower()
    assert "PDFs are read-only" in guidance


def test_documents_guidance_teaches_search_then_read_and_citation():
    guidance = DOCUMENTS.guidance

    assert guidance.index("search_documents") < guidance.index("read_document")
    for phrase in ("collection", "path", "page", "edit_document"):
        assert phrase in guidance


# --- collections ----------------------------------------------------------------------------------


def _collections_config(tmp_path, **overrides) -> AssistantConfig:
    """Two vaults beside Kokua's own folder: `personal` writable with a description, `work` read-only."""
    personal = tmp_path / "vaults" / "personal"
    work = tmp_path / "vaults" / "work"
    personal.mkdir(parents=True)
    work.mkdir(parents=True)
    (personal / "attention.md").write_text("# Attention\n\nNotes on self-attention in transformers.\n")
    (work / "roadmap.md").write_text("# Roadmap\n\nThe transformer migration lands next quarter.\n")
    base = {
        "data_dir": tmp_path / "data",
        "document_collections": [
            DocumentCollectionConfig("personal", personal, writable=True, description="Personal research notes."),
            DocumentCollectionConfig("work", work),
        ],
    }
    base.update(overrides)
    return AssistantConfig(**base)


def _state(config) -> LiveState:
    return LiveState(config=config, registry=with_parts(register([("kokua", [DOCUMENTS])]), config))


def _granted(state, *names: str) -> dict:
    """The document tools an agent declaring ``names`` holds, built the way every agent's are."""
    (toolset,) = select(list(names), state.registry, agent="assistant", entry_point="assistant")
    ctx = ToolsetContext(state=state, agent=None, agent_name="assistant")
    return {fn.__name__: fn for fn in toolset.build(ctx)}


def test_each_collection_is_a_part_and_a_writable_one_has_a_write_part(tmp_path):
    config = _collections_config(tmp_path)

    assert DOCUMENTS.parts(config) == {
        "kokua": ("kokua", False),
        "kokua_write": ("kokua", True),
        "personal": ("personal", False),
        "personal_write": ("personal", True),
        "work": ("work", False),
    }


def test_whole_documents_holds_every_collection(tmp_path):
    state = _state(_collections_config(tmp_path))
    tools = _granted(state, "documents")

    overview = tools["list_documents"]().split("\n\n")[0]
    assert overview.splitlines()[1:] == [
        "- kokua (writable): Documents you saved, and files the user put in Kokua's own documents folder.",
        "- personal (writable): Personal research notes.",
        "- work (read-only)",
    ]
    assert {"save_document", "edit_document"} <= tools.keys()
    state.close()


def test_the_model_sees_collection_names_never_their_folders(tmp_path):
    """A folder path says where the user keeps things on their machine, which the model has no use for: it
    addresses a document by collection and path within it."""
    config = _collections_config(tmp_path)
    state = _state(config)
    listing = _granted(state, "documents")["list_documents"]()

    assert "/attention.md" in listing
    for collection in config.document_collections:
        assert str(collection.path) not in listing
    assert str(config.documents_path) not in listing
    state.close()


def test_parts_build_is_scoped(tmp_path):
    state = _state(_collections_config(tmp_path))
    tools = _granted(state, "work")

    assert tools.keys() == {"list_documents", "read_document", "search_documents"}
    found = tools["search_documents"]("transformer")
    assert "[work] /roadmap.md" in found
    assert "personal" not in found and "kokua" not in found
    assert tools["read_document"]("/attention.md", collection="personal").startswith("No collection named")
    state.close()


def test_a_write_part_makes_only_its_own_collection_writable(tmp_path):
    state = _state(_collections_config(tmp_path))
    tools = _granted(state, "personal_write", "work")

    assert (
        tools["save_document"]("/new.md", "fresh", collection="personal")
        == "Saved /new.md in the 'personal' collection."
    )
    refused = tools["save_document"]("/new.md", "fresh", collection="work")
    assert refused.startswith("Refusing: the 'work' collection is read-only")
    state.close()


def test_naming_the_whole_beside_a_part_grants_the_whole(tmp_path):
    state = _state(_collections_config(tmp_path))
    tools = _granted(state, "work", "documents")

    assert "- kokua (writable)" in tools["list_documents"]()
    state.close()


def test_a_write_part_of_a_read_only_collection_says_how_to_make_it_writable(tmp_path):
    state = _state(_collections_config(tmp_path))

    with pytest.raises(ToolsetError, match=r"set writable = true on the \[\[documents.collection\]\] named 'work'"):
        select(["work_write"], state.registry, agent="assistant", entry_point="assistant")


def test_every_collection_holds_only_documents_by_extension(tmp_path):
    """A vault holds more than notes (Obsidian keeps canvases, attachments, and settings beside them), and
    a file that is not a document should not be listed, counted, or reported unreadable on every search."""
    config = _collections_config(tmp_path)
    personal = config.document_collections[0].path
    (personal / "board.canvas").write_text("{}")
    (personal / "paper.txt").write_text("plain text")
    (config.documents_path).mkdir(parents=True)
    (config.documents_path / "notes.json").write_text("{}")
    state = _state(config)
    listing = _granted(state, "documents")["list_documents"]()

    assert "/paper.txt" in listing
    assert "board.canvas" not in listing and "notes.json" not in listing
    state.close()


def test_stores_are_lazy(tmp_path):
    """Nothing is opened until an agent's documents toolset builds, and building one opens no index and
    embeds nothing: the index and the model wait for the first search."""
    config = _collections_config(tmp_path)
    state = _state(config)
    assert state.built_document_stores is None

    _granted(state, "documents")

    assert state.built_document_stores is not None
    assert not config.index_dir.exists()
    state.close()


def test_stores_are_built_once_per_process_and_shared(tmp_path):
    state = _state(_collections_config(tmp_path))
    first = state.document_stores()
    _granted(state, "work")

    assert state.document_stores() is first
    assert state.document_store is first["kokua"]
    assert list(first) == ["kokua", "personal", "work"]
    state.close()


def test_concurrent_first_builds_open_one_set_of_stores(tmp_path, monkeypatch):
    """A deferred toolset builds in a worker thread, so two agents loading it at once must not race to
    open two stores over one folder, which would give each its own index writer."""
    import kokua.registry.context as context

    built = []
    real = context.DocumentStore

    def slow_store(*args, **kwargs):
        built.append(kwargs.get("persist_path"))
        time.sleep(0.01)
        return real(*args, **kwargs)

    monkeypatch.setattr(context, "DocumentStore", slow_store)
    state = _state(_collections_config(tmp_path))
    threads = [threading.Thread(target=state.document_stores) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(built) == 3
    state.close()


def test_index_lives_under_index_dir(tmp_path):
    config = _collections_config(tmp_path)
    state = _state(config)
    _granted(state, "work")["search_documents"]("transformer")

    assert [path.name for path in config.index_dir.iterdir()] == ["work"]
    assert any((config.index_dir / "work").iterdir())
    state.close()


def test_close_stops_every_store(tmp_path, monkeypatch):
    state = _state(_collections_config(tmp_path))
    closed = []
    for name, store in state.document_stores().items():
        monkeypatch.setattr(store, "close", lambda name=name: closed.append(name))

    state.close()

    assert sorted(closed) == ["kokua", "personal", "work"]  # together, so in no fixed order


def test_close_opens_nothing_when_no_store_was_built(tmp_path):
    state = _state(_collections_config(tmp_path))
    state.close()
    assert state.built_document_stores is None


# --- the embedding model --------------------------------------------------------------------------


class _Spec:
    id = "fake:embedder"


class _Embedder:
    """A deterministic stand-in for an AIMU embedding client: what the index calls, and nothing else."""

    spec = _Spec()

    def __init__(self):
        self.calls = 0

    def embed(self, texts, input_type="document"):
        self.calls += 1
        return [[float(len(text)), 1.0] for text in texts]


class _BrokenEmbedder(_Embedder):
    """An embedding model that cannot load: what a failed download or a missing extra looks like."""

    def embed(self, texts, input_type="document"):
        raise OSError("could not download BAAI/bge-base-en-v1.5")


def _documents_settings(model: str) -> dict:
    return {"documents": {"embedding_model": model, "rerank": False}}


def test_one_embedding_client_is_shared_by_every_store(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr("kokua.registry.context.embedding_client_for", lambda model: made.append(model) or _Embedder())
    config = _collections_config(tmp_path, toolset_settings=_documents_settings("hf:example/model"))
    state = _state(config)

    _granted(state, "documents")
    _granted(state, "work")

    assert made == ["hf:example/model"]
    state.close()


def test_building_the_toolset_embeds_nothing(tmp_path, monkeypatch):
    embedder = _Embedder()
    monkeypatch.setattr("kokua.registry.context.embedding_client_for", lambda model: embedder)
    state = _state(_collections_config(tmp_path, toolset_settings=_documents_settings("hf:example/model")))

    _granted(state, "documents")

    assert embedder.calls == 0
    state.close()


def test_an_empty_embedding_model_is_keyword_only(real_embedding_factory, monkeypatch):
    from kokua.registry.context import embedding_client_for

    made = []
    monkeypatch.setattr("kokua.registry.context.EmbeddingClient", lambda model: made.append(model) or _Embedder())

    assert embedding_client_for("") is None
    assert made == []
    assert isinstance(embedding_client_for("hf:example/model"), _Embedder)
    assert made == ["hf:example/model"]


def test_an_unknown_embedding_model_names_the_setting(real_embedding_factory):
    from kokua.registry.context import embedding_client_for

    with pytest.raises(ToolsetError, match=r'\[documents\].embedding_model.*nonsense:model.*""'):
        embedding_client_for("nonsense:model")


def test_a_failing_embedding_model_leaves_keyword_search_working_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("kokua.registry.context.embedding_client_for", lambda model: _BrokenEmbedder())
    state = _state(_collections_config(tmp_path, toolset_settings=_documents_settings("hf:example/model")))
    search = _granted(state, "work")["search_documents"]

    # The first search starts the index's background embedding, which is where the model fails to load.
    deadline = time.monotonic() + 5
    found = search("transformer")
    while "semantic search is unavailable" not in found and time.monotonic() < deadline:
        time.sleep(0.02)
        found = search("transformer")

    assert "[work] /roadmap.md" in found
    assert "semantic search is unavailable" in found
    assert "could not download" in found
    assert "keyword matches only" in found
    state.close()


def test_compose_subagent_with_a_collection_is_scoped_and_read_only(tmp_path):
    """A composed worker draws from the whole registry, so a part name is how the entry agent hands it one
    vault to read rather than every collection with write access."""
    from kokua.toolsets.capabilities import _compose_spec

    config = _collections_config(tmp_path, agents={"assistant": AgentConfig(available=["capabilities"])})
    state = _state(config)
    spec = _compose_spec("reader", ["work"], "Summarize the roadmap.", state, extra_tools=None, model="ollama:test")
    tools = {fn.__name__: fn for fn in spec["tools"]}

    assert tools.keys() == {"list_documents", "read_document", "search_documents"}
    assert "[work] /roadmap.md" in tools["search_documents"]("transformer")
    state.close()
