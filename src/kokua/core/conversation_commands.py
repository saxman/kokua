"""What ``/new``, ``/conversations``, and ``/switch`` print.

The commands are dispatched in ``Assistant._serve_channel``, beside ``/stop`` and ``/diag``, because a
conversation is a core concept and a channel has no route to the book that owns them. Their wording
lives here for the reason ``core/diagnostics.py`` holds the ``/diag`` report: a command's reply is one
block of text with rules of its own (how much of an id is enough to name a conversation, what to say
when a fragment names two), and those rules read better together than wrapped around the dispatch.

These three are the whole set. Deleting, renaming, and pinning a conversation stay web-only: none of
them is needed to keep a terminal user from getting stuck, which is the line this feature draws.
"""

from __future__ import annotations

from kokua.core.conversations import ID_PREFIX_MIN

NEW = "new"
LIST = "conversations"
SWITCH = "switch"
COMMANDS = frozenset({NEW, LIST, SWITCH})

SWITCH_USAGE = f"Usage: /switch <id>, using at least the {ID_PREFIX_MIN} characters /conversations prints."


def short_id(conversation_id: str) -> str:
    """The leading fragment of an id shown to a user, which is also the shortest ``resolve`` accepts."""
    return conversation_id[:ID_PREFIX_MIN]


def conversation_list(items: list[dict]) -> str:
    """The reply to ``/conversations``: every conversation, newest first, as ``Assistant`` reports them.

    Uncapped, matching the web sidebar. A cap would need a second command to see past it, and a
    conversation you cannot see is the problem this whole feature exists to fix.
    """
    plural = "" if len(items) == 1 else "s"
    lines = [f"{len(items)} conversation{plural}, newest first. '*' is the one you are in."]
    for item in items:
        mark = "*" if item["active"] else " "
        running = "  (a turn is running)" if item.get("running") else ""
        lines.append(f"{mark} {short_id(item['id'])}  {item['title']}{running}")
    lines.append("Switch with /switch <id>, or start a fresh one with /new.")
    return "\n".join(lines)


def no_such_conversation(fragment: str, matches: list[str]) -> str:
    """Why ``/switch`` refused a fragment, in the three ways it can refuse one.

    An ambiguous fragment prints the full ids rather than longer fragments of them, so the reply
    always carries something a user can retype. Without that, a list showing only the first
    ``ID_PREFIX_MIN`` characters would be a dead end for exactly the conversations it cannot tell apart.
    """
    if len(fragment) < ID_PREFIX_MIN:
        return (
            f"{fragment!r} is too short to name a conversation. "
            f"Give at least {ID_PREFIX_MIN} characters of its id; /conversations lists them."
        )
    if not matches:
        return f"No conversation's id starts with {fragment!r}. /conversations lists them."
    return f"{fragment!r} names {len(matches)} conversations. Type more of one of these ids: " + ", ".join(matches)


def started_new(conversation_id: str) -> str:
    return f"Started a new conversation ({short_id(conversation_id)}). It is empty: nothing earlier is in view."


# The three functions below take a title already defaulted by the caller (Assistant._conversation_title),
# so the placeholder for an untitled conversation is written in one place rather than in each reply.
def switched(title: str, conversation_id: str) -> str:
    return f"Now in {title!r} ({short_id(conversation_id)})."


def already_here(title: str, conversation_id: str) -> str:
    return f"Already in {title!r} ({short_id(conversation_id)})."


def left_running(title: str, conversation_id: str, *, muted: bool) -> str:
    """Appended when the conversation just left still has a turn in flight.

    Worth a sentence because three things change silently at that moment: the turn keeps running and
    persists to the conversation it started in (switching never cancels one), and ``/stop`` now reaches
    the conversation in view instead of that one. ``muted`` is what the channel does with the turn's
    output, which differs between front ends: a channel that tracks the viewed conversation stops
    drawing it, and one that does not (the terminal) keeps printing it here.

    The third consequence is the one a user cannot undo, so it is stated rather than left to be
    discovered: a backgrounded turn's gated tool calls auto-deny, because ``HumanGate.approve`` will not
    prompt for a turn nobody is watching (see invariant 3 in ``core/turns.py``).
    """
    fate = (
        "its output no longer draws here"
        if muted
        else "its output still prints here, since this channel shows every turn"
    )
    return (
        f" A turn is still running in {title!r} ({short_id(conversation_id)}): it keeps going "
        f"and saves there, and {fate}. /stop now reaches this conversation, so stopping that one means "
        f"/switch {short_id(conversation_id)} first, and until you do, a tool call it makes that needs "
        f"your approval is denied rather than asked about."
    )
