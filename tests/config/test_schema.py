"""AssistantConfig: the derived paths and defaults."""

from __future__ import annotations


from kokua.cli import build_arg_parser, resolve_config
from kokua.config import AssistantConfig
from kokua.config.schema import AgentConfig


def test_sessions_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path)
    assert cfg.sessions_path == tmp_path / "sessions.json"


def test_logs_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path)
    assert cfg.logs_path == tmp_path / "logs"


def test_default_confirm_tools():
    """The code-level default and the shipped example agree: `resolve_config` returns exactly what the
    example's explicit `confirm_tools` line says, which is the same list `AssistantConfig` falls back to
    when a `config.toml` omits the key entirely."""
    assert AssistantConfig().confirm_tools == [
        "add_skill_script",
        "add_mcp_server",
        "execute_python",
        "run_command",
        "update_config",
    ]
    assert resolve_config(build_arg_parser().parse_args([])).confirm_tools == [
        "add_skill_script",
        "add_mcp_server",
        "execute_python",
        "run_command",
        "update_config",
    ]


# --- the default model: one resolution, carrying whatever the string carries ----------


def _stub_resolver(monkeypatch, value="ollama:qwen3.8:27b@http://gpu-box:11434"):
    """Stand in for AIMU's default resolver, recording how it was called."""
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return value

    monkeypatch.setattr("aimu.models.resolve_default_text_model", fake)
    return calls


def test_default_model_prefers_what_the_file_declares(monkeypatch):
    calls = _stub_resolver(monkeypatch)
    cfg = AssistantConfig(model="ollama:qwen3:8b")
    assert cfg.default_model == "ollama:qwen3:8b"
    assert calls == [], "a declared default must not be second-guessed by a probe"


def test_default_model_keeps_the_endpoint_aimu_resolved(monkeypatch):
    """The regression this property exists for.

    With [assistant].model unset the default comes from AIMU_LANGUAGE_MODEL, which may carry an
    ``@base_url``. Recovering it from a built client instead returns a resolved enum, which cannot
    hold an endpoint, so every sub-agent Kokua rebuilt from that answer ran against the provider
    default while the entry agent talked to the override.
    """
    _stub_resolver(monkeypatch)
    assert AssistantConfig().default_model == "ollama:qwen3.8:27b@http://gpu-box:11434"


def test_default_model_skips_the_huggingface_probe(monkeypatch):
    """Kokua builds async clients, and the aio surface cannot construct an in-process ``hf:`` model
    from a string, so a default resolved with the HuggingFace cache included could name a model no
    agent here can be built on."""
    calls = _stub_resolver(monkeypatch)
    AssistantConfig().default_model
    assert calls == [{"include_hf_cache": False}]


def test_default_model_is_resolved_once_per_config(monkeypatch):
    """Cached, because the fallback path probes a local server over HTTP and compose_subagent asks
    for the default on every call."""
    calls = _stub_resolver(monkeypatch)
    cfg = AssistantConfig()
    assert cfg.default_model == cfg.default_model
    assert len(calls) == 1


def test_model_for_falls_back_to_the_resolved_default(monkeypatch):
    """``model_for`` is total: with nothing declared anywhere it still answers with a string, so no
    caller has to reconstruct the default from somewhere lossier."""
    _stub_resolver(monkeypatch)
    cfg = AssistantConfig(agents={"assistant": AgentConfig()}, entry_agent="assistant")
    assert cfg.model_for("assistant") == "ollama:qwen3.8:27b@http://gpu-box:11434"
