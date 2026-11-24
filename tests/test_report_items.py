"""Findings and questions a report carries (`report_add(findings=, questions=)`), the stage move
in the same call (`report_add(stage=)`), and their triage: `backlog_add(from_report=,
finding=)` and `report_item_dismiss`."""

from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from relaybay.db import repo
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.mcp_server.server import build_mcp
from relaybay.service import Service

TOKEN = "ghp_" + "Ab12" * 8
FINDINGS = [
    {
        "title": "Cart total rounds down",
        "body": "0.005 is lost",
        "location": "web/cart.py:41",
        "kind": "BUG",
        "area": "checkout",
    },
    {"title": "Old route still served"},
]
QUESTIONS = [{"text": "Keep the old route?", "default": "keep it until the next release"}]


def caller(service: Service) -> Any:
    mcp = build_mcp(service)

    async def call(tool: str, **arguments: Any) -> str:
        result = await mcp.call_tool(tool, arguments)
        return "\n".join(c.text for c in result.content)

    return call


async def card(service: Service, stage: str = "dev", title: str = "Checkout") -> str:
    await service.upsert_project("acme-shop")
    return (await service.create_task("acme-shop", title, stage=stage)).key


async def open_items(service: Service, key: str) -> list[tuple[str, int, str]]:
    task = await service.get_task(key)
    found, _ = await repo.open_report_items(service._r, task_id=task.id, limit=50)
    return [(i.kind, i.n, i.title) for i in found]


# --- the stage move in the same call ----------------------------------------------------------


async def test_a_report_with_stage_moves_the_card_and_meets_the_report_rule(
    service: Service,
) -> None:
    key = await card(service)
    call = caller(service)
    text = await call(
        "report_add", task=key, body="verified · S · cart fixed", stage="test", session="dev-a"
    )
    assert text.splitlines()[:2] == [
        f"Report #1 added to **{key}** (verified · S): cart fixed",
        "Moved to test.",
    ]
    assert "Warning (rules)" not in text and "task_set_stage" not in text
    task = await service.get_task(key)
    assert task.stage == "test"
    events = [e for e in await service.replay(0, None, 100) if e.kind == "task.stage_changed"]
    assert events[-1].actor == "dev-a" and events[-1].payload["to"] == "test"


async def test_a_move_with_an_unmet_rule_warns_and_keeps_the_report(service: Service) -> None:
    key = await card(service)
    text = await caller(service)("report_add", task=key, body="verified · S · x", stage="done")
    assert "Moved to done." in text
    assert "Warning (rules): moving to done usually needs the progress field." in text
    assert (await service.get_task(key)).stage == "done"


async def test_the_same_stage_is_no_move(service: Service) -> None:
    key = await card(service)
    text = await caller(service)("report_add", task=key, body="changed · S · x", stage="dev")
    assert "Already in dev." in text


async def test_strict_rules_refuse_the_move_and_the_report_with_it(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", rules_mode="strict")
    await svc.start()
    try:
        key = await card(svc)
        with pytest.raises(ValidationError, match="moving to done requires the progress field"):
            await svc.add_report_checked(
                key, "verified · S · x", stage="done", findings=[{"title": "Kept?"}]
            )
        task = await svc.get_task(key)
        assert task.stage == "dev"
        assert await repo.list_reports(svc._r, task.id, 10) == []
        assert await open_items(svc, key) == []
        saved = await svc.add_report_checked(key, "verified · S · x", progress="Done", stage="done")
        assert saved.moved and saved.task.stage == "done"
    finally:
        await svc.close()


async def test_without_stage_the_answer_names_what_the_next_move_needs(service: Service) -> None:
    call = caller(service)
    dev = await card(service)
    text = await call("report_add", task=dev, body="changed · S · x")
    assert text.splitlines()[-1] == (
        'Next: task_update(stage="test") needs a report since the last stage change (done).'
    )
    done_next = await card(service, stage="test", title="Receipts")
    text = await call("report_add", task=done_next, body="changed · S · y")
    assert 'Next: task_update(stage="done") needs the progress field (missing).' in text
    review = await card(service, stage="review", title="Banner")
    text = await call("report_add", task=review, body="changed · S · z")
    assert 'Next: task_update(stage="test") needs' in text
    finished = await card(service, stage="done", title="Docs")
    text = await call("report_add", task=finished, body="changed · S · w")
    assert "Next:" not in text


# --- findings and questions -----------------------------------------------------------------


async def test_findings_and_questions_are_stored_with_their_numbers(service: Service) -> None:
    key = await card(service)
    text = await caller(service)(
        "report_add", task=key, body="verified · S · x", findings=FINDINGS, questions=QUESTIONS
    )
    assert "Stored 2 findings and 1 question for the coordinator to triage." in text
    assert await open_items(service, key) == [
        ("finding", 1, "Cart total rounds down"),
        ("finding", 2, "Old route still served"),
        ("question", 1, "Keep the old route?"),
    ]
    first = await repo.get_report_item(service._r, 1, "finding", 1)
    assert first is not None
    assert (first.body, first.location, first.item_kind, first.area) == (
        "0.005 is lost",
        "web/cart.py:41",
        "bug",
        "checkout",
    )
    question = await repo.get_report_item(service._r, 1, "question", 1)
    assert question is not None and question.question_default == "keep it until the next release"
    assert await service.list_backlog(project="acme-shop") == []  # nothing goes there by itself


@pytest.mark.parametrize(
    ("findings", "questions", "message"),
    [
        ([{"body": "no title"}], None, r"findings\[1\]\.title is required"),
        ([{"title": "ok"}, {"title": " "}], None, r"findings\[2\]\.title is required"),
        ([{"title": "x", "kind": "chore"}], None, r"findings\[1\]\.kind must be one of"),
        ([{"title": "x", "where": "y"}], None, r"findings\[1\]: unknown field\(s\) where"),
        ([{"title": "x" * 201}], None, r"findings\[1\]\.title is longer than 200"),
        ([{"title": "x"}] * 11, None, "at most 10 per report"),
        ("one", None, "findings must be a list"),
        (["one"], None, r"findings\[1\] must be an object"),
        ([{"title": 5}], None, r"findings\[1\]\.title must be text"),
        (None, [{"text": "Why?"}], r"questions\[1\]\.default is required"),
        (None, [{"default": "no"}], r"questions\[1\]\.text is required"),
    ],
)
async def test_a_wrong_shape_names_the_item_and_writes_nothing(
    service: Service, findings: Any, questions: Any, message: str
) -> None:
    key = await card(service)
    with pytest.raises(ValidationError, match=message):
        await service.add_report_checked(
            key, "changed · S · x", findings=findings, questions=questions
        )
    assert await repo.list_reports(service._r, (await service.get_task(key)).id, 10) == []


async def test_a_secret_in_an_item_refuses_the_report(service: Service) -> None:
    key = await card(service)
    with pytest.raises(ValidationError, match=r"findings\[1\]\.body.*secret"):
        await service.add_report_checked(
            key, "changed · S · x", findings=[{"title": "Key", "body": f"token {TOKEN}"}]
        )
    with pytest.raises(ValidationError, match=r"questions\[1\]\.default.*secret"):
        await service.add_report_checked(
            key, "changed · S · x", questions=[{"text": "Use it?", "default": f"use {TOKEN}"}]
        )
    assert await repo.list_reports(service._r, (await service.get_task(key)).id, 10) == []


# --- triage ---------------------------------------------------------------------------------


async def test_backlog_add_from_report_copies_the_finding_and_closes_it(service: Service) -> None:
    key = await card(service)
    await service.add_report_checked(key, "verified · S · x", findings=FINDINGS)
    call = caller(service)
    text = await call(
        "backlog_add", project="acme-shop", from_report=1, finding=1, session="coordinator"
    )
    assert "Added backlog item #1 in acme-shop: Cart total rounds down (new, bug" in text
    item = await service.backlog_item(1)
    assert (item.body_md, item.location, item.kind, item.area) == (
        "0.005 is lost",
        "web/cart.py:41",
        "bug",
        "checkout",
    )
    assert item.origin_task_key == key
    source = await repo.get_report_item(service._r, 1, "finding", 1)
    assert source is not None and source.backlog_id == item.id
    assert [n for _, n, _ in await open_items(service, key)] == [2]
    with pytest.raises(ToolError, match=r"Finding 1 of report #1 is already backlog item #1"):
        await call("backlog_add", project="acme-shop", from_report=1, finding=1)
    # Given fields win over the finding's.
    await call(
        "backlog_add", project="acme-shop", title="Route", kind="debt", from_report=1, finding=2
    )
    second = await service.backlog_item(2)
    assert (second.title, second.kind) == ("Route", "debt")


async def test_from_report_refusals(service: Service) -> None:
    key = await card(service)
    await service.add_report_checked(key, "changed · S · x", questions=QUESTIONS)
    with pytest.raises(ValidationError, match="from_report and finding go together"):
        await service.add_backlog_item("acme-shop", None, from_report=1)
    with pytest.raises(NotFoundError, match="Report #9 not found"):
        await service.add_backlog_item("acme-shop", None, from_report=9, finding=1)
    with pytest.raises(NotFoundError, match="Report #1 has no finding 1"):
        await service.add_backlog_item("acme-shop", None, from_report=1, finding=1)
    with pytest.raises(ValidationError, match="title must not be empty"):
        await service.add_backlog_item("acme-shop", None)
    assert await service.list_backlog(project="acme-shop") == []


async def test_dismiss_closes_an_item_once(service: Service) -> None:
    key = await card(service)
    await service.add_report_checked(key, "changed · S · x", findings=FINDINGS, questions=QUESTIONS)
    call = caller(service)
    text = await call(
        "report_item_dismiss",
        report=1,
        n=1,
        kind="question",
        reason="keep the route",
        session="coordinator",
    )
    assert text.startswith(f"Dismissed question 1 of report #1 on {key}.")
    item = await repo.get_report_item(service._r, 1, "question", 1)
    assert item is not None
    assert (item.dismissed_by, item.dismiss_reason) == ("coordinator", "keep the route")
    assert ("question", 1, "Keep the old route?") not in await open_items(service, key)
    with pytest.raises(ToolError, match=r"Question 1 of report #1 was dismissed by coordinator"):
        await call("report_item_dismiss", report=1, n=1, kind="question")
    with pytest.raises(ToolError, match="kind must be finding or question"):
        await call("report_item_dismiss", report=1, n=1, kind="idea")
    with pytest.raises(ValidationError, match="secret"):
        await service.dismiss_report_item(1, 1, reason=f"see {TOKEN}")
    await call("report_item_dismiss", report=1, n=1)
    with pytest.raises(ToolError, match="was dismissed"):
        await call("backlog_add", project="acme-shop", from_report=1, finding=1)
    events = [e for e in await service.replay(0, None, 100) if e.kind == "report.item_dismissed"]
    assert [(e.payload["item"], e.payload["n"]) for e in events] == [
        ("question", 1),
        ("finding", 1),
    ]


async def test_task_context_and_task_get_show_the_cards_own_open_items(service: Service) -> None:
    key = await card(service)
    other = await card(service, title="Receipts")
    many = [{"title": f"Finding {n}"} for n in range(1, 8)]
    await service.add_report_checked(key, "changed · S · x", findings=many)
    await service.add_report_checked(other, "changed · S · y", findings=[{"title": "Elsewhere"}])
    call = caller(service)
    context = await call("task_context", task=key)
    assert "## Open findings and questions of its reports" in context
    assert "7 open, for the coordinator to triage" in context
    assert f"- report #1 finding 1 on {key}: Finding 1" in context
    assert "Finding 6" not in context and f'… 2 more: task_get("{key}")' in context
    assert "Elsewhere" not in context
    detail = await call("task_get", task=key)
    assert "## Open findings and questions of its reports" in detail
    assert f"- report #1 finding 5 on {key}: Finding 5" in detail and "Finding 6" not in detail
    for n in range(1, 8):
        await service.dismiss_report_item(1, n)
    assert "Open findings" not in await call("task_context", task=key)
    assert "Open findings" not in await call("task_get", task=key)
