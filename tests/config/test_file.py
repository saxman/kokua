"""Tests for the TOML config file, precedence (CLI > file > default), and the data/ migration."""

from __future__ import annotations

import logging

import pytest

from kokua.config import paths
from kokua.config import file as settings
from kokua.config import schema
from kokua.config.schema import AssistantConfig
from kokua.config.settings_sources import build_settings_table, startup_schema
from kokua.cli import _init_config, build_arg_parser, resolve_config
from tests.helpers import core_table


def _write_config(text: str):
    """Write config.toml at the default location ($KOKUA_HOME/config.toml) and return its path."""
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _resolve(*argv):
    return resolve_config(build_arg_parser().parse_args(list(argv)))


def test_missing_default_file_is_an_error_naming_the_fix():
    """config.toml is required: the [agents.*] tables live only there and the assistant needs at least
    one, so starting without a file is not a state Kokua can be in."""
    paths.config_path().unlink()
    with pytest.raises(settings.ConfigError, match="kokua config init"):
        settings.load(table=core_table())


def test_file_overrides_built_in_defaults():
    _write_config(
        """
        [assistant]
        model = "anthropic:claude-sonnet-4-6"
        concurrent_tools = false
        [display]
        show_thinking = false
        [web]
        port = 9100
        """
    )
    cfg = _resolve()
    assert cfg.model == "anthropic:claude-sonnet-4-6"
    assert cfg.show_thinking is False
    assert cfg.concurrent_tools is False
    assert cfg.port == 9100


def test_logging_level_parses_from_file():
    _write_config(
        """
        [logging]
        level = "DEBUG"
        """
    )
    assert _resolve().log_level == "DEBUG"


def test_cli_overrides_file():
    _write_config(
        """
        [assistant]
        model = "from-file"
        [web]
        port = 9100
        """
    )
    cfg = _resolve("--model", "from-cli", "--port", "8500")
    assert cfg.model == "from-cli"
    assert cfg.port == 8500


def test_explicit_config_flag(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text('[frontend]\nname = "web"\n', encoding="utf-8")
    cfg = _resolve("--config", str(path))
    assert cfg.frontend == "web"


def test_explicit_missing_file_errors(tmp_path):
    with pytest.raises(settings.ConfigError, match="not found"):
        settings.load(str(tmp_path / "nope.toml"), table=core_table())


def test_unknown_key_raises():
    _write_config('[assistant]\nbogus = 1\nmodel = "m"\n')
    with pytest.raises(settings.ConfigError, match=r"unknown config key \[assistant\].bogus"):
        settings.load(table=core_table())


def test_the_retired_load_plugins_key_is_refused():
    """`load_plugins` gated the `kokua.toolsets` entry-point group without preventing a single import:
    `resolve_config` loads every entry point regardless, to learn which config sections are legal. Its
    only reachable effect was to turn a working `tools` declaration into an unknown-toolset error, so it
    is gone. Refused rather than ignored, so a config carrying it is told, instead of quietly keeping a
    setting it believes it set."""
    _write_config("[assistant]\nload_plugins = false\n")
    with pytest.raises(settings.ConfigError, match=r"unknown config key \[assistant\].load_plugins"):
        settings.load(table=core_table())


def test_type_mismatch_raises():
    _write_config('[web]\nport = "not-an-int"\n')
    with pytest.raises(settings.ConfigError, match=r"\[web\].port must be an integer"):
        settings.load(table=core_table())


def test_bool_rejected_for_numeric_field():
    _write_config("[web]\nport = true\n")
    with pytest.raises(settings.ConfigError, match=r"\[web\].port must be an integer"):
        settings.load(table=core_table())


def test_core_sections_includes_the_removed_key_sections():
    """A toolset named after a removed section (``tools``, the old subagent config) would otherwise pass
    the reserved-name check in ``config.settings_sources`` only to hit ``load``'s removed-key branch
    first, which runs before the schema and answers "[tools] is gone." for every key in the section --
    permanently unparseable rather than refused with the settings-collision message that names the fix."""
    assert "tools" in settings.core_sections()
    assert "subagents" in settings.core_sections()


def test_security_confirm_tools_from_file():
    _write_config('[security]\nconfirm_tools = ["add_skill_script"]\n')
    assert _resolve().confirm_tools == ["add_skill_script"]


@pytest.mark.parametrize(
    "pattern,fault",
    [
        ("agents.* ", "whitespace"),
        (" agents.*", "whitespace"),
        ("agents .*", "whitespace"),
        ("display", "no dot"),
        ("*.*", "last segment"),
        ("agents.*.*", "last segment"),
        ("agents.", "empty segment"),
        (".display", "empty segment"),
        ("a..b", "empty segment"),
        ("agent*.tools", "whole segment"),
    ],
)
def test_every_unmatchable_lock_pattern_fails_startup(tmp_path, pattern, fault):
    """Each of these is a pattern `store.locked_by` can never match, so accepting one would leave the
    user reading a lock they do not have: the file still says `agents.*` and the policy preamble still
    prints it. Whitespace is rejected rather than stripped, since the list is hand-authored and a silent
    repair hides the slip."""
    path = tmp_path / "config.toml"
    path.write_text(f'[security]\nlocked_config_keys = ["{pattern}"]\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError) as error:
        settings.load(str(path), table=core_table())
    assert repr(pattern) in str(error.value)
    assert fault in str(error.value)


@pytest.mark.parametrize(
    "pattern",
    [
        "*",
        "agents.*",
        "security.*",
        "display.*",
        "email.to",
        "paths.data_dir",
        "scheduling.task.*",
        "agents.researcher.generation.temperature",
    ],
)
def test_every_accepted_lock_pattern_form_still_loads(tmp_path, pattern):
    """The three documented forms, including the dotted sections a per-agent or per-task pattern needs:
    tightening either validator must not cost the forms they exist to protect. The deep dotted one is the
    case the vocabulary check has to leave alone, since `agents.researcher.generation` is a section only
    an agent named `researcher` brings into being."""
    path = tmp_path / "config.toml"
    path.write_text(f'[security]\nlocked_config_keys = ["{pattern}"]\n', encoding="utf-8")
    assert settings.load(str(path), table=core_table())["locked_config_keys"] == [pattern]


@pytest.mark.parametrize(
    "pattern,fault",
    [
        ("Agents.*", "no config section is named 'Agents'"),
        ("agnets.*", "no config section is named 'agnets'"),
        ("emial.to", "no config section is named 'emial'"),
        ("security.confirm_tool", "[security] has no key 'confirm_tool'"),
    ],
)
def test_a_lock_pattern_naming_no_real_section_or_key_fails_startup(tmp_path, pattern, fault):
    """Each of these is well-shaped and still matches nothing, which is the silent unlock the structural
    check cannot see: a mistyped section header ([secrity]) and a mistyped key already hard-error, so a
    mistyped *pattern* naming the same thing has to as well."""
    path = tmp_path / "config.toml"
    path.write_text(f'[security]\nlocked_config_keys = ["{pattern}"]\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError) as error:
        settings.load(str(path), table=core_table())
    assert repr(pattern) in str(error.value)
    assert fault in str(error.value)


@pytest.mark.parametrize("pattern", schema.DEFAULT_LOCKED_CONFIG_KEYS)
def test_every_shipped_lock_default_validates_under_a_narrow_and_a_full_table(tmp_path, pattern):
    """The vocabulary the check reads comes from the schema, and the schema depends on which toolsets are
    installed, so rejecting a default under some table would be a hard startup failure on a config that
    used to work. `scheduling.task.*` is the pattern that makes the point: `core_table()` omits the
    scheduling toolset's own section, so only `_TASK_SECTION` keeps that default loadable there."""
    path = tmp_path / "config.toml"
    path.write_text(f'[security]\nlocked_config_keys = ["{pattern}"]\n', encoding="utf-8")
    assert settings.load(str(path), table=core_table())["locked_config_keys"] == [pattern]
    full = settings.load(str(path), table=build_settings_table(), extra_schema=startup_schema())
    assert full["locked_config_keys"] == [pattern]


def test_the_shipped_example_lock_list_loads_under_a_narrow_and_a_full_table(tmp_path):
    """The example is what `kokua config init` writes, so a check it cannot satisfy would refuse the file
    Kokua itself hands the user."""
    path = tmp_path / "config.toml"
    path.write_text(settings.example_text(), encoding="utf-8")
    for table, extra in ((core_table(), None), (build_settings_table(), startup_schema())):
        overrides = settings.load(str(path), table=table, extra_schema=extra)
        assert overrides["locked_config_keys"] == list(schema.DEFAULT_LOCKED_CONFIG_KEYS)


def test_lock_patterns_load_as_a_field_override(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[security]\nlocked_config_keys = ["email.to", "agents.*"]\n', encoding="utf-8")
    overrides = settings.load(str(path), table=core_table())
    assert overrides["locked_config_keys"] == ["email.to", "agents.*"]


def test_an_unset_lock_list_leaves_the_shipped_default_in_force(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[assistant]\n", encoding="utf-8")
    assert "locked_config_keys" not in settings.load(str(path), table=core_table())
    assert AssistantConfig().locked_config_keys == list(schema.DEFAULT_LOCKED_CONFIG_KEYS)


def test_planning_flags_from_file():
    """[planning] is the planning toolset's own section, so its keys land in that toolset's bucket and
    the unset ones are seeded from the declaration rather than from an ``AssistantConfig`` default."""
    _write_config("[planning]\nplan_review = true\nresult_review = true\n")
    section = _resolve().toolset_settings["planning"]
    assert section["plan_review"] is True and section["result_review"] is True
    assert section["review_rounds"] == 2  # seeded from the declared default


def test_a_stale_generation_section_is_just_an_unknown_key():
    """[generation] is no longer a section the core parses, so it gets no special handling."""
    _write_config("[generation]\ntemperature = 0.3\n")
    with pytest.raises(settings.ConfigError, match=r"unknown config key \[generation\].temperature"):
        settings.load(table=core_table())


def test_generation_is_no_longer_a_reserved_section_name():
    """Nothing in the core owns the name now, so a toolset is free to claim it."""
    assert "generation" not in settings.core_sections()


def test_data_dir_override_redirects_leaf_paths(tmp_path):
    target = tmp_path / "elsewhere"
    _write_config(f'[paths]\ndata_dir = "{target}"\n')
    cfg = _resolve()
    assert cfg.data_dir == target
    assert cfg.skills_dir == target / "skills"
    assert cfg.sessions_path == target / "sessions.json"


def _init(*argv):
    return _init_config(build_arg_parser().parse_args(["config", "init", *argv]))


def test_config_init_writes_to_default_location():
    paths.config_path().unlink()
    assert _init() == 0
    assert paths.config_path().read_text(encoding="utf-8") == settings.example_text()


def test_config_init_refuses_to_overwrite_without_force():
    _write_config("# pre-existing\n")
    assert _init() == 1
    assert paths.config_path().read_text(encoding="utf-8") == "# pre-existing\n"


def test_config_init_force_overwrites():
    _write_config("# pre-existing\n")
    assert _init("--force") == 0
    assert paths.config_path().read_text(encoding="utf-8") == settings.example_text()


def test_config_init_custom_path(tmp_path):
    target = tmp_path / "custom" / "config.toml"
    assert _init("--path", str(target)) == 0
    assert target.read_text(encoding="utf-8") == settings.example_text()


def test_agent_tools_must_be_a_string_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[agents.bad]\ntools = [1, 2]\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="tools"):
        settings.load(str(path), table=core_table())


def test_agent_delegates_to_must_be_a_string_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[agents.bad]\ndelegates_to = [1]\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="delegates_to"):
        settings.load(str(path), table=core_table())


def test_subagents_unknown_top_level_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[subagents]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="bogus"):
        settings.load(str(path), table=core_table())


def test_mcp_server_tables_parse(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[[mcp.server]]\n"
        'url = "https://api.githubcopilot.com/mcp/"\n'
        'name = "github"\n'
        'token_env = "GITHUB_MCP_TOKEN"\n\n'
        "[[mcp.server]]\n"
        'url = "https://plain/mcp"\n'
        'name = "plain"\n',
        encoding="utf-8",
    )
    servers = settings.load(str(path), table=core_table())["mcp_servers"]
    assert [(s.url, s.token_env) for s in servers] == [
        ("https://api.githubcopilot.com/mcp/", "GITHUB_MCP_TOKEN"),
        ("https://plain/mcp", None),
    ]


def test_mcp_server_name_parses(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[mcp.server]]\nurl = "https://broker/mcp"\nname = "stocks"\n',
        encoding="utf-8",
    )
    servers = settings.load(str(path), table=core_table())["mcp_servers"]
    assert (servers[0].url, servers[0].name) == ("https://broker/mcp", "stocks")


def test_mcp_server_missing_url_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\ntoken_env = "X"\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="url"):
        settings.load(str(path), table=core_table())


def test_mcp_server_unknown_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[mcp.server]]\nurl = "https://x/mcp"\nname = "x"\nbearer = "nope"\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="bearer"):
        settings.load(str(path), table=core_table())


def test_mcp_oauth_callback_keys_parse(tmp_path):
    """A Kokua the browser reaches over the network needs the OAuth redirect pointed at it."""
    path = tmp_path / "config.toml"
    path.write_text('[mcp]\noauth_callback_host = "kokua.lan"\noauth_callback_port = 8765\n', encoding="utf-8")
    overrides = settings.load(str(path), table=core_table())
    assert overrides["mcp_oauth_callback_host"] == "kokua.lan"
    assert overrides["mcp_oauth_callback_port"] == 8765


def test_mcp_oauth_callback_port_rejects_a_non_integer(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[mcp]\noauth_callback_port = "8765"\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError, match="oauth_callback_port"):
        settings.load(str(path), table=core_table())


def test_mcp_unknown_top_level_key_raises(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[mcp]\nservers = ["https://x/mcp"]\n', encoding="utf-8")
    with pytest.raises(settings.ConfigError, match=r"\[mcp\].servers"):
        settings.load(str(path), table=core_table())


def test_agent_cache_cap_parsed(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[assistant]\nagent_cache_cap = 3\n")
    from kokua.config import file as settings

    overrides = settings.load(table=core_table())
    assert overrides["agent_cache_cap"] == 3


def test_coerce_config_string_scalars():
    assert settings.coerce_config_string("assistant", "model", "anthropic:x", table=core_table()) == "anthropic:x"
    assert settings.coerce_config_string("display", "show_thinking", "false", table=core_table()) is False
    assert settings.coerce_config_string("display", "show_tools", "true", table=core_table()) is True
    assert settings.coerce_config_string("web", "port", "9100", table=core_table()) == 9100


def test_coerce_config_string_list():
    assert settings.coerce_config_string(
        "security", "confirm_tools", "execute_python, update_config", table=core_table()
    ) == [
        "execute_python",
        "update_config",
    ]


def test_coerce_config_string_refuses_a_generation_key():
    with pytest.raises(settings.ConfigError, match=r"unknown config key \[generation\].temperature"):
        settings.coerce_config_string("generation", "temperature", "0.3", table=core_table())


def test_coerce_config_string_bad_int_raises():
    with pytest.raises(settings.ConfigError, match=r"\[web\].port"):
        settings.coerce_config_string("web", "port", "not-an-int", table=core_table())


def test_coerce_config_string_unknown_key_raises():
    with pytest.raises(settings.ConfigError, match="unknown config key"):
        settings.coerce_config_string("assistant", "bogus", "x", table=core_table())


def test_coerce_config_string_rejects_structured_sections():
    with pytest.raises(settings.ConfigError, match="mcp"):
        settings.coerce_config_string("mcp", "server", "x", table=core_table())


def test_shipped_example_loads_cleanly(caplog):
    """The example's active keys must parse without unknown-key or type warnings/errors."""
    _init()
    with caplog.at_level(logging.WARNING):
        overrides = settings.load(table=core_table())
    assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)
    assert overrides  # the example leaves several keys active at their default
    cfg = _resolve()
    assert cfg.show_thinking is True


def test_explicit_missing_file_is_also_an_error(tmp_path):
    """--config PATH pointing at nothing was already an error; the default location now behaves the
    same way, so the two paths do not disagree about whether a config is optional."""
    with pytest.raises(settings.ConfigError, match="config file not found"):
        settings.load(str(tmp_path / "nope.toml"), table=core_table())


def test_a_config_defining_no_agents_still_parses(tmp_path):
    """The file layer stays dumb about it: zero agents is a valid TOML config, and refusing to run on
    one is Assistant.create's call (see tests/core/test_build.py)."""
    path = tmp_path / "config.toml"
    path.write_text("[assistant]\nconcurrent_tools = true\n", encoding="utf-8")
    assert settings.load(str(path), table=core_table()).get("agents") is None


def test_a_contributed_section_parses_into_the_toolset_bucket(tmp_path):
    from kokua.config import file as settings
    from kokua.config.table import CORE_RUNTIME_SETTINGS, RuntimeSetting, SettingsTable

    path = tmp_path / "config.toml"
    path.write_text(
        "[agents.assistant]\ntools = []\n\n[widgets]\nverbose = true\nrounds = 4\n",
        encoding="utf-8",
    )
    table = SettingsTable(
        [
            *CORE_RUNTIME_SETTINGS,
            RuntimeSetting("verbose", "widgets", bool, toolset="widgets"),
            RuntimeSetting("rounds", "widgets", int, toolset="widgets"),
        ]
    )

    overrides = settings.load(str(path), table=table)

    assert overrides["toolset_settings"] == {"widgets": {"verbose": True, "rounds": 4}}


def test_an_unknown_key_in_a_contributed_section_is_rejected(tmp_path):
    from kokua.config import file as settings
    from kokua.config.table import CORE_RUNTIME_SETTINGS, SettingsTable

    path = tmp_path / "config.toml"
    path.write_text("[agents.assistant]\ntools = []\n\n[widgets]\nnonsense = 1\n", encoding="utf-8")

    with pytest.raises(settings.ConfigError, match=r"unknown config key \[widgets\].nonsense"):
        settings.load(str(path), table=SettingsTable(CORE_RUNTIME_SETTINGS))


def test_a_wrong_typed_contributed_value_is_rejected(tmp_path):
    from kokua.config import file as settings
    from kokua.config.table import CORE_RUNTIME_SETTINGS, RuntimeSetting, SettingsTable

    path = tmp_path / "config.toml"
    path.write_text('[agents.assistant]\ntools = []\n\n[widgets]\nrounds = "two"\n', encoding="utf-8")
    table = SettingsTable([*CORE_RUNTIME_SETTINGS, RuntimeSetting("rounds", "widgets", int, toolset="widgets")])

    with pytest.raises(settings.ConfigError, match="must be an integer"):
        settings.load(str(path), table=table)


def test_assistant_thinking_accepts_a_level_and_a_bool(tmp_path):
    _write_config('[assistant]\nthinking = "high"\n')
    assert settings.load(table=core_table())["thinking"] == "high"
    _write_config("[assistant]\nthinking = false\n")
    assert settings.load(table=core_table())["thinking"] is False
    _write_config("[assistant]\nthinking = true\n")
    assert settings.load(table=core_table())["thinking"] is True


def test_assistant_thinking_rejects_an_unknown_level(tmp_path):
    """A plausible typo: 'xhigh' is Qwen's own effort ceiling, and AIMU raises on it at request time."""
    _write_config('[assistant]\nthinking = "xhigh"\n')
    with pytest.raises(settings.ConfigError, match=r"\[assistant\].thinking"):
        settings.load(table=core_table())


def test_assistant_thinking_rejects_a_number(tmp_path):
    _write_config("[assistant]\nthinking = 2\n")
    with pytest.raises(settings.ConfigError, match=r"\[assistant\].thinking"):
        settings.load(table=core_table())


def test_coerce_config_string_thinking_takes_a_level_or_a_bool():
    """The union type must not be read as bool-only, which is what `bool in types` alone would do."""
    assert settings.coerce_config_string("assistant", "thinking", "high", table=core_table()) == "high"
    assert settings.coerce_config_string("assistant", "thinking", "false", table=core_table()) is False
    assert settings.coerce_config_string("assistant", "thinking", "true", table=core_table()) is True


def test_coerce_config_string_thinking_rejects_an_unknown_level():
    with pytest.raises(settings.ConfigError, match=r"\[assistant\].thinking"):
        settings.coerce_config_string("assistant", "thinking", "sort-of", table=core_table())


def _load_generation(tmp_path, body: str) -> dict:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return settings.load(str(path), table=core_table())


def test_the_generation_table_loads_every_key(tmp_path):
    overrides = _load_generation(
        tmp_path,
        """
[assistant.generation]
temperature = 0.7
top_p = 0.9
top_k = 40
min_p = 0.05
presence_penalty = 1.5
repetition_penalty = 1.05
max_tokens = 4096
context_length = 32768
""",
    )
    assert overrides["generation"] == {
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "min_p": 0.05,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.05,
        "max_tokens": 4096,
        "context_length": 32768,
    }


def test_a_config_with_no_generation_table_sets_nothing(tmp_path):
    """Absent must stay absent: this tier sits above the model card, so a default would shadow it."""
    overrides = _load_generation(tmp_path, '[assistant]\nmodel = "ollama:qwen3:8b"\n')
    assert "generation" not in overrides


def test_an_out_of_range_generation_value_is_refused(tmp_path):
    with pytest.raises(settings.ConfigError, match="temperature"):
        _load_generation(tmp_path, "[assistant.generation]\ntemperature = 5.0\n")


def test_a_zero_repetition_penalty_is_refused(tmp_path):
    """The bound is exclusive: 0.0 would suppress every repeated token outright."""
    with pytest.raises(settings.ConfigError, match="repetition_penalty"):
        _load_generation(tmp_path, "[assistant.generation]\nrepetition_penalty = 0.0\n")


def test_a_misspelled_generation_key_names_the_table_it_came_from(tmp_path):
    with pytest.raises(settings.ConfigError, match=r"\[assistant.generation\].temperture"):
        _load_generation(tmp_path, "[assistant.generation]\ntemperture = 0.7\n")


def test_a_boolean_is_not_a_number(tmp_path):
    """bool is an int subclass, so a plain isinstance check would accept `temperature = true`."""
    with pytest.raises(settings.ConfigError, match="temperature"):
        _load_generation(tmp_path, "[assistant.generation]\ntemperature = true\n")


def test_an_integer_is_accepted_where_a_float_is_expected(tmp_path):
    """TOML `temperature = 1` is an int, and refusing it would be a papercut with no upside."""
    assert _load_generation(tmp_path, "[assistant.generation]\ntemperature = 1\n")["generation"] == {"temperature": 1}


def test_a_context_length_of_zero_is_refused(tmp_path):
    with pytest.raises(settings.ConfigError, match="context_length"):
        _load_generation(tmp_path, "[assistant.generation]\ncontext_length = 0\n")


def test_an_unknown_nested_table_names_its_own_section(tmp_path):
    """The expansion is general, so any nested table gets a key error rather than a type complaint."""
    with pytest.raises(settings.ConfigError, match=r"\[assistant.sampling\]"):
        _load_generation(tmp_path, "[assistant.sampling]\ntemperature = 0.7\n")


def test_a_generation_float_coerces_from_the_tools_string():
    """update_config passes every value as a string, and these are the first float-typed keys."""
    assert settings.coerce_config_string("assistant.generation", "temperature", "0.7", table=core_table()) == 0.7


def test_a_generation_integer_coerces_from_the_tools_string():
    assert settings.coerce_config_string("assistant.generation", "context_length", "32768", table=core_table()) == 32768


def test_an_out_of_range_generation_string_is_refused():
    with pytest.raises(settings.ConfigError, match="temperature"):
        settings.coerce_config_string("assistant.generation", "temperature", "5", table=core_table())


def test_a_non_numeric_generation_string_is_refused():
    with pytest.raises(settings.ConfigError, match="temperature"):
        settings.coerce_config_string("assistant.generation", "temperature", "warm", table=core_table())


def test_an_agents_generation_table_coerces_through_the_agent_schema():
    """An agent's [<name>.generation] sub-table now resolves through AGENT_SCHEMA like any other agent
    key; whether the write is *allowed* is the lock list's decision (default: locked), not this
    function's, so a dotted agent section coerces a valid value instead of refusing outright."""
    value = settings.coerce_config_string("agents.researcher.generation", "temperature", "0.2", table=core_table())
    assert value == 0.2


def test_the_agent_write_schema_covers_exactly_the_agent_keys_load_accepts():
    """Two hand-kept lists, one for what `update_config` may write into an agent table and one for what
    `load` parses back out of it, and drift between them is silent in the worst direction: a key only
    `AGENT_SCHEMA` knows is written to the file and then rejected on the next read, which leaves every
    later agent write refused with "does not currently parse". `generation` is the one deliberate
    difference, being a sub-table `AGENT_SCHEMA` spells out one parameter at a time."""
    flat_agent_keys = {key for section, key in settings.AGENT_SCHEMA if section == "agents.*"}
    assert flat_agent_keys == set(settings._AGENT_KEYS) - {"generation"}


@pytest.mark.parametrize(
    "section,expected",
    [
        ("agents.researcher", "agents.*"),
        ("agents.researcher.generation", "agents.*.generation"),
        ("agents", "agents"),
        ("display", "display"),
        ("scheduling.task.brief", "scheduling.task.brief"),
    ],
)
def test_schema_section_wildcards_only_the_agent_name(section, expected):
    assert settings._schema_section(section) == expected


def test_coerce_reads_an_agent_tools_list():
    value = settings.coerce_config_string("agents.researcher", "tools", "time, memory", table=core_table())
    assert value == ["time", "memory"]


def test_coerce_reads_an_agent_thinking_level():
    assert settings.coerce_config_string("agents.researcher", "thinking", "high", table=core_table()) == "high"


def test_coerce_range_checks_an_agent_generation_parameter():
    with pytest.raises(settings.ConfigError) as error:
        settings.coerce_config_string("agents.researcher.generation", "temperature", "3.0", table=core_table())
    assert "temperature" in str(error.value)


def test_coerce_points_an_agent_generation_write_at_its_own_section():
    with pytest.raises(settings.ConfigError) as error:
        settings.coerce_config_string("agents.researcher", "generation", "{}", table=core_table())
    assert "agents.researcher.generation" in str(error.value)


def test_coerce_asks_a_bare_agents_write_to_name_an_agent():
    with pytest.raises(settings.ConfigError) as error:
        settings.coerce_config_string("agents", "researcher", "x", table=core_table())
    assert "name an agent" in str(error.value).lower()


def test_coerce_still_rejects_an_unknown_agent_key():
    with pytest.raises(settings.ConfigError):
        settings.coerce_config_string("agents.researcher", "nosuch", "x", table=core_table())


@pytest.mark.parametrize(
    "key, accepted, rejected",
    [
        ("temperature", 2.0, 2.0001),
        ("top_p", 1.0, 1.0001),
        ("top_k", 1, 0),
        ("min_p", 1.0, 1.0001),
        ("presence_penalty", -2.0, -2.0001),
        ("repetition_penalty", 0.0001, 0.0),
        ("max_tokens", 1, 0),
        ("context_length", 1, 0),
    ],
)
def test_generation_key_accepts_its_last_valid_value_and_rejects_the_next_one(tmp_path, key, accepted, rejected):
    """Each of the eight generation keys is checked against a range predicate; this pins the exact edge
    of that range so a `<` written for `<=`, or a wrong bound, fails a test instead of shipping.

    The pair per key is the last value the range accepts and the nearest one it does not, which reads the
    same whichever kind of bound it is: `temperature = 2.0` is accepted because its bound is inclusive,
    while `repetition_penalty = 0.0` is rejected because its bound is exclusive and `0.0001` is the
    smallest step this test can take past it.
    """
    overrides = _load_generation(tmp_path, f"[assistant.generation]\n{key} = {accepted}\n")
    assert overrides["generation"][key] == accepted

    with pytest.raises(settings.ConfigError, match=key):
        _load_generation(tmp_path, f"[assistant.generation]\n{key} = {rejected}\n")


def test_unknown_key_names_the_section_that_does_have_it():
    """The mistake that motivated this: `thinking` is an [assistant] key, and [assistant.generation]
    sits right under it in the file, so both a hand-edit and `update_config` land it in the sub-table."""
    _write_config('[assistant.generation]\nthinking = "medium"\n')
    with pytest.raises(settings.ConfigError, match=r"did you mean \[assistant\].thinking\?"):
        settings.load(table=core_table())


def test_unknown_key_lists_what_the_section_does_accept():
    _write_config('[web]\nhostname = "0.0.0.0"\n')
    with pytest.raises(settings.ConfigError, match=r"Accepted in \[web\]: host, port"):
        settings.load(table=core_table())


def test_load_raises_a_config_error_on_unparseable_toml():
    """`load` is reached from `update_config`'s dry run as well as from startup, so a syntax slip has to
    arrive in the same vocabulary every other fault in the file does."""
    _write_config("[assistant\nmodel = 'm'\n")
    with pytest.raises(settings.ConfigError, match="not valid TOML"):
        settings.load(table=core_table())


def test_unknown_key_in_an_unknown_section_stays_bare():
    """No section to enumerate and no other home for the key: the error says only what it knows."""
    with pytest.raises(settings.ConfigError, match=r"unknown config key \[widgets\].nonsense$"):
        settings.coerce_config_string("widgets", "nonsense", "1", table=core_table())


def test_unknown_key_offers_every_section_that_has_it():
    with pytest.raises(settings.ConfigError, match=r"\[email\].host or \[web\].host"):
        settings.coerce_config_string("logging", "host", "x", table=core_table())


def test_unknown_agent_key_lists_what_an_agent_table_accepts():
    """An agent's keys are schema-keyed under the folded `agents.*`, so hints looked up under the section
    the caller wrote would find nothing to enumerate for the one section whose keys are least guessable."""
    with pytest.raises(settings.ConfigError, match=r"Accepted in \[agents.researcher\]: delegates_to, description"):
        settings.coerce_config_string("agents.researcher", "nosuch", "x", table=core_table())


def test_agent_hint_names_a_placeholder_rather_than_the_wildcard():
    """A model acts on a hint verbatim, and `section="agents.*"` writes an agent literally named `*`."""
    with pytest.raises(settings.ConfigError, match=r"did you mean \[agents.<name>\].tools\?"):
        settings.coerce_config_string("display", "tools", "x", table=core_table())


@pytest.mark.parametrize(
    "section, key",
    [
        ("agents.*", "tools"),
        ("agents.*", "generation"),
        ("agents.*.generation", "temperature"),
        ("agents.", "tools"),
        ("agents..generation", "temperature"),
        ("agents. spaced", "tools"),
    ],
)
def test_a_section_whose_agent_name_could_not_be_written_as_given_is_refused(section, key):
    """`_schema_section` folds an agent's name to `*` for the lookup, and the folded forms fold to
    themselves, so every one of these resolved against the schema and wrote a quoted table: `[agents."*"]`,
    `[agents.""]`, `[agents." spaced"]`. Each is an agent under a name the caller did not ask for and
    cannot name again the same way, and `*` reads besides as the wildcard a lock pattern is written with.

    `generation` is in the list twice on purpose: the name is checked before the sub-table refusal, so
    `section="agents.*"` is answered about the name rather than pointed at `agents.*.generation`.
    """
    with pytest.raises(settings.ConfigError, match="does not name an agent"):
        settings.coerce_config_string(section, key, "0.7", table=core_table())


@pytest.mark.parametrize("name", ["report-writer", "stock-trader", "my_agent", "agent2"])
def test_an_agent_name_a_section_header_can_carry_is_still_accepted(name):
    """The rule is TOML's bare-key character set, not a blocklist of one character, and the shipped
    example's own `report-writer` is the case that has to keep working."""
    assert settings.coerce_config_string(f"agents.{name}", "tools", "time", table=core_table()) == ["time"]


def _write_task_config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_task_returns_a_record_with_its_name(tmp_path):
    record = settings.parse_task(
        "morning-brief",
        {"prompt": "Summarize my calendar", "schedule": {"type": "daily", "at": "09:00"}},
    )
    assert record == {
        "name": "morning-brief",
        "prompt": "Summarize my calendar",
        "schedule": {"type": "daily", "at": "09:00"},
    }


def test_parse_task_keeps_the_optional_fields_it_knows():
    record = settings.parse_task(
        "spent",
        {
            "prompt": "p",
            "schedule": {"type": "once", "at": "2026-08-20T09:00"},
            "enabled": False,
            "max_conversations": 0,
            "created_at": "2026-08-19T10:00:00",
            "fired_at": "2026-08-20T09:00:03",
        },
    )
    assert record["enabled"] is False
    assert record["max_conversations"] == 0
    assert record["fired_at"] == "2026-08-20T09:00:03"


def test_parse_task_rejects_an_unknown_key():
    with pytest.raises(settings.ConfigError, match=r"\[scheduling.task.x\].promt"):
        settings.parse_task("x", {"promt": "typo", "schedule": {"type": "interval", "seconds": 60}})


def test_parse_task_rejects_an_unknown_schedule_type():
    with pytest.raises(settings.ConfigError, match="schedule.type must be one of"):
        settings.parse_task("x", {"prompt": "p", "schedule": {"type": "hourly", "at": "09:00"}})


def test_parse_task_rejects_a_schedule_missing_its_required_key():
    with pytest.raises(settings.ConfigError, match="requires 'day' for a 'weekly' schedule"):
        settings.parse_task("x", {"prompt": "p", "schedule": {"type": "weekly", "at": "09:00"}})


def test_parse_task_rejects_an_unknown_schedule_key():
    with pytest.raises(settings.ConfigError, match="unknown key.*schedule: every"):
        settings.parse_task("x", {"prompt": "p", "schedule": {"type": "daily", "at": "09:00", "every": 2}})


def test_parse_task_requires_a_prompt():
    with pytest.raises(settings.ConfigError, match="requires 'prompt'"):
        settings.parse_task("x", {"schedule": {"type": "interval", "seconds": 60}})


def test_parse_task_rejects_a_negative_retention():
    with pytest.raises(settings.ConfigError, match="max_conversations must be 0"):
        settings.parse_task(
            "x", {"prompt": "p", "schedule": {"type": "interval", "seconds": 60}, "max_conversations": -1}
        )


def test_load_collects_task_tables_by_name(tmp_path):
    path = _write_task_config(
        tmp_path,
        """
[agents.assistant]
tools = []

[scheduling.task.morning-brief]
prompt = "Summarize my calendar"
schedule = { type = "daily", at = "09:00" }
""",
    )
    overrides = settings.load(str(path), table=core_table())
    assert set(overrides["scheduled_tasks"]) == {"morning-brief"}
    assert overrides["scheduled_tasks"]["morning-brief"]["prompt"] == "Summarize my calendar"


def test_a_task_table_and_the_toolsets_own_key_share_the_scheduling_section(tmp_path):
    # core_table() is core-only and would read max_task_conversations as an unknown key, so this one
    # case needs the real table: it is the case that proves the two halves of [scheduling] coexist.
    from kokua.config import settings_sources

    path = _write_task_config(
        tmp_path,
        """
[agents.assistant]
tools = []

[scheduling]
max_task_conversations = 5

[scheduling.task.morning-brief]
prompt = "Summarize my calendar"
schedule = { type = "daily", at = "09:00" }
""",
    )
    overrides = settings.load(
        str(path),
        table=settings_sources.build_settings_table(),
        extra_schema=settings_sources.startup_schema(),
    )
    assert set(overrides["scheduled_tasks"]) == {"morning-brief"}
    assert overrides["toolset_settings"]["scheduling"]["max_task_conversations"] == 5


def test_load_names_the_task_in_a_bad_table(tmp_path):
    path = _write_task_config(
        tmp_path,
        """
[agents.assistant]
tools = []

[scheduling.task.broken]
prompt = "p"
schedule = { type = "daily" }
""",
    )
    with pytest.raises(settings.ConfigError, match=r"\[scheduling.task.broken\].schedule requires 'at'"):
        settings.load(str(path), table=core_table())


def test_scheduling_is_not_a_reserved_core_section():
    # `core_sections` is what `settings_sources.declared_settings` refuses a toolset for claiming.
    # Routing the task tables on the dotted "scheduling.task" keeps the scheduling toolset's own
    # max_task_conversations declaration legal; adding "scheduling" to _STRUCTURED_SECTIONS would not.
    assert "scheduling" not in settings.core_sections()


# --- per-turn effort requests off a channel message -------------------------------------------


def test_thinking_request_reads_each_wire_word():
    assert settings.thinking_request("off") is False
    assert settings.thinking_request("low") == "low"
    assert settings.thinking_request("medium") == "medium"
    assert settings.thinking_request("high") == "high"


def test_thinking_request_is_case_and_whitespace_insensitive():
    assert settings.thinking_request("  HIGH ") == "high"


def test_thinking_request_falls_back_to_the_config_for_anything_else():
    """A value off a message is transport input, not a declaration, so an unrecognized one degrades to
    the configured effort instead of failing a turn someone is waiting on. `_thinking` raises for the
    same input, which is the difference between the two entry points."""
    assert settings.thinking_request("xhigh") is None  # Qwen's own ceiling, the plausible typo
    assert settings.thinking_request("default") is None
    assert settings.thinking_request("") is None
    assert settings.thinking_request(None) is None
    assert settings.thinking_request(True) is None
    assert settings.thinking_request(3) is None


def test_thinking_request_and_the_file_validator_agree_on_the_levels():
    """One vocabulary, two entry points. A level the file accepts must be a level a message can ask for."""
    for level in settings._THINKING_LEVELS:
        assert settings.thinking_request(level) == settings._thinking("assistant", "thinking", level)
