"""The ``messaging`` toolset: an agent can address a message to another agent on this turn's bus.

Two things are worth knowing before reading the tools below. A receipt from ``send_message`` is an
accept, not a delivery promise: the run it names may finish before its next round boundary, so the
sender cannot honestly be told its message will be read, only that it was handed to the bus for
whichever of the named runs gets there first (see invariant 10 in ``core/turns.py``). And the roster
``list_agents`` shows names every run that has *opened a reader this turn*, not every run still
open: nothing in AIMU's protocol signals that a run has ended, and inferring it from reader-open order
would be the identity-by-ordering that protocol itself warns against (see
``core.messaging.MessageBus._register``). So an address on the roster may already name a run that has
finished, and a message sent to it is accepted and later reported undelivered rather than refused.
"""

from __future__ import annotations

from aimu.tools import tool

from kokua.core.messaging import EVERYONE, USER, current_address, current_bus, matches
from kokua.registry.registry import Toolset

NO_TURN = "No turn is currently running, so there is nobody to send to."
NO_ROSTER = "No turn is currently running, so there is no roster to list."


@tool
def send_message(to: str, text: str) -> str:
    """Send a message to another agent running in this same turn.

    `to` is an exact address from `list_agents` (like `researcher#1`), a bare label reaching every
    run under that name (`researcher` reaches every researcher running right now), or `everyone`.
    Call `list_agents` first if you are not sure who is reachable.

    The reply you get back says the message was accepted, naming who it was accepted for -- it is not
    a promise that any of them will read it: a run that already finished will never drain anything,
    and the bus cannot tell you that in advance. If nobody ends up reading it, you will see a report
    saying so once this turn ends.

    Args:
        to: An exact address, a bare label, or `everyone`.
        text: The message to send.
    """
    bus = current_bus.get()
    if bus is None:
        return NO_TURN
    roster = bus.roster()
    matched = [address for address in roster if matches(to, address)]
    if to != EVERYONE and not matched:
        available = ", ".join(roster) if roster else "nobody yet"
        return f"No agent matches {to!r}. Addressable right now: {available}, or 'everyone'."
    # The sender's own address comes off a contextvar rather than an argument: a tool is a plain
    # callable AIMU invokes with nothing in its arguments or its call stack naming the run that is
    # calling it, so this is the only way back to that fact. See `current_address`'s own comment in
    # `core/messaging.py` for why it is set in two places rather than once. Deliberately not defaulted
    # to `USER` when unset (which a real run never leaves unset: AIMU opens a reader, and so sets
    # this, before any tool call): `close` re-runs a `USER`-sent message as the user's own next turn,
    # so mislabelling an agent's message that way would put words in the user's mouth.
    bus.send(text, sender=current_address.get(), to=to)
    named = ", ".join(matched) if matched else "everyone currently on the roster"
    return f"Accepted for {named}. Delivered to whichever of them reads it next; you'll see a report if none do."


@tool
def list_agents() -> str:
    """List every agent reachable with `send_message` in this turn, plus the two standing selectors.

    Each line is one address. A worker's address may already name a run that has finished: nothing
    tells this list that, so a name here is "opened a reader this turn", not "still running".
    """
    bus = current_bus.get()
    if bus is None:
        return NO_ROSTER
    lines = [f"- {address}" for address in bus.roster()]
    lines.append(f"- {USER}: the person this turn is for.")
    lines.append(f"- {EVERYONE}: every one of the above at once.")
    return "\n".join(lines)


TOOLSET = Toolset(
    name="messaging",
    description="Send a message to another agent running in this turn, and list who is reachable.",
    build=lambda ctx: [send_message, list_agents],
    cross_cutting=True,
    # Both permitted directions need it: the entry agent addresses a worker, and a worker answers
    # its parent, so this cannot be restricted to the agent Kokua constructs directly the way `skills`
    # is.
    entry_point_only=False,
)
