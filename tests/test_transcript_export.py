"""The Markdown export: what a reader judging a run has to be able to see."""

from aimu.sessions import Session

from kokua.transcript_export import render_markdown


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
