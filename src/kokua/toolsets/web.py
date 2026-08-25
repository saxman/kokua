"""AIMU's web search and page retrieval, wrapped as a toolset.

This defines no tools of its own. It hands an agent the callables AIMU's ``builtin.web`` group already
provides, which is why a wrapper file is all there is to read here.

The one AIMU group that carries ``guidance``, and the reason is a failure mode a schema cannot fix: a
tool schema says what ``web_search`` does but never when to prefer it over the model's own memory, and a
model answering from recall does not reach for the tool at all. The other groups are reached for when a
task plainly needs them (a file, a calculation), so a sentence there would only cost prompt tokens.
"""

from __future__ import annotations

from aimu.tools import builtin

from kokua.registry import Toolset


GUIDANCE = (
    " Your own knowledge has a training cutoff. When an answer could have changed since then, or the "
    "user could check it against a source (news, prices, releases, published figures, who holds a role, "
    "what a page says today), look it up with the web tools rather than recalling it, and say where the "
    "answer came from."
)


TOOLSET = Toolset(
    name="web",
    description="Web search and page retrieval.",
    build=lambda ctx: list(builtin.web),
    guidance=GUIDANCE,
)
