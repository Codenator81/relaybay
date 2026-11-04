"""Working rules: defaults, config and project overrides, the hash, read access and warn checks."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.config import load_settings
from relaybay.db.connection import connect
from relaybay.domain.errors import ValidationError
from relaybay.domain.rules import (
    DEFAULT_RULES,
    Rules,
    merge_project_rules,
    merge_rules,
    validate_rules,
)
from relaybay.mcp_server import format as fmt
from relaybay.mcp_server.server import instructions
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import LiveServer, run_in_app
from tests.test_board import HTMX
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect

OLD = "2000-01-01T00:00:00.000+00:00"
REPORT_WARNING = (
    "Warning (rules): moving to test usually needs a report since the last stage change."
)
PROGRESS_WARNING = "Warning (rules): moving to done usually needs the progress field."


async def seed_project_rules(db: Path, slug: str, body: object) -> None:
    """Store a project override the way an owner would by hand (no write tool in this stage)."""
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO project_rules (project_id, body, updated_at)"
            " SELECT id, ?, ? FROM project WHERE slug = ?",
            (body if isinstance(body, str) else json.dumps(body), OLD, slug),
        )
    finally:
        await conn.close()


async def age_reports(db: Path, key: str) -> None:
    """Date the task's reports far back, i.e. before any stage change of the test."""
    conn = await connect(db)
    try:
        await conn.execute(
            "UPDATE report SET created_at = ? WHERE task_id = (SELECT id FROM task WHERE key = ?)",
            (OLD, key),
        )
    finally:
        await conn.close()


# --- the document ------------------------------------------------------------


def test_defaults_require_a_report_before_test_and_progress_before_done() -> None:
    rules = Rules(DEFAULT_RULES)
    assert rules.requirements("test") == [{"kind": "report_since_stage_change"}]
    assert rules.requirements("done") == [{"kind": "field_present", "field": "progress"}]
    assert rules.requirements("dev") == [{"kind": "plan_accepted", "unless_size": ["S"]}]
    fmt_rules = DEFAULT_RULES["report_format"]
    assert fmt_rules["states"][0] == "inspected" and fmt_rules["sizes"] == ["S", "M", "L"]
    assert fmt.rules_line(rules) == (
        f"Rules {rules.hash}: accepted plan before dev, report before test, progress before done"
        " (read relaybay://rules)"
    )


def test_hash_is_short_and_stable() -> None:
    first = Rules(DEFAULT_RULES).hash
    reordered = json.loads(json.dumps(DEFAULT_RULES, sort_keys=True))
    assert len(first) == 8 and Rules(reordered).hash == first
    changed = merge_rules(DEFAULT_RULES, {"guidance": "Other"})
    assert Rules(changed).hash != first
    assert Rules(DEFAULT_RULES, project="acme-shop").hash == first  # the body decides
    assert Rules(DEFAULT_RULES, mode="strict").hash != first  # and the mode


def test_merge_per_stage_and_guidance() -> None:
    override = validate_rules(
        {
            "stage_requirements": {"test": [], "live": [{"kind": "report_since_stage_change"}]},
            "report_format": {"sizes": ["S", "M"]},
            "guidance": "Project text.",
        },
        "test",
    )
    merged = merge_rules(DEFAULT_RULES, override)
    assert merged["stage_requirements"]["test"] == []
    assert merged["stage_requirements"]["done"] == DEFAULT_RULES["stage_requirements"]["done"]
    assert merged["report_format"]["sizes"] == ["S", "M"]
    assert merged["report_format"]["states"] == DEFAULT_RULES["report_format"]["states"]
    assert merged["guidance"] == "Project text."
    project = merge_project_rules(DEFAULT_RULES, override)
    assert project["guidance"] == DEFAULT_RULES["guidance"] + "\n\nProject text."
    assert DEFAULT_RULES["stage_requirements"]["test"]  # the defaults are not mutated


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"colour": 1}, "unknown rules key(s) colour"),
        ({"stage_requirements": {"qa": []}}, "unknown stage 'qa'"),
        ({"stage_requirements": {"test": {}}}, "stage_requirements.test must be a list"),
        ({"stage_requirements": {"test": [{"kind": "x"}]}}, "test[0].kind must be one of"),
        ({"stage_requirements": {"done": [{"kind": "field_present"}]}}, "field must be a task"),
        (
            {"stage_requirements": {"test": [{"kind": "report_since_stage_change", "a": 1}]}},
            "unknown key(s) for report_since_stage_change: a",
        ),
        ({"report_format": {"states": ["shipped"]}}, "unknown value(s) shipped"),
        ({"report_format": {"sizes": []}}, "sizes must be a non-empty list"),
        ({"guidance": 3}, "guidance must be a string"),
        ([], "rules must be a table/object"),
    ],
)
def test_validation_errors_name_the_source_and_key(data: object, message: str) -> None:
    with pytest.raises(ValueError, match=r"^cfg: ") as caught:
        validate_rules(data, "cfg")
    assert message in str(caught.value)


# --- config --------------------------------------------------------------------


def test_config_overrides_global_rules_and_mode(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'rules_mode = "strict"\n'
        "[rules]\n"
        'guidance = "Ask the owner before live."\n'
        'description_template = "Goal:\\nWhy:"\n'
        "[rules.stage_requirements]\n"
        "test = []\n"
        'live = [{ kind = "field_present", field = "branch" }]\n',
        encoding="utf-8",
    )
    settings = load_settings(home=tmp_path, environ={})
    assert settings.rules_mode == "strict"
    rules = Rules(settings.rules)
    assert rules.requirements("test") == []
    assert rules.requirements("live") == [{"kind": "field_present", "field": "branch"}]
    assert rules.requirements("done") == DEFAULT_RULES["stage_requirements"]["done"]
    assert settings.rules["guidance"] == "Ask the owner before live."
    assert settings.rules["description_template"] == "Goal:\nWhy:"
    env = load_settings(home=tmp_path, environ={"RELAYBAY_RULES_MODE": "warn"})
    assert env.rules_mode == "warn"
    assert load_settings(home=tmp_path / "none", environ={}).rules == DEFAULT_RULES


def test_bad_rules_config_fails_at_startup(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("[rules.stage_requirements]\nqa = []\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"config\.toml \[rules\]: stage_requirements"):
        load_settings(home=tmp_path, environ={})
    (tmp_path / "config.toml").write_text('rules_mode = "loud"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="rules_mode must be one of warn, strict"):
        load_settings(home=tmp_path, environ={})


# --- service -------------------------------------------------------------------


async def test_project_override_is_merged(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    await seed_project_rules(
        service.db_path,
        "acme-shop",
        {"stage_requirements": {"test": []}, "guidance": "Acme: demo every Friday."},
    )
    acme = await service.rules("acme-shop")
    assert acme.project == "acme-shop" and acme.requirements("test") == []
    assert acme.body["guidance"].endswith("Acme: demo every Friday.")
    assert acme.hash != service.global_rules.hash
    demo = await service.rules("demo-api")
    assert demo.hash == service.global_rules.hash and demo.requirements("test")
    assert fmt.rules_line(acme) == (
        f"Rules v1 (global v1, project v0), hash {acme.hash}: accepted plan before dev, progress"
        " before done"
        " (read relaybay://rules/acme-shop)"
    )

    task = await service.create_task("acme-shop", "Checkout", stage="dev")
    saved = await service.set_stage_checked(task.key, "test")
    assert saved.warnings == []  # the project dropped the report rule


async def test_broken_project_override_falls_back_to_global_rules(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "relaybay.sqlite"
    svc = Service(db)
    await svc.start()
    await svc.upsert_project("acme-shop")
    await svc.upsert_project("demo-api")
    await svc.close()
    await seed_project_rules(db, "acme-shop", {"stage_requirements": {"qa": []}})
    await seed_project_rules(db, "demo-api", "{not json")

    svc = Service(db)
    with caplog.at_level("WARNING", logger="relaybay"):
        await svc.start()  # the server still starts
    try:
        assert "project_rules for 'acme-shop': stage_requirements" in caplog.text
        rules = await svc.rules("acme-shop")
        assert rules.body == svc.global_rules.body and rules.hash == svc.global_rules.hash
        error = rules.override_error or ""
        assert error.startswith("project_rules for 'acme-shop': stage_requirements")
        line = fmt.rules_line(rules)
        assert line.endswith(f"; project override ignored: {error}")
        assert f"- project override ignored: {error}" in fmt.rules_text(rules)
        assert fmt.snapshot(await svc.snapshot("acme-shop")).splitlines()[0] == line

        # The project keeps working under the global rules.
        task = await svc.create_task("acme-shop", "Checkout", stage="dev")
        saved = await svc.set_stage_checked(task.key, "test")
        assert saved.task.stage == "test" and saved.warnings == [REPORT_WARNING]
        caplog.clear()
        with caplog.at_level("WARNING", logger="relaybay"):
            await svc.rules("acme-shop")
        assert caplog.text == ""  # logged once, not on every read
        demo = await svc.rules("demo-api")
        assert "project_rules for 'demo-api': not valid JSON" in (demo.override_error or "")
    finally:
        await svc.close()


async def test_warnings_for_test_without_a_fresh_report(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="dev")
    await service.add_report(task.key, "changed · S · first pass")
    await age_reports(service.db_path, task.key)  # reported before the move to dev

    saved = await service.set_stage_checked(task.key, "test", note="ready")
    assert saved.task.stage == "test" and saved.task.stage_note == "ready"  # the write succeeds
    assert saved.warnings == [REPORT_WARNING]
    events = await service.events_since(0)
    moved = [e for e in events if e.kind == "task.stage_changed"][-1]
    assert moved.payload["warnings"] == [REPORT_WARNING]

    await service.set_stage(task.key, "dev")
    await service.add_report(task.key, "verified · S · fixed")
    again = await service.update_task_checked(task.key, {"stage": "test"})
    assert again.warnings == []
    moved = [e for e in await service.events_since(0) if e.kind == "task.stage_changed"][-1]
    assert "warnings" not in moved.payload


async def test_warnings_for_done_without_progress(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="live")
    saved = await service.update_task_checked(task.key, {"stage": "done"})
    assert saved.task.stage == "done" and saved.warnings == [PROGRESS_WARNING]

    other = await service.create_task("acme-shop", "Payment", stage="live")
    with_progress = await service.update_task_checked(
        other.key, {"stage": "done", "progress": "- cards\n- retries"}
    )
    assert with_progress.warnings == []
    # No stage change, no check.
    assert (await service.update_task_checked(task.key, {"branch": "feat/x"})).warnings == []


async def test_strict_mode_refuses_the_move(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", rules_mode="strict")
    await svc.start()
    try:
        await svc.upsert_project("acme-shop")
        task = await svc.create_task("acme-shop", "Checkout", stage="live")
        with pytest.raises(ValidationError, match="moving to done requires the progress field"):
            await svc.set_stage(task.key, "done")
        fresh = await svc.get_task(task.key)
        assert (fresh.stage, fresh.version) == ("live", 1)
        await svc.update_task(task.key, {"size": "S"})  # S tasks need no accepted plan
        back = await svc.set_stage_checked(task.key, "dev")
        assert back.warnings == []
    finally:
        await svc.close()


async def test_snapshot_starts_with_the_rules_line(service: Service) -> None:
    await service.upsert_project("acme-shop", name="Acme Shop")
    text = fmt.snapshot(await service.snapshot("acme-shop"))
    first, second = text.splitlines()[:2]
    assert first == (
        f"Rules v1 (global v1, project v0), hash {service.global_rules.hash}: accepted plan"
        " before dev, report before test, progress before done (read relaybay://rules/acme-shop)"
    )
    assert second == "# acme-shop — Acme Shop"


def test_rules_markdown() -> None:
    text = fmt.rules_text(Rules(DEFAULT_RULES, project="acme-shop"))
    assert text.startswith(f"# Rules (hash {Rules(DEFAULT_RULES).hash})")
    assert "- scope: project acme-shop" in text
    assert "- mode: warn" in text
    assert "- first line: `state · size · gist`" in text
    assert "- moving to test needs a report since the last stage change" in text
    assert "- moving to done needs the progress field" in text
    assert "## Guidance" in text and "## Description template" not in text


def test_instructions_carry_the_contract() -> None:
    for phrase in (
        "project_snapshot",
        "local_",
        "next_step",
        "waiting_on",
        "progress",
        "`state · size · gist`",
        "inspected, changed, verified, committed, pushed, blocked",
        "task_patch",
        "relaybay://rules",
        "Warning (rules)",
        "`hook_session`",
    ):
        assert phrase in instructions(Rules(DEFAULT_RULES)), phrase


def test_instructions_follow_the_configured_report_format() -> None:
    rules = Rules(merge_rules(DEFAULT_RULES, {"report_format": {"sizes": ["S", "M"]}}))
    text = instructions(rules)
    assert "size is one of S, M." in text
    assert "size is one of S, M, L" not in text


# --- MCP and board ------------------------------------------------------------


async def test_mcp_rules_resource_tool_and_warning(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert "rules_get" in tools
        await call(client, "project_upsert", slug="acme-shop")
        global_text = await call(client, "rules_get")
        assert global_text.startswith("# Rules (hash ") and "- scope: global" in global_text
        assert "- scope: project acme-shop" in await call(client, "rules_get", project="acme-shop")

        resource = await client.read_resource("relaybay://rules")
        assert resource.contents[0].text == global_text
        project = (await client.read_resource("relaybay://rules/acme-shop")).contents[0].text
        assert "- scope: project acme-shop" in project

        snapshot = await call(client, "project_snapshot", project="acme-shop")
        assert snapshot.startswith("Rules ") and "relaybay://rules/acme-shop" in snapshot

        await call(client, "task_create", project="acme-shop", title="Checkout", stage="live")
        moved = await call(client, "task_set_stage", task="acme-shop-1", stage="done")
        assert moved.splitlines()[0].startswith("Moved **acme-shop-1**")
        assert moved.splitlines()[1] == PROGRESS_WARNING
        patched = await call(client, "task_patch", task="acme-shop-1", fields={"stage": "test"})
        assert patched.splitlines()[1] == REPORT_WARNING
        await call(client, "report_add", task="acme-shop-1", body="verified · S · ok")
        clean = await call(client, "task_patch", task="acme-shop-1", fields={"stage": "live"})
        assert "Warning" not in clean

        missing = await client.call_tool("rules_get", {"project": "nope"})
        assert missing.is_error and "not found" in missing.content[0].text


def test_board_save_shows_the_rule_warning(board: TestClient, seeded: str, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, age_reports, service.db_path, seeded)
    task = run_in_app(board, service.get_task, seeded)
    form = {"title": task.title, "stage": "test", "version": str(task.version)}
    form["csrf_token"] = csrf_token(token)
    response = board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert response.status_code == 200 and 'class="form-ok"' in response.text
    assert f'<p class="form-warning" role="status">{REPORT_WARNING}</p>' in response.text
    assert run_in_app(board, service.get_task, seeded).stage == "test"
