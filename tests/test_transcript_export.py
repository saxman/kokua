"""The Markdown export: what a reader judging a run has to be able to see."""

import re

from aimu.sessions import Session

from kokua.transcript_export import _render_item, _render_subagent, render_markdown


def _session(messages, metadata=None):
    meta = {"created_at": "2026-08-25T14:00:00", "updated_at": "2026-08-25T15:31:00"}
    meta.update(metadata or {})
    return Session(key="3f9a1c2b4d5e6f70", metadata=meta, messages=list(messages))


def test_header_carries_the_title_the_id_and_the_times():
    session = _session([{"role": "user", "content": "hello"}], {"title": "A conversation"})
    out = render_markdown(session)
    assert "# A conversation" in out
    assert "3f9a1c2b4d5e6f70" in out
    assert "2026-08-25 14:00" in out


def test_an_untitled_conversation_still_gets_a_heading():
    out = render_markdown(_session([{"role": "user", "content": "hello"}]))
    assert out.startswith("# ")


def test_a_turn_gets_a_heading_with_its_user_text_and_the_answer():
    session = _session(
        [
            {"role": "user", "content": "what is it", "timestamp": "2026-08-25T14:19:07"},
            {"role": "assistant", "content": "it is this", "timestamp": "2026-08-25T14:19:59"},
        ]
    )
    out = render_markdown(session)
    assert "## Turn 1" in out
    assert "14:19" in out
    assert "what is it" in out
    assert "it is this" in out


def test_reasoning_is_included_because_the_point_is_to_judge_it():
    session = _session(
        [
            {"role": "user", "content": "think"},
            {"role": "assistant", "content": "done", "thinking": "first I consider the options"},
        ]
    )
    out = render_markdown(session)
    assert "first I consider the options" in out


def test_a_verbose_trace_reasoning_segment_is_also_included():
    """replay_items emits `thinking` from an assistant message's own field, and `reasoning` from a
    verbose workflow trace's phase segments. They are two distinct item types, and a renderer that
    handled one and dropped the other would still pass the sibling test above, so this one must
    exercise the trace path specifically: a `trace` entry keyed by the turn's user message index,
    holding phase segments with a `text` field, which is exactly what replay_items reads to emit a
    `phase` item followed by a `reasoning` item.
    """
    session = _session(
        [{"role": "user", "content": "plan it"}],
        {"trace": {"0": [{"label": "Planner", "detail": "", "text": "weighing option A against B"}]}},
    )
    out = render_markdown(session)
    assert "weighing option A against B" in out


def test_the_model_and_effort_behind_a_turn_are_named():
    """Recorded per turn, because a conversation outlives the config that started it."""
    session = _session(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
        {"model": {"0": "openai-compat:qwen3@host:8000"}, "thinking": {"0": "high"}},
    )
    out = render_markdown(session)
    assert "openai-compat:qwen3@host:8000" in out
    assert "high" in out


def test_a_message_with_no_timestamp_gets_no_caption_rather_than_a_made_up_one():
    """Transcripts persisted before AIMU's inert timestamping have none, and inventing one would
    make the export lie about when a turn happened."""
    session = _session([{"role": "user", "content": "old"}, {"role": "assistant", "content": "reply"}])
    out = render_markdown(session)
    assert "1970" not in out
    assert "None" not in out


def test_an_empty_conversation_renders_a_header_and_says_it_is_empty():
    out = render_markdown(_session([]))
    assert "# " in out
    assert "no messages" in out.lower()


def test_multiple_turns_are_numbered_in_order():
    session = _session(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "b"},
        ]
    )
    out = render_markdown(session)
    assert out.index("## Turn 1") < out.index("## Turn 2")


def test_a_task_id_in_metadata_appears_in_the_header():
    """task_id is what nests a run under its scheduled task, and the thing a reader comparing runs
    would filter on, so its presence must actually change the rendered output."""
    session = _session([{"role": "user", "content": "hi"}], {"task_id": "nightly-digest"})
    out = render_markdown(session)
    assert "nightly-digest" in out


def test_no_task_id_means_no_task_line():
    """A conversation with no task_id must not fabricate one, so the header must differ from a
    session that has one."""
    without = render_markdown(_session([{"role": "user", "content": "hi"}]))
    with_task = render_markdown(_session([{"role": "user", "content": "hi"}], {"task_id": "nightly-digest"}))
    assert "nightly-digest" not in without
    assert "nightly-digest" in with_task


def test_a_tool_call_shows_its_name_arguments_and_result():
    session = _session(
        [
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "the contents"},
            {"role": "assistant", "content": "here it is"},
        ]
    )
    out = render_markdown(session)
    assert "read_file" in out
    assert "a.txt" in out
    assert "the contents" in out


def test_a_payload_containing_a_fence_does_not_break_out_of_its_block():
    """The single most likely silent failure in the export.

    Tool results and messages routinely contain triple-backtick fences. A block opened with a
    fence no longer than one inside its payload ends early, and every heading after it renders as
    code. The renderer opens with a longer fence than any run in the content.
    """
    payload = "here is code:\n```python\nprint('hi')\n```\ndone"
    session = _session(
        [
            {"role": "user", "content": "show me"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": payload},
            {"role": "assistant", "content": "there"},
        ]
    )
    out = render_markdown(session)
    assert "````" in out, "the block must open with a fence longer than the one inside it"
    opening = next(line for line in out.splitlines() if line.startswith("````"))
    assert len(opening.rstrip()) > 3


def test_a_payload_of_nested_fences_escalates_past_the_longest_run():
    payload = "a\n````\nb\n````\nc"
    session = _session(
        [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": payload},
        ]
    )
    out = render_markdown(session)
    assert "`````" in out


def test_an_over_long_payload_is_cut_and_says_so():
    """Uncapped, one read_file over a large document buries the turn being judged. Cut silently,
    the export reads as complete when it is not."""
    session = _session(
        [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "x" * 5000},
        ]
    )
    out = render_markdown(session, max_payload_chars=100)
    assert "truncated" in out
    assert "5000" in out


def test_the_cap_can_be_lifted():
    session = _session(
        [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "y" * 5000},
        ]
    )
    out = render_markdown(session, max_payload_chars=None)
    assert "truncated" not in out
    assert "y" * 5000 in out


def test_a_tool_call_with_no_result_says_so_rather_than_showing_an_empty_block():
    """A turn cancelled mid-call leaves a call with no answer, which is worth seeing."""
    session = _session(
        [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
            },
        ]
    )
    out = render_markdown(session)
    assert "no result" in out.lower()


# --- item types the renderer dispatches on ---------------------------------------------------


def test_an_image_only_user_message_still_opens_a_turn_with_its_cost():
    """A user message with no text (an image sent alone) yields no ``"user"`` item at all; its image
    item has to open the turn instead, or the heading, the cost line, and the model/effort line all
    go missing while the header total still counts the turn. Regresses if the turn-opening item's
    ``message_index`` is only ever stamped on a ``"user"``-typed item."""
    session = _session(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://ex.test/a.png"}}]}],
        {
            "usage": {
                "0": {"calls": 1, "input_tokens": 50, "output_tokens": 5, "model_seconds": 1.0, "wall_seconds": 1.5}
            }
        },
    )
    out = render_markdown(session)
    assert "## Turn 1" in out
    assert "a.png" in out
    assert "50" in out, "the turn's own cost must still render, not just the header total"


def test_an_image_item_with_a_url_names_it():
    assert _render_item({"type": "image", "url": "https://ex.test/a.png"}, None) == ["_[image: https://ex.test/a.png]_"]


def test_an_image_item_without_a_url_still_renders_a_placeholder():
    """Not reachable through a real message (``image_refs_of`` only ever returns a non-empty url),
    but ``replay_items``'s output shape does not guarantee one, so the renderer must not assume it."""
    assert _render_item({"type": "image", "url": ""}, None) == ["_[image]_"]


def test_a_loop_continuation_item_names_the_injected_prompt():
    session = _session(
        [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "partial"},
            {"role": "user", "content": "keep going", "provenance": "continuation"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert "[continued: keep going]" in render_markdown(session)


def test_a_forced_wrap_up_item_says_the_cap_was_reached():
    """Exported as a different phrase from a nudge, because the two told the model opposite things
    and an export is what someone reads when the run is long over."""
    session = _session(
        [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "partial"},
            {"role": "user", "content": "answer now", "provenance": "final_answer"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert "[tool-use limit reached: answer now]" in render_markdown(session)


def test_a_cards_injected_round_is_rendered_inside_the_card():
    """The card and the top level share one label helper, so a reader sees one phrasing
    throughout."""
    events = [
        {"id": "r-1", "role": "researcher", "task": "compare pricing", "status": "running"},
        {"id": "r-1", "append": {"kind": "loop", "reason": "final_answer", "text": "answer now"}},
        {"id": "r-1", "status": "done"},
    ]
    out = "\n".join(_render_subagent(events, None))
    assert "[tool-use limit reached: answer now]" in out


def test_a_phase_with_detail_shows_both_the_label_and_the_detail():
    session = _session(
        [{"role": "user", "content": "plan it"}],
        {"trace": {"0": [{"label": "Planner", "detail": "round 1", "text": ""}]}},
    )
    out = render_markdown(session)
    assert "**Planner**" in out
    assert "(round 1)" in out


def test_a_phase_with_no_detail_shows_only_the_label():
    session = _session(
        [{"role": "user", "content": "plan it"}],
        {"trace": {"0": [{"label": "Planner", "detail": "", "text": ""}]}},
    )
    out = render_markdown(session)
    assert "**Planner**" in out
    assert "(" not in out.split("**Planner**")[1].split("\n")[0]


def test_an_unrecognized_item_type_gets_a_visible_placeholder_not_silence():
    """A type ``replay_items`` grows after this renderer was written must still show up as
    something, rather than vanishing from the export with no trace it was ever there."""
    assert _render_item({"type": "a-future-type"}, None) == ["_(unrendered item type: a-future-type)_"]


# --- cost, delegation, and failure -----------------------------------------------------------
#
# metadata["usage"][idx] is what core/metrics.py's TurnMetrics.record actually produces: always
# calls/model_seconds/wall_seconds/models, plus input_tokens/output_tokens only when at least one
# call reported them, plus reported_calls alongside those two, plus by_agent only when more than
# one agent contributed. metadata["subagent"][idx] is a *sequence* of lifecycle events per spawn id
# (SubagentReporter in core/subagents.py): a create event carrying "role"/"task"/"status": "running"
# (plus "model"/"thinking" when configured), zero or more {"id", "append": {"kind": ...}} chunks,
# and a terminal {"id", "status": "done"|"stopped"|"error"} that may itself carry a final "append".
# replay_items emits one {"type": "subagent", **event} item per event, so a card is several items
# sharing one "id" that the renderer has to regroup.


def test_a_turns_cost_is_reported_beside_it():
    session = _session(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
        {
            "usage": {
                "0": {
                    "calls": 4,
                    "input_tokens": 41230,
                    "output_tokens": 3120,
                    "model_seconds": 37.4,
                    "wall_seconds": 52.1,
                    "models": ["m"],
                }
            }
        },
    )
    out = render_markdown(session)
    assert "41,230" in out, "large figures are grouped, because a reader compares them by eye"
    assert "3,120" in out
    # The call count as a standalone token. Asserting a bare "4" would pass on the fixture's own
    # "14:00" timestamp (from _session's default created_at) without the count ever rendering.
    assert re.search(r"\b4\b\s*(model )?calls?", out), "the number of model calls should be stated"


def test_a_turn_whose_provider_reported_no_tokens_says_not_reported():
    """Never zero. A stored absence rendered as 0 is a false claim about what the run cost."""
    session = _session(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
        {"usage": {"0": {"calls": 2, "model_seconds": 3.0, "wall_seconds": 4.0, "models": ["m"]}}},
    )
    out = render_markdown(session)
    assert "not reported" in out
    assert "0 in" not in out


def test_a_turn_with_partially_reported_calls_is_qualified_not_presented_as_complete():
    """reported_calls below calls means only some of the turn's model calls told us their token
    counts. The summed figure is still real, but showing it unqualified would let a partial sum
    read as a complete one."""
    session = _session(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
        {
            "usage": {
                "0": {
                    "calls": 5,
                    "reported_calls": 3,
                    "input_tokens": 300,
                    "output_tokens": 30,
                    "model_seconds": 2.0,
                    "wall_seconds": 3.0,
                }
            }
        },
    )
    out = render_markdown(session)
    assert "300" in out
    assert "3 of 5" in out, "the qualifier must name both how many calls reported and how many ran"


def test_totals_across_the_conversation_appear_in_the_header():
    session = _session(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "b"},
        ],
        {
            "usage": {
                "0": {"calls": 1, "input_tokens": 100, "output_tokens": 10, "model_seconds": 1.0, "wall_seconds": 2.0},
                "2": {"calls": 1, "input_tokens": 200, "output_tokens": 20, "model_seconds": 3.0, "wall_seconds": 4.0},
            }
        },
    )
    out = render_markdown(session)
    header = out.split("## Turn 1")[0]
    # "30" alone would be satisfied by "300" containing it as a prefix, so both figures are pinned
    # to the word next to them rather than asserted as bare substrings.
    assert "300 in" in header
    assert "30 out" in header


def test_the_headers_total_is_qualified_when_some_turns_measured_nothing():
    """One turn made 5 calls a local provider never reported tokens for; another turn made 1
    measured call. Six calls happened, one was measured, and summing the two turns' calls while
    dropping reported_calls would let the single measured call's figure stand in for the whole
    conversation's, unqualified: the same rule that keeps a per-turn figure from presenting a
    partial sum as complete has to hold one level up, in the header total, or it isn't a rule."""
    session = _session(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "b"},
        ],
        {
            "usage": {
                "0": {"calls": 5, "model_seconds": 5.0, "wall_seconds": 6.0},
                "2": {
                    "calls": 1,
                    "reported_calls": 1,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "model_seconds": 1.0,
                    "wall_seconds": 2.0,
                },
            }
        },
    )
    out = render_markdown(session)
    header = out.split("## Turn 1")[0]
    assert "6 model calls" in header or re.search(r"\b6\b\s*(model )?calls?", header)
    assert "1 of 6" in header, "the total must be qualified, not presented as a complete six-call figure"


def test_a_conversation_with_no_usage_omits_totals_rather_than_printing_zeros():
    session = _session([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}])
    out = render_markdown(session)
    assert "Totals" not in out


def test_a_spawned_sub_agent_appears_with_its_own_model_and_cost():
    session = _session(
        [{"role": "user", "content": "delegate"}, {"role": "assistant", "content": "done"}],
        {
            "subagent": {
                "0": [
                    {"id": "s1", "role": "research", "task": "research the thing", "status": "running", "model": "m2"},
                    {"id": "s1", "status": "done", "append": {"kind": "answer", "text": "found it"}},
                ]
            },
            "usage": {
                "0": {
                    "calls": 5,
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "model_seconds": 9.0,
                    "wall_seconds": 10.0,
                    "by_agent": {
                        "assistant": {"calls": 2, "input_tokens": 100, "output_tokens": 10},
                        "subagent-research": {"calls": 3, "input_tokens": 900, "output_tokens": 90},
                    },
                }
            },
        },
    )
    out = render_markdown(session)
    assert "research the thing" in out
    assert "found it" in out
    assert "m2" in out
    assert "900" in out
    assert "subagent-research" in out, "the measured breakdown must name the agent it measured, not just a number"
    assert "not counted" not in out.lower(), "a measured spawn must not also be flagged as uncounted"


def test_spawns_that_happened_without_being_measured_are_named_as_uncounted():
    """Distinct from "no spawns happened". An entry-agent total presented as the whole run's cost
    would read as a cheap delegating run, which is the opposite of true."""
    session = _session(
        [{"role": "user", "content": "delegate"}, {"role": "assistant", "content": "done"}],
        {
            "subagent": {
                "0": [
                    {"id": "s1", "role": "subagent", "task": "research", "status": "running"},
                    {"id": "s1", "status": "done", "append": {"kind": "answer", "text": "found it"}},
                ]
            },
            "usage": {
                "0": {
                    "calls": 2,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "model_seconds": 1.0,
                    "wall_seconds": 2.0,
                }
            },
        },
    )
    out = render_markdown(session)
    assert "not counted" in out.lower()


def test_a_multi_round_sub_agent_groups_all_its_events_into_one_card():
    """A spawn's lifecycle arrives as several events sharing one id: a create, then whatever it
    produced, then a terminal status. replay_items emits one item per event, so the renderer has to
    regroup them into a single card instead of one heading per event."""
    session = _session(
        [{"role": "user", "content": "delegate"}, {"role": "assistant", "content": "done"}],
        {
            "subagent": {
                "0": [
                    {"id": "s1", "role": "research", "task": "look into it", "status": "running"},
                    {"id": "s1", "append": {"kind": "reasoning", "text": "checking sources"}},
                    {
                        "id": "s1",
                        "append": {"kind": "tool", "name": "web_search", "arguments": "{}", "response": "results"},
                    },
                    {"id": "s1", "status": "done", "append": {"kind": "answer", "text": "the answer"}},
                ]
            }
        },
    )
    out = render_markdown(session)
    assert out.count("research") >= 1
    assert out.count("Sub-agent") == 1, "one spawn is one card, not one heading per lifecycle event"
    assert "checking sources" in out
    assert "web_search" in out
    assert "the answer" in out


def test_two_interleaved_spawns_each_get_their_own_card():
    """Concurrent spawns append into the same list in emission order, so a card has to be assembled
    by id, not by contiguous position."""
    session = _session(
        [{"role": "user", "content": "delegate"}, {"role": "assistant", "content": "done"}],
        {
            "subagent": {
                "0": [
                    {"id": "s1", "role": "a", "task": "task a", "status": "running"},
                    {"id": "s2", "role": "b", "task": "task b", "status": "running"},
                    {"id": "s1", "status": "done", "append": {"kind": "answer", "text": "answer a"}},
                    {"id": "s2", "status": "done", "append": {"kind": "answer", "text": "answer b"}},
                ]
            }
        },
    )
    out = render_markdown(session)
    assert out.count("Sub-agent") == 2
    assert "answer a" in out
    assert "answer b" in out


def test_a_sub_agent_answer_containing_a_fence_does_not_break_the_document():
    """A spawn's answer is arbitrary model-produced text, exactly like a tool result, and can
    contain a fence of its own. Rendered with a fence no longer than the payload's own, everything
    after it would render as code."""
    payload = "here:\n```python\nx = 1\n```\ndone"
    session = _session(
        [{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}],
        {
            "subagent": {
                "0": [
                    {"id": "s1", "role": "coder", "task": "write it", "status": "running"},
                    {"id": "s1", "status": "done", "append": {"kind": "answer", "text": payload}},
                ]
            }
        },
    )
    out = render_markdown(session)
    assert "````" in out


def test_a_reviewer_verdict_round_renders_its_issues():
    """A workflow reviewer's verdict shares the "subagent" item type with a real spawn but is a
    different shape entirely: planning/runner.py's ``_verdict_event`` persists it as
    ``{"role", "status", "issues": [...], "round"}`` with **no** "id" at all ("the persisted
    (id-less) form of a reviewer verdict, for replay", per that function's own docstring). It has to
    render (its issues are the whole point of the round) and must not trip the uncounted-delegation
    note, since no delegation happened at all."""
    session = _session(
        [{"role": "user", "content": "plan it"}, {"role": "assistant", "content": "done"}],
        {
            "subagent": {
                "0": [{"role": "Plan reviewer", "status": "rejected", "issues": ["skips error handling"], "round": 0}]
            }
        },
    )
    out = render_markdown(session)
    assert "skips error handling" in out
    assert "not counted" not in out.lower()


def test_an_approved_verdict_with_no_issues_still_renders():
    """An approved round's ``issues`` list is empty, not absent; the card must say it approved
    rather than rendering nothing (or breaking on an empty list where text was expected)."""
    session = _session(
        [{"role": "user", "content": "plan it"}, {"role": "assistant", "content": "done"}],
        {"subagent": {"0": [{"role": "Plan reviewer", "status": "approved", "issues": [], "round": 0}]}},
    )
    out = render_markdown(session)
    assert "approved" in out.lower()


def test_multiple_id_less_verdict_rounds_in_one_turn_each_get_their_own_card():
    """Every round is already a complete, standalone record (there is no lifecycle to reassemble,
    unlike a spawn's create/chunks/terminal sequence), and none of them carries an id. Grouping by
    id would put every id-less event in the same bucket and collapse two rounds of one reviewer
    plus one round of another into a single mislabeled card, discarding two of the three verdicts."""
    session = _session(
        [{"role": "user", "content": "plan it"}, {"role": "assistant", "content": "done"}],
        {
            "subagent": {
                "0": [
                    {"role": "Plan reviewer", "status": "rejected", "issues": ["missing rollback plan"], "round": 0},
                    {"role": "Plan reviewer", "status": "approved", "issues": [], "round": 1},
                    {"role": "Result reviewer", "status": "rejected", "issues": ["unverified claim"], "round": 0},
                ]
            }
        },
    )
    out = render_markdown(session)
    assert "missing rollback plan" in out
    assert "unverified claim" in out
    assert out.count("Plan reviewer") == 2
    assert out.count("Result reviewer") == 1


def test_a_failed_turn_carries_its_reason_after_the_output_it_cut_short():
    session = _session(
        [{"role": "user", "content": "do it"}, {"role": "assistant", "content": "partial"}],
        {"failure": {"0": "couldn't reach the model server: connection refused"}},
    )
    out = render_markdown(session)
    assert "connection refused" in out
    assert out.index("partial") < out.index("connection refused")


def test_a_failure_notice_renders_as_a_blockquote_aside():
    session = _session(
        [{"role": "user", "content": "do it"}, {"role": "assistant", "content": "partial"}],
        {"failure": {"0": "boom"}},
    )
    out = render_markdown(session)
    assert re.search(r"^> .*boom", out, re.MULTILINE)


def test_a_very_long_failure_reason_is_capped_like_any_other_payload():
    session = _session(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "partial"}],
        {"failure": {"0": "x" * 5000}},
    )
    out = render_markdown(session, max_payload_chars=100)
    assert "truncated" in out


def test_a_scheduled_runs_task_is_named_in_the_header():
    session = _session([{"role": "user", "content": "run"}], {"task_id": "nightly-digest"})
    assert "nightly-digest" in render_markdown(session).split("## Turn")[0]
