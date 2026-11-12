"""Rule versions: first start and config changes, rules_update / rules_propose, Settings → Rules,
the report format check, the tracker-log requirement, the "rules updated" hint and MCP prompts."""

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import load_settings
from relaybay.db import repo
from relaybay.db.connection import connect
from relaybay.demo import DemoError, _has_board, create_demo
from relaybay.domain.errors import ConfirmationNeeded, ConflictError, ValidationError
from relaybay.domain.rules import DEFAULT_RULES, merge_rules
from relaybay.mcp_server import format as fmt
from relaybay.mcp_server.server import build_mcp
from relaybay.security import CSRF_FIELD, csrf_token, load_or_create_token
from relaybay.service import Service, collect_notes
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import LATEST
from tests.test_rules import seed_project_rules

DESKTOP = "local_00000000-0000-0000-0000-00000000000c"
NOW = "2026-01-01T00:00:00.000+00:00"
FAKE_TOKEN = "ghp_" + "a1B2" * 9
HOOK_ID = "5d6e7f80-1a2b-4c3d-9e8f-0a1b2c3d4e5f"
PATCH = {"guidance": "Report often."}
ORIGIN = {"Origin": BASE}


def config_rules(guidance: str) -> dict:
    return merge_rules(DEFAULT_RULES, {"guidance": guidance})


def dump(db: Path) -> tuple[int, list[object]]:
    with contextlib.closing(sqlite3.connect(db)) as raw:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        return version, list(raw.iterdump())


async def started(db: Path, **kwargs) -> Service:
    service = Service(db, **kwargs)
    await service.start()
    return service


# --- first start and config --------------------------------------------------------------------


async def test_the_first_start_makes_version_1_once(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db)
    await service.upsert_project("acme-shop")
    assert service.global_rules.global_version == 1
    assert await service.events_since(0, "acme-shop", 5)  # the project's event, none for rules
    versions = await service.rule_versions("global")
    assert [(v.number, v.author) for v in versions] == [(1, "config")]
    await service.close()

    service = await started(db)
    try:
        assert [v.number for v in await service.rule_versions("global")] == [1]
        rules = await service.rules("acme-shop")
        assert rules.version_text == "Rules v1 (global v1, project v0)"
        assert fmt.rules_line(rules).startswith(
            f"Rules v1 (global v1, project v0), hash {rules.hash}: "
        )
    finally:
        await service.close()


async def test_upgrade_from_the_previous_migration_with_data(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(
        e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name[:4] <= "0017"
    )
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 17;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 1, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO task (project_id, key, title, stage, created_at, updated_at)"
            " VALUES (1, 'acme-shop-1', 'Checkout', 'dev', ?, ?)",
            (NOW, NOW),
        )
    finally:
        await conn.close()
    await seed_project_rules(db, "acme-shop", {"stage_requirements": {"test": []}})

    for _ in range(2):  # a second start changes nothing
        service = await started(db)
        try:
            assert (await service.get_task("acme-shop-1")).title == "Checkout"
            rules = await service.rules("acme-shop")
            assert rules.requirements("test") == []
            assert rules.version_text == "Rules v2 (global v1, project v1)"
            versions = await service.rule_versions("acme-shop")
            assert [(v.number, v.author) for v in versions] == [(1, "project_rules")]
            assert versions[0].body == {"stage_requirements": {"test": []}}
        finally:
            await service.close()
    with sqlite3.connect(db) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == LATEST
    raw.close()


async def test_a_config_change_makes_a_version_and_an_unchanged_one_keeps_accepted_ones(
    tmp_path: Path,
) -> None:
    db = tmp_path / "relaybay.sqlite"
    first = config_rules("From the file.")
    service = await started(db, rules=first)
    proposal = await service.propose_rules("global", PATCH, "shorter", session="agent")
    await service.decide_rule_proposal(proposal.id, True, actor="owner")
    assert service.global_rules.global_version == 2
    await service.close()

    # The same file: the accepted version stays in force and nothing new is written.
    service = await started(db, rules=first)
    assert service.global_rules.global_version == 2
    assert service.global_rules.body["guidance"] == "Report often."
    await service.close()

    # An edited file wins over what was accepted, and says so in the history.
    service = await started(db, rules=config_rules("Edited."))
    try:
        assert service.global_rules.global_version == 3
        assert service.global_rules.body["guidance"] == "Edited."
        newest = (await service.rule_versions("global"))[0]
        assert newest.author == "config" and "config.toml" in newest.reason
        assert "Edited." in newest.diff and "Report often." in newest.diff
    finally:
        await service.close()


async def test_a_hand_edited_project_row_becomes_a_version_and_a_broken_one_does_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db)
    await service.upsert_project("acme-shop")
    await service.close()
    await seed_project_rules(db, "acme-shop", {"guidance": "One."})
    service = await started(db)
    await service.close()
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE project_rules SET body = ?", (json.dumps({"guidance": "Two."}),))
    raw.close()
    service = await started(db)
    assert (await service.rule_versions("acme-shop"))[0].reason.endswith("by hand).")
    assert (await service.rules("acme-shop")).project_version == 2
    await service.close()

    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE project_rules SET body = '{not json'")
    raw.close()
    service = await started(db)
    try:
        rules = await service.rules("acme-shop")
        assert rules.override_error and rules.project_version == 2
        assert len(await service.rule_versions("acme-shop")) == 2
    finally:
        await service.close()


def execute(db: Path, sql: str, *args: object) -> None:
    with contextlib.closing(sqlite3.connect(db)) as raw:
        raw.execute(sql, args)
        raw.commit()


@pytest.mark.parametrize("broken", ["{oops", json.dumps({"nonsense": 1})])
async def test_a_broken_global_version_falls_back_and_never_stops_the_server(
    tmp_path: Path, broken: str, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db)
    proposal = await service.propose_rules("global", PATCH, "why", session="agent")
    await service.decide_rule_proposal(proposal.id, True, actor="owner")
    await service.close()

    # The newest is broken: the one before it is in force.
    execute(db, "UPDATE rule_version SET body = ? WHERE number = 2", broken)
    with caplog.at_level("ERROR", logger="relaybay"):
        service = await started(db)
    try:
        assert "rule_version #2 (global v2)" in caplog.text
        assert service.global_rules.global_version == 1
        assert service.global_rules.body == DEFAULT_RULES
        error = await service.rule_version_error("global") or ""
        assert "rule_version #2 (global v2)" in error and "v1 is in force" in error
    finally:
        await service.close()

    # Every version broken: the config. The next one is numbered after the highest row.
    execute(db, "UPDATE rule_version SET body = ?", broken)
    execute(db, "UPDATE rule_set SET current_version = 1")
    service = await started(db, coordinators=["lead"])
    try:
        assert service.global_rules.body == DEFAULT_RULES
        assert "the config is in force" in (await service.rule_version_error("global") or "")
        assert await service.update_rules("global", PATCH, "why", session="lead") == ("global", 3)
        with pytest.raises(ValidationError, match=r"broken version.*rule_version #1"):
            await service.rollback_rules("global", 1, confirmed=True)
    finally:
        await service.close()


async def test_a_broken_project_version_falls_back_to_the_row(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db)
    await service.upsert_project("acme-shop")
    await service.close()
    await seed_project_rules(db, "acme-shop", {"guidance": "Kept."})
    await (await started(db)).close()
    execute(db, "UPDATE rule_version SET body = '{oops' WHERE author = 'project_rules'")
    service = await started(db)
    try:
        rules = await service.rules("acme-shop")
        assert rules.body["guidance"].endswith("Kept.")
        versions = await service.rule_versions("acme-shop")
        assert [v.number for v in versions] == [2, 1]  # the good row became a new version
        assert versions[1].body_error and not versions[0].body_error
    finally:
        await service.close()


def test_settings_rules_shows_a_broken_version(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    settings = load_settings(home=tmp_path, environ={})
    token = load_or_create_token(settings.token_path)

    async def seed() -> None:
        service = await started(db)
        proposal = await service.propose_rules("global", PATCH, "why", session="agent")
        await service.decide_rule_proposal(proposal.id, True, actor="owner")
        await service.close()

    asyncio.run(seed())
    execute(db, "UPDATE rule_version SET body = '{oops' WHERE number = 2")
    with TestClient(create_app(settings), base_url=BASE) as board:
        board.get(f"/login?token={token}", follow_redirects=False)
        page = board.get("/settings/rules")
    assert page.status_code == 200
    assert "Broken version: rule_version #2 (global v2)" in page.text
    assert "v1 is in force" in page.text and "Roll back to v2" not in page.text


async def test_a_release_changing_the_defaults_replaces_nothing(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db, rules=DEFAULT_RULES, rules_table={})
    proposal = await service.propose_rules("global", PATCH, "why", session="agent")
    await service.decide_rule_proposal(proposal.id, True, actor="owner")
    await service.close()

    newer = config_rules("Reworded by a release.")
    service = await started(db, rules=newer, rules_table={})
    try:
        assert service.global_rules.global_version == 2
        assert service.global_rules.body["guidance"] == "Report often."
        assert len(await service.rule_versions("global")) == 2
    finally:
        await service.close()


def test_settings_hold_the_raw_rules_table(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).rules_table == {}
    (tmp_path / "config.toml").write_text('[rules]\nguidance = "Mine."\n', encoding="utf-8")
    settings = load_settings(home=tmp_path, environ={})
    assert settings.rules_table == {"guidance": "Mine."}
    assert settings.rules["guidance"] == "Mine."


async def test_a_project_called_global_does_not_share_the_global_set(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db, coordinators=["lead"])
    await service.upsert_project("global", name="G")
    await service.close()
    await seed_project_rules(db, "global", {"guidance": "Only this project."})
    for _ in range(2):
        service = await started(db, coordinators=["lead"])
        assert [v.author for v in await service.rule_versions("global")] == ["config"]
        assert service.global_rules.body["guidance"] == DEFAULT_RULES["guidance"]
        project = await service.rules("global")
        assert project.body["guidance"].endswith("Only this project.")
        assert project.project_version == 1
        await service.close()

    service = await started(db, coordinators=["lead"])
    try:
        assert await service.rule_scopes() == ["global", "project:global"]
        assert await service.update_rules("global", PATCH, "why", session="lead") == ("global", 2)
        scope = "project:global"
        patch = {"guidance": "Other."}
        assert await service.update_rules(scope, patch, "why", session="lead") == (scope, 2)
        assert service.global_rules.body["guidance"] == "Report often."
        assert (await service.rules("global")).body["guidance"].endswith("Other.")
    finally:
        await service.close()


# --- rules_update ------------------------------------------------------------------------------


async def test_without_a_coordinator_every_change_is_a_proposal(service: Service) -> None:
    with pytest.raises(ValidationError, match=r"No coordinator.*rules_propose"):
        await service.update_rules("global", PATCH, "why", session="agent")
    assert service.global_rules.global_version == 1


async def test_a_desktop_entry_needs_the_sessions_own_hook_id(tmp_path: Path) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=[DESKTOP])
    try:
        await service.upsert_project("acme-shop")
        task = await service.create_task("acme-shop", "Work", session=DESKTOP)
        # The id alone is printed by task_list, so it proves nothing.
        with pytest.raises(ValidationError, match="hook_session"):
            await service.update_rules("global", PATCH, "why", session=DESKTOP)
        # The call's own link does not count, and leaves no link behind.
        with pytest.raises(ValidationError, match="hook_session"):
            await service.update_rules(
                "global", PATCH, "why", session=DESKTOP, hook_session=HOOK_ID
            )
        assert (await service._known_session(DESKTOP)).cc_session_id is None

        await service.add_report(task.key, "changed · S · x", session=DESKTOP, hook_session=HOOK_ID)
        with pytest.raises(ValidationError, match="hook_session"):
            await service.update_rules("global", PATCH, "why", session=DESKTOP, hook_session="n")
        with pytest.raises(ValidationError, match="hook_session"):
            await service.update_rules(
                "global", PATCH, "why", session="other", hook_session=HOOK_ID
            )
        made = await service.update_rules(
            "global", PATCH, "why", session=DESKTOP, hook_session=HOOK_ID
        )
        assert made == ("global", 2)
    finally:
        await service.close()


async def test_a_title_cannot_claim_a_name_entry_in_the_same_call(tmp_path: Path) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    try:
        with pytest.raises(ValidationError, match="coordinator"):
            await service.update_rules(
                "global", PATCH, "why", session=DESKTOP, session_title="lead"
            )
    finally:
        await service.close()


def test_a_direct_update_is_a_notice_in_the_inbox_and_in_activity(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["lead"]\n', encoding="utf-8")
    settings = load_settings(home=tmp_path, environ={})
    token = load_or_create_token(settings.token_path)
    with TestClient(create_app(settings), base_url=BASE) as board:
        board.get(f"/login?token={token}", follow_redirects=False)
        service: Service = board.app.state.service
        evil = "<script>alert(1)</script>"
        run_in_app(board, service.update_rules, "global", PATCH, f"why {evil}", session="lead")
        inbox = board.get("/inbox").text
        assert "Rules changed directly" in inbox and evil not in inbox
        assert "/settings/rules?scope=global#rule-version-2" in inbox
        assert "rules.updated" in board.get("/activity").text
        assert "rules changed directly: global v2" in run_in_app(board, service.portfolio_digest)

        version = run_in_app(board, service.rule_versions, "global")[0]
        assert post(board, token, f"/settings/rules/notices/{version.id}/read").status_code == 200
        assert "Rules changed directly" not in board.get("/inbox").text
        assert "rules.notice_read" in board.get("/activity").text
        assert post(board, token, "/settings/rules/notices/999/read").status_code == 404
        assert (
            board.post(f"/settings/rules/notices/{version.id}/read", headers=ORIGIN).status_code
            == 403
        )

        # Proposals and rollbacks are the owner's own decision: no notice.
        proposal = run_in_app(board, service.propose_rules, "global", {"guidance": "A."}, "x")
        run_in_app(board, service.decide_rule_proposal, proposal.id, True)
        assert "Rules changed directly" not in board.get("/inbox").text


@pytest.mark.parametrize("entry", [DESKTOP, "lead", "the-boss"])
async def test_a_coordinator_is_matched_by_desktop_id_name_or_alias(
    tmp_path: Path, entry: str
) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=[entry])
    try:
        await service.upsert_project("acme-shop")
        task = await service.create_task("acme-shop", "Checkout")
        await service.bind_session(task.key, DESKTOP, session_title="lead")
        await service.bind_session(task.key, "the-boss")  # the name becomes the session's alias
        await service.add_report(task.key, "changed · S · x", session=DESKTOP, hook_session=HOOK_ID)
        scope, number = await service.update_rules(
            "global", PATCH, "shorter", session=DESKTOP, session_title="lead", hook_session=HOOK_ID
        )
        assert (scope, number) == ("global", 2)
        assert service.global_rules.body["guidance"] == "Report often."
        assert (await service.rule_versions("global"))[0].author == "lead"
        # Project rules are merged on the new global ones right away.
        assert (await service.rules("acme-shop")).body["guidance"] == "Report often."
    finally:
        await service.close()


async def test_a_non_coordinator_is_pointed_to_rules_propose(tmp_path: Path) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=[DESKTOP])
    try:
        for session in ("someone", None):
            with pytest.raises(ValidationError, match="rules_propose"):
                await service.update_rules("global", PATCH, "why", session=session)
        # A refused call leaves no trace, not even the session.
        assert await repo.get_session_by_name(service._r, "someone") is None
    finally:
        await service.close()


@pytest.mark.parametrize(
    ("scope", "patch", "reason", "message"),
    [
        ("global", {"nonsense": 1}, "why", "unknown rules key"),
        ("global", {"stage_requirements": {"qa": []}}, "why", "unknown stage"),
        ("global", {"report_format": {"states": ["typed"]}}, "why", "unknown value"),
        ("global", {}, "why", "non-empty"),
        ("global", [], "why", "non-empty"),
        ("global", PATCH, "", "reason"),
        ("global", {"guidance": f"token={FAKE_TOKEN}"}, "why", "guidance"),
        ("global", {"description_template": FAKE_TOKEN}, "why", "description_template"),
        ("global", {"report_format": {"first_line": FAKE_TOKEN}}, "why", "first_line"),
        ("global", PATCH, f"see {FAKE_TOKEN}", "reason"),
        ("acme-shop/live", PATCH, "why", "scope"),
    ],
)
async def test_an_invalid_change_is_refused_with_the_reason(
    tmp_path: Path, scope: str, patch: object, reason: str, message: str
) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    try:
        await service.upsert_project("acme-shop")
        with pytest.raises(ValidationError, match=message):
            await service.update_rules(scope, patch, reason, session="lead")
        with pytest.raises(ValidationError, match=message):
            await service.propose_rules(scope, patch, reason, session="agent")
        assert await service.rule_proposals() == []
        assert service.global_rules.global_version == 1
    finally:
        await service.close()


async def test_a_patch_merges_on_top_of_the_current_version(tmp_path: Path) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    try:
        await service.upsert_project("acme-shop")
        task = await service.create_task("acme-shop", "Checkout")

        await service.update_rules(
            "acme-shop", {"stage_requirements": {"test": []}}, "a", session="lead"
        )
        await service.update_rules("acme-shop", {"guidance": "Demo."}, "b", session="lead")
        rules = await service.rules("acme-shop")
        assert rules.requirements("test") == [] and rules.body["guidance"].endswith("Demo.")
        assert rules.version_text == "Rules v3 (global v1, project v2)"
        # Written through to the row the rules are read from, and in force for checks at once.
        saved = await service.set_stage_checked(task.key, "test")
        assert saved.warnings == []

        # null removes the override: the project follows the global rules again.
        await service.update_rules(
            "acme-shop", {"stage_requirements": {"test": None}}, "c", session="lead"
        )
        assert (await service.rules("acme-shop")).requirements("test")
        # On the global rules null goes back to the built-in default.
        await service.update_rules("global", PATCH, "d", session="lead")
        await service.update_rules("global", {"guidance": None}, "e", session="lead")
        assert service.global_rules.body["guidance"] == DEFAULT_RULES["guidance"]

        with pytest.raises(ValidationError, match="changes nothing"):
            await service.update_rules("acme-shop", {"guidance": "Demo."}, "f", session="lead")
    finally:
        await service.close()


# --- proposals and rollback --------------------------------------------------------------------


async def test_accept_checks_again_against_the_current_version(service: Service) -> None:
    await service.upsert_project("acme-shop")
    first = await service.propose_rules("acme-shop", PATCH, "why", session="agent")
    second = await service.propose_rules("acme-shop", PATCH, "same", session="other")
    assert [p.id for p in (await service.inbox(["owner"])).rule_proposals] == [first.id, second.id]
    assert (await service.rules("acme-shop")).project_version == 0  # nothing changed yet

    accepted = await service.decide_rule_proposal(first.id, True, actor="owner")
    assert (accepted.status, accepted.version, accepted.decided_by) == ("accepted", 1, "owner")
    assert (await service.rule_versions("acme-shop"))[0].author == "agent, accepted by owner"

    # The second one was fine when written; on top of the first it changes nothing.
    with pytest.raises(ValidationError, match="changes nothing"):
        await service.decide_rule_proposal(second.id, True, actor="owner")
    assert (await service.rule_proposal(second.id)).status == "open"

    rejected = await service.decide_rule_proposal(second.id, False, actor="owner")
    assert rejected.status == "rejected" and rejected.version is None
    assert [p.id for p in await service.decided_rule_proposals("acme-shop")] == [
        second.id,
        first.id,
    ]
    assert (await service.inbox(["owner"])).rule_proposals == []
    with pytest.raises(ConflictError, match="already rejected"):
        await service.decide_rule_proposal(second.id, True)


async def test_a_proposal_that_became_invalid_is_refused_on_accept(service: Service) -> None:
    proposal = await service.propose_rules("global", PATCH, "why", session="agent")
    with sqlite3.connect(service.db_path) as raw:
        raw.execute(
            "UPDATE rule_proposal SET patch = ? WHERE id = ?",
            (json.dumps({"nonsense": 1}), proposal.id),
        )
    raw.close()
    with pytest.raises(ValidationError, match="unknown rules key"):
        await service.decide_rule_proposal(proposal.id, True)
    assert (await service.rule_proposal(proposal.id)).status == "open"
    assert service.global_rules.global_version == 1


async def test_rollback_makes_a_new_version_equal_to_the_old_one(service: Service) -> None:
    original = service.global_rules.body
    proposal = await service.propose_rules("global", PATCH, "why", session="agent")
    await service.decide_rule_proposal(proposal.id, True, actor="owner")

    with pytest.raises(ConfirmationNeeded, match="v1"):
        await service.rollback_rules("global", 1)
    assert service.global_rules.global_version == 2

    scope, made = await service.rollback_rules("global", 1, confirmed=True, actor="owner")
    assert (scope, made) == ("global", 3)
    assert service.global_rules.body == original and service.global_rules.global_version == 3
    newest = (await service.rule_versions("global"))[0]
    assert newest.reason == "Rolled back to v1." and newest.author == "owner"

    with pytest.raises(ConflictError, match="already equal"):
        await service.rollback_rules("global", 1, confirmed=True)
    assert await service.rollback_rules("global", 2, confirmed=True) == ("global", 4)


async def test_a_secret_cannot_come_in_as_a_key_removed_by_null(service: Service) -> None:
    for patch in (
        {FAKE_TOKEN: None, "guidance": "Changed."},
        {"report_format": {FAKE_TOKEN: None}, "guidance": "Changed."},
        {"stage_requirements": {FAKE_TOKEN: None}, "guidance": "Changed."},
    ):
        with pytest.raises(ValidationError, match="unknown") as refused:
            await service.propose_rules("global", patch, "why", session="agent")
        assert FAKE_TOKEN not in str(refused.value)
    assert await service.rule_proposals() == []


async def test_a_proposal_shows_the_diff_against_the_current_version(service: Service) -> None:
    await service.upsert_project("acme-shop")
    first = await service.propose_rules("acme-shop", PATCH, "one", session="agent")
    second = await service.propose_rules("acme-shop", {"guidance": "Other."}, "two", session="a")
    third = await service.propose_rules("acme-shop", PATCH, "three", session="agent")
    diffs = await service.rule_proposal_diffs()
    assert '+  "guidance": "Report often."' in diffs[first.id]

    await service.decide_rule_proposal(first.id, True, actor="owner")
    diffs = await service.rule_proposal_diffs()
    assert first.id not in diffs
    assert '-  "guidance": "Report often."' in diffs[second.id]
    assert '+  "guidance": "Other."' in diffs[second.id]
    assert diffs[third.id].startswith("No longer applies:")


# --- report format and tracker log -------------------------------------------------------------


async def test_a_report_without_the_format_warns_and_strict_refuses(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db)
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    with collect_notes() as notes:
        _, report = await service.add_report(task.key, "did some work")
    assert report.summary == "did some work"  # saved as before
    assert len(notes) == 1 and notes[0].startswith("Warning (rules): the first line of a report")
    assert "state · size · gist" in notes[0]
    with collect_notes() as notes:
        await service.add_report(task.key, "verified locally · M · ok (verified: pytest)")
    assert notes == []
    await service.close()

    service = await started(db, rules_mode="strict")
    try:
        before = await service.last_reports([await service.get_task(task.key)])
        with pytest.raises(ValidationError, match=r"first line of a report.*Nothing changed"):
            await service.add_report(task.key, "did some work")
        assert await service.last_reports([await service.get_task(task.key)]) == before
        await service.add_report(task.key, "verified · M · ok")
    finally:
        await service.close()


async def test_the_report_format_comes_from_the_rules(tmp_path: Path) -> None:
    narrow = {"report_format": {"states": ["verified"], "sizes": ["L"]}}
    service = await started(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    try:
        await service.upsert_project("acme-shop")
        task = await service.create_task("acme-shop", "Checkout")
        await service.update_rules("acme-shop", narrow, "narrow", session="lead")
        with collect_notes() as notes:
            await service.add_report(task.key, "changed · S · not allowed here")
        assert "says changed · S" in notes[0]
    finally:
        await service.close()


async def test_require_tracker_log_on_the_way_to_done(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db, require_tracker_log=True)
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", progress="All of it")
    saved = await service.set_stage_checked(task.key, "done")
    assert len(saved.warnings) == 1 and "tracker_logged" in saved.warnings[0]
    assert saved.task.stage == "done"
    assert "tracker log before done" in fmt.rules_line(await service.rules("acme-shop"))
    await service.close()

    service = await started(db, require_tracker_log=True, rules_mode="strict")
    try:
        other = await service.create_task("acme-shop", "Payment", progress="All of it")
        with pytest.raises(ValidationError, match="tracker_logged"):
            await service.set_stage_checked(other.key, "done")
        fine = await service.update_task_checked(
            other.key, {"stage": "done", "tracker_logged": True}
        )
        assert fine.task.stage == "done" and fine.warnings == []
    finally:
        await service.close()

    # As a rule kind it needs no config key.
    service = await started(db, coordinators=["lead"])
    try:
        rule = {"stage_requirements": {"done": [{"kind": "require_tracker_log"}]}}
        await service.update_rules("acme-shop", rule, "log first", session="lead")
        third = await service.create_task("acme-shop", "Search")
        assert (await service.set_stage_checked(third.key, "done")).warnings
    finally:
        await service.close()


async def test_a_custom_first_line_is_not_checked(tmp_path: Path) -> None:
    service = await started(
        tmp_path / "relaybay.sqlite", rules_mode="strict", coordinators=["lead"]
    )
    try:
        await service.upsert_project("acme-shop")
        custom = {"report_format": {"first_line": "gist (state, size)"}}
        await service.update_rules("global", custom, "ours", session="lead")
        task = await service.create_task("acme-shop", "Checkout")
        await service.update_task(task.key, {"size": "S"})
        with collect_notes() as notes:
            await service.add_report(task.key, "fixed the cart (changed, S)")
        assert notes == []
        # The report counts, so what moving to test needs stays satisfiable in strict mode.
        await service.set_stage(task.key, "dev")
        await service.add_report(task.key, "fixed the rest (verified, S)")
        moved = await service.set_stage_checked(task.key, "test")
        assert moved.task.stage == "test"
    finally:
        await service.close()


async def test_a_tracker_warning_gives_one_inbox_row_and_goes_with_the_mark(
    tmp_path: Path,
) -> None:
    service = await started(tmp_path / "relaybay.sqlite", require_tracker_log=True)
    try:
        await service.upsert_project("acme-shop")
        task = await service.create_task("acme-shop", "Checkout", progress="All")
        assert (await service.set_stage_checked(task.key, "done")).warnings
        inbox = await service.inbox(["owner"], require_tracker_log=True)
        assert (inbox.count, len(inbox.rule_warnings), len(inbox.not_logged)) == (1, 0, 1)
        await service.update_task(task.key, {"tracker_logged": True})
        inbox = await service.inbox(["owner"], require_tracker_log=True)
        assert (inbox.count, len(inbox.rule_warnings), len(inbox.not_logged)) == (0, 0, 0)

        # Another requirement of the same move still shows.
        other = await service.create_task("acme-shop", "Payment")
        await service.set_stage(other.key, "done")
        inbox = await service.inbox(["owner"], require_tracker_log=True)
        warned = {t.key: w for t, w in inbox.rule_warnings}
        assert len(warned[other.key]) == 1 and "progress" in warned[other.key][0]
    finally:
        await service.close()


async def test_a_failing_refresh_never_fails_the_start_or_a_committed_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def gone() -> None:
        raise AttributeError("no attribute _lowlevel_server")

    service = Service(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    service.rules_changed = gone
    with caplog.at_level("WARNING", logger="relaybay"):
        await service.start()
        await service.update_rules("global", PATCH, "why", session="lead")
    try:
        assert "no attribute _lowlevel_server" in caplog.text
        assert service.global_rules.global_version == 2
    finally:
        await service.close()


async def test_refreshing_instructions_survives_an_sdk_change(
    service: Service, caplog: pytest.LogCaptureFixture
) -> None:
    mcp = build_mcp(service)
    mcp._lowlevel_server = None
    with caplog.at_level("WARNING", logger="relaybay"):
        service.rules_changed()
    assert "SDK change" in caplog.text


async def test_a_failing_hint_never_fails_a_committed_write(
    service: Service, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: object) -> None:
        raise RuntimeError("hint went wrong")

    monkeypatch.setattr(service, "note_rules_update", broken)
    mcp = build_mcp(service)
    with caplog.at_level("ERROR", logger="relaybay"):
        result = await mcp.call_tool("project_upsert", {"slug": "acme-shop", "session": "agent"})
    assert "acme-shop" in str(result)
    assert "rules-updated hint" in caplog.text
    assert (await service.get_project("acme-shop")).slug == "acme-shop"


async def test_a_malformed_session_gets_no_hint_and_no_error(service: Service) -> None:
    await service.note_rules_update("local_bad")


def test_a_bad_coordinators_entry_stops_the_load_naming_the_key(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["local_abc"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"coordinators.*local_abc"):
        load_settings(home=tmp_path, environ={})
    with pytest.raises(ValueError, match=r"coordinators.*local_abc"):
        load_settings(home=tmp_path / "other", environ={"RELAYBAY_COORDINATORS": "lead, local_abc"})


def test_a_refused_demo_changes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"

    async def seed() -> None:
        service = await started(db, rules=config_rules("From config."))
        await service.upsert_project("acme-shop")
        await service.close()

    asyncio.run(seed())
    with contextlib.closing(sqlite3.connect(db)) as raw:
        raw.execute("DROP TABLE saved_filter")  # as if 0019 were still to come
        raw.execute("PRAGMA user_version = 18")
        raw.commit()
    before = dump(db)

    assert asyncio.run(_has_board(db)) is True
    assert dump(db) == before
    with pytest.raises(DemoError):
        create_demo(db)
    assert dump(db) == before


async def test_has_board_on_empty_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite"
    with contextlib.closing(sqlite3.connect(empty)):
        pass
    assert await _has_board(empty) is False
    await (await started(tmp_path / "new.sqlite")).close()
    assert await _has_board(tmp_path / "new.sqlite") is False


# --- the "rules updated" line ------------------------------------------------------------------


async def hint(service: Service, session: str, project: str | None = None) -> list[str]:
    with collect_notes() as notes:
        await service.note_rules_update(session, project)
    return notes


async def test_the_updated_line_comes_once_per_version_and_session(tmp_path: Path) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    try:
        await service.upsert_project("acme-shop")
        await service.upsert_project("demo-api")
        for name in ("one", "two"):
            await service.create_task("acme-shop", "Work", session=name)
        assert await hint(service, "unknown", "acme-shop") == []  # nobody to tell
        assert await hint(service, "one", "acme-shop") == []  # first met: nothing is older
        assert await hint(service, "two") == []

        await service.update_rules("global", PATCH, "why", session="lead")
        line = "rules updated to v2: re-read relaybay://rules/acme-shop"
        assert await hint(service, "one", "acme-shop") == [line]
        assert await hint(service, "one", "acme-shop") == []  # once
        assert await hint(service, "one", "demo-api") == []  # the global change was said
        assert await hint(service, "two") == ["rules updated to v2: re-read relaybay://rules"]

        # A project's first own version reaches a session that knew the project without one.
        await service.update_rules("acme-shop", {"guidance": "Demo."}, "why", session="lead")
        assert await hint(service, "one", "acme-shop") == [
            "rules updated to v3: re-read relaybay://rules/acme-shop"
        ]
        assert await hint(service, "one", "demo-api") == []
    finally:
        await service.close()


# --- snapshot, digests, inbox ------------------------------------------------------------------


async def test_snapshot_and_digests_name_the_version(tmp_path: Path) -> None:
    service = await started(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    try:
        await service.upsert_project("acme-shop")
        await service.update_rules("acme-shop", {"guidance": "Demo."}, "why", session="lead")
        text = "Rules v2 (global v1, project v1)"
        assert fmt.snapshot(await service.snapshot("acme-shop")).startswith(text)
        assert text in await service.project_digest("acme-shop")
        assert "Rules v1 (global v1)" in await service.portfolio_digest()
        task = await service.create_task("acme-shop", "Checkout")
        assert text in await service.task_context(task.key)
        assert f"- version: {text}" in fmt.rules_text(await service.rules("acme-shop"))

        await service.propose_rules("global", PATCH, "why", session="agent")
        assert "rules proposal #1 on global" in await service.portfolio_digest()
    finally:
        await service.close()


# --- MCP ---------------------------------------------------------------------------------------


async def test_rules_tools_and_prompts_over_mcp(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", session="agent")
        await call(client, "task_create", project="acme-shop", title="Checkout", session="agent")

        refused = await client.call_tool(
            "rules_update",
            {"scope": "global", "patch": PATCH, "reason": "why", "session": "agent"},
        )
        assert refused.is_error and "rules_propose" in refused.content[0].text

        saved = await call(
            client, "rules_propose", scope="global", patch=PATCH, reason="why", session="agent"
        )
        assert "Rules proposal #1 for global recorded" in saved

        warned = await call(client, "report_add", task="acme-shop-1", body="no format here")
        assert "Warning (rules): the first line of a report" in warned

        prompts = {p.name: p for p in (await client.list_prompts()).prompts}
        assert set(prompts) == {"start_task", "finish_stage"}
        for prompt in prompts.values():
            arguments = {a.name: a.required for a in prompt.arguments}
            assert arguments == {"project": True, "task": False}

        started_text = (
            (await client.get_prompt("start_task", {"project": "acme-shop", "task": "acme-shop-1"}))
            .messages[0]
            .content.text
        )
        assert "acme-shop-1" in started_text and "Checklist:" in started_text
        assert "Rules v1 (global v1, project v0)" in started_text
        assert "plan_set" in started_text

        finished = await client.get_prompt(
            "finish_stage", {"project": "acme-shop", "task": "acme-shop-1"}
        )
        text = finished.messages[0].content.text
        assert "report_add" in text and "Moving to dev needs: an accepted plan" in text
        assert "## Stage requirements" in text

        no_task = await client.get_prompt("start_task", {"project": "acme-shop"})
        assert "pick a task or create one" in no_task.messages[0].content.text
        with pytest.raises(MCPError, match="not found"):
            await client.get_prompt("start_task", {"project": "nowhere"})
        await call(client, "project_upsert", slug="demo-api")
        with pytest.raises(MCPError, match="not in project"):
            await client.get_prompt("finish_stage", {"project": "demo-api", "task": "acme-shop-1"})


async def test_rules_update_by_a_configured_coordinator_over_mcp(
    live_server: Callable[[], LiveServer], tmp_path: Path
) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["lead"]\n', encoding="utf-8")
    live = live_server()
    assert live.settings.coordinators == ["lead"]
    async with mcp_connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", session="agent")
        refused = await client.call_tool(
            "rules_update", {"scope": "global", "patch": PATCH, "reason": "x", "session": "agent"}
        )
        assert refused.is_error and "coordinator sessions" in refused.content[0].text
        saved = await call(
            client, "rules_update", scope="global", patch=PATCH, reason="x", session="lead"
        )
        assert "Rules of global are now version 2: Rules v2 (global v2)." in saved
        assert "rules updated" not in saved.split("\n", 1)[0]

        # Another session hears about it once, with its next write.
        told = await call(client, "task_create", project="acme-shop", title="A", session="agent")
        assert "rules updated to v2: re-read relaybay://rules/acme-shop" in told
        again = await call(client, "task_create", project="acme-shop", title="B", session="agent")
        assert "rules updated" not in again


def test_coordinators_setting(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).coordinators == []
    env = {"RELAYBAY_COORDINATORS": f"lead, {DESKTOP}"}
    assert load_settings(home=tmp_path, environ=env).coordinators == ["lead", DESKTOP]
    (tmp_path / "config.toml").write_text("coordinators = [1]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="coordinators must be a list"):
        load_settings(home=tmp_path, environ={})


async def test_instructions_follow_the_current_version(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = await started(db)
    narrow = {"report_format": {"first_line": "state · size · what happened"}}
    proposal = await service.propose_rules("global", narrow, "clearer", session="agent")
    await service.decide_rule_proposal(proposal.id, True, actor="owner")
    await service.close()

    settings = load_settings(home=tmp_path, environ={})
    load_or_create_token(settings.token_path)
    app = create_app(settings)
    with TestClient(app, base_url=BASE):
        assert "state · size · what happened" in app.state.mcp.instructions


# --- Settings → Rules --------------------------------------------------------------------------


def post(board: TestClient, token: str, path: str, **fields: object):
    return board.post(
        path,
        data={CSRF_FIELD: csrf_token(token), **fields},
        headers=ORIGIN,
        follow_redirects=True,
    )


def test_settings_rules_page_shows_history_and_escapes_agent_text(
    board: TestClient, token: str
) -> None:
    service: Service = board.app.state.service
    evil = "<script>alert(1)</script>"

    async def seed() -> None:
        await service.upsert_project("acme-shop")
        accepted = await service.propose_rules(
            "global", {"guidance": f"Careful {evil}"}, f"why {evil}", session=f"agent {evil}"
        )
        await service.decide_rule_proposal(accepted.id, True, actor="owner")
        await service.propose_rules("acme-shop", PATCH, f"open {evil}", session="agent")

    run_in_app(board, seed)
    page = board.get("/settings/rules")
    assert page.status_code == 200
    assert "Rules v2 (global v2)" in page.text
    assert 'href="/settings/rules"' in page.text and "<h1>Rules</h1>" in page.text
    assert "Roll back to v1" in page.text and "Roll back to v2" not in page.text
    assert evil not in page.text and "&lt;script&gt;" in page.text
    assert 'id="rule-proposal-2"' in page.text

    inbox = board.get("/inbox")
    assert "Rules proposals" in inbox.text and evil not in inbox.text
    assert "/settings/rules?scope=acme-shop#rule-proposal-2" in inbox.text

    fragment = board.get("/settings/rules/proposals")
    assert 'id="rule-proposal-2"' in fragment.text and evil not in fragment.text

    project = board.get("/settings/rules?scope=acme-shop")
    assert "Rules v2 (global v2, project v0)" in project.text
    assert "no rules of its own" in project.text


def test_settings_rules_accept_reject_and_rollback_with_a_confirm(
    board: TestClient, token: str
) -> None:
    service: Service = board.app.state.service

    async def seed() -> None:
        await service.upsert_project("acme-shop")
        await service.propose_rules("acme-shop", PATCH, "one", session="agent")
        await service.propose_rules("acme-shop", {"guidance": "Other."}, "two", session="agent")

    run_in_app(board, seed)
    page = post(board, token, "/settings/rules/proposals/1/accept")
    assert "Rules proposal #1 accepted as v1." in page.text
    assert page.url.path == "/settings/rules" and page.url.query == b"scope=acme-shop"
    page = post(board, token, "/settings/rules/proposals/2/reject")
    assert "Rules proposal #2 rejected." in page.text
    assert "Decided proposals" in page.text

    page = post(board, token, "/settings/rules/proposals/2/accept")
    assert page.status_code == 400 and "already rejected" in page.text
    assert post(board, token, "/settings/rules/proposals/99/accept").status_code == 404
    assert post(board, token, "/settings/rules/proposals/1/maybe").status_code == 404

    # A second version to roll back from.
    async def again() -> None:
        proposal = await service.propose_rules("global", PATCH, "three", session="agent")
        await service.decide_rule_proposal(proposal.id, True, actor="owner")

    run_in_app(board, again)
    asked = post(board, token, "/settings/rules/rollback", scope="global", number=1)
    assert "Roll the global rules back to v1?" in asked.text
    assert 'name="confirm_live" value="1"' in asked.text
    assert board.app.state.service.global_rules.global_version == 2

    done = post(board, token, "/settings/rules/rollback", scope="global", number=1, confirm_live=1)
    assert "The global rules are now v3, equal to v1." in done.text
    assert service.global_rules.global_version == 3


def test_settings_rules_writes_need_the_csrf_token_and_the_cookie(
    board: TestClient, token: str
) -> None:
    answer = board.post(
        "/settings/rules/rollback", data={"scope": "global", "number": 1}, headers=ORIGIN
    )
    assert answer.status_code == 403
    bearer = board.post(
        "/settings/rules/rollback",
        data={"scope": "global", "number": 1, CSRF_FIELD: csrf_token(token)},
        headers={**ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
