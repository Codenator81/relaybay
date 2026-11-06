"""Digests: pulse, budgets, reader cursors, SessionStart context, MCP tools and the board."""

import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from starlette.testclient import TestClient

from relaybay.config import load_settings
from relaybay.db import repo
from relaybay.db.connection import connect
from relaybay.domain import digest
from relaybay.domain import digest_limits as limits
from relaybay.domain.comments import CONTEXT_MAX
from relaybay.domain.errors import ValidationError
from relaybay.domain.hooks import parse_hook, session_start_context
from relaybay.domain.plan import Plan, PlanStep, reminder_line
from relaybay.domain.sessions import parse_session_id
from relaybay.service import Service, collect_notes
from tests.conftest import LiveServer, run_in_app
from tests.test_comments import context_of, fire, linked_task
from tests.test_hooks import CC_A, CC_B, CWD, DESKTOP_A, hook
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import NOW, make_old_db


def END(_complete: bool) -> str:
    return "End."


def ago(**delta: int) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="milliseconds")


async def fire_payload(service: Service, payload: dict[str, Any]) -> Any:
    await service.record_hook(payload)
    return await service.hook_answer(payload)


# --- budgets --------------------------------------------------------------------------------


def _sections() -> list[digest.Section]:
    return [
        digest.Section("# Head", ["state line"], "task_get", 0),
        digest.Section("## Decisions", [f"- #{n} text" for n in range(500)], "decision_list", 1),
        digest.Section("## Notes", [f"- note {n}" for n in range(500)], "memory_search", 2),
    ]


def test_lowest_priority_shrinks_first_and_says_where_the_rest_is() -> None:
    text = digest.render(_sections(), END, 3000).text
    assert len(text) <= 3000
    assert "… 500 more: memory_search" in text and "- note 0" not in text
    assert "- #0 text" in text and "more: decision_list" in text
    assert text.startswith("# Head\nstate line\nMore: task_get") and text.endswith("End.")


def test_a_section_that_fits_says_how_to_drill_down() -> None:
    sections = [digest.Section("# Head", ["x"], "task_get", 0)]
    assert digest.render(sections, END, 500).text == "# Head\nx\nMore: task_get\n\nEnd."


def test_whole_lines_are_cut_never_part_of_one() -> None:
    lines = [f"- line {n:03} " + "x" * 50 for n in range(100)]
    sections = [
        digest.Section("# Head", ["state"], "task_get", 0),
        digest.Section("## List", lines, "tool", 1),
    ]
    text = digest.render(sections, END, 1000).text
    shown = [x for x in text.split("\n") if x.startswith("- line")]
    assert shown and all(x in lines for x in shown)
    assert f"… {100 - len(shown)} more: tool" in text


def test_the_first_section_stays_when_the_others_do_not_fit() -> None:
    sections = [
        digest.Section("# Head", ["state"], "task_get", 0),
        *[digest.Section(f"## {'S' * 100}{n}", ["x"], "t" * 100, 1) for n in range(10)],
    ]
    text = digest.render(sections, END, limits.BUDGET_MIN).text
    assert len(text) <= limits.BUDGET_MIN
    assert text.startswith("# Head\nstate") and text.endswith("End.")


def test_a_cap_limits_lines_within_budget() -> None:
    section = digest.Section("## L", [f"- {n}" for n in range(50)], "events_since", 1, cap=3)
    text = digest.render([digest.Section("# H", [], "x", 0), section], END, 5000).text
    assert "- 2" in text and "- 3\n" not in text and "… 47 more: events_since" in text


@pytest.mark.parametrize("budget", [1, 500, 501, 1500, 40_000])
def test_output_never_exceeds_the_budget(budget: int) -> None:
    sections = [
        digest.Section("# " + "H" * 3000, ["y" * 5000] * 50, "t" * 3000, 0, line_max=1600),
        digest.Section("## N", ["z" * 400] * 500, "tool", 1),
    ]
    assert len(digest.render(sections, lambda _: "F" * 40, budget).text) <= budget


def test_index_lines_are_one_line() -> None:
    section = digest.Section("# Multi\nline", ["first\nsecond\r\n\tthird", "  ", "\n"], "t", 0)
    assert section.title == "# Multi line"
    assert section.lines == ["first second third"]


def test_doc_links_are_found_once_in_order() -> None:
    links = digest.doc_links(
        "See relaybay://doc/acme-shop/spec#gift-cards, and (relaybay://doc/demo-api/guide).",
        "Again relaybay://doc/acme-shop/spec#gift-cards",
        None,
    )
    assert [x.uri for x in links] == [
        "relaybay://doc/acme-shop/spec#gift-cards",
        "relaybay://doc/demo-api/guide",
    ]


async def test_hostile_task_stays_in_budget(service: Service) -> None:
    await service.upsert_project("acme-shop")
    links = " ".join(f"relaybay://doc/acme-shop/d{n}#a" for n in range(30))
    task = await service.create_task(
        "acme-shop",
        "Huge\ntitle",
        description="d\n" * 4000 + links,
        progress="p\n" * 4000,
        next_step="n\n" * 1000,
    )
    for n in range(500):
        await service.add_decision(
            "acme-shop", f"Decision {n}\nsecond line", task=task.key if n % 2 else None
        )
    for budget, limit in ((10, 500), (500, 500), (1500, 1500), (99_999, 40_000)):
        text = await service.task_context(task.key, budget=budget)
        assert len(text) <= limit
        assert text.startswith(f"# {task.key} — Huge title")
    full = await service.task_context(task.key)
    assert len(full) <= limits.DEFAULT_BUDGET
    assert "Decision 499 second line" in full  # collapsed to one line
    assert "more: decision_list" in full and "more: doc_get" in full
    project = await service.project_digest("acme-shop", budget=2000)
    assert len(project) <= 2000 and "more: decision_list" in project


# --- pulse ----------------------------------------------------------------------------------


async def test_pulse_limits_secret_scan_and_event(service: Service) -> None:
    await service.upsert_project("acme-shop")
    with pytest.raises(ValidationError, match=r"this text has 6 line\(s\) and 11 character"):
        await service.set_pulse("acme-shop", "a\nb\nc\nd\ne\nf")
    with pytest.raises(ValidationError, match=r"this text has 1 line\(s\) and 601 character"):
        await service.set_pulse("acme-shop", "x" * 601)
    with pytest.raises(ValidationError, match="must not be empty"):
        await service.set_pulse("acme-shop", "  \n ")
    with pytest.raises(ValidationError, match=r"secret|key|token"):
        await service.set_pulse("acme-shop", "password: hunter2hunter2")
    assert (await service.get_project("acme-shop")).pulse is None

    project = await service.set_pulse("acme-shop", "a\nb\r\nc\nd\ne", session="manager")
    assert project.pulse == "a\nb\nc\nd\ne" and project.pulse_by == "manager" and project.pulse_at
    event = (await service.events_since(0, "acme-shop"))[-1]
    assert (event.kind, event.actor) == ("project.pulse_set", "manager")
    assert event.payload["text"] == "a b c d e"


async def test_stage_move_notes_a_missing_or_stale_pulse_once(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    await service.set_pulse("demo-api", "Old news.", at=ago(days=8))
    first = await service.create_task("acme-shop", "One")
    second = await service.create_task("acme-shop", "Two")
    third = await service.create_task("demo-api", "Three")

    await service.set_stage(first.key, "dev")  # no MCP caller: the once is not used up
    with collect_notes() as notes:
        saved = await service.set_stage_checked(second.key, "dev")
    pulse = [n for n in notes if "pulse" in n]
    assert len(pulse) == 1 and pulse[0].startswith("Note: the pulse of acme-shop is missing")
    assert not any("pulse" in w for w in saved.warnings)
    with collect_notes() as notes:
        await service.set_stage_checked(first.key, "review")
    assert not [n for n in notes if "pulse" in n]  # once per project per run
    with collect_notes() as notes:
        await service.update_task(third.key, {"title": "Three again"})
    assert not [n for n in notes if "pulse" in n]  # only a stage move
    with collect_notes() as notes:
        await service.set_stage_checked(third.key, "dev")
    assert [n for n in notes if "demo-api is older than 7 days" in n]


async def test_stage_move_with_a_fresh_pulse_has_no_note(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.set_pulse("acme-shop", "On track.", at=ago(days=6))
    task = await service.create_task("acme-shop", "One")
    with collect_notes() as notes:
        await service.set_stage_checked(task.key, "dev")
    assert not [n for n in notes if "pulse" in n]


# --- digests --------------------------------------------------------------------------------


async def age_events(service: Service, **delta: int) -> None:
    async with service._tx() as tx:
        await tx.conn.execute("UPDATE event SET ts = ?", (ago(**delta),))


async def test_portfolio_digest_sections(service: Service) -> None:
    await service.upsert_project("acme-shop", name="Acme Shop")
    await service.upsert_project("demo-api")
    await service.set_pulse("acme-shop", "Checkout first.\nSecond line.")
    await service.set_pulse("demo-api", "Stable.", at=ago(days=30))
    old = await service.create_task("acme-shop", "Forgotten", stage="dev")
    await service.create_task("acme-shop", "Fresh", stage="review", waiting_on="Owner")
    done = await service.create_task("acme-shop", "Finished", stage="done")
    await service.add_decision("acme-shop", "Recent decision")
    last_week = await service.add_decision("acme-shop", "Old decision")
    await service.set_block("house-rules", "Short\nreports.", 0)
    async with service._tx() as tx:
        await tx.conn.execute("UPDATE task SET updated_at = ?", (ago(days=6),))
        await tx.conn.execute(
            "UPDATE task SET updated_at = ? WHERE title = 'Fresh'", (ago(hours=1),)
        )
        await tx.conn.execute(
            "UPDATE decision SET recorded_at = ? WHERE id = ?", (ago(days=8), last_week.id)
        )
    text = await service.portfolio_digest()
    lines = text.split("\n")
    assert lines[0] == "# Portfolio"
    project = next(x for x in lines if x.startswith("- **acme-shop**"))
    assert "dev 1 · review 1 · done 1" in project and "Checkout first." in project
    assert "Second line" not in project and "stale" not in project
    assert any(x.startswith("- **demo-api**") and "30d ago, stale" in x for x in lines)
    assert "waits for the owner: [review]" in text
    assert "Recent decision" in text and "Old decision" not in text
    stale = text.split("## Tasks without progress for 5 days")[1].split("##")[0]
    assert old.key in stale and "Fresh" not in stale and done.key not in stale
    assert "- **house-rules** (v1): Short reports." in text
    assert "Events up to #" in text and "pass session" in text


async def test_project_digest_sections_and_note_flags(service: Service) -> None:
    await service.upsert_project("acme-shop", name="Acme Shop")
    await service.upsert_project("demo-api")
    await service.set_pulse("acme-shop", "Line one.\nLine two.")
    await service.create_task("acme-shop", "Backlog", next_step="Think", stage="plan")
    await service.create_task("acme-shop", "Working", next_step="Build", stage="dev")
    await service.create_task("acme-shop", "Question", stage="test", waiting_on="me")
    await service.create_task("demo-api", "Elsewhere")
    await service.add_decision("acme-shop", "Current")
    await service.save_environment("acme-shop", {"name": "stage", "kind": "test"})
    await service.set_block("handover", "Read this.", 0, project="acme-shop")
    await service.add_note("project", "project", "Read one", "Body", project="acme-shop")
    await service.add_note(
        "global", "user", "Old fact", "Body", review_at="2020-01-01", force_new=True, op="add"
    )
    await service.get_note(1)

    text = await service.project_digest("acme-shop")
    assert text.startswith("# acme-shop — Acme Shop\nTasks: plan 1 · dev 1 · test 1")
    assert "Line one.\nLine two." in text
    tasks = text.split("## Tasks")[1].split("##")[0]
    assert tasks.index("Working") < tasks.index("Question") < tasks.index("Backlog")
    assert "[dev]" in tasks and "next: Build" in tasks and "Elsewhere" not in text
    questions = text.split("## Open questions")[1].split("##")[0]
    assert "Question" in questions and "Working" not in questions
    assert "- stage (test): agents " in text and "Read this." in text
    notes = text.split("## Notes")[1].split("##")[0].strip().split("\n")
    assert "Old fact" in notes[0] and "[verify]" in notes[0] and "[never read]" in notes[0]
    assert "Read one" in notes[1] and "[" not in notes[1].split("]", 1)[1]


async def test_task_context_links_comments_and_notes(service: Service) -> None:
    task = await linked_task(service, "Gift card refunds")
    body = "# Spec\n\n## Gift cards\nFirst.\nSecond.\nThird.\nFourth.\n\n## Other\nNo."
    await service.update_document("acme-shop", "spec", body, "draft", 0)
    await service.update_task(
        task.key,
        {
            "description": "See relaybay://doc/acme-shop/spec#gift-cards and"
            " relaybay://doc/acme-shop/spec#nope and relaybay://doc/acme-shop/gone#x",
            "progress": "Half done",
        },
    )
    await service.add_decision("acme-shop", "Project wide")
    await service.add_decision("acme-shop", "Task only", task=task.key)
    other = await service.create_task("acme-shop", "Other")
    await service.add_decision("acme-shop", "Of another task", task=other.key)
    await service.add_note("project", "project", "Refunds policy", "Body", project="acme-shop")
    await service.add_note("project", "project", "Unrelated cats", "Body", project="acme-shop")
    await service.add_note("role", "user", "Refunds for a role", "Body", role="manager")
    first = await service.add_owner_comment(task.key, "Delivered words")
    await fire(service, "PostToolUse")  # delivers it to the linked session
    second = await service.add_owner_comment(task.key, "Secret words")

    text = await service.task_context(task.key, session=DESKTOP_A)
    assert "Done so far: Half done" in text
    assert "**Gift cards** — First. Second. Third." in text and "Fourth" not in text
    assert "broken: relaybay://doc/acme-shop/spec#nope (no such section)" in text
    assert "broken: relaybay://doc/acme-shop/gone#x (no such document)" in text
    assert "Task only" in text and "Project wide" in text and "Of another task" not in text
    assert f"2 unread: #{first.id}, #{second.id}" in text
    assert "Delivered words" in text and "Secret words" not in text
    assert "Refunds policy" in text and "Unrelated cats" not in text and "for a role" not in text
    assert "Rules " in text and "stage dev" in text
    # A stranger sees ids only, and nothing was marked delivered by reading.
    assert "Delivered words" not in await service.task_context(task.key, session="someone-else")
    _, comments = await service.list_comments(task.key)
    assert comments[1].delivered_at is None


# --- reader cursors -------------------------------------------------------------------------


async def test_cursor_shows_only_what_is_new_and_since_event_does_not_move_it(
    service: Service,
) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    await service.update_task(task.key, {"next_step": "Go"})
    first = await service.project_digest("acme-shop", session="manager")
    assert "## Changes in the last 24 hours" in first
    assert f"{task.key} Checkout: 1 task.created, 1 task.updated" in first
    reached = int(first.rsplit("#", 1)[1].split(" ")[0])
    assert "read position of manager saved" in first

    second = await service.project_digest("acme-shop", session="manager")
    assert "## Changes since your last read" in second and "No changes." in second
    assert "nothing new for manager" in second

    await service.add_report(task.key, "changed · S · done")
    third = await service.project_digest("acme-shop", session="manager", since_event=reached)
    assert f"## Changes since event #{reached}" in third and "1 report.added" in third
    assert "no cursor moved" in third
    # since_event left the cursor: the same change shows again, and then it is gone.
    assert "1 report.added" in await service.project_digest("acme-shop", session="manager")
    assert "No changes." in await service.project_digest("acme-shop", session="manager")
    # Each level and ref has its own position; another reader starts over.
    assert "1 report.added" in await service.task_context(task.key, session="manager")
    assert "1 report.added" in await service.project_digest("acme-shop", session="other")
    assert "1 report.added" in await service.portfolio_digest(session="manager")


async def test_first_read_and_no_reader_show_the_last_24_hours(service: Service) -> None:
    await service.upsert_project("acme-shop")
    old = await service.create_task("acme-shop", "Old one")
    await age_events(service, hours=25)
    new = await service.create_task("acme-shop", "New one")
    for text in (
        await service.portfolio_digest(),
        await service.project_digest("acme-shop"),
        await service.portfolio_digest(session="first-time"),
    ):
        assert f"{new.key} New one: 1 task.created" in text
        assert f"{old.key} Old one: 1" not in text
    cursors = await service._r.execute_fetchall("SELECT reader, level FROM reader_cursor")
    assert [tuple(r) for r in cursors] == [("first-time", "portfolio")]


async def test_changes_leave_out_noise_and_group_per_task(service: Service) -> None:
    task = await linked_task(service)
    await service.add_owner_comment(task.key, "Hello")
    await fire(service, "PostToolUse")  # status changes and a delivery mark
    await service.update_task(task.key, {"next_step": "One"})
    await service.update_task(task.key, {"next_step": "Two"})
    text = await service.project_digest("acme-shop")
    line = next(x for x in text.split("\n") if x.startswith(f"- {task.key} Checkout:"))
    assert "2 task.updated" in line and "1 comment.added" in line
    for noise in digest.NOISE_KINDS:
        assert noise not in text


async def test_changes_are_capped_with_events_since(service: Service) -> None:
    await service.upsert_project("acme-shop")
    for n in range(digest.CHANGES_MAX + 4):
        await service.create_task("acme-shop", f"Task {n}")
    text = await service.project_digest("acme-shop")
    assert '… 5 more: events_since(project="acme-shop", since_id=0)' in text


async def test_task_context_hides_the_callers_own_hook_activity(service: Service) -> None:
    task = await linked_task(service)
    session = await service._exact_session(CC_A)
    assert session is not None
    async with service._tx() as tx:
        for sid in (session.id, None):
            await tx.event(
                "live.action",
                "x",
                project_id=task.project_id,
                task_id=task.id,
                session_id=sid,
                environment="live",
            )
    own = await service.task_context(task.key, session=DESKTOP_A)
    assert "1 live.action" in own  # another session's action only: 2 would count its own too
    theirs = await service.task_context(task.key, session="someone-else")
    assert "2 live.action" in theirs


# --- SessionStart ---------------------------------------------------------------------------


async def test_session_start_injects_the_newest_bound_task(service: Service) -> None:
    older = await linked_task(service, "Older task")
    newer = await service.create_task(
        "acme-shop", "Newer task", stage="dev", session=DESKTOP_A, description="What it is"
    )
    await service.set_plan(newer.key, [{"title": "Build it", "details": "Why it matters."}])
    await service.update_plan_step(newer.key, 1, "in_progress")
    await service.set_plan(older.key, [{"title": "Old step"}])
    text = context_of(await fire(service, "SessionStart", source="clear"))
    assert text.startswith(f"Your relaybay hook session: {CC_A}.")
    assert f"\n# {newer.key} — Newer task\n" in text and "About: What it is" in text
    others = text.split("## Your other bound tasks")[1].split("##")[0]
    assert older.key in others and "'Old step'" in others
    # The plan is in the context once; the reminder counts as sent.
    assert text.count("relaybay plan reminder") == 0 and "'Build it'" in text
    assert await fire(service, "PostToolUse", tool_name="Bash") is None
    assert "read position of" in text and len(text) < CONTEXT_MAX


async def test_session_start_without_a_task_gets_the_short_project_digest(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", session_context_project_chars=800)
    await svc.start()
    try:
        await svc.upsert_project("acme-shop", repo_path=CWD)
        await svc.set_pulse("acme-shop", "On track.")
        for n in range(40):
            await svc.create_task("acme-shop", f"Task {n}", next_step="x" * 100)
        task = await svc.create_task("acme-shop", "Asked", waiting_on="owner", stage="dev")
        await svc.add_owner_comment(task.key, "Comment words")
        text = context_of(await fire(svc, "SessionStart", cwd=CWD))
        added = text.split("\n", 1)[1]
        assert added.startswith("# acme-shop") and len(added) <= 800
        assert "On track." in added and "Asked" in added and "Comment words" not in added
        assert "more: task_list" in added
    finally:
        await svc.close()


async def test_session_start_elsewhere_adds_nothing(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.upsert_project("demo-api", repo_path=CWD)  # the folder of two projects
    alone = await fire(service, "SessionStart", cwd=CWD)
    assert "\n" not in context_of(alone)
    assert "\n" not in context_of(await fire(service, "SessionStart", CC_B, cwd="/elsewhere"))


async def test_session_context_off_is_the_old_answer(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", session_context="off")
    await svc.start()
    try:
        task = await linked_task(svc)
        await svc.set_plan(task.key, [{"title": "Build it"}])
        text = context_of(await fire(svc, "SessionStart", cwd=CWD))
        assert text.count("\n") == 3
        assert "relaybay plan reminder:" in text and "# " not in text
    finally:
        await svc.close()


@pytest.mark.parametrize("linked", [True, False])
async def test_a_failure_in_the_injection_keeps_the_old_answer(
    service: Service,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    linked: bool,
) -> None:
    if linked:
        task = await linked_task(service)
        await service.set_plan(task.key, [{"title": "Build it"}])
    else:
        await service.upsert_project("acme-shop", repo_path=CWD)

    async def broken(*args: object, **kwargs: object) -> list:
        raise RuntimeError("database is on fire")

    monkeypatch.setattr("relaybay.db.repo.list_environments", broken)
    text = context_of(await fire(service, "SessionStart", cwd=CWD))
    assert text.startswith(f"Your relaybay hook session: {CC_A}.")
    assert "# " not in text
    assert ("relaybay plan reminder:" in text) is linked
    assert "Adding context to SessionStart failed" in caplog.text


async def test_injection_moves_the_cursor_and_writes_only_for_a_change(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await linked_task(service)
    first = context_of(await fire(service, "SessionStart"))
    assert "## Changes in the last 24 hours" in first
    calls = 0
    real = service._tx

    def counted() -> object:
        nonlocal calls
        calls += 1
        return real()

    payload = hook("SessionStart", CC_A, source="compact")
    await service.record_hook(payload)  # a status event: noise, the cursor need not move
    monkeypatch.setattr(service, "_tx", counted)
    second = context_of(await service.hook_answer(payload))
    assert calls == 0 and "## Changes since your last read" in second and "No changes." in second

    monkeypatch.setattr(service, "_tx", real)
    await service.add_report(task.key, "changed · S · more")
    monkeypatch.setattr(service, "_tx", counted)
    third = context_of(await service.hook_answer(payload))
    assert calls == 1 and "1 report.added" in third
    assert "1 report.added" not in context_of(await service.hook_answer(payload))
    assert "1 report.added" not in await service.task_context(task.key, session=DESKTOP_A)


async def test_session_start_stays_under_the_limit_and_fast(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    for n in range(200):
        await service.create_task(
            "acme-shop", f"Task {n}", description="d " * 2000, next_step="n " * 100
        )
    for n in range(200):
        await service.add_decision("acme-shop", f"Decision {n} " + "x " * 500)
    bound = await linked_task(service, "Task bound")
    await service.update_task(bound.key, {"description": "d " * 5000, "progress": "p " * 5000})

    for label, payload in (
        ("task", hook("SessionStart", CC_A, cwd=CWD)),
        ("project", hook("SessionStart", CC_B, cwd=CWD)),
    ):
        await service.record_hook(payload)
        best = 10.0
        for _ in range(3):
            started = time.perf_counter()
            answer = await service.hook_answer(payload)
            best = min(best, time.perf_counter() - started)
        assert len(context_of(answer)) < CONTEXT_MAX - 400, label
        assert best < 0.15, f"{label}: {best:.3f}s"


# --- config, migration, MCP, board ----------------------------------------------------------


def test_config_keys_and_validation(tmp_path: Path) -> None:
    settings = load_settings(home=tmp_path, environ={})
    assert (settings.pulse_stale_days, settings.task_stale_days) == (7, 5)
    assert settings.digest_budget_chars == 12_000 and settings.session_context == "auto"
    assert settings.session_context_project_chars == 1_500
    env = {
        "RELAYBAY_PULSE_STALE_DAYS": "3",
        "RELAYBAY_TASK_STALE_DAYS": "2",
        "RELAYBAY_DIGEST_BUDGET_CHARS": "5000",
        "RELAYBAY_SESSION_CONTEXT": "OFF",
        "RELAYBAY_SESSION_CONTEXT_PROJECT_CHARS": "900",
    }
    settings = load_settings(home=tmp_path, environ=env)
    assert (settings.pulse_stale_days, settings.task_stale_days) == (3, 2)
    assert settings.digest_budget_chars == 5000 and settings.session_context == "off"
    assert settings.session_context_project_chars == 900
    for key, value, message in (
        ("RELAYBAY_PULSE_STALE_DAYS", "0", "pulse_stale_days must be"),
        ("RELAYBAY_DIGEST_BUDGET_CHARS", "100", "digest_budget_chars must be"),
        ("RELAYBAY_DIGEST_BUDGET_CHARS", "50000", "from 500 to 40000"),
        ("RELAYBAY_SESSION_CONTEXT", "maybe", "session_context must be"),
    ):
        with pytest.raises(ValueError, match=message):
            load_settings(home=tmp_path, environ={key: value})


async def test_upgrade_from_0013_keeps_data_and_adds_pulse_and_cursors(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=13)
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO event (ts, kind, project_id, task_id, actor, payload)"
            " VALUES (?, 'task.created', 1, 1, 'demo', '{}')",
            (NOW,),
        )
    finally:
        await conn.close()
    svc = Service(db)
    await svc.start()
    try:
        project = await svc.get_project("acme-shop")
        assert project.name == "Acme Shop" and project.pulse is None and project.pulse_at is None
        assert (await svc.get_task("acme-shop-1")).title == "Checkout page"
        assert await svc.last_event_id() == 1
        text = await svc.project_digest("acme-shop", session="manager")
        assert "No pulse yet" in text and "Checkout page" in text
        rows = await svc._r.execute_fetchall(
            "SELECT reader, level, ref_id, last_event_id FROM reader_cursor"
        )
        assert [tuple(r) for r in rows] == [("manager", "project", project.id, 2)]
    finally:
        await svc.close()


async def test_mcp_digest_tools(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("project_pulse_set", "portfolio_digest", "project_digest", "task_context"):
            assert "hook_session" in tools[name].input_schema["properties"], name
            assert tools[name].annotations.read_only_hint is False, name
        await call(client, "project_upsert", slug="acme-shop")
        key = "acme-shop-1"
        await call(client, "task_create", project="acme-shop", title="Checkout")
        moved = await call(client, "task_set_stage", task=key, stage="plan")
        assert "Note:" not in moved  # nothing moved
        moved = await call(client, "task_set_stage", task=key, stage="dev", session="agent")
        assert "Note: the pulse of acme-shop is missing" in moved
        saved = await call(client, "project_pulse_set", project="acme-shop", text="All\ngood.")
        assert saved == "Pulse of **acme-shop** saved (2 line(s), 9/600 characters)."
        with pytest.raises((MCPError, AssertionError), match="at most 5 lines"):
            await call(client, "project_pulse_set", project="acme-shop", text="a\nb\nc\nd\ne\nf")

        text = await call(client, "portfolio_digest", session="coordinator")
        assert "**acme-shop**" in text and "All" in text
        text = await call(client, "project_digest", project="acme-shop", budget=600)
        assert len(text) <= 600 and "acme-shop" in text
        text = await call(client, "task_context", task=key, since_event=0)
        assert text.startswith(f"# {key} — Checkout") and "no cursor moved" in text


def test_board_shows_pulse_age_and_stale_mark(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(board, service.upsert_project, "demo-api", name="Demo API")
    assert "No pulse yet" in board.get("/p/acme-shop/docs").text
    run_in_app(board, service.set_pulse, "acme-shop", "Fresh <b>news</b>.\nLine two.")
    run_in_app(board, service.set_pulse, "demo-api", "Old news.", at=ago(days=9))

    projects = board.get("/").text
    assert "Fresh &lt;b&gt;news&lt;/b&gt;." in projects and "Old news." in projects
    assert projects.count('class="stale-mark"') == 1 and "9d ago" in projects
    fresh = board.get("/p/acme-shop/docs").text
    assert "Fresh &lt;b&gt;news&lt;/b&gt;." in fresh and 'class="stale-mark"' not in fresh
    stale = board.get("/p/demo-api/docs").text
    assert "Old news." in stale and 'class="stale-mark"' in stale


def test_digest_module_does_no_io() -> None:
    source = Path(digest.__file__).read_text(encoding="utf-8")
    for word in ("aiosqlite", "relaybay.db", "open("):
        assert word not in source


def test_instructions_start_with_the_digest_once() -> None:
    from relaybay.domain.rules import DEFAULT_RULES, Rules
    from relaybay.mcp_server.server import instructions

    text = instructions(Rules(DEFAULT_RULES))
    assert text.count("Start with the digest for your level") == 1
    assert "1. Start with the digest for your level" in text and "16." not in text
    assert "14. A tool call refused with 'relaybay brake on'" in text
    assert "15. A 'Session message from" in text
    assert "13. A finding outside your task" in text and "12. A message framed as external" in text
    assert "project_snapshot" in text and "project_pulse_set" in text
    assert "rules line" in text and "relaybay://rules/<project>" in text


def test_reminder_line_has_one_period() -> None:
    step = PlanStep(
        1, 1, 1, "Build", "Why it matters.", "in_progress", None, None, None, None, "x", NOW, NOW
    )
    line = reminder_line("acme-shop-1", "dev", Plan("accepted", None, None, [step]), [])
    assert line is not None and line.endswith("Why it matters.") and ".." not in line
    step.details_md = "No period"
    line = reminder_line("acme-shop-1", "dev", Plan("accepted", None, None, [step]), [])
    assert line is not None and line.endswith("No period.")


# --- review findings ------------------------------------------------------------------------


def nested(cc_id: str = CC_B, **extra: Any) -> dict[str, Any]:
    return hook(
        "SessionStart",
        cc_id,
        cwd=CWD,
        relaybay_host_session=DESKTOP_A,
        relaybay_host_attended="0",
        **extra,
    )


async def cursor_rows(service: Service) -> list[tuple]:
    rows = await service._r.execute_fetchall(
        "SELECT reader, level, ref_id, last_event_id FROM reader_cursor"
    )
    return [tuple(r) for r in rows]


def test_attended_tells_an_explicit_no_from_missing() -> None:
    base = hook("SessionStart", relaybay_host_session=DESKTOP_A)
    assert parse_hook(base).attended is None and not parse_hook(base).nested
    for value in ("", None, ["0"], 0):
        parsed = parse_hook(base | {"relaybay_host_attended": value})
        assert parsed.attended is None and not parsed.nested
    assert parse_hook(base | {"relaybay_host_attended": "1"}).attended is True
    no = parse_hook(base | {"relaybay_host_attended": "0"})
    assert no.attended is False and no.nested
    assert not parse_hook(hook("SessionStart", relaybay_host_attended="0")).nested


async def test_nested_start_never_takes_a_bound_window_that_is_not_exactly_linked(
    service: Service,
) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    context = context_of(await fire_payload(service, nested()))
    assert context == session_start_context(CC_B)
    desktop = await repo.get_session_by_external_id(service._r, parse_session_id(DESKTOP_A))
    assert desktop is not None and desktop.cc_session_id is None and not desktop.cc_exact
    child = await repo.get_session_by_cc(service._r, CC_B)
    assert child is not None and child.external_id is None and not child.cc_exact

    await service.add_owner_comment(task.key, "For the window only")
    assert await service.hook_answer(hook("PreToolUse", CC_B)) is None
    _, comments = await service.list_comments(task.key)
    assert comments[0].delivered_at is None
    # An empty field is what an older desktop app sends: it still links as before.
    older = hook("SessionStart", CC_A, cwd=CWD, relaybay_host_session=DESKTOP_A)
    linked = await service.record_hook(older | {"relaybay_host_attended": ""})
    assert linked is not None and linked.external_id is not None and linked.cc_exact


async def test_nested_child_gets_only_the_hook_line_even_when_linked_itself(
    service: Service,
) -> None:
    task = await linked_task(service, cc_id=CC_B)
    await service.set_plan(task.key, [{"title": "Build it"}])
    await service.add_owner_comment(task.key, "Words")
    answer = await fire_payload(service, nested())
    assert context_of(answer) == session_start_context(CC_B)
    assert await cursor_rows(service) == []


async def test_a_budget_cut_keeps_the_cursor_and_the_next_read_has_the_changes(
    service: Service,
) -> None:
    await service.upsert_project("acme-shop")
    for n in range(20):
        await service.create_task("acme-shop", f"Task {n}", next_step="x" * 100)
    cut = await service.project_digest("acme-shop", session="manager", budget=500)
    assert len(cut) <= 500 and "read position of manager saved" not in cut
    assert "not moved" in cut or "Events up to" not in cut
    assert await cursor_rows(service) == []

    whole = await service.project_digest("acme-shop", session="manager")
    assert "Task 0: 1 task.created" in whole and "read position of manager saved" in whole
    position = (await cursor_rows(service))[0][3]

    await service.update_task("acme-shop-1", {"next_step": "New"})
    await service.update_task("acme-shop-2", {"next_step": "New"})
    dropped = await service.project_digest("acme-shop", session="manager", budget=500)
    assert (await cursor_rows(service))[0][3] == position  # held back at the old position
    assert "read position of manager saved" not in dropped
    assert "1 task.updated" in await service.project_digest("acme-shop", session="manager")
    assert (await cursor_rows(service))[0][3] > position


async def test_the_line_cap_is_no_budget_cut(service: Service) -> None:
    await service.upsert_project("acme-shop")
    for n in range(digest.CHANGES_MAX + 4):
        await service.create_task("acme-shop", f"Task {n}")
    text = await service.project_digest("acme-shop", session="manager")
    assert "… 5 more: events_since(" in text and "read position of manager saved" in text
    assert len(await cursor_rows(service)) == 1


def test_render_reports_a_dropped_changes_section() -> None:
    changes = digest.Section("## Changes", ["- a"], "t" * 700, 9, key=digest.CHANGES_KEY)
    head = digest.Section("# H", ["x"], "y", 0)
    seen: list[bool] = []

    def footer(complete: bool) -> str:
        seen.append(complete)
        return f"saved={complete}"

    small = digest.render([head, changes], footer, 500)
    assert not small.changes_complete and small.kept == {"changes": 0}
    assert small.text.endswith("saved=False") and "- a" not in small.text
    big = digest.render([head, changes], footer, 5000)
    assert big.changes_complete and big.kept == {"changes": 1} and big.text.endswith("saved=True")


async def test_a_guessed_row_gets_no_cursor_and_its_id_is_not_printed(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    await fire(service, "SessionStart", CC_A, cwd=CWD)
    row = await repo.get_session_by_cc(service._r, CC_A)
    assert row is not None and row.external_id is not None and not row.cc_exact  # the guess
    text = context_of(await fire(service, "SessionStart", CC_A, cwd=CWD))
    assert "# acme-shop" in text and "local_" not in text and row.external_id not in text
    assert await cursor_rows(service) == []


async def test_a_new_context_is_not_told_since_your_last_read(service: Service) -> None:
    task = await linked_task(service)
    await fire(service, "SessionStart", source="compact")  # sets the cursor
    await service.add_report(task.key, "changed · S · read by the old context")
    assert "1 report.added" in context_of(await fire(service, "SessionStart", source="compact"))
    assert "since your last read" in context_of(
        await fire(service, "SessionStart", source="resume")
    )
    for source in ("startup", "clear"):
        text = context_of(await fire(service, "SessionStart", source=source))
        assert "since your last read" not in text
        assert "## Changes in the last 24 hours" in text and "1 report.added" in text


async def test_a_new_context_reads_from_a_cursor_older_than_a_day(service: Service) -> None:
    task = await linked_task(service)
    await fire(service, "SessionStart", source="compact")
    position = (await cursor_rows(service))[0][3]
    await service.add_report(task.key, "changed · S · old news")
    await age_events(service, hours=30)
    text = context_of(await fire(service, "SessionStart", source="clear"))
    assert f"## Changes since this session last read (events after #{position})" in text
    assert "1 report.added" in text and "since your last read" not in text


async def test_a_new_context_with_nothing_new_writes_nothing(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = await linked_task(service)
    await fire(service, "SessionStart", source="compact")
    await service.add_report(task.key, "changed · S · seen")
    await fire(service, "SessionStart", source="compact")
    payload = hook("SessionStart", CC_A, source="startup")
    await service.record_hook(payload)

    def no_write() -> None:
        raise AssertionError("no write transaction expected")

    monkeypatch.setattr(service, "_tx", no_write)
    assert "1 report.added" in context_of(await service.hook_answer(payload))


async def test_the_reminder_goes_out_when_the_budget_cut_its_line(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", digest_budget_chars=500)
    await svc.start()
    try:
        task = await linked_task(svc)
        await svc.update_task(
            task.key, {"next_step": "n" * 400, "stage_note": "s" * 400, "branch": "b" * 400}
        )
        await svc.set_plan(task.key, [{"title": "Build it", "details": "Why."}])
        text = context_of(await fire(svc, "SessionStart"))
        assert "relaybay plan reminder:" in text and "'Build it'" in text
        assert len(text) < CONTEXT_MAX
    finally:
        await svc.close()


async def test_the_reminder_is_not_sent_when_its_line_is_in_the_text(service: Service) -> None:
    task = await linked_task(service)
    await service.set_plan(task.key, [{"title": "Build it", "details": "Why."}])
    text = context_of(await fire(service, "SessionStart"))
    assert "relaybay plan reminder:" not in text and text.count("'Build it'") == 1


async def test_changes_drill_line_names_the_project(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    project = await service.project_digest("acme-shop")
    assert 'More: events_since(project="acme-shop", since_id=0)' in project
    assert 'More: events_since(project="acme-shop", since_id=0)' in await service.task_context(
        task.key
    )
    assert "More: events_since(since_id=0)" in await service.portfolio_digest()


def test_config_does_not_load_the_digest_or_search_modules() -> None:
    code = (
        "import sys, relaybay.config, relaybay.hook_client;"
        "bad = {'snowballstemmer', 'relaybay.domain.memory', 'relaybay.domain.digest'};"
        "sys.exit(str(bad & set(sys.modules)) if bad & set(sys.modules) else 0)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
