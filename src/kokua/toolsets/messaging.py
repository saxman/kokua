"""The ``messaging`` toolset: an agent can address a message to another agent on this turn's bus.

Three things are worth knowing before reading the tools below. A receipt from ``send_message`` is an
accept, not a delivery promise: the run it names may finish before its next round boundary, so the
sender cannot honestly be told its message will be read, only that it was handed to the bus for
whichever of the named runs gets there first (see invariant 10 in ``core/turns.py``). The roster
``list_agents`` shows names every run that has *opened a reader this turn*, not every run still
open: nothing in AIMU's protocol signals that a run has ended, and inferring it from reader-open order
would be the identity-by-ordering that protocol itself warns against (see
``core.messaging.MessageBus._register``). So an address on the roster may already name a run that has
finished, and a message sent to it is accepted and later reported undelivered rather than refused.

And ``send_message`` refuses outright, rather than guessing, when it cannot say who is calling it: a
tool has no argument and nothing on the call stack naming the run invoking it, so the sender comes off
``core/messaging.py``'s ``current_address`` contextvar, which only a run that opened its own reader
ever sets. A worker whose spec omits an inbox (``toolsets/capabilities.py``'s composed worker was the
one shipped case, fixed alongside this check) never sets it and is refused rather than sent under
whoever spawned it; see ``core/subagents.py``'s ``SubagentReporter`` for the other half, which keeps a
*finished* nested run's leftover address from reaching a still-running caller's own later calls.
"""

from __future__ import annotations

from aimu.tools import tool

from kokua.core.messaging import EVERYONE, current_address, current_bus, matches
from kokua.registry.registry import Toolset

NO_TURN = "No turn is currently running, so there is nobody to send to."
NO_ROSTER = "No turn is currently running, so there is no roster to list."
# Said when `current_address` is unset: this run never opened a reader, so it has nothing to claim
# as its own return address. See `send_message`'s own comment for why that is refused rather than
# guessed at.
NO_ADDRESS = (
    "This run has no address of its own on this turn's bus, so it cannot send. (A normally spawned "
    "agent always has one; seeing this means whatever built this run did not wire it up.)"
)
# Refused for the same reason `Assistant._offer_message` refuses blank text from the user: AIMU
# discards whitespace at the drain, so an accepted blank message would be delivered to nobody and
# then reported undelivered for words that were never there. Checked explicitly rather than left to
# `_for_model`'s own prefix making the drained text non-blank by accident, which would hide the gap
# today and reopen it the moment that rendering changes.
BLANK_TEXT = "Blank text was not sent; there is nothing for another agent to read."


@tool
def send_message(to: str, text: str) -> str:
    """Send a message to another agent running in this same turn.

    `to` is an exact address from `list_agents` (like `researcher#1`), a bare label reaching every
    run under that name (`researcher` reaches every researcher running right now), or `everyone`.
    Call `list_agents` first if you are not sure who is reachable.

    The reply you get back says the message was accepted, naming who it was accepted for -- it is not
    a promise that any of them will read it: a run that already finished will never drain anything,
    and the bus cannot tell you that in advance. If nobody ends up reading it, nothing comes back to
    you about that: your own run will already be over by the time anyone could know, so the user
    (not you) is the one told.

    To brief an agent you are about to spawn, send to `everyone` *before* you spawn it. A message
    stays on the bus and each run reads from the start of it, so a run that did not exist when you
    sent it still reads it on its first round. `everyone` is the only selector that can do this: an
    exact address or a bare label is refused until a run with that name is already on the roster. The
    receipt for such a broadcast names only who was on the roster when you sent it, which is the
    smaller set; the agents you spawn afterwards read it too, and are not listed because they do not
    exist yet.

    Args:
        to: An exact address, a bare label, or `everyone`.
        text: The message to send.
    """
    bus = current_bus.get()
    if bus is None:
        return NO_TURN
    # The sender's own address comes off a contextvar rather than an argument: a tool is a plain
    # callable AIMU invokes with nothing in its arguments or its call stack naming the run that is
    # calling it, so this is the only way back to that fact. Checked, and refused on `None`, rather
    # than passed straight to `bus.send`: `current_address` is not scoped to this one call the way an
    # argument would be, so a run whose own spec never gave it a reader (see the module docstring)
    # would otherwise send under whoever happened to run immediately before it shared this Context.
    # Refusing turns that into a loud failure a reader of this code can trace, rather than a silent
    # one a reader of the bus cannot: `close` re-runs a `USER`-sent message as the user's own next
    # turn, so a default here that guessed `USER` would be the one case worse than guessing nothing.
    sender = current_address.get()
    if sender is None:
        return NO_ADDRESS
    if not text.strip():
        return BLANK_TEXT
    roster = bus.roster()
    matched = [address for address in roster if matches(to, address)]
    if to != EVERYONE and not matched:
        available = ", ".join(roster) if roster else "nobody yet"
        return f"No agent matches {to!r}. Addressable right now: {available}, or 'everyone'."
    bus.send(text, sender=sender, to=to)
    named = ", ".join(matched) if matched else "everyone currently on the roster"
    return f"Accepted for {named}. Delivered to whichever of them reads it next, if any of them do."


@tool
def list_agents() -> str:
    """List every agent reachable with `send_message` in this turn, plus `everyone`.

    Each line is one address, and the one marked `(you)` is your own: a message sent there comes back
    to you, so it never reaches whoever else carries your label. A worker's address may already name
    a run that has finished: nothing tells this list that, so a name here is "opened a reader this
    turn", not "still running". The person you are talking to is not on this list and cannot be
    addressed with `send_message`: they are who a turn is run *for*, not a run this bus has a reader
    for.
    """
    bus = current_bus.get()
    if bus is None:
        return NO_ROSTER
    # Read from the same contextvar `send_message` takes its sender from, so the mark and the return
    # address on a message can never disagree.
    caller = current_address.get()
    lines = [f"- {address} (you)" if address == caller else f"- {address}" for address in bus.roster()]
    lines.append(f"- {EVERYONE}: every one of the above at once.")
    return "\n".join(lines)


TOOLSET = Toolset(
    name="messaging",
    description="Send a message to another agent running in this turn, and list who is reachable.",
    build=lambda ctx: [send_message, list_agents],
    cross_cutting=True,
    # Not restricted to the agent Kokua constructs directly, the way `skills` is, because a worker is
    # one of the senders: a worker addresses a sibling running beside it, or answers its parent. Note
    # what this is *not* an argument about. Receiving needs no toolset at all (the loop drains the
    # reader a worker's spec opened, declared or not), so what a declaration buys anyone is the
    # ability to send, which is why the entry agent's own declaration is justified separately: it buys
    # `list_agents`, and the one send an orchestrator can usefully make, which is a broadcast ahead of
    # a spawn (see `MessageBus.reader` on a cursor opening at zero).
    entry_point_only=False,
)
