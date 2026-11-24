"""The task dialog's Overview order: what waits for the owner first, then the next step, the plan,
the last report, deploy notes and the texts, the secondary blocks folded; a done card leads with
its last report. A block with nothing in it is left out."""

import re

from starlette.testclient import TestClient

from relaybay.service import Service
from tests.conftest import run_in_app
from tests.test_hooks import DESKTOP_A
from tests.test_session_card import dialog, project_task

EMPTY_TEXTS = (
    "No reports yet",
    "No open question",
    "No open request",
    "No comments yet",
    "No description yet",
    "No progress summary yet",
    "Loading…",
)


def headings(html: str) -> list[str]:
    """The block titles of the Overview, in order (a title's count badge left out)."""
    overview = html.split('role="tabpanel"', 2)[1]
    found = re.findall(
        r'<details class="panel fold[^"]*" data-fold="[^"]+"[^>]*>\s*<summary><h2[^>]*>(.*?)</h2>',
        overview,
    )
    return [re.sub(r"\s*<span.*", "", title) for title in found]


def fold(html: str, name: str) -> str:
    """The opening tag of the fold `name`."""
    match = re.search(rf'<details class="panel fold[^"]*" data-fold="{name}"[^>]*>', html)
    assert match, name
    return match.group(0)


def test_a_fresh_card_shows_no_empty_block(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service)
    for html in (dialog(board, task.key), board.get(f"/t/{task.key}").text):
        for text in EMPTY_TEXTS:
            assert text not in html, text
        for name in ("approvals", "questions", "report", "unread", "now", "plan-steps", "deploy"):
            assert f'data-fold="{name}"' not in html, name
        assert 'data-fold="description"' not in html and 'data-fold="progress"' not in html
        # The comment form stays reachable, folded into "Add comment".
        assert fold(html, "add-comment") and ">Add comment</h2></summary>" in html
        assert 'class="comment-form"' in html and f'id="comments-{task.key}"' in html
        # Task details always has data (the stage), folded.
        assert "open" not in fold(html, "details")


def test_a_block_with_data_is_shown(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(
        board, project_task, service, description="Why: the cart", next_step="Fix the sum"
    )
    run_in_app(board, service.add_agent_comment, task.key, "A reply for you")
    html = dialog(board, task.key)
    assert " open" in fold(html, "description") and "<p>Why: the cart</p>" in html
    assert " open" in fold(html, "now") and "Fix the sum" in html
    assert " open" in fold(html, "unread") and "A reply for you" in html
    # With comments the box is the plain section again.
    assert 'data-fold="add-comment"' not in html and ">Comments</h2>" in html


def test_a_report_opens_the_last_report_fold(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, project_task, service)
    run_in_app(board, service.add_report, task.key, "changed · S · first pass\n\nthe body")
    html = dialog(board, task.key)
    tag = fold(html, "report")
    assert tag.startswith('<details class="panel fold task-report"') and tag.endswith(" open>")
    assert f'<h2 id="report-title-{task.key}">Last report</h2>' in html


def test_an_open_card_puts_what_waits_for_the_owner_first(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(
        board,
        project_task,
        service,
        description="Why: the cart",
        progress="- the sum",
        next_step="Fix the sum",
    )
    run_in_app(board, service.update_task, task.key, {"deploy_notes": "Run migration 12"})
    run_in_app(board, service.set_plan, task.key, ["Write the sum", "Test it"])
    run_in_app(board, service.add_report, task.key, "changed · S · first pass")
    run_in_app(board, service.add_decision, "acme-shop", "Keep the cart", task=task.key)
    run_in_app(
        board,
        service.request_approval,
        task.key,
        "push",
        "feat/cart",
        "2 commits",
        session=DESKTOP_A,
    )
    run_in_app(
        board,
        service.ask_question,
        task.key,
        "Rename the button?",
        "keep it",
        default_after="2h",
        session=DESKTOP_A,
    )
    html = dialog(board, task.key)
    order = headings(html)
    expected = [
        "Questions",
        "Next",
        "Approvals",
        "Plan",
        "Last report",
        "Deploy notes",
        "Description",
        "Done so far",
        "Task details",
    ]
    assert [h for h in order if h in expected] == expected, order
    assert order.index("Decisions") > order.index("Task details")
    for name in ("approvals", "questions", "now", "plan-steps", "report", "deploy", "description"):
        assert " open" in fold(html, name), name
    for name in ("details", "session", "decisions"):
        assert " open" not in fold(html, name), name
    # The view renders them itself; their live refresh targets stay.
    assert f'id="approvals-{task.key}"' in html and f'id="questions-{task.key}"' in html
    assert f'hx-get="/p/acme-shop/t/{task.key}/questions" hx-trigger="rb:refresh"' in html


def test_a_done_card_leads_with_its_last_report(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(
        board,
        project_task,
        service,
        description="Why: the cart",
        progress="- the sum",
        next_step="Nothing",
    )
    run_in_app(board, service.update_task, task.key, {"deploy_notes": "Run migration 12"})
    run_in_app(board, service.set_plan, task.key, ["Write the sum"])
    run_in_app(board, service.add_report, task.key, "verified · S · all done")
    run_in_app(board, service.update_task, task.key, {"stage": "done"})
    html = dialog(board, task.key)
    order = headings(html)
    assert order[0] == "Last report", order
    assert " open" in fold(html, "report")
    for name in ("done-now", "done-plan-steps", "done-deploy", "done-description"):
        assert " open" not in fold(html, name), name
    for name in ("done-progress", "details"):
        assert " open" not in fold(html, name), name
