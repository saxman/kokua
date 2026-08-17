"""What a custom turn strategy is, and the two tiers Kokua can drive one at.

A workflow is a declaration carried by a :class:`kokua.toolsets.Toolset`: an agent that declares the
toolset gets the workflow's command, and nothing else does. The declaration builds an
``aimu.aio.AsyncRunner``, which is AIMU's abstract base for every agent and workflow, so AIMU's own
``aimu.aio.workflows`` (Chain, Parallel, Router, EvaluatorOptimizer, PlanExecuteEvaluator) are usable
here with no adapter, and ``AsyncRunner.as_tool()`` is how a workflow reaches the model as a tool
without any mechanism of Kokua's own.

Two tiers, probed once by :func:`is_rich` the way ``ChannelUI`` probes an optional frame:

**Base tier** is any ``AsyncRunner``. Kokua streams ``run()`` into the reply and owns catch-up, index
resolution, and persistence itself, so the runner needs to know nothing about Kokua. The cost is
presentation fidelity: AIMU's ``PlanExecuteEvaluator``, for instance, runs to completion and yields a
single chunk, so it arrives in one lump with no live progress.

**Rich tier** additionally implements ``run_turn()``, which is handed the channel, the human-decision
slot, and control of the agent's transcript. Deep planning needs all three: it shows phases and
reviewer cards, pauses for a human approve/edit/reject, and rewrites the transcript so a planned turn
is saved as a plain user/assistant pair rather than as planner scaffolding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from aimu.aio import AsyncRunner


@dataclass
class WorkflowResult:
    """What a workflow hands back for the caller to persist.

    ``committed`` is False when the turn produced no message to anchor to (deep planning's rejected
    plan is the case that motivates it); otherwise ``user_index`` locates the committed user message
    and the other two fields are that turn's reload-replay metadata.

    Only a run that returned normally produces one of these. A cancelled or failed run raises instead,
    which is why the core reads the index from :attr:`WorkflowContext.user_index` rather than from
    here.
    """

    committed: bool
    user_index: int = -1
    subagent_events: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)


@dataclass
class WorkflowContext:
    """One turn's view of the assistant, handed to a rich-tier workflow.

    Deliberately exposes no turn gate. The turn's gate hold is already taken by the caller, and a
    workflow taking a second one deadlocks against a waiting exclusive hold (see the concurrency
    invariants at the top of ``kokua.core.turns``).

    ``decide`` asks the user something and waits: it takes a prompt coroutine factory, a parser from
    the raw reply to the answer, and the answer to use when the request is abandoned. The vocabulary
    is the workflow's, so the core stays free of any one workflow's reply words.

    ``commit_user_message`` publishes this turn's committed user message: the core resolves where it
    landed, rewrites the workflow's scaffolding prompt back to the user's own words, and records the
    index here for the caller to read in a ``finally``. That is what lets a cancelled or failed run
    still anchor its sub-agent cards.
    """

    agent: Any
    ui: Any
    config: Any
    settings: Any
    msg: Any
    state: Any
    decide: Optional[Callable[..., Awaitable[Any]]]
    commit_user_message: Optional[Callable[[int, str], None]]
    _user_index: int = -1

    @property
    def user_index(self) -> int:
        """Where this turn's committed user message sits, or -1 if nothing was committed.

        -1 rather than an optimistic guess: a card filed at an index no message occupies would attach
        itself to whatever the *next* turn commits there.
        """
        return self._user_index

    def publish_user_index(self, index: int) -> None:
        self._user_index = index


@dataclass(frozen=True)
class Workflow:
    """One named turn strategy an agent can offer, declared on the toolset that carries it.

    ``command`` is the word after the slash, without it: ``"plan"`` is reached as ``/plan``. ``usage``
    is the one-line reply to an invocation with no argument, so the workflow owns that sentence rather
    than the serve loop guessing it.

    ``build`` is called once per invoked turn with that turn's context. Returning a fresh runner per
    turn is what lets a rich workflow hold per-turn state (an in-flight trace, a round counter) without
    resetting anything.
    """

    name: str
    description: str
    command: str
    usage: str
    build: Callable[[WorkflowContext], "AsyncRunner"]


def is_rich(runner: Any) -> bool:
    """Whether ``runner`` drives its own turn (rich tier) or Kokua drives it (base tier).

    A capability probe rather than an isinstance check, for the same reason ``ChannelUI`` probes its
    optional frames: a third party's runner should not have to import a Kokua base class to opt in.
    """
    return callable(getattr(runner, "run_turn", None))
