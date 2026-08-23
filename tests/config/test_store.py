"""Tests for the comment-preserving config.toml write layer."""

from __future__ import annotations

import tomllib

import pytest

from kokua.config import store as config_store
from kokua.config import file as settings
from tests.helpers import core_table


def _read(path):
    with path.open("rb") as file:
        return tomllib.load(file)


def test_set_value_seeds_from_example_when_file_absent(tmp_path):
    path = tmp_path / "config.toml"
    config_store.set_value(path, "assistant", "model", "anthropic:claude-opus-4-8")
    text = path.read_text(encoding="utf-8")
    # Seeded from the shipped example, so its documentation comments are present.
    assert "# Kokua configuration" in text
    assert _read(path)["assistant"]["model"] == "anthropic:claude-opus-4-8"


def test_set_value_preserves_existing_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# keep me\n[assistant]\nmodel = "old"\n', encoding="utf-8")
    config_store.set_value(path, "assistant", "model", "new")
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert _read(path)["assistant"]["model"] == "new"


def test_set_value_creates_missing_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[assistant]\nmodel = "m"\n', encoding="utf-8")
    config_store.set_value(path, "logging", "level", "DEBUG")
    assert _read(path)["logging"]["level"] == "DEBUG"
    assert _read(path)["assistant"]["model"] == "m"


def test_set_value_writes_a_float_into_a_missing_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.set_value(path, "widgets", "ratio", 0.3)
    assert _read(path)["widgets"]["ratio"] == 0.3


def test_unset_value_removes_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[assistant]\nmodel = "m"\nmemory = true\n', encoding="utf-8")
    config_store.unset_value(path, "assistant", "model")
    data = _read(path)
    assert "model" not in data["assistant"]
    assert data["assistant"]["memory"] is True


def test_unset_value_missing_key_is_no_op(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[assistant]\nmemory = true\n", encoding="utf-8")
    config_store.unset_value(path, "assistant", "model")
    assert _read(path)["assistant"]["memory"] is True


def test_setting_a_dotted_section_writes_a_real_sub_table(tmp_path):
    """`doc["assistant.generation"] = ...` would write a *quoted* top-level key, not a sub-table."""
    path = tmp_path / "config.toml"
    path.write_text('[assistant]\nmodel = "ollama:qwen3:8b"\n', encoding="utf-8")

    config_store.set_value(path, "assistant.generation", "temperature", 0.7)

    text = path.read_text(encoding="utf-8")
    assert "[assistant.generation]" in text
    assert '"assistant.generation"' not in text
    assert tomllib.loads(text)["assistant"]["generation"]["temperature"] == 0.7


def test_unsetting_a_dotted_section_key_removes_it(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[assistant.generation]\ntemperature = 0.7\ntop_p = 0.9\n", encoding="utf-8")

    config_store.unset_value(path, "assistant.generation", "temperature")

    assert tomllib.loads(path.read_text(encoding="utf-8"))["assistant"]["generation"] == {"top_p": 0.9}


def test_add_mcp_server_appends_and_is_readable_by_settings(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://plain/mcp")
    config_store.add_mcp_server(path, "https://auth/mcp", token_env="TOK")
    servers = settings.load(str(path), table=core_table())["mcp_servers"]
    assert [(s.url, s.token_env) for s in servers] == [
        ("https://plain/mcp", None),
        ("https://auth/mcp", "TOK"),
    ]


def test_add_mcp_server_updates_existing_url(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://x/mcp")
    config_store.add_mcp_server(path, "https://x/mcp", token_env="TOK")
    servers = settings.load(str(path), table=core_table())["mcp_servers"]
    assert [(s.url, s.token_env) for s in servers] == [("https://x/mcp", "TOK")]


def test_add_mcp_server_two_urls_on_one_host_get_distinct_names(tmp_path):
    """Two endpoints on one host derive the same base name; the write has to disambiguate them itself,
    since a successful add_mcp_server call must not be able to produce a config the registry's collision
    check would later reject at boot."""
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://broker.example.com/mcp/quotes")
    config_store.add_mcp_server(path, "https://broker.example.com/mcp/orders")
    servers = settings.load(str(path), table=core_table())["mcp_servers"]
    names = [s.name for s in servers]
    assert len(set(names)) == len(names)
    assert names[0] == "broker-example-com"
    assert names[1] == "broker-example-com-2"

    from kokua.toolsets.agents import build_registry
    from kokua.config.schema import AgentConfig, AssistantConfig

    config = AssistantConfig(
        agents={"assistant": AgentConfig(tools=names)},
        entry_agent="assistant",
        load_plugins=False,
        mcp_servers=servers,
    )
    registry = build_registry(config)  # must not raise a collision
    assert set(names) <= set(registry)


def test_add_mcp_server_replacing_an_existing_url_keeps_its_hand_edited_name(tmp_path):
    """Re-adding an already-recorded URL must not silently rename an entry a human named by hand: that
    name may be the one an [agents.*] table (locked by default) already references."""
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\nurl = "https://x/mcp"\nname = "custom-name"\n', encoding="utf-8")
    config_store.add_mcp_server(path, "https://x/mcp", token_env="TOK")
    servers = settings.load(str(path), table=core_table())["mcp_servers"]
    assert [(s.url, s.name, s.token_env) for s in servers] == [("https://x/mcp", "custom-name", "TOK")]


def test_remove_mcp_server(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config_store.add_mcp_server(path, "https://a/mcp")
    config_store.add_mcp_server(path, "https://b/mcp")
    assert config_store.remove_mcp_server(path, "https://a/mcp") is True
    servers = settings.load(str(path), table=core_table())["mcp_servers"]
    assert [s.url for s in servers] == ["https://b/mcp"]


def test_remove_mcp_server_absent_returns_false(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\nurl = "https://a/mcp"\n', encoding="utf-8")
    assert config_store.remove_mcp_server(path, "https://gone/mcp") is False


# The names an [[mcp.server]] entry is minted with. They live here, with the write, so `config`
# imports nothing above it and `mcp/servers.py` can record a runtime-added server through this module.


def test_name_from_url_replaces_dots_in_the_host_with_hyphens():
    assert config_store.name_from_url("https://broker.example.com/mcp") == "broker-example-com"


def test_name_from_url_falls_back_to_mcp_when_there_is_no_host():
    assert config_store.name_from_url("not-a-url") == "mcp"


def test_disambiguate_name_returns_the_base_when_free():
    assert config_store.disambiguate_name("stocks", set()) == "stocks"


def test_disambiguate_name_appends_a_numeric_suffix_on_collision():
    assert config_store.disambiguate_name("stocks", {"stocks"}) == "stocks-2"
    assert config_store.disambiguate_name("stocks", {"stocks", "stocks-2"}) == "stocks-3"


# --- The programmatic-write policy and the apply path ------------------------------------------------


async def _noop_apply(section, key, value):
    return None


def test_is_locked_covers_the_named_keys_and_every_agent_table():
    defaults = config_store.DEFAULT_LOCKED_CONFIG_KEYS
    assert config_store.is_locked("security", "confirm_tools", defaults)
    assert config_store.is_locked("email", "to", defaults)
    assert config_store.is_locked("paths", "data_dir", defaults)
    # By prefix, because a section name is per-agent and cannot be enumerated ahead of time.
    assert config_store.is_locked("agents", "assistant", defaults)
    assert config_store.is_locked("agents.researcher", "tools", defaults)
    assert not config_store.is_locked("display", "show_tools", defaults)


DEFAULTS = config_store.DEFAULT_LOCKED_CONFIG_KEYS


def test_locked_by_returns_the_pattern_that_matched():
    assert config_store.locked_by("agents.researcher", "tools", DEFAULTS) == "agents.*"
    assert config_store.locked_by("email", "to", DEFAULTS) == "email.to"
    assert config_store.locked_by("display", "show_tools", DEFAULTS) is None


def test_locked_by_section_wildcard_covers_the_section_and_its_descendants():
    patterns = ["scheduling.task.*"]
    assert config_store.locked_by("scheduling.task", "anything", patterns) == "scheduling.task.*"
    assert config_store.locked_by("scheduling.task.brief", "prompt", patterns) == "scheduling.task.*"
    # The parent section is not a descendant of the pattern, so an ordinary setting beside the tasks
    # stays writable.
    assert config_store.locked_by("scheduling", "max_task_conversations", patterns) is None


def test_locked_by_exact_pattern_needs_the_key_to_match_too():
    assert config_store.locked_by("email", "to", ["email.to"]) == "email.to"
    assert config_store.locked_by("email", "host", ["email.to"]) is None


def test_locked_by_bare_star_matches_everything():
    assert config_store.locked_by("display", "show_tools", ["*"]) == "*"


def test_the_lock_list_key_is_locked_whatever_the_list_says():
    assert config_store.is_locked("security", "locked_config_keys", [])
    assert config_store.is_locked("security", "locked_config_keys", ["display.*"])


def test_an_empty_list_locks_nothing_else():
    assert not config_store.is_locked("agents.researcher", "tools", [])
    assert not config_store.is_locked("email", "to", [])


def test_read_text_reports_an_absent_file_as_none(tmp_path):
    path = tmp_path / "config.toml"
    assert config_store.read_text(path) is None
    path.write_text("[assistant]\n", encoding="utf-8")
    assert "[assistant]" in config_store.read_text(path)


async def test_apply_setting_refuses_a_key_the_users_list_locks(tmp_path):
    path = tmp_path / "config.toml"
    with pytest.raises(config_store.SettingLocked) as error:
        await config_store.apply_setting(
            path,
            "display",
            "show_tools",
            "false",
            _noop_apply,
            table=core_table(),
            locked=["display.*"],
        )
    assert error.value.pattern == "display.*"
    assert not path.exists()


async def test_apply_setting_writes_a_key_the_users_list_does_not_lock(tmp_path):
    path = tmp_path / "config.toml"
    result = await config_store.apply_setting(
        path,
        "email",
        "to",
        "someone@example.com",
        _noop_apply,
        table=core_table(),
        locked=[],
    )
    assert result.value == "someone@example.com"
    assert _read(path)["email"]["to"] == "someone@example.com"


async def test_apply_setting_persists_a_cold_setting_without_touching_the_session(tmp_path):
    applied = []
    path = tmp_path / "config.toml"

    result = await config_store.apply_setting(
        path,
        "web",
        "port",
        "9100",
        lambda *a: applied.append(a),
        table=core_table(),
        locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
    )

    assert result.hot is False and result.value == 9100
    assert _read(path)["web"]["port"] == 9100
    assert applied == []


async def test_apply_setting_resolves_a_cold_toolset_key_from_the_extra_schema(tmp_path):
    """A cold declaration is in no table, so ``extra_schema`` is the only thing that makes it resolvable
    here. Without it the assistant's own ``update_config`` refuses a key sitting in the user's file, which
    is what this pins: the same call is an unknown key when the cold half is not passed."""
    cold = {("widgets", "endpoint"): ("widgets.endpoint", (str,), "a string", None)}
    path = tmp_path / "config.toml"

    result = await config_store.apply_setting(
        path,
        "widgets",
        "endpoint",
        "https://set/by-tool",
        _noop_apply,
        table=core_table(),
        locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
        extra_schema=cold,
    )

    assert result.hot is False and result.value == "https://set/by-tool"
    assert _read(path)["widgets"]["endpoint"] == "https://set/by-tool"

    with pytest.raises(settings.ConfigError, match=r"unknown config key \[widgets\].endpoint"):
        await config_store.apply_setting(
            path,
            "widgets",
            "endpoint",
            "https://x",
            _noop_apply,
            table=core_table(),
            locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
        )


async def test_apply_setting_type_checks_a_cold_toolset_key(tmp_path):
    cold = {("widgets", "rounds"): ("widgets.rounds", (int,), "an integer", None)}
    path = tmp_path / "config.toml"

    with pytest.raises(settings.ConfigError, match=r"\[widgets\].rounds must be an integer"):
        await config_store.apply_setting(
            path,
            "widgets",
            "rounds",
            "many",
            _noop_apply,
            table=core_table(),
            locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
            extra_schema=cold,
        )

    assert not path.exists()


async def test_apply_setting_applies_a_hot_setting_before_persisting_it(tmp_path):
    applied = []

    async def apply_hot(section, key, value):
        applied.append((section, key, value))

    path = tmp_path / "config.toml"
    result = await config_store.apply_setting(
        path,
        "display",
        "show_tools",
        "false",
        apply_hot,
        table=core_table(),
        locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
    )

    assert result.hot is True and applied == [("display", "show_tools", False)]
    assert _read(path)["display"]["show_tools"] is False


async def test_apply_setting_does_not_persist_a_hot_setting_that_failed_to_apply(tmp_path):
    """The ordering is the point: a value that breaks the live session must not be left to break the
    next startup too."""

    async def apply_hot(section, key, value):
        raise RuntimeError("bad flag")

    path = tmp_path / "config.toml"
    with pytest.raises(config_store.HotApplyFailed) as failure:
        await config_store.apply_setting(
            path,
            "display",
            "show_tools",
            "false",
            apply_hot,
            table=core_table(),
            locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
        )

    assert "bad flag" in str(failure.value)
    assert not path.exists()


async def test_apply_setting_refuses_a_locked_key_and_rejects_a_bad_value(tmp_path):
    path = tmp_path / "config.toml"

    with pytest.raises(config_store.SettingLocked):
        await config_store.apply_setting(
            path,
            "email",
            "to",
            "attacker@x.com",
            _noop_apply,
            table=core_table(),
            locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
        )
    with pytest.raises(settings.ConfigError):
        await config_store.apply_setting(
            path,
            "web",
            "port",
            "not-a-number",
            _noop_apply,
            table=core_table(),
            locked=config_store.DEFAULT_LOCKED_CONFIG_KEYS,
        )

    assert not path.exists()


# --- Scheduled task reads and writes -----------------------------------------------------------------

_TASK_BODY = """# my assistant
[assistant]
model = "test-model"

[scheduling]
max_task_conversations = 5

[scheduling.task.morning-brief]
# the important one
prompt = "Summarize my calendar"
schedule = { type = "daily", at = "09:00" }
"""


def _task_config(tmp_path, body: str = _TASK_BODY):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_tasks_injects_the_name_from_the_key(tmp_path):
    records = config_store.load_tasks(_task_config(tmp_path))
    assert [r["name"] for r in records] == ["morning-brief"]
    assert records[0]["schedule"] == {"type": "daily", "at": "09:00"}


def test_load_tasks_is_empty_for_a_file_with_no_tasks(tmp_path):
    assert config_store.load_tasks(_task_config(tmp_path, "[assistant]\nmodel = 'm'\n")) == []


def test_load_tasks_raises_on_unparseable_toml(tmp_path):
    with pytest.raises(settings.ConfigError, match="not valid TOML"):
        config_store.load_tasks(_task_config(tmp_path, "[scheduling.task.x\nprompt = 'p'\n"))


def test_load_tasks_raises_on_an_invalid_table(tmp_path):
    body = "[scheduling.task.x]\nprompt = 'p'\nschedule = { type = 'nope' }\n"
    with pytest.raises(settings.ConfigError, match="schedule.type must be one of"):
        config_store.load_tasks(_task_config(tmp_path, body))


def test_write_task_adds_a_table_and_keeps_the_rest_of_the_file(tmp_path):
    path = _task_config(tmp_path)
    config_store.write_task(
        path,
        "weekly-review",
        {"name": "weekly-review", "prompt": "Review", "schedule": {"type": "weekly", "day": "fri", "at": "16:00"}},
    )
    text = path.read_text(encoding="utf-8")
    assert "# my assistant" in text
    assert "# the important one" in text
    assert "[scheduling.task.weekly-review]" in text
    assert {r["name"] for r in config_store.load_tasks(path)} == {"morning-brief", "weekly-review"}


def test_write_task_patches_in_place_and_keeps_the_tables_comments(tmp_path):
    path = _task_config(tmp_path)
    record = config_store.load_tasks(path)[0]
    record["enabled"] = False
    config_store.write_task(path, "morning-brief", record)
    text = path.read_text(encoding="utf-8")
    assert "# the important one" in text
    assert "enabled = false" in text


def test_write_task_leaves_an_unchanged_inline_table_and_its_comment_alone(tmp_path):
    """A write that only changes `enabled` must not re-render `schedule`, an unrelated key whose value
    did not change: re-rendering would turn its inline-table form into a nested `[...schedule]` table
    and detach the comment written directly above it. This is the defect a plain-scalar comment
    (`prompt`, in the other patch-in-place test) cannot pin, since a scalar's rendering does not
    change on reassignment the way a table-valued key's does."""
    path = _task_config(
        tmp_path,
        "[scheduling.task.morning-brief]\n"
        'prompt = "Summarize my calendar"\n'
        "# only in the morning\n"
        'schedule = { type = "daily", at = "09:00" }\n',
    )
    record = config_store.load_tasks(path)[0]
    record["enabled"] = False
    config_store.write_task(path, "morning-brief", record)
    text = path.read_text(encoding="utf-8")
    assert 'schedule = { type = "daily", at = "09:00" }' in text
    assert "[scheduling.task.morning-brief.schedule]" not in text
    lines = text.splitlines()
    schedule_line = next(i for i, line in enumerate(lines) if line.startswith("schedule ="))
    assert lines[schedule_line - 1] == "# only in the morning"


def test_write_task_omits_enabled_when_true_and_drops_none(tmp_path):
    path = _task_config(tmp_path, "[assistant]\nmodel = 'm'\n")
    config_store.write_task(
        path,
        "x",
        {
            "name": "x",
            "prompt": "p",
            "schedule": {"type": "interval", "seconds": 60},
            "enabled": True,
            "max_conversations": None,
        },
    )
    text = path.read_text(encoding="utf-8")
    assert "enabled" not in text
    assert "max_conversations" not in text


def test_write_task_removes_a_key_the_record_no_longer_has(tmp_path):
    path = _task_config(tmp_path)
    config_store.write_task(
        path,
        "morning-brief",
        {"name": "morning-brief", "prompt": "p", "schedule": {"type": "interval", "seconds": 60}, "enabled": False},
    )
    config_store.write_task(
        path,
        "morning-brief",
        {"name": "morning-brief", "prompt": "p", "schedule": {"type": "interval", "seconds": 60}},
    )
    assert "enabled" not in path.read_text(encoding="utf-8")


def test_remove_task_reports_whether_it_removed_one(tmp_path):
    path = _task_config(tmp_path)
    assert config_store.remove_task(path, "morning-brief") is True
    assert config_store.remove_task(path, "morning-brief") is False
    assert config_store.load_tasks(path) == []


def test_rename_task_moves_the_table_with_its_contents(tmp_path):
    path = _task_config(tmp_path)
    config_store.rename_task(path, "morning-brief", "daily-brief")
    records = config_store.load_tasks(path)
    assert [r["name"] for r in records] == ["daily-brief"]
    assert records[0]["prompt"] == "Summarize my calendar"


def test_rename_task_is_a_no_op_for_an_absent_task(tmp_path):
    path = _task_config(tmp_path)
    config_store.rename_task(path, "nope", "other")
    assert [r["name"] for r in config_store.load_tasks(path)] == ["morning-brief"]


# A comment above a task's header is the user's annotation of *that* task. tomlkit does not store it
# next to the header, so these assert on the rendered text: the whole defect they guard is placement.
_TWO_COMMENTED_TASKS = """[scheduling]

# fires the daily digest, do not disable
[scheduling.task.digest]
prompt = "digest"
schedule = { type = "daily", at = "09:00" }

# weekly cleanup
[scheduling.task.cleanup]
prompt = "cleanup"
schedule = { type = "weekly", day = "sun", at = "03:00" }
"""


def _line_above_header(text: str, name: str) -> str:
    lines = text.splitlines()
    return lines[lines.index(f"[scheduling.task.{name}]") - 1]


def test_rename_task_keeps_each_comment_with_its_own_task(tmp_path):
    path = _task_config(tmp_path, _TWO_COMMENTED_TASKS)
    config_store.rename_task(path, "digest", "morning-digest")
    text = path.read_text(encoding="utf-8")
    assert _line_above_header(text, "morning-digest") == "# fires the daily digest, do not disable"
    assert _line_above_header(text, "cleanup") == "# weekly cleanup"
    assert [r["name"] for r in config_store.load_tasks(path)] == ["morning-digest", "cleanup"]


def test_rename_task_leaves_the_table_where_it_was(tmp_path):
    path = _task_config(tmp_path, _TWO_COMMENTED_TASKS)
    config_store.rename_task(path, "digest", "morning-digest")
    text = path.read_text(encoding="utf-8")
    assert text.index("[scheduling.task.morning-digest]") < text.index("[scheduling.task.cleanup]")
    assert text == _TWO_COMMENTED_TASKS.replace("[scheduling.task.digest]", "[scheduling.task.morning-digest]")


def test_remove_task_takes_its_own_comment_and_leaves_the_neighbours(tmp_path):
    path = _task_config(tmp_path, _TWO_COMMENTED_TASKS)
    assert config_store.remove_task(path, "digest") is True
    text = path.read_text(encoding="utf-8")
    assert "do not disable" not in text
    assert _line_above_header(text, "cleanup") == "# weekly cleanup"


def test_remove_task_takes_the_comment_of_the_last_task_too(tmp_path):
    path = _task_config(tmp_path, _TWO_COMMENTED_TASKS)
    assert config_store.remove_task(path, "cleanup") is True
    text = path.read_text(encoding="utf-8")
    assert "weekly cleanup" not in text
    assert _line_above_header(text, "digest") == "# fires the daily digest, do not disable"


def test_a_comment_separated_by_a_blank_line_stays_with_the_task_above_it(tmp_path):
    """The ambiguous case, resolved by adjacency: a blank line ends a task's own comment run, so a note
    left below a task's keys reads as that task's and goes when it goes."""
    body = (
        "[scheduling.task.digest]\n"
        'prompt = "digest"\n'
        "# a parting note about digest\n"
        "\n"
        "# weekly cleanup\n"
        "[scheduling.task.cleanup]\n"
        'prompt = "cleanup"\n'
    )
    path = _task_config(tmp_path, body)
    assert config_store.remove_task(path, "digest") is True
    text = path.read_text(encoding="utf-8")
    assert "a parting note about digest" not in text
    assert _line_above_header(text, "cleanup") == "# weekly cleanup"


@pytest.mark.parametrize("name", ["a.b", "morning brief", 'say "hi"', "2nd-run", "tab\tname"])
def test_a_task_name_needing_a_quoted_key_survives_the_round_trip(tmp_path, name):
    """Names come from a user or the model, and TOML needs a quoted key for a dot, a space, a quote, or
    a leading digit. tomlkit has to quote it on write and tomllib has to read it back as one key."""
    path = _task_config(tmp_path, "[assistant]\nmodel = 'm'\n")
    config_store.write_task(path, name, {"name": name, "prompt": "p", "schedule": {"type": "interval", "seconds": 60}})
    assert [r["name"] for r in config_store.load_tasks(path)] == [name]
    config_store.rename_task(path, name, "plain")
    assert [r["name"] for r in config_store.load_tasks(path)] == ["plain"]
    config_store.rename_task(path, "plain", name)
    assert [r["name"] for r in config_store.load_tasks(path)] == [name]
    assert config_store.remove_task(path, name) is True
    assert config_store.load_tasks(path) == []


def test_task_tables_are_locked_against_programmatic_writes():
    assert config_store.is_locked("scheduling.task.morning-brief", "prompt", DEFAULTS) is True
    assert config_store.is_locked("scheduling.task", "anything", DEFAULTS) is True
    # The toolset's own setting in the parent section stays writable.
    assert config_store.is_locked("scheduling", "max_task_conversations", DEFAULTS) is False


# --- Hand-edited task shapes -------------------------------------------------------------------------
#
# `write_task` only ever produces one `[scheduling.task.<name>]` header per task, but TOML lets a hand
# edit write the same data as an inline table or split one task across several fragments, and
# `load_tasks` reads both. A remove or rename that misses those shapes tells the user a task is gone
# while leaving it (or half of it) on disk.

_INLINE_TASK_BODIES = {
    "inline-task-table": (
        "[scheduling]\n"
        'task = { alpha = { prompt = "p", schedule = { type = "interval", seconds = 60 } },'
        ' beta = { prompt = "q", schedule = { type = "interval", seconds = 30 } } }\n'
    ),
    "dotted-inline-task": (
        "[scheduling]\n"
        'task.alpha = { prompt = "p", schedule = { type = "interval", seconds = 60 } }\n'
        'task.beta = { prompt = "q", schedule = { type = "interval", seconds = 30 } }\n'
    ),
}

_SPLIT_TASK_BODIES = {
    "fragments-around-another-section": (
        "[scheduling.task.alpha]\n"
        'prompt = "p"\n'
        "\n"
        "[email]\n"
        'to = "someone@example.com"\n'
        "\n"
        "[scheduling.task.alpha.schedule]\n"
        'type = "interval"\n'
        "seconds = 60\n"
        "\n"
        "[scheduling.task.beta]\n"
        'prompt = "q"\n'
        'schedule = { type = "interval", seconds = 30 }\n'
    ),
    "dotted-fragments": (
        "[scheduling]\n"
        'task.alpha.prompt = "p"\n'
        'task.beta.prompt = "q"\n'
        'task.alpha.schedule = { type = "interval", seconds = 60 }\n'
        'task.beta.schedule = { type = "interval", seconds = 30 }\n'
    ),
    "dotted-fragments-in-one-section": (
        "[scheduling.task]\n"
        'alpha.prompt = "p"\n'
        'beta.prompt = "q"\n'
        'alpha.schedule = { type = "interval", seconds = 60 }\n'
        'beta.schedule = { type = "interval", seconds = 30 }\n'
    ),
}


@pytest.mark.parametrize("body", _INLINE_TASK_BODIES.values(), ids=_INLINE_TASK_BODIES)
def test_remove_task_removes_a_task_written_as_an_inline_table(tmp_path, body):
    path = _task_config(tmp_path, body)
    assert config_store.remove_task(path, "alpha") is True
    text = path.read_text(encoding="utf-8")
    assert "alpha" not in text
    assert [r["name"] for r in config_store.load_tasks(path)] == ["beta"]
    assert config_store.remove_task(path, "alpha") is False


@pytest.mark.parametrize("body", _INLINE_TASK_BODIES.values(), ids=_INLINE_TASK_BODIES)
def test_rename_task_renames_a_task_written_as_an_inline_table(tmp_path, body):
    path = _task_config(tmp_path, body)
    config_store.rename_task(path, "alpha", "renamed")
    text = path.read_text(encoding="utf-8")
    assert "alpha" not in text
    records = {r["name"]: r for r in config_store.load_tasks(path)}
    assert set(records) == {"renamed", "beta"}
    assert records["renamed"]["prompt"] == "p"
    assert records["renamed"]["schedule"] == {"type": "interval", "seconds": 60}
    assert records["beta"]["schedule"] == {"type": "interval", "seconds": 30}


@pytest.mark.parametrize("body", _SPLIT_TASK_BODIES.values(), ids=_SPLIT_TASK_BODIES)
def test_remove_task_removes_every_fragment_of_a_split_task(tmp_path, body):
    path = _task_config(tmp_path, body)
    assert config_store.remove_task(path, "alpha") is True
    text = path.read_text(encoding="utf-8")
    assert "alpha" not in text
    records = config_store.load_tasks(path)
    assert [r["name"] for r in records] == ["beta"]
    assert records[0]["schedule"] == {"type": "interval", "seconds": 30}


@pytest.mark.parametrize("body", _SPLIT_TASK_BODIES.values(), ids=_SPLIT_TASK_BODIES)
def test_rename_task_renames_every_fragment_of_a_split_task(tmp_path, body):
    path = _task_config(tmp_path, body)
    config_store.rename_task(path, "alpha", "renamed")
    text = path.read_text(encoding="utf-8")
    assert "alpha" not in text
    records = {r["name"]: r for r in config_store.load_tasks(path)}
    assert set(records) == {"renamed", "beta"}
    assert records["renamed"]["prompt"] == "p"
    assert records["renamed"]["schedule"] == {"type": "interval", "seconds": 60}
    assert records["beta"] == {"name": "beta", "prompt": "q", "schedule": {"type": "interval", "seconds": 30}}


def test_removing_a_split_task_leaves_a_neighbouring_section_intact(tmp_path):
    path = _task_config(tmp_path, _SPLIT_TASK_BODIES["fragments-around-another-section"])
    assert config_store.remove_task(path, "alpha") is True
    assert _read(path)["email"] == {"to": "someone@example.com"}


def test_renaming_over_a_split_task_removes_all_of_the_task_it_replaces(tmp_path):
    path = _task_config(tmp_path, _SPLIT_TASK_BODIES["fragments-around-another-section"])
    config_store.rename_task(path, "beta", "alpha")
    records = config_store.load_tasks(path)
    assert [r["name"] for r in records] == ["alpha"]
    assert records[0]["schedule"] == {"type": "interval", "seconds": 30}
