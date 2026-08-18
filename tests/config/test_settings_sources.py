"""Where the live settings table comes from: a toolset's declared settings reaching config.toml."""

from __future__ import annotations

import pytest

from kokua.cli import build_arg_parser, resolve_config
from kokua.config import paths
from kokua.config import file as settings
from kokua.config import settings_sources
from kokua.config.schema import AssistantConfig
from kokua.config.settings_sources import (
    build_settings_table,
    declaring_toolsets,
    seed_toolset_defaults,
    startup_schema,
)
from kokua.config.table import CORE_RUNTIME_SETTINGS, TYPE_LABELS
from kokua.toolsets.registry import Setting, Toolset, ToolsetError

HOT = Setting("rounds", int, 2, hot=True)
COLD = Setting("endpoint", str, "https://example.invalid")


def _toolset(*declared: Setting, name: str = "widgets") -> Toolset:
    return Toolset(name=name, description="A test capability.", build=lambda ctx: [], settings=declared)


def _write_config(text: str):
    path = paths.config_path()
    path.write_text(text, encoding="utf-8")
    return path


def _resolve(*argv):
    return resolve_config(build_arg_parser().parse_args(list(argv)))


def test_a_hot_declaration_becomes_a_namespaced_table_entry():
    table = build_settings_table([_toolset(HOT, COLD)])
    entry = table.by_toml("widgets", "rounds")
    assert (entry.wire_key, entry.kind, entry.toolset) == ("widgets.rounds", int, "widgets")
    assert table.is_hot("widgets", "rounds") is True


def test_a_cold_declaration_stays_out_of_the_table_but_still_has_a_schema_entry():
    """A cold setting must parse and reject a wrong type; it just cannot change without a restart."""
    table = build_settings_table([_toolset(HOT, COLD)])
    assert table.by_toml("widgets", "endpoint") is None
    assert table.is_hot("widgets", "endpoint") is False
    assert startup_schema([_toolset(HOT, COLD)]) == {
        ("widgets", "endpoint"): ("widgets.endpoint", (str,), "a string", None)
    }


def test_kokuas_own_settings_survive_a_contributed_table():
    assert build_settings_table([_toolset(HOT)]).by_field("show_tools") is not None


def test_seeding_fills_a_declared_default_without_overwriting_what_the_file_set():
    config = AssistantConfig(toolset_settings={"widgets": {"rounds": 9}})

    seed_toolset_defaults(config, [_toolset(HOT, COLD)])

    assert config.toolset_settings["widgets"] == {"rounds": 9, "endpoint": COLD.default}


def test_seeding_leaves_no_bucket_for_a_toolset_that_declares_nothing():
    config = AssistantConfig()
    seed_toolset_defaults(config, [_toolset()])
    assert config.toolset_settings == {}


def test_a_toolset_named_after_a_core_section_may_not_declare_settings():
    """A toolset's section is always its own name, and a contributed entry wins the schema merge, so a
    plugin named ``email`` declaring ``host`` would route [email].host into its own bucket and leave
    ``AssistantConfig.email_host`` unset -- the email capability switching itself off in a config nobody
    edited. Every section the core parses is refused, including the structured tables, the ones only the
    settings table declares (``display``), and a section a removed key used to live in (``tools``,
    ``subagents``) -- without which a toolset named ``tools`` would pass this check only to hit
    ``load``'s "[tools] is gone." branch for every key instead."""
    for reserved in (
        "email",
        "display",
        "assistant",
        "security",
        "paths",
        "logging",
        "web",
        "generation",
        "agents",
        "tools",
        "subagents",
    ):
        with pytest.raises(ToolsetError, match=rf"\[{reserved}\] is a config.toml section"):
            build_settings_table([_toolset(HOT, name=reserved)])


def test_a_toolset_named_after_a_core_section_is_fine_while_it_declares_nothing():
    """Only claiming the section's keys is refused, so a name that merely collides costs nothing until the
    toolset wants settings of its own."""
    assert build_settings_table([_toolset(name="email")]).settings == CORE_RUNTIME_SETTINGS
    assert startup_schema([_toolset(name="email")]) == {}


def test_the_shipped_planning_toolset_owns_its_section():
    """The handover this mechanism exists for: ``[planning]`` reaches the table and the seeded config
    through the same declaration path a third party's toolset uses, with no core entry behind it."""
    table = build_settings_table()

    assert table.by_toml("planning", "plan_review").toolset == "planning"
    assert table.by_toml("planning", "plan_review").wire_key == "planning.plan_review"
    assert table.by_toml("planning", "review_rounds") is None  # cold: a startup-only key
    assert ("planning", "review_rounds") in startup_schema()
    assert not any(setting.section == "planning" for setting in CORE_RUNTIME_SETTINGS)

    config = AssistantConfig()
    seed_toolset_defaults(config)
    assert config.toolset_settings["planning"] == {
        "plan_review": False,
        "plan_review_agent": False,
        "result_review": False,
        "show_reasoning": False,
        "review_rounds": 2,
    }


def test_a_reserved_name_is_also_refused_for_a_cold_declaration():
    with pytest.raises(ToolsetError, match=r"\[logging\] is a config.toml section"):
        startup_schema([_toolset(COLD, name="logging")])


def test_an_unsupported_setting_type_is_refused_by_name():
    """``TYPE_LABELS`` is the whole supported set: an unlisted kind has no error label for the parser and
    no branch in the panel sanitizer, so it would be a bare KeyError at startup or a value silently
    dropped on every save. A clear refusal is the honest answer, not a new type."""
    with pytest.raises(ToolsetError, match="unsupported type float"):
        build_settings_table([_toolset(Setting("threshold", float, 0.5, hot=True))])
    with pytest.raises(ToolsetError, match="must be one of: bool, int, str"):
        startup_schema([_toolset(Setting("threshold", float, 0.5))])


def test_the_supported_types_are_exactly_the_labelled_ones():
    """Pins the derivation: the refusal message and the schema's labels read the same table."""
    for kind in TYPE_LABELS:
        assert build_settings_table([_toolset(Setting("k", kind, None, hot=True))]).by_toml("widgets", "k") is not None


def test_one_toolset_declaring_a_key_twice_is_refused():
    with pytest.raises(ToolsetError, match="declares setting 'rounds' twice"):
        build_settings_table([_toolset(HOT, Setting("rounds", bool, False, hot=True))])


def test_two_toolsets_sharing_a_name_with_colliding_hot_keys_is_refused_by_name():
    """Without this check, the collision would only surface as a bare ``ValueError`` out of
    ``SettingsTable.__init__`` (a colliding location), which ``cli.main`` does not catch as a
    ``ToolsetError`` and so would print as a traceback instead of naming the mistake."""
    first = _toolset(HOT, name="planning")
    second = _toolset(Setting("rounds", bool, False, hot=True), name="planning")
    with pytest.raises(ToolsetError, match="two toolsets are both named 'planning'"):
        build_settings_table([first, second])


def test_two_toolsets_sharing_a_name_with_non_colliding_cold_keys_still_merge_and_are_refused():
    """Even when the two declare different keys, letting both through would merge them into one
    ``[planning]`` bucket as if a single toolset had declared every key -- refused the same way as an
    outright key collision, at the same seam, before either bucket is built."""
    first = _toolset(COLD, name="planning")
    second = _toolset(Setting("other", str, "x"), name="planning")
    with pytest.raises(ToolsetError, match="two toolsets are both named 'planning'"):
        startup_schema([first, second])


def test_a_dotted_toolset_name_is_refused():
    """``config.file.load`` routes a toolset's key by splitting its schema target on the first '.', so a
    dotted toolset name would silently file a value under the wrong bucket instead of the one seeding
    fills."""
    with pytest.raises(ToolsetError, match=r"toolset 'my\.pack' may not contain '\.'"):
        build_settings_table([_toolset(HOT, name="my.pack")])


def test_kokuas_core_toolsets_are_a_declaring_source():
    from kokua.toolsets.core import CORE_TOOLSETS

    assert {t.name for t in CORE_TOOLSETS} <= {t.name for t in declaring_toolsets()}


def test_an_mcp_server_cannot_own_a_config_section():
    """An MCP toolset's existence comes *from* the config file, so it cannot contribute to the schema
    that parses that file: naming a section after one is an unknown key, not a settings section."""
    _write_config('[[mcp.server]]\nurl = "https://broker/mcp"\nname = "stocks"\n\n[stocks]\nrounds = 3\n')

    with pytest.raises(settings.ConfigError, match=r"unknown config key \[stocks\].rounds"):
        _resolve()


def test_a_declared_section_parses_and_is_seeded_and_recorded_as_configured(monkeypatch):
    monkeypatch.setattr(settings_sources, "declaring_toolsets", lambda: [_toolset(HOT, COLD)])
    _write_config('[widgets]\nrounds = 5\nendpoint = "https://set/by-hand"\n')

    config = _resolve()

    assert config.toolset_settings["widgets"] == {"rounds": 5, "endpoint": "https://set/by-hand"}
    assert config.configured_sections == ("widgets",)


def test_a_bare_section_header_with_no_keys_still_counts_as_configured(monkeypatch):
    """A user who drops a toolset from ``tools`` but leaves its section header untouched -- every key
    commented out, exactly how the shipped ``config.example.toml`` ships ``[planning]`` -- must still
    trip the configured-but-undeclared warning. With no keys set, parsing the section produces no
    ``toolset_settings`` entry at all, so the check has to look at the file's own section names, not at
    what got parsed out of them."""
    monkeypatch.setattr(settings_sources, "declaring_toolsets", lambda: [_toolset(HOT, COLD)])
    _write_config('[widgets]\n# rounds = 5\n# endpoint = "https://set/by-hand"\n')

    config = _resolve()

    assert config.toolset_settings["widgets"] == {"rounds": HOT.default, "endpoint": COLD.default}
    assert config.configured_sections == ("widgets",)


def test_the_shipped_planning_section_with_every_key_commented_out_is_still_configured():
    """The real-world trigger for the warning above, with no monkeypatching: the shipped example leaves
    ``[planning]`` with every key commented out, and dropping "planning" from ``tools`` while leaving that
    header alone must not go unreported."""
    _write_config("[planning]\n# every key commented out, as the shipped example ships\n")

    config = _resolve()

    assert config.configured_sections == ("planning",)


def test_a_wrong_typed_cold_declaration_is_rejected(monkeypatch):
    monkeypatch.setattr(settings_sources, "declaring_toolsets", lambda: [_toolset(HOT, COLD)])
    _write_config("[widgets]\nendpoint = 3\n")

    with pytest.raises(settings.ConfigError, match=r"\[widgets\].endpoint must be a string"):
        _resolve()


def test_a_section_only_kokua_defaulted_is_not_reported_as_configured(monkeypatch):
    """Seeding fills every declared key, so afterwards only ``configured_sections`` can still tell a
    section the user wrote from one Kokua supplied."""
    monkeypatch.setattr(settings_sources, "declaring_toolsets", lambda: [_toolset(HOT, COLD)])
    _write_config("[display]\nshow_tools = false\n")

    config = _resolve()

    assert config.toolset_settings["widgets"] == {"rounds": HOT.default, "endpoint": COLD.default}
    assert config.configured_sections == ()
