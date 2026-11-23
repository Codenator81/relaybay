"""A card's change document `spec-<task key>`: its format, the "changing" marks on the main
spec's headings (board page, doc_get, doc_outline), task_context's section, the warning after a
write and the hints of a move to done."""

from collections.abc import Callable

import pytest
from starlette.testclient import TestClient

from relaybay.domain import change_doc as cdoc
from relaybay.service import Service, collect_notes
from tests.conftest import LiveServer, run_in_app
from tests.test_mcp import call, connect

SPEC = (
    "# Shop\n\n## Gift cards\nCards expire in a year.\n\n### Refunds\nNone.\n\n"
    "## Payouts\nWeekly.\n\n## Vouchers\nOld.\n"
)
CHANGE = (
    "# Gift cards v2\nWhat changes.\n\n"
    "## spec#gift-cards\nMODIFIED:\nCards expire in two years.\nWas: Cards expire in a year.\n\n"
    "ADDED:\n### Gift card refunds\nWithin 14 days.\n\n"
    "## spec#vouchers\nREMOVED:\n"
)


# --- the parser -------------------------------------------------------------------------------


def test_parse_reads_ops_the_was_line_and_a_new_section_under_its_parent() -> None:
    parsed = cdoc.parse(CHANGE)

    assert parsed.problems == []
    modified, added, removed = parsed.blocks
    assert (modified.doc, modified.anchor, modified.op) == ("spec", "gift-cards", "MODIFIED")
    assert modified.text == "Cards expire in two years."
    assert modified.was == "Cards expire in a year."
    assert (added.target, added.op) == ("spec#gift-cards", "ADDED")
    assert added.text == "### Gift card refunds\nWithin 14 days."
    assert (removed.target, removed.op, removed.text) == ("spec#vouchers", "REMOVED", "")
    assert parsed.targets() == [("spec", "gift-cards"), ("spec", "vouchers")]


def test_parse_keeps_fenced_text_and_an_op_line_with_text() -> None:
    body = "## spec#payouts\nADDED: Monthly too.\n```\n## not a heading\nREMOVED:\n```\n"
    parsed = cdoc.parse(body)

    assert parsed.problems == []
    [block] = parsed.blocks
    assert block.text == "Monthly too.\n```\n## not a heading\nREMOVED:\n```"


def test_parse_reports_format_problems() -> None:
    body = (
        "## Gift cards\nADDED:\nText.\n\n"
        "## spec#payouts\nSome text first.\nCHANGED:\nMore.\n\n"
        "## spec#vouchers\nMODIFIED:\n"
    )
    problems = cdoc.parse(body).problems

    assert any("'## Gift cards' is not '## <doc>#<anchor>'" in p for p in problems)
    assert any("text under spec#payouts before its" in p for p in problems)
    assert any("unknown change CHANGED:" in p for p in problems)
    assert any("MODIFIED under spec#vouchers has no text" in p for p in problems)
    assert cdoc.parse("Just words.").problems == [
        "no '## <doc>#<anchor>' heading: name the main-spec section each change is for"
    ]


# --- marks, task_context, hints (service) -----------------------------------------------------


@pytest.fixture
async def acme(service: Service) -> Service:
    await service.upsert_project("acme-shop", name="Acme Shop")
    await service.update_document("acme-shop", "spec", SPEC, "first", 0, kind="spec")
    return service


async def card_with_change(svc: Service, title: str, body: str = CHANGE, stage: str = "dev"):
    task = await svc.create_task("acme-shop", title, stage=stage, session="worker")
    await svc.update_document("acme-shop", f"spec-{task.key}", body, "delta", 0, session="worker")
    return task


async def test_outline_and_section_read_carry_the_marks_of_open_cards(acme: Service) -> None:
    task = await card_with_change(acme, "Gift cards v2")
    linked = await acme.create_task("acme-shop", "Payouts", stage="plan", session="worker")
    await acme.link_task_section(linked.key, "spec", "payouts", session="worker")
    await acme.link_task_section(task.key, "spec", "payouts", session="worker")

    doc, rows, _ = await acme.document_outline("acme-shop", "spec")
    marks = await acme.change_marks(doc, rows)

    # The linked section counts only for a card with a change document.
    assert marks == {
        "gift-cards": [cdoc.ChangeMark(task.key, "dev", f"spec-{task.key}")],
        "vouchers": [cdoc.ChangeMark(task.key, "dev", f"spec-{task.key}")],
        "payouts": [cdoc.ChangeMark(task.key, "dev", f"spec-{task.key}")],
    }
    line = acme.change_mark_line("acme-shop", marks["gift-cards"][0])
    assert line == f"changing on dev: {task.key} · relaybay://doc/acme-shop/spec-{task.key}"


async def test_done_and_cancelled_cards_mark_nothing(acme: Service) -> None:
    done = await card_with_change(acme, "Done one")
    cancelled = await card_with_change(acme, "Given up")
    await acme.update_task(done.key, {"stage": "done"})
    await acme.update_task(cancelled.key, {"cancelled": "not needed"})

    doc, rows, _ = await acme.document_outline("acme-shop", "spec")
    assert await acme.change_marks(doc, rows) == {}


async def test_task_context_lists_targets_problems_and_other_cards(acme: Service) -> None:
    first = await card_with_change(
        acme, "Gift cards v2", CHANGE + "\n## spec#nowhere\nADDED:\nX.\n"
    )
    second = await card_with_change(
        acme, "Gift card fees", "## spec#gift-cards\nADDED:\nA fee.\n", stage="test"
    )

    text = await acme.task_context(first.key)

    section = text.split("## Change document")[1].split("\n## ")[0]
    assert f"- spec-{first.key} · version 1" in section
    assert "- spec#gift-cards · MODIFIED" in section
    assert "- spec#gift-cards · ADDED" in section
    assert "- spec#vouchers · REMOVED" in section
    assert "- problem: spec#nowhere: spec has no section nowhere" in section
    assert f"Other cards changing the same sections: {second.key} (test) spec#gift-cards" in section
    other = await acme.task_context(second.key)
    assert f"{first.key} (dev) spec#gift-cards" in other
    plain = await acme.create_task("acme-shop", "No change doc", stage="dev", session="worker")
    assert "## Change document" not in await acme.task_context(plain.key)


async def test_done_hints_name_a_change_document_left_and_deploy_notes_not_done(
    acme: Service,
) -> None:
    task = await card_with_change(acme, "Gift cards v2")
    await acme.update_task(task.key, {"deploy_notes": "Run the migration first."})

    with collect_notes() as notes:
        await acme.set_stage(task.key, "done")

    assert any(
        n.startswith(f"Hint: {task.key} is done but its change document spec-{task.key}")
        and "delete=true" in n
        for n in notes
    )
    assert any("deploy notes are not carried out" in n for n in notes)

    clean = await acme.create_task("acme-shop", "Clean", stage="dev", session="worker")
    await acme.update_task(clean.key, {"deploy_notes": "Flag on.", "deploy_done": True})
    with collect_notes() as notes:
        await acme.set_stage(clean.key, "done")
    assert not [n for n in notes if "is done but" in n]


async def test_a_cancelled_card_gets_no_done_hints(acme: Service) -> None:
    task = await card_with_change(acme, "Given up")
    await acme.update_task(task.key, {"deploy_notes": "Never ran.", "cancelled": "dropped"})

    with collect_notes() as notes:
        await acme.set_stage(task.key, "done")
    assert not [n for n in notes if "is done but" in n]


# --- the board page ---------------------------------------------------------------------------


def test_board_page_shows_the_mark_under_each_changing_heading(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(board, service.update_document, "acme-shop", "spec", SPEC, "first", 0)
    task = run_in_app(board, service.create_task, "acme-shop", "Gift cards v2", stage="review")
    slug = f"spec-{task.key}"
    run_in_app(board, service.update_document, "acme-shop", slug, CHANGE, "delta", 0)

    page = board.get("/p/acme-shop/docs/spec").text

    _, _, rest = page.partition('id="gift-cards"')
    mark = rest.split("</h2>", 1)[1].split("<p>", 1)[0]
    assert 'class="doc-changing">changing on review:' in mark
    assert f'<a href="/t/{task.key}">{task.key}</a>' in mark
    assert f'<a href="/p/acme-shop/docs/{slug}">{slug}</a>' in mark
    payouts = page.split('id="payouts"')[1].split('id="vouchers"')[0]
    assert "doc-changing" not in payouts


# --- MCP: doc_get, doc_outline, doc_update ----------------------------------------------------


async def test_mcp_doc_reads_show_marks_and_a_write_warns_on_problems(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", name="Acme Shop")
        await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="spec",
            body=SPEC,
            reason="first",
            base_version=0,
        )
        created = await call(client, "task_create", project="acme-shop", title="Gift cards v2")
        key = created.split("**")[1]
        saved = await call(
            client,
            "doc_update",
            project="acme-shop",
            doc=f"spec-{key}",
            body=CHANGE + "\n## spec#nowhere\nADDED:\nX.\n",
            reason="delta",
            base_version=0,
        )
        assert f"Warning: change document spec-{key}: spec#nowhere: spec has no section" in saved

        section = await call(
            client, "doc_get", project="acme-shop", doc="spec", section="gift-cards"
        )
        lines = section.split("\n")
        assert lines[0].startswith("<!-- acme-shop/spec section #gift-cards")
        assert lines[1].startswith("<!-- changing on ") and f": {key} · " in lines[1]
        assert f"relaybay://doc/acme-shop/spec-{key}" in lines[1]
        payouts = await call(client, "doc_get", project="acme-shop", doc="spec", section="payouts")
        assert "changing on" not in payouts

        outline = await call(client, "doc_outline", project="acme-shop", doc="spec")
        gift = next(x for x in outline.split("\n") if "#gift-cards" in x)
        assert gift.endswith(f" · changing: {key}")
        assert "changing" not in next(x for x in outline.split("\n") if "#payouts" in x)

        plain = await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="notes",
            body="# N\n\n## A\nB.\n",
            reason="n",
            base_version=0,
        )
        assert "Warning" not in plain
