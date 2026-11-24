"""Questions to the owner: asking over MCP, the owner's answer on the board, delivery through
the asking session's hooks, defaults by deadline, the Inbox and the tray."""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import Settings
from relaybay.db import repo
from relaybay.domain import questions as qrules
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.domain.models import Event
from relaybay.domain.notifications import classify
from relaybay.mcp_server.server import build_mcp
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_hooks import CC_A, CWD, DESKTOP_A, DESKTOP_B, hook

RUN_CC = CC_A
COORDINATOR = f"local_{uuid.UUID(int=9)}"


@pytest.fixture
async def svc(tmp_path: Path) -> Any:
    service = Service(tmp_path / "relaybay.sqlite", coordinators=["coord"])
    await service.start()
    folder = tmp_path / "acme-shop"
    folder.mkdir()
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP_A, hook_session=RUN_CC
    )
    try:
        yield service
    finally:
        await service.close()


async def started_run(svc: Service) -> Any:
    """The desktop session bound to acme-shop-1, working in the project's folder."""
    task = await svc.get_task("acme-shop-1")
    project = await svc.get_project("acme-shop")
    return SimpleNamespace(cwd=project.repo_path, session_name=task.session_name)


async def owner_line(svc: Service, question_id: int) -> str:
    """The owner comment that carries the answer to the session."""
    q = await svc.get_question(question_id)
    assert q.comment_id is not None
    comment = await repo.get_comment(svc._r, q.comment_id)
    assert comment is not None
    return comment.body_md


async def ask(svc: Service, run: Any, text: str = "Which provider?", **kw: Any) -> Any:
    kw.setdefault("default", "Stripe")
    asked, _ = await svc.ask_question(
        "acme-shop-1", text, session=run.session_name, hook_session=RUN_CC, **kw
    )
    return asked


async def events(svc: Service, kind: str) -> list[Event]:
    return [e for e in await svc.events_since(0, limit=500) if e.kind == kind]


def post_tool(cc_id: str, cwd: str) -> dict[str, Any]:
    return {"hook_event_name": "PostToolUse", "session_id": cc_id, "cwd": cwd, "tool_name": "Read"}


# --- a run --------------------------------------------------------------------------------


async def test_a_non_blocking_question_closes_with_its_default_visibly(svc: Service) -> None:
    run = await started_run(svc)
    q = await ask(svc, run, default="Stripe", default_after="30m")
    assert not q.blocking and q.default_after is not None
    assert await svc.close_overdue_questions() == 0
    async with svc._tx() as tx:
        await repo.update_question(
            tx.conn, q.id, {"default_after": "2020-01-01T00:00:00.000+00:00"}
        )
    _, listed = await svc.list_questions("acme-shop-1", "all")  # a read closes it
    [closed] = listed
    assert (closed.status, closed.answered_by, closed.answer) == ("defaulted", "default", "Stripe")
    view = await svc.card_questions(await svc.get_task("acme-shop-1"))
    assert not view.open and [c.id for c in view.closed] == [q.id]
    assert [e.payload["question_id"] for e in await events(svc, "question.defaulted")] == [q.id]
    later = await svc.answer_question(q.id, text="Adyen after all")
    assert later.status == "answered" and later.defaulted_at is not None
    assert (
        "Adyen after all\nThis changes course: the question had closed with its default"
        in await owner_line(svc, q.id)
    )


async def test_a_withdrawn_question_hands_the_card_back(svc: Service) -> None:
    run = await started_run(svc)
    q = await ask(svc, run, blocking=True)
    assert (await svc.get_task("acme-shop-1")).waiting_on == "owner"
    with pytest.raises(ValidationError, match="Only the session that asked"):
        await svc.withdraw_question(q.id, session="someone-else")
    gone = await svc.withdraw_question(q.id, session=run.session_name)
    assert gone.status == "withdrawn" and gone.withdrawn_by == run.session_name
    assert (await svc.get_task("acme-shop-1")).waiting_on == run.session_name
    with pytest.raises(ValidationError, match="is withdrawn"):
        await svc.answer_question(q.id, text="late")
    with pytest.raises(ValidationError, match="only an open one"):
        await svc.withdraw_question(q.id, session=run.session_name)


async def test_a_coordinator_withdraws_any_question(svc: Service) -> None:
    run = await started_run(svc)
    q = await ask(svc, run)
    gone = await svc.withdraw_question(q.id, session="coord")
    assert gone.status == "withdrawn" and gone.withdrawn_by == "coord"


async def test_a_card_waiting_for_its_question_counts_once_in_the_inbox(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    await service.ask_question(task.key, "One?", "a", blocking=True, session=DESKTOP_A)
    inbox = await service.inbox(["owner"])
    assert len(inbox.questions) == 1 and [t.key for t in inbox.waiting] == [task.key]
    assert inbox.count == 1


# --- the rules of asking ---------------------------------------------------------------------


async def test_a_look_question_carries_the_url_and_account_and_blocks(svc: Service) -> None:
    run = await started_run(svc)
    look = await ask(
        svc,
        run,
        "Check the checkout page: the total under the button",
        kind="look",
        default="fine",
        url="http://127.0.0.1:5173/checkout",
        account="demo-buyer",
    )
    assert (look.kind, look.blocking, look.choices) == ("look", True, ["fine", "remarks"])
    assert (look.look_url, look.look_account) == ("http://127.0.0.1:5173/checkout", "demo-buyer")
    [asked] = await events(svc, "question.asked")
    assert asked.payload["url"] == look.look_url and asked.payload["account"] == "demo-buyer"
    with pytest.raises(ValidationError, match="Write the remarks"):
        await svc.answer_question(look.id, choice="remarks")
    done = await svc.answer_question(look.id, choice="remarks", text="The total is cut off")
    assert done.answer == "The total is cut off"
    assert (await owner_line(svc, look.id)).endswith("): The total is cut off")


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"text": "Sign in with password: Hunter2-x9"}, "looks like it contains a secret"),
        ({"text": "Use the key ghp_abcdefghijklmnopqrstuvwxyz0123"}, "github-token"),
        ({"account": "demo:Hunter2"}, "never user:password"),
        ({"url": "https://demo:pw@shop.test/"}, "must not carry a user or password"),
        ({"url": "https://shop.test/?token=abcdef123456"}, "secret"),
        ({"url": None}, "needs the url"),
        ({"url": "file:///etc/passwd"}, "http or https"),
        ({"blocking": False}, "always blocking"),
        ({"options": ["yes", "no"]}, "fine and remarks"),
    ],
)
async def test_a_look_question_is_refused_with_a_secret_or_without_its_url(
    svc: Service, fields: dict[str, Any], refusal: str
) -> None:
    run = await started_run(svc)
    values: dict[str, Any] = {
        "text": "Look at the cart",
        "kind": "look",
        "default": "fine",
        "url": "http://127.0.0.1:5173/cart",
        "account": "demo-buyer",
    } | fields
    text = values.pop("text")
    with pytest.raises(ValidationError, match=refusal):
        await ask(svc, run, text, **values)
    assert not await repo.list_questions(svc._r)


async def test_without_a_default_or_session_nothing_is_asked_and_five_are_open_at_most(
    svc: Service,
) -> None:
    run = await started_run(svc)
    with pytest.raises(ValidationError, match="needs a default"):
        await ask(svc, run, default="-")
    with pytest.raises(ValidationError, match="needs your session"):
        await svc.ask_question("acme-shop-1", "Which?", "Stripe")
    with pytest.raises(ValidationError, match="knows no live session with hook_session"):
        await svc.ask_question("acme-shop-1", "Which?", "Stripe", hook_session="cc-unknown")
    with pytest.raises(ValidationError, match="default_after is for non-blocking"):
        await ask(svc, run, blocking=True, default_after="2h")
    with pytest.raises(ValidationError, match="url and account are for look"):
        await ask(svc, run, url="https://shop.test/")
    for n in range(qrules.OPEN_MAX):
        await ask(svc, run, f"Question {n}?")
    with pytest.raises(ValidationError, match="already has 5 open questions"):
        await ask(svc, run, "One more?")
    first = (await repo.list_questions(svc._r))[-1]
    await svc.withdraw_question(first.id, session=run.session_name)
    await ask(svc, run, "One more?")  # room again


def test_deadlines_are_times_with_a_zone_or_durations() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    assert qrules.deadline(None, False, now) == "2026-11-12T11:00:00.000+00:00"
    assert qrules.deadline("90m", False, now) == "2026-11-12T11:30:00.000+00:00"
    assert qrules.deadline("2026-11-13T09:00:00Z", False, now) == "2026-11-13T09:00:00.000+00:00"
    assert qrules.deadline(None, True, now) is None
    for bad, why in (
        ("2026-11-13T09:00:00", "time zone"),
        ("soon", "a duration"),
        ("31d", "at most 30 days"),
        ("2026-11-12T10:00:30Z", "at least a minute"),
    ):
        with pytest.raises(ValidationError, match=why):
            qrules.deadline(bad, False, now)


# --- a desktop session ------------------------------------------------------------------------


async def test_a_desktop_session_gets_the_answer_as_an_owner_comment_once(
    service: Service,
) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    q, hint = await service.ask_question(
        task.key, "Rename the button?", "keep it", blocking=True, session=DESKTOP_A
    )
    assert "owner comment" in hint
    assert (await service.get_task(task.key)).waiting_on == "owner"  # a desktop session asks
    await service.answer_question(q.id, text="Call it Pay now")
    payload = hook("PostToolUse", CC_A)
    await service.record_hook(payload)
    answer = await service.hook_answer(payload)
    assert answer is not None
    text = answer["hookSpecificOutput"]["additionalContext"]
    line = f'Owner\'s answer to question {q.id} ("Rename the button?"): Call it Pay now'
    assert f"Owner comment on {task.key} (#" in text and line in text
    assert await service.hook_answer(payload) is None  # once
    found = await service.get_question(q.id)
    assert found.delivered_at is not None and found.comment_id is not None
    [delivered] = [e for e in await service.events_since(0) if e.kind == "question.delivered"]
    assert delivered.payload["question_id"] == q.id
    task = await service.get_task(task.key)
    assert task.waiting_on == q.asked_by and task.next_step and str(q.id) in task.next_step


async def test_the_card_names_the_next_open_blocking_question(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    first, _ = await service.ask_question(task.key, "One?", "a", blocking=True, session=DESKTOP_A)
    second, _ = await service.ask_question(task.key, "Two?", "b", blocking=True, session=DESKTOP_A)
    await service.answer_question(first.id, text="yes")
    task = await service.get_task(task.key)
    assert task.waiting_on == "owner" and f"Answer question {second.id}" in (task.next_step or "")
    await service.answer_question(second.id, text="no")
    assert (await service.get_task(task.key)).waiting_on == first.asked_by


async def test_only_the_asker_withdraws_a_desktop_question(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    q, _ = await service.ask_question(task.key, "Rename?", "keep", session=DESKTOP_A)
    with pytest.raises(ValidationError, match="or a coordinator"):
        await service.withdraw_question(q.id, session=DESKTOP_B)
    assert (await service.withdraw_question(q.id, session=DESKTOP_A)).status == "withdrawn"
    with pytest.raises(NotFoundError):
        await service.withdraw_question(q.id + 100, session=DESKTOP_A)


# --- MCP ---------------------------------------------------------------------------------------


async def test_mcp_asks_lists_withdraws_and_never_answers(service: Service) -> None:
    mcp = build_mcp(service)

    async def call(tool: str, **arguments: Any) -> str:
        result = await mcp.call_tool(tool, arguments)
        return "\n".join(c.text for c in result.content)

    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.create_task("acme-shop", "Checkout", stage="dev")
    names = {t.name for t in await mcp.list_tools()}
    assert {"question_ask", "question_list", "question_withdraw"} <= names
    assert not [
        n
        for n in names
        if "question" in n and n not in ("question_ask", "question_list", "question_withdraw")
    ]
    assert not [n for n in names if "answer" in n or "decide" in n]
    asked = await call(
        "question_ask",
        task="acme-shop-1",
        text="Which provider?",
        default="Stripe",
        options=["Stripe", "Adyen"],
        session="checkout-dev",
    )
    assert asked.startswith("Question #1 asked on **acme-shop-1** (question). Go on with the")
    listed = await call("question_list")
    assert "#1 acme-shop-1 (question, asked by checkout-dev" in listed
    assert "Options: Stripe, Adyen; default: Stripe" in listed
    with pytest.raises(ToolError, match="needs a default"):
        await mcp.call_tool(
            "question_ask", {"task": "acme-shop-1", "text": "Why?", "default": "-", "session": "x"}
        )
    with pytest.raises(ToolError, match="Only the session that asked"):
        await mcp.call_tool("question_withdraw", {"question": 1, "session": "someone"})
    gone = await call("question_withdraw", question=1, session="checkout-dev")
    assert gone == "Question #1 on acme-shop-1 withdrawn."
    assert await call("question_list") == "No open questions on all cards."
    assert "withdrawn by checkout-dev" in await call("question_list", status="all")


def test_the_tray_tells_a_new_blocking_question_only() -> None:
    payload = {
        "key": "acme-shop-1",
        "project": "acme-shop",
        "question_id": 3,
        "question_kind": "look",
        "blocking": True,
        "text": "Check the cart",
        "asked_by": "acme-shop-1 run 1",
    }
    event = Event(
        1, "2026-11-12T10:00:00Z", "question.asked", 1, 1, None, "acme-shop-1 run 1", payload
    )
    notice = classify(event, {"owner"})
    assert notice is not None and notice.kind == "question"
    assert notice.title == "Look at this on acme-shop-1 from acme-shop-1 run 1"
    assert notice.text == "Check the cart"
    quiet = Event(2, event.ts, event.kind, 1, 1, None, event.actor, payload | {"blocking": False})
    assert classify(quiet, {"owner"}) is None


# --- the board ---------------------------------------------------------------------------------


@pytest.fixture
def board(settings: Settings, token: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        service: Service = client.app.state.service
        run_in_app(client, service.upsert_project, "acme-shop", repo_path=CWD)
        run_in_app(client, service.create_task, "acme-shop", "Checkout", stage="dev")
        yield client


def test_the_inbox_and_the_card_show_questions_and_the_owner_answers(
    board: TestClient, token: str
) -> None:
    service: Service = board.app.state.service
    look, _ = run_in_app(
        board,
        service.ask_question,
        "acme-shop-1",
        "Check the cart",
        "fine",
        kind="look",
        url="http://127.0.0.1:5173/cart",
        account="demo-buyer",
        session=DESKTOP_A,
    )
    other, _ = run_in_app(
        board,
        service.ask_question,
        "acme-shop-1",
        "Rename the button?",
        "keep it",
        default_after="2h",
        session=DESKTOP_A,
    )
    inbox = board.get("/inbox").text
    assert 'id="inbox-questions"' in inbox and "Check the cart" in inbox
    assert '<a href="http://127.0.0.1:5173/cart" target="_blank"' in inbox
    assert "<code>demo-buyer</code>" in inbox and "look at this" in inbox
    assert "goes on with the default at" in inbox and "blocking" in inbox
    card = board.get("/p/acme-shop/t/acme-shop-1/questions").text
    assert f'id="question-{look.id}"' in card and f'id="question-{other.id}"' in card
    view = board.get("/p/acme-shop?task=acme-shop-1").text
    assert 'hx-get="/p/acme-shop/t/acme-shop-1/questions"' in view

    bearer = board.post(
        f"/questions/{look.id}/answer",
        data={"csrf_token": csrf_token(token), "choice": "fine"},
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
    no_csrf = board.post(
        f"/questions/{look.id}/answer", data={"choice": "fine"}, headers={"Origin": BASE}
    )
    assert no_csrf.status_code == 403
    refused = board.post(
        f"/questions/{look.id}/answer",
        data={"csrf_token": csrf_token(token), "choice": "remarks", "from": "card"},
        headers={"Origin": BASE, "HX-Request": "true"},
    )
    assert refused.status_code == 200 and "Write the remarks" in refused.text
    done = board.post(
        f"/questions/{look.id}/answer",
        data={"csrf_token": csrf_token(token), "choice": "fine", "from": "inbox"},
        headers={"Origin": BASE, "HX-Request": "true"},
    )
    assert done.status_code == 200 and f"Question {look.id} answered." in done.text
    assert f'id="question-{look.id}"' not in done.text  # it left the Inbox
    found = run_in_app(board, service.get_question, look.id)
    assert (found.status, found.answer, found.answered_by) == ("answered", "fine", "owner")
    plain = board.post(
        f"/questions/{other.id}/answer",
        data={"csrf_token": csrf_token(token), "text": "Pay now"},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    assert plain.status_code == 303 and plain.headers["location"].startswith("/p/acme-shop")
    audit = board.get("/audit?changes=1").text
    assert "question.answered" in audit and "question.asked" in audit


async def test_the_server_loop_closes_overdue_questions(svc: Service) -> None:
    run = await started_run(svc)
    q = await ask(svc, run, default_after="5m")
    async with svc._tx() as tx:
        await repo.update_question(
            tx.conn, q.id, {"default_after": "2020-01-01T00:00:00.000+00:00"}
        )
    loop = asyncio.create_task(svc.question_loop(0.01))
    try:
        for _ in range(200):
            if (await svc.get_question(q.id)).status == "defaulted":
                break
            await asyncio.sleep(0.01)
    finally:
        loop.cancel()
    assert (await svc.get_question(q.id)).status == "defaulted"
