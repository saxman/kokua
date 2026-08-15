"""AIMU's built-in tool groups, its two persistent stores, and skills, wrapped as toolsets.

These wrap tools AIMU provides rather than defining any, which is why they live here instead of in a
subsystem's ``tools.py``: that convention marks the files holding Kokua's own ``@tool`` definitions.

The generative groups (audio, speech, transcription) each need their ``AIMU_*_MODEL`` env var and raise
at call time otherwise, so an agent declaring one is opting into that requirement. AIMU's fourth
generative group, ``image``, is not registered here; see the comment at its omission in ``_GROUPS``.

``cross_cutting`` is set for the capabilities an agent holds to manage itself. ``time`` is one of them
even though it is an AIMU group like ``fs``: an agent keeps a clock for its own scheduling and "when"
questions, so holding it does not make an agent a domain worker.
"""

from __future__ import annotations

from aimu.skills import make_skill_authoring_tool, make_skill_script_tool
from aimu.tools import builtin
from aimu.tools.builtin import make_document_tools, make_memory_tools

from kokua.toolsets.registry import Toolset

_GROUPS = {
    "web": (builtin.web, "Web search and page retrieval."),
    "fs": (builtin.fs, "Read files and list directories on this machine."),
    "compute": (builtin.compute, "Run Python, shell commands, and calculations."),
    "time": (builtin.time, "The current date and time, and timezone conversion."),
    "misc": (builtin.misc, "Assorted utilities."),
    # AIMU's own "image" group is deliberately not registered here. Kokua's `image` toolset
    # (kokua.toolsets.image, a plugin) supersedes it: both contribute a tool named `generate_image`, but
    # AIMU's saves into a folder inside the aimu package, which the web front end cannot serve, while
    # Kokua's saves into the servable `images_path`. Registering both would let a name collision (which
    # this registry otherwise treats as a startup error) instead decide, via first-wins deduplication,
    # which implementation an agent silently gets -- the exact failure a single namespace exists to rule
    # out. See tests/toolsets/test_builtin.py for the test pinning this.
    "audio": (builtin.audio, "Audio generation. Needs AIMU_AUDIO_MODEL."),
    "speech": (builtin.speech, "Text to speech. Needs AIMU_SPEECH_MODEL."),
    "transcription": (builtin.transcription, "Speech to text. Needs AIMU_TRANSCRIPTION_MODEL."),
}

MEMORY_GUIDANCE = (
    " You have a persistent memory across conversations. When the user shares a durable fact about "
    "themselves or a preference worth remembering, call `store_memory` to save it, and call "
    "`search_memories` to recall such facts when they would help. Do not store transient chit-chat."
)

DOCUMENTS_GUIDANCE = (
    " For longer reference material the user provides (notes, documents), call `save_document` with a "
    "descriptive path and `search_documents` to find relevant passages later."
)

SKILLS_GUIDANCE = (
    " When the user teaches you a repeatable procedure worth remembering, call `author_skill` to save "
    "it as a reusable skill; name skills in kebab-case (lowercase words joined by hyphens, e.g. "
    "'weekly-review'), never with underscores or spaces. When a procedure can be automated, call "
    "`add_skill_script` to attach a runnable Python or shell script to a skill; the script becomes a "
    "tool you can run immediately, even in the same turn. If a script fails, fix it by calling "
    "`add_skill_script` again with the SAME filename to overwrite it (a different filename just "
    "creates a duplicate and leaves the broken script). Scripts run with full access to this "
    "machine, so only automate what the user asked for."
)


def _group_toolset(name: str) -> Toolset:
    group, description = _GROUPS[name]
    return Toolset(
        name=name,
        description=description,
        build=lambda ctx, _group=group: list(_group),
        cross_cutting=name == "time",
    )


BUILTIN_TOOLSETS: tuple[Toolset, ...] = tuple(_group_toolset(name) for name in _GROUPS) + (
    Toolset(
        name="memory",
        description="Facts about the user, remembered across conversations.",
        build=lambda ctx: make_memory_tools(ctx.state.memory_store),
        guidance=MEMORY_GUIDANCE,
        cross_cutting=True,
    ),
    Toolset(
        name="documents",
        description="Longer reference documents the user provides, searchable across conversations.",
        build=lambda ctx: make_document_tools(ctx.state.document_store),
        guidance=DOCUMENTS_GUIDANCE,
        cross_cutting=True,
    ),
    Toolset(
        name="skills",
        description="Author skills and attach runnable scripts to them.",
        build=lambda ctx: [
            make_skill_authoring_tool(ctx.state.skill_manager, ctx.config.skills_dir),
            make_skill_script_tool(ctx.agent, ctx.state.skill_manager, ctx.config.skills_dir),
        ],
        guidance=SKILLS_GUIDANCE,
        cross_cutting=True,
        entry_point_only=True,
    ),
)
