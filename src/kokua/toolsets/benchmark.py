"""Measure how fast the model this session runs on answers: time to first token, and output speed.

Contributes one tool, ``benchmark_model``, that streams a fixed short prompt and reports the two
numbers a user actually feels. They are separated deliberately: time to first token is dominated by
prompt processing and queueing, output speed by decoding, and a slow reply is one or the other. Folding
them into a single "seconds per response" would hide which.

**The tool takes no arguments**, which is the whole of its safety argument (the one
``backup_kokua_state`` makes): the model comes from ``config.toml``, so there is nothing the model can
redirect and nothing a per-call approval would protect.

Four decisions worth knowing before changing this:

- **It builds its own client rather than using the agent's.** The tool runs inside the very turn that
  agent is executing, and the token count comes off ``client.last_usage``, which every provider
  overwrites per request: reading it from the live client would race that turn writing it. The
  benchmark client also carries an *empty* system message, which is what keeps a short prompt short.
  Kokua's real system message plus tool block runs to thousands of tokens, and charging that to time to
  first token would measure Kokua's prompt rather than the model.
- **It inherits the declared sampling profile and reasoning effort**, since a benchmark of settings
  nobody runs is not worth reading. The only per-call parameter is ``max_tokens``, which bounds the
  wall-clock cost of four requests. ``max_tokens`` is AIMU's portable spelling, renamed per provider
  (``num_predict`` on Ollama), so the cap does not need a provider branch here.
- **What it measures is the session, not the caller.** The model is the entry agent's, because "the
  model this session runs on" is the question, and the report says so in as many words. Any agent may
  hold the toolset: a worker on a model of its own gets a true answer about the session rather than
  about itself, which is why the header names the agent instead of leaving a reader to assume. Scoping
  it to the holder would mean a toolset's ``build`` knowing which agent it is building for, and the
  context does not carry that.
- **An in-process model cannot be benchmarked**, and the tool says so rather than trying. AIMU's async
  factory refuses to build an ``hf:`` or ``llamacpp:`` client from a string, because a second client
  over one model would load its weights a second time. That refusal arrives here as
  ``ModelClientError``, and reporting it is the whole handling.

Token counts come from ``client.last_usage`` when the provider reports it, which the Ollama, Anthropic,
and OpenAI-compatible providers all do even on a streamed call (they populate it from the stream's
terminal chunk, contrary to what ``last_usage``'s own docstring says). Where a provider reports nothing,
stream chunks are counted instead and the report says the figure is approximate: a chunk is usually one
token but nothing guarantees it, and a number presented as exact when it is a guess is worse than a
labelled guess.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from statistics import median
from typing import Optional, Union

from aimu.tools import tool

from kokua.config import AssistantConfig
from kokua.registry.registry import Toolset

TOOLSET_NAME = "benchmark"

# Short and mechanical on purpose: a prompt inviting reasoning or recall would make the output length,
# and so the run's cost, depend on the model rather than on the cap below.
PROMPT = "Count from 1 to 100, one number per line, with no other words."

# The output cap, which bounds one run rather than shaping the measurement: output speed is a rate, so
# it does not depend on how many tokens are generated, and time to first token does not either.
MAX_OUTPUT_TOKENS = 256

# One discarded warmup plus this many timed runs. The warmup exists because a first request can carry a
# cold model load worth many seconds on Ollama, which would swamp a single measurement; three timed runs
# are what make a median and a range meaningful without paying for more requests than a chat turn should.
TIMED_RUNS = 3

RUN_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Measurement:
    """One completed run. ``tokens_counted_from_chunks`` marks a token count that is a chunk tally
    rather than a provider's own number, which is what the report has to hedge."""

    ttft_seconds: float
    decode_seconds: float
    output_tokens: int
    tokens_counted_from_chunks: bool

    @property
    def tokens_per_second(self) -> Optional[float]:
        """Output speed, or ``None`` when the whole response arrived in one chunk.

        A single chunk leaves no decode window to divide by. Returning ``None`` rather than a very
        large number keeps the caller from reporting an artifact of the transport as a model's speed.
        """
        if self.decode_seconds <= 0:
            return None
        return self.output_tokens / self.decode_seconds


async def measure_run(client, *, thinking: Optional[Union[bool, str]], timeout_seconds: float) -> Optional[Measurement]:
    """Time one streamed generation, or return ``None`` if it did not finish in ``timeout_seconds``.

    A timeout is a result, not an error: a model too slow or too busy to answer is exactly what a
    benchmark is asking about, and raising here would cost the runs that already succeeded.

    Empty chunks are skipped for both metrics. Ollama's stream opens and closes with parts that carry no
    text, and counting the trailing one as a token would inflate the count while treating the leading one
    as the first token would report a time to first token of nearly zero on every run.
    """
    tokens_from_chunks = 0
    first_token_at: Optional[float] = None
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_seconds):
            stream = await client.generate(
                PROMPT,
                generate_kwargs={"max_tokens": MAX_OUTPUT_TOKENS},
                stream=True,
                thinking=thinking,
            )
            async for chunk in stream:
                if not isinstance(chunk.content, str) or not chunk.content:
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic()
                tokens_from_chunks += 1
            finished = time.monotonic()
    except TimeoutError:
        return None

    if first_token_at is None:
        return None

    reported = (client.last_usage or {}).get("output_tokens")
    return Measurement(
        ttft_seconds=first_token_at - started,
        decode_seconds=finished - first_token_at,
        output_tokens=reported or tokens_from_chunks,
        tokens_counted_from_chunks=not reported,
    )


def _spread(values: list[float], digits: int, unit: str, range_unit: str) -> str:
    """A median with the range it came from, or the bare value when there is only one.

    ``range_unit`` is separate from ``unit`` because the two metrics read best differently: seconds
    want the unit on every number, a rate wants it once (``50.0 tokens/sec median (25.0 to 100.0)``).
    """
    middle = f"{median(values):.{digits}f}{unit}"
    if len(values) == 1:
        return middle
    ordered = sorted(values)
    return f"{middle} median ({ordered[0]:.{digits}f}{range_unit} to {ordered[-1]:.{digits}f}{range_unit})"


def _render_thinking(thinking: Optional[Union[bool, str]]) -> str:
    """The reasoning effort as a word, since reasoning tokens land inside both metrics and a reader
    comparing two reports needs to know whether they were on. Mirrors ``core.diagnostics``: a Python
    ``False`` is not what the user wrote in ``config.toml`` or wants to read back."""
    if thinking is None:
        return "unset"
    if thinking is True:
        return "on"
    if thinking is False:
        return "off"
    return str(thinking)


# What separates a warmup that loaded the model from one that was merely first. Both conditions have to
# hold: the ratio alone flags jitter on a fast local model (0.05s against 0.01s is five times as long and
# means nothing), and the absolute gap alone flags a slow endpoint where every request is seconds.
_COLD_START_RATIO = 3.0
_COLD_START_GAP_SECONDS = 0.5


def _was_cold(warmup: Measurement, timed_ttfts: list[float]) -> bool:
    """Whether the warmup looks like it paid a model load, rather than just being the first request.

    Asked so the report explains the warmup figure only when the explanation is the story. A hint
    printed whichever way the numbers came out teaches a reader to skip the line, including on the runs
    where it is the answer.
    """
    reference = median(timed_ttfts)
    return (
        warmup.ttft_seconds >= reference * _COLD_START_RATIO
        and warmup.ttft_seconds - reference >= _COLD_START_GAP_SECONDS
    )


def format_report(
    model: str,
    thinking: Optional[Union[bool, str]],
    *,
    warmup: Optional[Measurement],
    runs: list[Optional[Measurement]],
) -> str:
    """The benchmark as text for the model to relay, naming what was measured and what was not.

    Every way this measurement can be partial gets a line of its own rather than being dropped: runs
    that timed out are counted, a token count that is a chunk tally is called approximate, and a run
    that arrived in one chunk is named as having no speed to report. A benchmark that silently omits
    the runs that went badly reads as a faster model than the one you have.
    """
    completed = [run for run in runs if run is not None]
    lines = [
        f"Benchmark of {model}, the model this session's main agent runs on "
        f"(thinking: {_render_thinking(thinking)}). {len(runs)} timed runs of a fixed short prompt "
        f"capped at {MAX_OUTPUT_TOKENS} output tokens."
    ]

    if not completed:
        lines.append(f"- every one of the {len(runs)} timed runs timed out, so there is nothing to report")
    else:
        lines.append(f"- time to first token: {_spread([run.ttft_seconds for run in completed], 2, 's', 's')}")
        speeds = [run.tokens_per_second for run in completed if run.tokens_per_second is not None]
        if speeds:
            lines.append(f"- output speed: {_spread(speeds, 1, ' tokens/sec', '')}")
        no_window = len(completed) - len(speeds)
        if no_window:
            lines.append(
                f"- {no_window} of {len(completed)} completed runs arrived in a single chunk, leaving no "
                "decode window, so no output speed could be measured for them"
            )
        if any(run.tokens_counted_from_chunks for run in completed):
            lines.append(
                "- token counts are approximate: this provider reports no usage on a streamed call, so "
                "stream chunks were counted instead"
            )

    timed_out = len(runs) - len(completed)
    if timed_out and completed:
        lines.append(f"- {timed_out} of {len(runs)} timed runs timed out and are not counted above")

    if warmup is None:
        lines.append("- the discarded warmup run timed out, which on its own suggests a model under load")
    else:
        warmup_line = f"- the discarded warmup run reached its first token in {warmup.ttft_seconds:.2f}s"
        if completed and _was_cold(warmup, [run.ttft_seconds for run in completed]):
            warmup_line += ", far longer than the runs after it: the model was not resident and had to be loaded"
        lines.append(warmup_line)
    return "\n".join(lines)


def build(config: AssistantConfig) -> list:
    @tool
    async def benchmark_model() -> str:
        """Measure the speed of the model you are running on: time to first token and tokens per second.

        Runs one discarded warmup request and three timed ones against a short fixed prompt, so it
        costs a few seconds of model time. Reports a median and a range for each metric. Cannot
        benchmark an in-process model (an hf: or llamacpp: one), and says so if that is what is
        configured.
        """
        # Imported inside the tool, not at module level: `core.build` reaches `toolsets/`, so a
        # module-level import here would close that cycle. The same reason `toolsets/config.py` imports
        # `validate_model_string` in the body of its tool.
        from kokua.core.build import ModelClientError, build_model_client

        model = config.model_for(config.entry_agent)
        try:
            client = build_model_client(config, "", config.entry_agent)
        except ModelClientError as error:
            return (
                f"Could not build a client to benchmark {model}: {error}. Note that an in-process model "
                "(hf:, llamacpp:) cannot be benchmarked: a second client over one model would load its "
                "weights a second time, so AIMU refuses to build one."
            )

        thinking = config.thinking_for(config.entry_agent)
        warmup = await measure_run(client, thinking=thinking, timeout_seconds=RUN_TIMEOUT_SECONDS)
        runs = [
            await measure_run(client, thinking=thinking, timeout_seconds=RUN_TIMEOUT_SECONDS) for _ in range(TIMED_RUNS)
        ]
        return format_report(model, thinking, warmup=warmup, runs=runs)

    return [benchmark_model]


TOOLSET = Toolset(
    name=TOOLSET_NAME,
    description="Measure the speed of the model this session runs on: time to first token and tokens per second.",
    build=lambda ctx: build(ctx.config),
    cross_cutting=True,
)
