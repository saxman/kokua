import json

from aimu.events import ModelTurnFinished, RequestPrepared, RunFinished, ToolCalled

from kokua import payloads
from kokua.core.requests import TurnRequests


def _prepared(run_id="lead-1", iteration=0, payload=None, parent=None, endpoint="http://batlin.local:8000/v1"):
    return RequestPrepared(
        agent="assistant",
        iteration=iteration,
        run_id=run_id,
        parent_run_id=parent,
        provider="OpenAICompatClient",
        model="qwen",
        endpoint=endpoint,
        payload=payload if payload is not None else {"messages": [{"role": "user", "content": "hi"}]},
    )


def _finished(run_id="lead-1", iteration=0, text="hello", stop_reason="stop", error=None, tool_calls=None):
    return ModelTurnFinished(
        agent="assistant",
        iteration=iteration,
        run_id=run_id,
        model="qwen",
        text=text,
        stop_reason=stop_reason,
        error=error,
        tool_calls=tool_calls or [],
        thinking="because",
    )


def test_a_call_counts_the_tool_calls_its_response_requested_with_capture_on_or_off(tmp_path):
    """The web UI moves a round's button onto the tool card its response asked for, which only a round
    that requested tools can have; the count says which rounds those are, without fetching anything."""
    for capture in (True, False):
        requests = TurnRequests(payloads_path=tmp_path / str(capture), capture=capture)
        requests(_prepared())
        requests(
            _finished(tool_calls=[{"name": "a", "arguments": {}, "id": "1"}, {"name": "b", "arguments": {}, "id": "2"}])
        )
        requests(_prepared(iteration=1))
        requests(_finished(iteration=1))
        assert [call["tool_call_count"] for call in requests.record()["calls"]] == [2, 0], capture


def test_a_call_pairs_its_request_with_the_turn_that_answered_it(tmp_path):
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(_prepared())
    requests(_finished())

    (call,) = requests.record()["calls"]
    assert (call["run_id"], call["round"], call["stop_reason"], call["endpoint"]) == (
        "lead-1",
        0,
        "stop",
        "http://batlin.local:8000/v1",
    )
    assert json.loads(payloads.read_text(tmp_path, call["payload_ref"])) == {
        "messages": [{"role": "user", "content": "hi"}]
    }
    response = json.loads(payloads.read_text(tmp_path, call["response_ref"]))
    assert response == {"text": "hello", "thinking": "because", "tool_calls": []}
    assert call["payload_chars"] == len(payloads.read_text(tmp_path, call["payload_ref"]))
    assert (call["message_count"], call["tool_count"]) == (1, None)


def test_two_requests_in_one_round_are_both_kept_in_order(tmp_path):
    """A FallbackClient tries one client per attempt within a round; the failed attempt is what a
    debugger most wants to see."""
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(_prepared(endpoint="http://primary:8000/v1"))
    requests(_finished(text=None, error=ConnectionError("down")))
    requests(_prepared(endpoint="http://backup:8000/v1"))
    requests(_finished())

    calls = requests.record()["calls"]
    assert [(c["endpoint"], c["error"]) for c in calls] == [
        ("http://primary:8000/v1", "ConnectionError"),
        ("http://backup:8000/v1", None),
    ]


def test_concurrent_runs_pair_by_run_id(tmp_path):
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(_prepared(run_id="a", parent="lead-1"))
    requests(_prepared(run_id="b", parent="lead-1"))
    requests(_finished(run_id="b", text="from b"))
    requests(_finished(run_id="a", text="from a"))

    by_run = {
        c["run_id"]: json.loads(payloads.read_text(tmp_path, c["response_ref"]))["text"]
        for c in requests.record()["calls"]
    }
    assert by_run == {"a": "from a", "b": "from b"}


def test_a_run_ending_is_recorded_with_its_rule_and_budget(tmp_path):
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(RunFinished(agent="assistant", iteration=10, run_id="lead-1", ended_by="wrap_up", max_iterations=10))

    assert requests.record()["endings"] == [
        {
            "run_id": "lead-1",
            "parent_run_id": None,
            "agent": "assistant",
            "ended_by": "wrap_up",
            "round": 10,
            "max_iterations": 10,
            "error": None,
        }
    ]


def test_capture_off_writes_no_files_but_keeps_calls_and_endings(tmp_path):
    requests = TurnRequests(payloads_path=tmp_path / "payloads", capture=False)
    requests(_prepared())
    requests(_finished())
    requests(RunFinished(agent="assistant", run_id="lead-1", ended_by="answered", max_iterations=10))

    record = requests.record()
    assert record["calls"][0]["payload_ref"] is None and record["calls"][0]["response_ref"] is None
    assert record["endings"][0]["ended_by"] == "answered"
    assert not (tmp_path / "payloads").exists()


def test_a_failed_write_records_the_call_without_a_reference(tmp_path, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("kokua.core.requests.payloads.save_text", boom)
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(_prepared())
    requests(_finished())
    requests(_prepared(iteration=1))
    requests(_finished(iteration=1))

    calls = requests.record()["calls"]
    assert [c["payload_ref"] for c in calls] == [None, None]
    assert sum("could not be saved" in r.getMessage() for r in caplog.records) == 1  # once per turn


def test_an_unserializable_payload_is_recorded_without_a_reference(tmp_path, caplog):
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(_prepared(payload={"bad": object()}))
    requests(_finished())

    assert requests.record()["calls"][0]["payload_ref"] is None
    assert sum("could not be serialized" in r.getMessage() for r in caplog.records) == 1


def test_a_response_is_not_serialized_when_capture_is_off(tmp_path, caplog):
    requests = TurnRequests(payloads_path=tmp_path, capture=False)
    requests(_prepared())
    requests(_finished(tool_calls=[object()]))

    assert requests.record()["calls"][0]["response_ref"] is None
    assert not any("could not be serialized" in r.getMessage() for r in caplog.records)


def test_a_response_pairs_with_its_runs_most_recent_request_and_an_orphan_stays_unanswered(tmp_path):
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(_prepared(endpoint="http://a"))
    requests(_prepared(endpoint="http://b"))
    requests(_finished(text="answer"))

    by_endpoint = {c["endpoint"]: c for c in requests.record()["calls"]}
    assert by_endpoint["http://a"]["response_ref"] is None
    assert by_endpoint["http://a"]["stop_reason"] is None
    response = json.loads(payloads.read_text(tmp_path, by_endpoint["http://b"]["response_ref"]))
    assert response["text"] == "answer"


def test_a_finished_turn_with_no_request_is_still_a_call(tmp_path):
    """A structured (schema=) turn reports itself without a RequestPrepared on some paths."""
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(_finished())

    (call,) = requests.record()["calls"]
    assert call["payload_ref"] is None and call["response_ref"] is not None


def test_other_events_are_ignored_and_an_empty_turn_records_nothing(tmp_path):
    requests = TurnRequests(payloads_path=tmp_path, capture=True)
    requests(ToolCalled(agent="assistant", name="t"))
    assert requests.record() is None


def test_notify_reports_each_completed_call_and_each_ending(tmp_path):
    seen = []
    requests = TurnRequests(
        payloads_path=tmp_path, capture=True, notify=lambda kind, record: seen.append((kind, record["run_id"]))
    )
    requests(_prepared())
    requests(_finished())
    requests(RunFinished(agent="assistant", run_id="lead-1", ended_by="answered", max_iterations=10))

    assert seen == [("request", "lead-1"), ("ending", "lead-1")]


def test_a_raising_notify_does_not_lose_the_record(tmp_path):
    def boom(kind, record):
        raise RuntimeError("ui gone")

    requests = TurnRequests(payloads_path=tmp_path, capture=True, notify=boom)
    requests(_prepared())
    requests(_finished())
    assert len(requests.record()["calls"]) == 1


def test_the_record_names_the_entry_run(tmp_path):
    requests = TurnRequests(payloads_path=tmp_path, capture=False, entry_run_id="lead-1")
    requests(_prepared())
    assert requests.record()["entry_run_id"] == "lead-1"
