"""AIMU's web search and page retrieval, wrapped as a toolset.

This defines no tools of its own. It hands an agent the callables AIMU's ``builtin.web`` group already
provides, then the three ``make_page_tools`` builds, which is why a wrapper file is all there is to read
here. There are two ways to read a page, deliberately: ``get_web_content`` returns the whole of it (up to
20,000 characters a call), and ``fetch_page`` stores it and returns only the passages that match the
question, which costs far less context but ranks by shared words and can miss an answer phrased
differently. Neither is right for every read, so both stay and the guidance says when to use which.

The pages ``fetch_page`` stores live in ``data/pages``, one directory for every agent: a handle a worker
returns has to be readable by the agent it reports to, so a store per ``build`` would not do. Nothing
prunes it.

It carries ``guidance`` for a failure mode a schema cannot fix: a tool schema says what ``web_search``
does but never when to prefer it over the model's own memory, and a model answering from recall does not
reach for the tool at all. ``compute`` and ``time`` carry guidance for the same failure in another form
(a figure worked out in prose, a date assumed from training). ``fs`` carries none, because a model asked
about a file has nothing to answer from but the file.
"""

from __future__ import annotations

from aimu.tools import builtin
from aimu.tools.builtin import make_page_tools

from kokua.registry import Toolset


GUIDANCE = (
    " Your own knowledge has a training cutoff. When an answer could have changed since then, or the "
    "user could check it against a source (news, prices, releases, published figures, who holds a role, "
    "what a page says today), look it up with the web tools rather than recalling it, and say where the "
    "answer came from. To read a page, prefer `fetch_page` with the question you are trying to answer: it "
    "returns the passages that match, verbatim and with their offsets, instead of the whole page. It ranks "
    "by shared words, so it can miss an answer worded differently: if the passages do not answer the "
    "question, search with `find_in_page` or read on with `read_page` before concluding the page does not "
    "say it. Use `get_web_content` when you need the whole page."
)


TOOLSET = Toolset(
    name="web",
    description="Web search and page retrieval.",
    build=lambda ctx: [*builtin.web, *make_page_tools(directory=ctx.config.pages_path)],
    guidance=GUIDANCE,
)
