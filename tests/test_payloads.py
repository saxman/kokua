"""Mock-only tests for oversized payload storage: content addressing, references, and the route."""

from __future__ import annotations

from kokua import payloads
from kokua.config import AssistantConfig
from tests.channels import example_agents
from kokua.frontends.web import build_app


def _config(tmp_path, **overrides) -> AssistantConfig:
    base = {"data_dir": tmp_path, "agents": example_agents(), "entry_agent": "assistant"}
    base.update(overrides)
    return AssistantConfig(**base)


def test_save_text_writes_a_content_addressed_file(tmp_path):
    reference = payloads.save_text(tmp_path, "a long tool response")

    assert reference.startswith("/payloads/")
    path = payloads.reference_to_path(tmp_path, reference)
    assert path is not None and path.is_file()
    assert path.read_text(encoding="utf-8") == "a long tool response"


def test_identical_text_is_stored_once(tmp_path):
    # A dedicated subdirectory, not tmp_path itself: the autouse isolate_state fixture already
    # seeds tmp_path with a kokua-home directory, which would throw off a bare directory-listing
    # count.
    store = tmp_path / "payloads"
    first = payloads.save_text(store, "the same bytes")
    second = payloads.save_text(store, "the same bytes")

    assert first == second
    assert len(list(store.iterdir())) == 1


def test_is_reference_rejects_other_strings(tmp_path):
    assert payloads.is_reference(payloads.save_text(tmp_path, "x"))
    assert not payloads.is_reference("/images/abc.png")
    assert not payloads.is_reference("https://example.com/thing")
    assert not payloads.is_reference("")


def test_reference_to_path_refuses_traversal(tmp_path):
    assert payloads.reference_to_path(tmp_path, "/payloads/../config.toml") is None
    assert payloads.reference_to_path(tmp_path, "/payloads/") is None
    assert payloads.reference_to_path(tmp_path, "/elsewhere/abc") is None


def test_route_serves_a_stored_payload(tmp_path):
    from starlette.testclient import TestClient

    from tests.helpers import MockAsyncModelClient

    config = _config(tmp_path)
    reference = payloads.save_text(config.payloads_path, "the full response")
    client = TestClient(build_app(config, client=MockAsyncModelClient([])))

    response = client.get(reference)
    assert response.status_code == 200
    assert response.text == "the full response"
    assert client.get("/payloads/not-a-real-digest").status_code == 404
    assert client.get("/payloads/sub/evil").status_code == 404  # traversal blocked by the route converter


def test_payloads_path_sits_under_data_dir(tmp_path):
    assert _config(tmp_path).payloads_path == tmp_path / "payloads"
