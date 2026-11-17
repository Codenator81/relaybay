"""The session card on the board (design 0025): the state line with one Open, Copy the brief, the
card's order with the comment box outside every refetched fragment, the state word on the board
card, and why a comment has not arrived."""

import base64
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

from starlette.testclient import TestClient

from relaybay.service import Service
from tests.conftest import run_in_app
from tests.test_hooks import CC_A, CWD, DESKTOP_A, hook
from tests.test_review import png

HTMX = {"HX-Request": "true"}
VOID = frozenset(("input", "br", "img", "meta", "link", "hr", "source", "col", "area", "wbr"))


async def project_task(service: Service, title: str = "Checkout", **fields: object):
    await service.upsert_project("acme-shop", repo_path=CWD)
    return await service.create_task("acme-shop", title, stage="dev", **fields)


def dialog(board: TestClient, key: str) -> str:
    response = board.get(f"/p/acme-shop/t/{key}/dialog", headers=HTMX)
    assert response.status_code == 200
    return response.text


class Fragments(HTMLParser):
    """For each element of interest: the stack of its ancestors' attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.forms: list[list[dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = dict(attrs)
        if tag == "form" and "comment-form" in (found.get("class") or ""):
            self.forms.append([a for _, a in self.stack])
        if tag not in VOID:
            self.stack.append((tag, found))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def comment_form_ancestors(html: str) -> list[dict[str, str | None]]:
    parser = Fragments()
    parser.feed(html)
    assert len(parser.forms) == 1, "one comment box"
    return parser.forms[0]


def test_the_comment_box_is_outside_every_refetched_fragment(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service)
    for html in (dialog(board, task.key), board.get(f"/t/{task.key}").text):
        ancestors = comment_form_ancestors(html)
        refetched = [a for a in ancestors if "hx-get" in a or "hx-trigger" in a]
        assert refetched == [], refetched
    # The fragments a live event swaps hold no comment box.
    for path in ("view", "comments"):
        fragment = board.get(f"/p/acme-shop/t/{task.key}/{path}").text
        assert "comment-form" not in fragment


def test_the_card_order_is_state_report_comments_then_folds(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service)
    run_in_app(board, service.add_report, task.key, "changed · S · first pass")
    html = dialog(board, task.key)
    marks = [
        'class="panel state-line',
        'class="panel task-report"',
        'class="comment-thread"',
        'data-fold="session"',
        'data-fold="details"',
        'data-fold="description"',
        'class="comment-form"',
    ]
    places = [html.index(mark) for mark in marks]
    assert places == sorted(places)


def test_no_session_offers_the_brief(board: TestClient) -> None:
    service: Service = board.app.state.service
    long = ("Why: " + "the cart total is wrong. " * 40).strip()
    task = run_in_app(board, project_task, service, description=long, next_step="Fix the sum")
    image = base64.b64encode(png()).decode()
    run_in_app(board, service.attach_file, task.key, content_base64=image, filename="cart.png")
    html = dialog(board, task.key)
    assert 'class="state-word" id="state-acme-shop-1">No session</h2>' in html
    assert "Copy the brief" in html and "Open</" not in html
    brief = run_in_app(board, service.task_brief, task)
    assert brief.startswith("relaybay card acme-shop-1 (acme-shop): Checkout")
    assert 'task_bind_session("acme-shop-1"' in brief and "hook_session" in brief
    assert "Next step: Fix the sum" in brief and "## Description" in brief and long in brief
    [attachment] = run_in_app(board, service.review, task.key)[1].loose
    assert f"- cart.png: {service._file_path(attachment)}" in brief
    assert brief.rstrip().endswith("`state · size · gist`.")
    assert "relaybay card acme-shop-1" in html  # the fold with the brief to select by hand


def test_a_working_desktop_session_opens_in_the_desktop_app(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service, session=DESKTOP_A)
    run_in_app(board, service.record_hook, hook("SessionStart"))
    html = dialog(board, task.key)
    assert ">Working</h2>" in html and "Copy the brief" not in html
    assert f'class="btn primary open-desktop" href="claude://claude.ai/epitaxy/{DESKTOP_A}"' in html
    assert "started (startup)" in html  # the session's last activity
    board_page = board.get("/p/acme-shop").text
    assert 'class="card-state state-working"' in board_page and ">Working</span>" in board_page


def terminal_task(service: Service, *, ended: bool):
    async def make():
        task = await project_task(service, session="acme-shop")
        start = hook("SessionStart", relaybay_entrypoint="cli")
        await service.record_hook(start)
        await service.update_task_checked(
            task.key, {"branch": "x"}, session="acme-shop", hook_session=CC_A
        )
        if ended:
            await service.record_hook(hook("SessionEnd"))
        return task

    return make


def test_a_live_terminal_session_is_not_opened(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, terminal_task(service, ended=False))
    html = dialog(board, task.key)
    assert ">Working</h2>" in html
    assert 'disabled title="running in a terminal">Open</button>' in html
    assert '<p class="state-note muted">running in a terminal</p>' in html


def test_an_ended_terminal_session_is_resumed_and_gets_the_brief(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, terminal_task(service, ended=True))
    # A terminal session's end unbinds it; the card is about the ended session still.
    assert run_in_app(board, service.get_task, task.key).session_id is None
    html = dialog(board, task.key)
    assert ">Ended</h2>" in html and "Copy the brief" in html
    [row] = [r.session for r in run_in_app(board, service.list_sessions)]
    assert f'action="/sessions/{row.id}/open-terminal"' in html
    assert "resumes it in a new terminal" in html
    card = board.get(f"/p/acme-shop/t/{task.key}/card").text
    assert 'class="card-state state-ended"' in card and "dot-ended" in card


def test_an_unknown_surface_has_no_open_only_the_reason(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service, session="acme-shop")
    run_in_app(board, service.record_hook, hook("SessionStart"))
    html = dialog(board, task.key)
    assert "Open</" not in html
    assert "not known whether it runs in the desktop app or a terminal" in html


def test_waits_for_you_names_the_reasons_on_the_dialog_and_the_card(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service, session=DESKTOP_A, waiting_on="owner")
    run_in_app(board, service.record_hook, hook("SessionStart"))
    html = dialog(board, task.key)
    assert ">Waits for you</h2>" in html and '<p class="state-reasons">waiting on owner</p>' in html
    card = board.get(f"/p/acme-shop/t/{task.key}/card").text
    assert 'class="card-state state-waits" title="Waits for you: waiting on owner"' in card


def test_why_a_comment_has_not_arrived(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service)
    run_in_app(board, service.add_owner_comment, task.key, "Make it green", actor="owner")
    thread = board.get(f"/p/acme-shop/t/{task.key}/comments").text
    assert "not delivered yet: no session bound" in thread
    assert "A new comment waits: no session bound." in thread

    run_in_app(board, service.bind_session, task.key, "acme-shop")
    run_in_app(board, service.record_hook, hook("SessionStart"))
    run_in_app(
        board,
        service.update_task_checked,
        task.key,
        {"branch": "x"},
        session="acme-shop",
        hook_session=CC_A,
    )
    run_in_app(board, service.record_hook, hook("Stop"))
    thread = board.get(f"/p/acme-shop/t/{task.key}/comments").text
    assert "not delivered yet: the session is idle and the wake-up hook is not waiting" in thread


def test_the_inbox_lists_comments_waiting_longer_than_an_hour(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service)
    run_in_app(board, service.add_owner_comment, task.key, "Old one", actor="owner")
    run_in_app(board, service.add_owner_comment, task.key, "New one", actor="owner")
    assert "inbox-undelivered" not in board.get("/inbox").text
    db: Path = service.db_path
    with sqlite3.connect(db) as raw:
        raw.execute(
            "UPDATE comment SET created_at = '2026-01-01T00:00:00.000+00:00' WHERE body_md = ?",
            ("Old one",),
        )
    inbox = board.get("/inbox").text
    assert 'id="inbox-undelivered"' in inbox and "Comments not delivered" in inbox
    assert ": no session bound." in inbox and "Old one" in inbox and "New one" not in inbox
    assert run_in_app(board, service.inbox, ["owner"]).count >= 1
