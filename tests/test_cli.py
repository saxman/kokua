"""The CLI surface: argument parsing and the defaults < file < flags precedence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


from kokua.cli import build_arg_parser, resolve_config
from kokua.config import paths
from tests.channels import _config


def test_arg_parser_defaults():
    args = build_arg_parser().parse_args([])
    assert args.model is None
    assert args.config is None
    assert args.frontend is None  # resolve_config falls back to the "cli" default


def test_default_config_lives_under_state_dir():
    cfg = resolve_config(build_arg_parser().parse_args([]))
    assert cfg.data_dir == paths.data_dir()
    assert cfg.frontend == "cli"


def test_arg_parser_overrides():
    args = build_arg_parser().parse_args(
        [
            "--model",
            "anthropic:claude-sonnet-4-6",
            "--frontend",
            "web",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
        ]
    )
    cfg = resolve_config(args)
    assert cfg.model == "anthropic:claude-sonnet-4-6"
    assert cfg.frontend == "web"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000


def test_system_flag_sets_the_override_not_the_fallback():
    """--system lands in system_message_override, distinct from system_message (the [assistant].
    system_message fallback), so an override can be told apart from that field merely being at its
    default. tests/toolsets/test_guidance.py covers the assembly-time half of that hop."""
    cfg = resolve_config(build_arg_parser().parse_args(["--system", "Be terse."]))
    assert cfg.system_message_override == "Be terse."


def test_system_flag_unset_leaves_no_override():
    assert resolve_config(build_arg_parser().parse_args([])).system_message_override is None


def test_two_mcp_urls_on_one_host_get_distinct_names():
    """Two endpoints on one host derive the same base name (see mcp.servers.name_from_url); the flag has
    to disambiguate them itself, since there is no way to name a --mcp-derived server explicitly."""
    args = build_arg_parser().parse_args(
        ["--mcp", "https://broker.example.com/mcp/quotes", "--mcp", "https://broker.example.com/mcp/orders"]
    )
    servers = resolve_config(args).mcp_servers
    names = [s.name for s in servers]
    assert names == ["broker-example-com", "broker-example-com-2"]

    from kokua.config.schema import AgentConfig, AssistantConfig
    from kokua.core.agents import build_registry

    config = AssistantConfig(
        agents={"assistant": AgentConfig(available=names)},
        entry_agent="assistant",
        mcp_servers=servers,
    )
    registry = build_registry(config)  # must not raise a collision
    assert set(names) <= set(registry)


def test_confirm_tools_flag_parses():
    cfg = resolve_config(build_arg_parser().parse_args(["--confirm-tools", "add_skill_script, execute_python"]))
    assert cfg.default_confirm == ["add_skill_script", "execute_python"]


def test_confirm_tools_flag_empty_disables():
    assert resolve_config(build_arg_parser().parse_args(["--confirm-tools", ""])).default_confirm == []


def test_cli_frontend_reports_model_client_error(tmp_path, monkeypatch, capsys):
    from kokua.core.assistant import ModelClientError
    from kokua.frontends import cli as cli_frontend

    async def boom(*args, **kwargs):
        raise ModelClientError("no default could be resolved; set AIMU_LANGUAGE_MODEL")

    monkeypatch.setattr(cli_frontend.Assistant, "create", boom)
    args = build_arg_parser().parse_args([])
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli_frontend.run(_config(tmp_path), args))
    assert exc.value.code == 1
    assert "no default could be resolved" in capsys.readouterr().err


# --- main() dispatch --------------------------------------------------------------------------


def _run_main(monkeypatch, argv: list[str], expect_exit: int | None = None):
    """Run `main()` with argv. Pass ``expect_exit`` for a subcommand that exits with a status code."""
    monkeypatch.setattr("sys.argv", ["kokua", *argv])
    from kokua.cli import main

    if expect_exit is None:
        return main()
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == expect_exit
    return None


def test_main_lists_frontends(monkeypatch, capsys):
    _run_main(monkeypatch, ["--list-frontends"])
    out = capsys.readouterr().out
    assert "cli:" in out and "web:" in out


def test_main_lists_every_provider_kind_of_toolset(monkeypatch, capsys):
    """The single namespace needs one discovery command, so this lists the whole registry rather than the
    plugin entry points: a list omitting the built-in groups and core capabilities would read as "web and
    scheduling are not available to you", which is the opposite of true. Asserted one name per provider
    kind so the flag cannot silently narrow back to plugins only."""
    path = paths.config_path()
    path.write_text(
        path.read_text(encoding="utf-8") + '\n[[mcp.server]]\nurl = "https://broker/mcp"\nname = "stocks"\n',
        encoding="utf-8",
    )
    _run_main(monkeypatch, ["--list-toolsets"])
    out = capsys.readouterr().out

    assert "web:" in out  # a wrapper over an AIMU tool group
    assert "scheduling:" in out  # a Kokua capability over one of its own subsystems
    assert "aimu_agents:" in out  # a toolset carrying a whole AIMU agent
    assert "stocks:" in out  # a server configured in [[mcp.server]]
    # Grouped, because a flat list of names would not tell a user where any of them comes from. Every
    # shipped toolset now arrives through the one entry-point group, so they share a heading; what the
    # grouping still separates is a shipped toolset from a configured server, an installed skill, and a
    # third party's package. No third-party plugin is installed in this test environment, so "plugin:"
    # itself is not asserted here; test_agents.py's collision test covers that label with a synthetic one.
    for provider in ("built-in toolset:", "MCP server:"):
        assert provider in out, provider


def test_listing_frontends_does_not_read_the_config(monkeypatch, capsys):
    """Which front ends exist is a property of the install, not of config.toml, so this listing answers
    before anything is configured. (--list-toolsets deliberately differs: the registry it prints depends
    on the file, so it resolves the config first.)"""

    def explode(*args, **kwargs):
        raise AssertionError("main() built a config/assistant for a plain listing")

    monkeypatch.setattr("kokua.cli.resolve_config", explode)
    _run_main(monkeypatch, ["--list-frontends"])
    assert "cli:" in capsys.readouterr().out


def test_main_reports_an_unknown_frontend_with_the_available_ones(monkeypatch, tmp_path):
    import pytest

    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(settings.example_text(), encoding="utf-8")  # config is required
    with pytest.raises(KeyError) as caught:
        _run_main(monkeypatch, ["--frontend", "nope"])
    message = str(caught.value)
    assert "nope" in message and "cli" in message  # names the miss and lists the choices


def test_main_reports_a_missing_config_without_a_traceback(monkeypatch, capsys, tmp_path):
    """Starting with no config.toml is now the ordinary first-run mistake, so it has to read as an
    instruction rather than as a crash."""
    import pytest

    from kokua.config import paths

    paths.config_path().unlink()
    with pytest.raises(SystemExit) as caught:
        _run_main(monkeypatch, [])
    assert caught.value.code != 0
    err = capsys.readouterr().err
    assert "config file not found" in err
    assert "kokua config init" in err
    assert "Traceback" not in err


def test_main_reports_a_config_with_no_agents_without_a_traceback(monkeypatch, capsys):
    import pytest

    from kokua.config import paths

    # configure_logging runs before the front end and calls faulthandler.enable(), which needs a real
    # stderr file descriptor that pytest's capture does not provide. Logging is not what is under test.
    monkeypatch.setattr("kokua.cli.configure_logging", lambda config: None)
    paths.config_path().write_text('[assistant]\nagent = "assistant"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        _run_main(monkeypatch, ["--frontend", "cli"])
    assert caught.value.code != 0
    err = capsys.readouterr().err
    assert "[agents." in err and "Traceback" not in err


# ---------------------------------------------------------------------------
# kokua skills
#
# The bundled skills ship with the repository rather than the package, so both commands work from a
# checkout and say where to get one otherwise. Both run before the preflight: copying files needs no
# AIMU surface, so an out-of-date sibling should not block installing a skill.
# ---------------------------------------------------------------------------


def test_skills_list_names_every_bundled_skill_with_its_description(monkeypatch, capsys):
    _run_main(monkeypatch, ["skills", "list"], expect_exit=0)
    out = capsys.readouterr().out
    for name in ("dice-roller", "email-report", "markdown-to-pdf"):
        assert name in out
    assert "Render Markdown to a PDF" in out  # the description, read from the frontmatter


def test_skills_install_copies_into_the_configured_skills_folder(monkeypatch, tmp_path, capsys):
    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(settings.example_text(), encoding="utf-8")

    _run_main(monkeypatch, ["skills", "install", "dice-roller"], expect_exit=0)

    installed = tmp_path / "data" / "skills" / "dice-roller"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "scripts" / "roll.py").is_file()
    assert "dice-roller" in capsys.readouterr().out


def test_skills_install_refuses_to_overwrite_without_force(monkeypatch, tmp_path, capsys):
    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(settings.example_text(), encoding="utf-8")
    _run_main(monkeypatch, ["skills", "install", "dice-roller"], expect_exit=0)
    marker = tmp_path / "data" / "skills" / "dice-roller" / "EDITED"
    marker.write_text("a local edit", encoding="utf-8")
    capsys.readouterr()

    _run_main(monkeypatch, ["skills", "install", "dice-roller"], expect_exit=0)

    assert marker.is_file()  # a local edit survives, because nothing was overwritten
    assert "already installed" in capsys.readouterr().out


def test_skills_install_overwrites_with_force(monkeypatch, tmp_path):
    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(settings.example_text(), encoding="utf-8")
    _run_main(monkeypatch, ["skills", "install", "dice-roller"], expect_exit=0)
    marker = tmp_path / "data" / "skills" / "dice-roller" / "EDITED"
    marker.write_text("a local edit", encoding="utf-8")

    _run_main(monkeypatch, ["skills", "install", "dice-roller", "--force"], expect_exit=0)

    assert not marker.exists()  # replaced wholesale, so a stale file cannot linger


def test_skills_install_names_the_available_skills_on_a_typo(monkeypatch, tmp_path, capsys):
    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(settings.example_text(), encoding="utf-8")

    _run_main(monkeypatch, ["skills", "install", "dice-rollr"], expect_exit=1)

    out = capsys.readouterr().out
    assert "dice-rollr" in out
    assert "dice-roller" in out  # so the fix is in the same message as the mistake


def test_an_installed_skill_is_discoverable_in_the_namespace(monkeypatch, tmp_path, capsys):
    """Installing is only useful if the registry then sees it, which is what makes it declarable."""
    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(settings.example_text(), encoding="utf-8")
    _run_main(monkeypatch, ["skills", "install", "dice-roller"], expect_exit=0)
    capsys.readouterr()

    _run_main(monkeypatch, ["--list-toolsets"])

    out = capsys.readouterr().out
    assert "skill:" in out
    assert "dice-roller" in out


def test_main_web_reports_a_broken_agents_table_as_an_instruction(monkeypatch, tmp_path, capsys):
    """`kokua-web` reports an unresolvable [agents.*] table the way `kokua` does.

    The two console scripts reach the web front end by different routes, so the ConfigError that
    `main` prints as one line used to leave `main_web` dumping a traceback for the same mistake.
    """
    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    # Substituted into the shipped example rather than written out, so this exercises the real file. The
    # asserted-on string is `[agents.coder]`'s tools line, which is why it fails loudly if that line is
    # edited without editing this: a no-op replace leaves a *valid* config and the test stops testing.
    broken = settings.example_text().replace(
        'available = ["fs", "fs_write", "compute", "time", "messaging"]', 'available = ["fs", "nope", "time"]'
    )
    assert "nope" in broken, "the [agents.coder] tools line moved; this replace no longer breaks anything"
    text = broken
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["kokua-web"])

    from kokua.cli import main_web

    with pytest.raises(SystemExit) as excinfo:
        main_web()

    assert excinfo.value.code == 2
    assert "nope" in capsys.readouterr().err


def test_the_retired_plugins_flag_is_rejected():
    """Removed with `[assistant].load_plugins`, the setting it overrode. argparse refusing the flag is
    better than accepting one that no longer reaches anything."""
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--no-plugins"])


# --- export -----------------------------------------------------------------------------------


def _seeded_home(monkeypatch, tmp_path, messages, metadata=None, key="abcdef0123456789"):
    """A KOKUA_HOME holding a config and one stored conversation, and that conversation's key.

    Calling this more than once against the same `tmp_path` (with a different `key`) seeds a second
    conversation into the same store, which is how the ambiguous-prefix test below gets two real,
    store-saved sessions sharing a fragment rather than two hand-built ones.
    """
    from aimu.sessions import Session, TinyDBSessionStore

    from kokua.config import file as settings  # the alias this file already uses, see line 170

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    if not config_path.exists():
        config_path.write_text(settings.example_text(), encoding="utf-8")
    sessions_path = tmp_path / "data" / "sessions.json"
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    store = TinyDBSessionStore(str(sessions_path))
    meta = {"created_at": "2026-08-25T14:00:00", "updated_at": "2026-08-25T15:00:00", "title": "A run"}
    meta.update(metadata or {})
    session = Session(key=key, metadata=meta, messages=list(messages))
    store.save(session)
    return session.key


_TWO_MESSAGES = [
    {"role": "user", "content": "what is it", "timestamp": "2026-08-25T14:19:07"},
    {"role": "assistant", "content": "it is this", "timestamp": "2026-08-25T14:19:59"},
]


def test_export_writes_a_markdown_file_and_prints_its_path(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "latest"], expect_exit=0)
    printed = capsys.readouterr().out.strip()
    assert printed.endswith(".md")
    written = Path(printed).read_text(encoding="utf-8")
    assert written.startswith("# ")
    assert "it is this" in written


def test_export_resolves_a_unique_id_prefix(monkeypatch, tmp_path, capsys):
    """Anyone who read an id out of a listing will shorten it, so a prefix has to resolve."""
    key = _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", key[:8]], expect_exit=0)
    assert capsys.readouterr().out.strip().endswith(".md")


def test_export_to_stdout_with_a_dash(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "latest", "-o", "-"], expect_exit=0)
    out = capsys.readouterr().out
    assert out.startswith("# ")
    assert "what is it" in out


def test_export_full_reads_a_spilled_sub_agent_response_back_off_disk(monkeypatch, tmp_path, capsys):
    """`_export` must pass `config.payloads_path` through to `render_markdown`: without it, `--full`
    on a conversation carrying a spilled sub-agent tool response (core/subagents.py) could only ever
    show the RESPONSE_PREVIEW_CHARS preview already stored in metadata, silently abridging what the
    flag promises to keep whole."""
    from kokua import payloads

    full_text = "w" * 9000
    reference = payloads.save_text(tmp_path / "data" / "payloads", full_text)
    key = _seeded_home(
        monkeypatch,
        tmp_path,
        [{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}],
        metadata={
            "subagent": {
                "0": [
                    {"id": "s1", "role": "worker", "task": "fetch", "status": "running"},
                    {
                        "id": "s1",
                        "append": {
                            "kind": "tool",
                            "name": "fetch_url",
                            "arguments": "{}",
                            "response": full_text[:4000],
                            "response_ref": reference,
                            "response_bytes": len(full_text),
                        },
                    },
                    {"id": "s1", "status": "done"},
                ]
            }
        },
    )
    _run_main(monkeypatch, ["export", key, "--full", "-o", "-"], expect_exit=0)
    assert full_text in capsys.readouterr().out


def test_export_of_an_unknown_id_reports_it_and_exits_nonzero(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "999999999999"], expect_exit=2)
    assert "999999999999" in capsys.readouterr().err


def test_export_of_an_ambiguous_prefix_reports_the_count_and_writes_nothing(monkeypatch, tmp_path, capsys):
    """Two real, store-saved conversations sharing a fragment must not silently open either one:
    `resolve()` itself refuses to guess between them, and the message has to say so, distinctly from
    an unknown id, or a reader goes hunting for a typo that isn't there."""
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES, key="abcdef01aaaaaaaa")
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES, key="abcdef01bbbbbbbb")

    _run_main(monkeypatch, ["export", "abcdef01"], expect_exit=2)

    err = capsys.readouterr().err
    assert "2" in err  # names how many conversations shared the fragment
    assert "abcdef01" in err
    assert "no conversation found" not in err  # distinct from the unknown-id message above
    # Neither candidate was opened: `_export` never reaches the write step on this path, so the
    # downloads folder it would have written into was never even created.
    assert not (tmp_path / "data" / "downloads").exists()


def test_export_reports_a_busy_store_rather_than_a_truncated_conversation(monkeypatch, tmp_path, capsys):
    """TinyDB rewrites the whole file on save, so a read racing the daemon's persist can land on a
    partial one. Presenting half a conversation as the conversation is the failure to avoid."""
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)

    def torn(*args, **kwargs):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr("kokua.cli.TinyDBSessionStore", torn)
    _run_main(monkeypatch, ["export", "latest"], expect_exit=2)
    assert "busy" in capsys.readouterr().err.lower()


def test_export_builds_no_assistant(monkeypatch, tmp_path):
    """`export` never reaches `Assistant.create`, since an export must work with the model server down.

    Narrow to what this actually checks: patching `Assistant.create` alone does not prove no model
    client or agent is built by any other route (a direct `aio.client()` call would pass this test
    undetected). It pins the one construction path `_export` is documented to avoid.
    """
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)

    def boom(*args, **kwargs):
        raise AssertionError("export must not build an assistant")

    monkeypatch.setattr("kokua.core.assistant.Assistant.create", boom)
    _run_main(monkeypatch, ["export", "latest", "-o", "-"], expect_exit=0)


def test_export_full_and_trimmed_land_in_different_files(monkeypatch, tmp_path, capsys):
    """`kokua export X --full` then `kokua export X` must not leave one file.

    The CLI derives its default path the same way the `export_conversation` tool does, so it had the
    same collision: the second run replaced the full export with a trimmed one, under a name that
    says nothing about which it is.
    """
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)

    _run_main(monkeypatch, ["export", "latest", "--full"], expect_exit=0)
    full_path = capsys.readouterr().out.strip()
    _run_main(monkeypatch, ["export", "latest"], expect_exit=0)
    trimmed_path = capsys.readouterr().out.strip()

    assert full_path != trimmed_path
    assert Path(full_path).exists()
    assert Path(trimmed_path).exists()


def test_export_output_flag_still_wins_over_the_fidelity_name(monkeypatch, tmp_path, capsys):
    """`-o` names the file outright, so fidelity never renames what the caller asked for."""
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    target = tmp_path / "mine.md"

    _run_main(monkeypatch, ["export", "latest", "--full", "-o", str(target)], expect_exit=0)

    assert capsys.readouterr().out.strip() == str(target)
    assert target.exists()


def test_export_turn_prints_the_per_turn_document(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "latest", "--turn", "1", "-o", "-"], expect_exit=0)
    out = capsys.readouterr().out
    assert out.startswith("# A run: turn 1")
    assert "what is it" in out


def test_export_turn_out_of_range_exits_two_with_the_range(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "latest", "--turn", "9"], expect_exit=2)
    assert "turns 1 to 1" in capsys.readouterr().err


def test_export_turn_default_path_ends_in_turn_suffix(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "latest", "--turn", "1", "--full"], expect_exit=0)
    assert capsys.readouterr().out.strip().endswith(".turn-1.md")


def test_export_turn_with_full_notes_on_stderr_that_full_has_no_effect(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "latest", "--turn", "1", "--full", "-o", "-"], expect_exit=0)
    captured = capsys.readouterr()
    assert "--full has no effect with --turn" in captured.err
    assert "--full" not in captured.out


def test_export_turn_without_full_prints_no_note_and_says_when_no_calls_were_recorded(monkeypatch, tmp_path, capsys):
    _seeded_home(monkeypatch, tmp_path, _TWO_MESSAGES)
    _run_main(monkeypatch, ["export", "latest", "--turn", "1", "-o", "-"], expect_exit=0)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "_No model calls were recorded for this turn._" in captured.out


# --- kokua agents and the startup summary -----------------------------------------------------


def _write_example_config(monkeypatch, tmp_path, transform=lambda text: text):
    from kokua.config import file as settings

    monkeypatch.setenv("KOKUA_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(transform(settings.example_text()), encoding="utf-8")


def test_summary_line_per_agent():
    from kokua.cli import access_summary_lines
    from kokua.registry.registry import register
    from kokua.config.schema import AgentConfig

    config = _config(
        Path("/tmp"),
        agents={
            "assistant": AgentConfig(available=[f"a{n}" for n in range(12)], resident=[f"a{n}" for n in range(7)]),
            "coder": AgentConfig(available=[f"b{n}" for n in range(5)], confirm=["compute.run_command"], review=[]),
        },
        default_confirm=["x", "y", "z"],
        default_review=["p", "q", "r"],
    )

    assert access_summary_lines(config, register([])) == [
        "assistant: 12 available, 7 resident, 3 confirm, 3 review (from [assistant.tools])",
        "coder: 5 available, 5 resident, 1 confirm, 0 review (from [agents.coder.tools])",
    ]


def test_agents_command_prints_each_agents_three_axes(monkeypatch, tmp_path, capsys):
    _write_example_config(monkeypatch, tmp_path)

    _run_main(monkeypatch, ["agents"])

    out = capsys.readouterr().out
    assert "assistant\n  available: " in out
    assert "  resident:  memory, config, conversations, planning, capabilities, time, messaging" in out
    assert "  resident:  every available toolset" in out
    confirm = "skills.add_skill_script, mcp.add_mcp_server, config.update_config"
    review = "compute.run_command, compute.execute_python, fs_write"
    assert f"  confirm:   {confirm}  (from [assistant.tools])" in out
    assert f"  review:    {review}  (from [assistant.tools])" in out
    assert f"composed workers, confirm (from [assistant.tools]): {confirm}" in out
    assert f"composed workers, review (from [assistant.tools]): {review}" in out


def test_agents_command_shows_a_declared_pair_source(monkeypatch, tmp_path, capsys):
    anchor = 'available = ["fs", "fs_write", "compute", "time", "messaging"]'

    def declare_pair(text):
        assert anchor in text
        return text.replace(anchor, anchor + '\nconfirm = ["compute.run_command"]\nreview = []')

    _write_example_config(monkeypatch, tmp_path, declare_pair)

    _run_main(monkeypatch, ["agents"])

    assert "  confirm:   compute.run_command  (from [agents.coder.tools])" in capsys.readouterr().out


_CODER_AVAILABLE = 'available = ["fs", "fs_write", "compute", "time", "messaging"]'


def _declare_coder_all(text):
    assert _CODER_AVAILABLE in text
    return text.replace(_CODER_AVAILABLE, 'available = "all"\nresident = ["time"]')


def _coder_config_and_registry():
    from kokua.core.agents import build_registry

    config = resolve_config(build_arg_parser().parse_args([]))
    return config, build_registry(config)


def test_agents_command_expands_all_in_trust_order(monkeypatch, tmp_path, capsys):
    from kokua.core.agents import available_for

    _write_example_config(monkeypatch, tmp_path, _declare_coder_all)
    config, registry = _coder_config_and_registry()
    expanded = available_for(config, registry, "coder")

    _run_main(monkeypatch, ["agents"])

    out = capsys.readouterr().out
    assert f"  available: all ({len(expanded)}): {', '.join(expanded)}" in out
    assert "  resident:  time" in out


def test_summary_counts_an_all_agent(monkeypatch, tmp_path):
    from kokua.cli import access_summary_lines
    from kokua.core.agents import available_for

    _write_example_config(monkeypatch, tmp_path, _declare_coder_all)
    config, registry = _coder_config_and_registry()
    expanded = available_for(config, registry, "coder")

    lines = access_summary_lines(config, registry)

    coder = next(line for line in lines if line.startswith("coder:"))
    assert coder.startswith(f"coder: all ({len(expanded)}) available, 1 resident, ")


def test_summary_is_skipped_when_the_registry_cannot_assemble(monkeypatch, tmp_path, capsys):
    from kokua.cli import print_access_summary
    from kokua.registry.registry import ToolsetError

    def collide(config):
        raise ToolsetError("two toolsets claim the name 'dup'")

    _write_example_config(monkeypatch, tmp_path)
    config, _ = _coder_config_and_registry()
    monkeypatch.setattr("kokua.core.agents.build_registry", collide)

    print_access_summary(config)

    assert capsys.readouterr().err == ""


def test_agents_command_reports_a_bad_config_and_exits_2(monkeypatch, tmp_path, capsys):
    def break_coder(text):
        broken = text.replace(
            'available = ["fs", "fs_write", "compute", "time", "messaging"]', 'available = ["fs", "nope", "time"]'
        )
        assert "nope" in broken, "the [agents.coder] tools line moved; this replace no longer breaks anything"
        return broken

    _write_example_config(monkeypatch, tmp_path, break_coder)

    _run_main(monkeypatch, ["agents"], expect_exit=2)

    assert "nope" in capsys.readouterr().err


def test_agents_command_builds_no_toolset(monkeypatch, tmp_path):
    _write_example_config(monkeypatch, tmp_path)

    def refuse(*args, **kwargs):
        raise AssertionError("kokua agents must not build a toolset")

    monkeypatch.setattr("kokua.registry.registry.build_tools", refuse)
    monkeypatch.setattr("kokua.core.build.build_tools", refuse)

    _run_main(monkeypatch, ["agents"])


def _start(monkeypatch, capsys, argv: list[str]) -> tuple[str, list]:
    """Run startup against a stub front end; return stderr as it stood when the front end ran, and the
    config it was handed."""
    from kokua import plugins
    from kokua.plugins import FrontEnd

    stderr_at_run: list[str] = []
    configs: list = []

    async def run(config, args):
        stderr_at_run.append(capsys.readouterr().err)
        configs.append(config)

    # configure_logging enables faulthandler, which needs a real stderr fileno that capture does not give.
    monkeypatch.setattr("kokua.cli.configure_logging", lambda config: None)
    monkeypatch.setattr(plugins, "get_frontend", lambda name: FrontEnd(name="stub", description="", run=run))
    _run_main(monkeypatch, argv)
    return stderr_at_run[0], configs


def test_startup_keeps_the_summary_off_stderr_by_default(monkeypatch, tmp_path, capsys):
    _write_example_config(monkeypatch, tmp_path)

    stderr, _ = _start(monkeypatch, capsys, [])

    assert "assistant: " not in stderr


def test_debug_flag_prints_the_summary_and_logs_at_debug(monkeypatch, tmp_path, capsys):
    _write_example_config(monkeypatch, tmp_path)

    stderr, configs = _start(monkeypatch, capsys, ["--debug"])

    assert "assistant: " in stderr and "resident" in stderr
    assert configs[0].log_level == "DEBUG"


def test_logging_level_debug_prints_the_summary(monkeypatch, tmp_path, capsys):
    _write_example_config(monkeypatch, tmp_path, lambda text: text.replace('level = "INFO"', 'level = "DEBUG"'))

    stderr, _ = _start(monkeypatch, capsys, [])

    assert "assistant: " in stderr


def test_the_summary_is_logged_whatever_the_level(monkeypatch, tmp_path, capsys, caplog):
    _write_example_config(monkeypatch, tmp_path)

    with caplog.at_level("INFO", logger="kokua.cli"):
        _start(monkeypatch, capsys, [])

    assert any(record.getMessage().startswith("assistant: ") for record in caplog.records)
