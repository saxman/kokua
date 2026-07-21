"""Turn a caught exception into a concise, user-facing description.

Provider-agnostic: it inspects only the exception chain, never a specific library's types. A model
request that fails deep in a provider SDK often carries the useful reason on ``__cause__`` (e.g. an
``openai.APIConnectionError`` whose own message is just "Connection error." but whose cause is an
``httpx.ConnectError: [Errno 61] Connection refused``), so this walks to the root cause and includes it.
"""

from __future__ import annotations


def _format(exc: BaseException) -> str:
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _root_cause(exc: BaseException) -> BaseException:
    """The deepest linked cause, following ``__cause__`` (explicit ``raise ... from``) and, when absent
    and not suppressed, the implicit ``__context__``. Guards against a cyclic chain."""
    seen = {id(exc)}
    current = exc
    while True:
        nxt = current.__cause__
        if nxt is None and not current.__suppress_context__:
            nxt = current.__context__
        if nxt is None or id(nxt) in seen:
            return current
        seen.add(id(nxt))
        current = nxt


def describe_error(exc: BaseException, *, max_length: int = 300) -> str:
    """A one-line description of *exc* including its root cause, truncated to *max_length*."""
    top = _format(exc)
    root = _root_cause(exc)
    text = top if root is exc else f"{top} (caused by {_format(root)})"
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text
