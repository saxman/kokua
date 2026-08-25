"""The benchmark toolset: one timed run, the summary over several, and the tool that composes them.

Timing is measured against a fake client whose stream sleeps for real, so `measure_run` is exercised
with a genuine clock rather than an injected one. That makes absolute numbers unassertable, which is
why the summary and the report are pure functions over `Measurement` values: everything about medians,
ranges, and the wording of a degraded result is tested with hand-built numbers, and the timing tests
assert only what a real clock can promise (an ordering, a positive value, a token count).
"""

from __future__ import annotations

import asyncio

import pytest
from aimu.models import StreamChunk, StreamingContentType

from kokua.config import AssistantConfig
from kokua.toolsets import benchmark
from kokua.toolsets.benchmark import (
    MAX_OUTPUT_TOKENS,
    PROMPT,
    TIMED_RUNS,
    TOOLSET,
    Measurement,
    build,
    format_report,
    measure_run,
)


class FakeStreamingClient:
    """An AIMU async client stand-in: one scripted stream per ``generate`` call.

    A script is a list of ``(delay_seconds, content)`` pairs. ``last_usage`` follows the real
    providers: cleared when the stream opens, populated only once it has been fully consumed.
    """

    def __init__(self, scripts, usage=None):
        self._scripts = list(scripts)
        self._usage = usage
        self.calls = []
        self.last_usage = None

    async def generate(self, prompt, generate_kwargs=None, stream=False, thinking=None, **_):
        self.calls.append({"prompt": prompt, "generate_kwargs": generate_kwargs, "thinking": thinking})
        return self._stream(self._scripts[len(self.calls) - 1])

    async def _stream(self, script):
        self.last_usage = None
        for delay, content in script:
            if delay:
                await asyncio.sleep(delay)
            yield StreamChunk(StreamingContentType.GENERATING, content)
        self.last_usage = self._usage


def _text(chunks):
    """A script of instant chunks, for a test that cares about counting rather than timing."""
    return [(0, content) for content in chunks]


def _measurement(ttft=0.5, decode=2.0, tokens=100, from_chunks=False) -> Measurement:
    return Measurement(
        ttft_seconds=ttft, decode_seconds=decode, output_tokens=tokens, tokens_counted_from_chunks=from_chunks
    )


# --- one timed run ---------------------------------------------------------------------------


async def test_measure_run_times_the_first_token_and_takes_the_provider_token_count():
    client = FakeStreamingClient([[(0.05, "a"), (0.01, "b"), (0.01, "c")]], usage={"output_tokens": 42})

    run = await measure_run(client, thinking=None, timeout_seconds=5)

    assert run.ttft_seconds >= 0.05
    assert run.decode_seconds > 0
    assert run.output_tokens == 42
    assert run.tokens_counted_from_chunks is False


async def test_measure_run_charges_only_decoding_to_the_decode_window():
    """The wait for the first token is prompt processing, and must not slow the reported speed."""
    client = FakeStreamingClient([[(0.15, "a"), (0.01, "b")]], usage={"output_tokens": 2})

    run = await measure_run(client, thinking=None, timeout_seconds=5)

    assert run.decode_seconds < run.ttft_seconds


async def test_measure_run_counts_chunks_when_the_provider_reports_no_usage():
    client = FakeStreamingClient([_text(["a", "b", "c"])], usage=None)

    run = await measure_run(client, thinking=None, timeout_seconds=5)

    assert run.output_tokens == 3
    assert run.tokens_counted_from_chunks is True


async def test_measure_run_ignores_empty_chunks():
    """Ollama's stream opens and closes with parts carrying no text; neither is a token, and the
    leading one is not the first token either."""
    client = FakeStreamingClient([[(0, ""), (0.05, "a"), (0.01, "b"), (0, "")]], usage=None)

    run = await measure_run(client, thinking=None, timeout_seconds=5)

    assert run.output_tokens == 2
    assert run.ttft_seconds >= 0.05


async def test_measure_run_reports_a_timeout_instead_of_raising():
    client = FakeStreamingClient([[(5, "a")]], usage=None)

    assert await measure_run(client, thinking=None, timeout_seconds=0.05) is None


async def test_measure_run_caps_the_output_and_passes_the_configured_thinking():
    """The cap is the only per-call parameter: everything else stays the client's own declared
    profile, so the numbers describe the model as the assistant actually runs it."""
    client = FakeStreamingClient([_text(["a"])], usage={"output_tokens": 1})

    await measure_run(client, thinking="high", timeout_seconds=5)

    assert client.calls[0]["generate_kwargs"] == {"max_tokens": MAX_OUTPUT_TOKENS}
    assert client.calls[0]["thinking"] == "high"
    assert client.calls[0]["prompt"] == PROMPT


# --- the report ------------------------------------------------------------------------------


def test_report_gives_the_median_and_the_range_for_both_metrics():
    runs = [
        _measurement(ttft=0.10, decode=1.0, tokens=100),  # 100 tok/s
        _measurement(ttft=0.30, decode=4.0, tokens=100),  # 25 tok/s
        _measurement(ttft=0.20, decode=2.0, tokens=100),  # 50 tok/s
    ]

    report = format_report("ollama:qwen3:8b", None, warmup=_measurement(), runs=runs)

    assert "0.20s median (0.10s to 0.30s)" in report
    assert "50.0 tokens/sec median (25.0 to 100.0)" in report


def test_report_names_the_model_and_the_thinking_setting():
    report = format_report("ollama:qwen3:8b", "high", warmup=_measurement(), runs=[_measurement()])

    assert "ollama:qwen3:8b" in report
    assert "thinking: high" in report


def test_report_calls_an_undeclared_thinking_setting_unset_rather_than_none():
    report = format_report("ollama:qwen3:8b", None, warmup=_measurement(), runs=[_measurement()])

    assert "thinking: unset" in report
    assert "None" not in report


def test_report_gives_the_warmup_its_own_line():
    report = format_report("m", None, warmup=_measurement(ttft=9.0), runs=[_measurement(ttft=0.2)])

    assert "warmup" in report
    assert "9.00s" in report


def test_report_reads_a_much_slower_warmup_as_a_model_that_had_to_be_loaded():
    report = format_report("m", None, warmup=_measurement(ttft=9.0), runs=[_measurement(ttft=0.2)])

    assert "not resident" in report


def test_report_does_not_cry_cold_load_over_a_warmup_in_line_with_the_rest():
    """A hint that fires whichever way the numbers came out is not a hint. On a fast local model the
    warmup lands within noise of the median, and saying "the model may not have been resident" there
    trains a reader to skip the line on the runs where it is true."""
    report = format_report("m", None, warmup=_measurement(ttft=0.17), runs=[_measurement(ttft=0.13)])

    assert "0.17s" in report
    assert "not resident" not in report


def test_report_says_the_warmup_timed_out_when_it_did():
    report = format_report("m", None, warmup=None, runs=[_measurement()])

    assert "warmup" in report
    assert "timed out" in report


def test_report_marks_token_counts_approximate_when_the_provider_reported_none():
    report = format_report("m", None, warmup=_measurement(), runs=[_measurement(from_chunks=True)])

    assert "approximate" in report


def test_report_does_not_hedge_a_provider_reported_token_count():
    report = format_report("m", None, warmup=_measurement(), runs=[_measurement(from_chunks=False)])

    assert "approximate" not in report


def test_report_declines_to_divide_by_a_zero_length_decode_window():
    """A one-chunk response leaves no decode window to divide by, so there is no speed to report."""
    report = format_report("m", None, warmup=_measurement(), runs=[_measurement(decode=0.0, tokens=1)])

    assert "tokens/sec median" not in report
    assert "single chunk" in report


def test_report_still_gives_a_speed_when_only_some_runs_had_no_decode_window():
    runs = [_measurement(decode=0.0, tokens=1), _measurement(decode=2.0, tokens=100)]

    report = format_report("m", None, warmup=_measurement(), runs=runs)

    assert "50.0 tokens/sec" in report


def test_report_counts_the_runs_that_timed_out():
    report = format_report("m", None, warmup=_measurement(), runs=[_measurement(), None, None])

    assert "2 of 3" in report
    assert "timed out" in report


def test_report_says_so_when_every_run_timed_out():
    report = format_report("m", None, warmup=None, runs=[None, None, None])

    assert "median" not in report
    assert "timed out" in report


# --- the tool --------------------------------------------------------------------------------


def _tool(config=None):
    (fn,) = build(config or AssistantConfig())
    return fn


def _install_client(monkeypatch, client):
    """Replace the client builder every path to a model goes through, so no request is made."""
    from kokua.core import build as core_build

    monkeypatch.setattr(core_build, "build_model_client", lambda *args, **kwargs: client)
    return client


def test_build_offers_one_tool():
    assert [fn.__name__ for fn in build(AssistantConfig())] == ["benchmark_model"]


def test_toolset_is_named_benchmark():
    assert TOOLSET.name == "benchmark"


async def test_benchmark_model_discards_a_warmup_before_the_timed_runs(monkeypatch):
    scripts = [_text(["a"]) for _ in range(TIMED_RUNS + 1)]
    client = _install_client(monkeypatch, FakeStreamingClient(scripts, usage={"output_tokens": 1}))

    report = await _tool()()

    assert len(client.calls) == TIMED_RUNS + 1
    assert "median" in report


async def test_benchmark_model_reports_the_model_the_entry_agent_runs_on(monkeypatch):
    config = AssistantConfig(model="ollama:qwen3:8b")
    _install_client(monkeypatch, FakeStreamingClient([_text(["a"])] * (TIMED_RUNS + 1), usage={"output_tokens": 1}))

    assert "ollama:qwen3:8b" in await _tool(config)()


async def test_benchmark_model_explains_an_in_process_model_it_cannot_build_a_client_for(monkeypatch):
    from kokua.core import build as core_build

    def refuse(*args, **kwargs):
        raise core_build.ModelClientError("aio.client() cannot construct in-process models from a string")

    monkeypatch.setattr(core_build, "build_model_client", refuse)

    report = await _tool()()

    assert "cannot" in report
    assert "in-process" in report


async def test_benchmark_model_reports_a_model_that_never_answers(monkeypatch):
    """A benchmark whose every run timed out is a result, not an exception the turn has to carry."""
    monkeypatch.setattr(benchmark, "RUN_TIMEOUT_SECONDS", 0.05)
    _install_client(monkeypatch, FakeStreamingClient([[(5, "a")]] * (TIMED_RUNS + 1)))

    assert "timed out" in await _tool()()


@pytest.mark.parametrize("thinking", [None, False, "high"])
async def test_benchmark_model_measures_with_the_configured_reasoning_effort(monkeypatch, thinking):
    config = AssistantConfig(thinking=thinking)
    client = _install_client(
        monkeypatch, FakeStreamingClient([_text(["a"])] * (TIMED_RUNS + 1), usage={"output_tokens": 1})
    )

    await _tool(config)()

    assert all(call["thinking"] == thinking for call in client.calls)


def test_any_agent_may_hold_the_toolset():
    """Nothing about this capability needs the agent Kokua built directly, unlike `skills`, whose script
    tool cannot be constructed without a live agent object. What it measures is a property of the
    session, so a worker holding it gets a real answer; the report names whose model it is."""
    assert TOOLSET.entry_point_only is False


def test_report_says_whose_model_the_figures_describe():
    """The tool answers a question about the session, not about its caller. An agent on a model of its
    own could otherwise read these figures as its own, so the report names the agent as well as the
    model rather than leaving the reader to assume."""
    report = format_report("ollama:qwen3:8b", None, warmup=_measurement(), runs=[_measurement()])

    assert "main agent" in report


def test_toolset_counts_as_self_management_rather_than_domain_work():
    assert TOOLSET.cross_cutting is True


def test_the_toolset_is_registered_as_a_plugin():
    """Registered through the same `kokua.toolsets` entry-point group a third party's would use, which
    is what `--list-toolsets` and every agent's declaration resolve against."""
    from kokua import plugins

    assert plugins.discover_toolsets()["benchmark"] is TOOLSET
