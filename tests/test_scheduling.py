from kokua import paths
from kokua.config import AssistantConfig


def test_scheduled_tasks_path_under_data_dir(tmp_path):
    cfg = AssistantConfig(data_dir=tmp_path)
    assert cfg.scheduled_tasks_path == tmp_path / "scheduled_tasks.json"


def test_paths_scheduled_tasks_path_under_state(monkeypatch, tmp_path):
    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    assert paths.scheduled_tasks_path() == tmp_path / "data" / "scheduled_tasks.json"
