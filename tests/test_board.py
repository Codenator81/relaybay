import re
from pathlib import Path

from starlette.testclient import TestClient

import relaybay
from relaybay.app import create_app
from relaybay.config import load_settings
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app

HTMX = {"HX-Request": "true", "Origin": BASE}


def test_projects_page_lists_counts(board: TestClient, seeded: str) -> None:
    page = board.get("/")
    assert page.status_code == 200
    assert "Acme Shop" in page.text and 'href="/p/acme-shop"' in page.text
    assert 'sse-connect="/events"' in page.text
    fragment = board.get("/fragments/projects")
    assert "stage-count stage-dev" in fragment.text


def test_board_page_renders_columns_and_cards(board: TestClient, seeded: str) -> None:
    page = board.get("/p/acme-shop")
    assert page.status_code == 200
    for stage in ("plan", "dev", "test", "live", "done"):
        assert f'id="col-{stage}"' in page.text
        assert f'id="tab-{stage}"' in page.text
    assert f'id="card-{seeded}"' in page.text
    assert "Checkout page" in page.text and "feat/x" in page.text
    assert "first pass" not in page.text  # the last report lives in the task dialog
    # Agent text is escaped, never rendered as HTML.
    assert "<b>bold</b>" not in page.text
    # All projects: the Inbox count in the sidebar; app.js keeps only this project's events.
    assert 'sse-connect="/events"' in page.text and 'data-project="acme-shop"' in page.text


def test_fragments(board: TestClient, seeded: str) -> None:
    column = board.get("/p/acme-shop/col/dev")
    assert column.status_code == 200 and f'id="card-{seeded}"' in column.text
    card = board.get(f"/p/acme-shop/t/{seeded}/card")
    assert card.text.lstrip().startswith("<article")
    assert f'href="/p/acme-shop?task={seeded}"' in card.text  # the card opens the task dialog
    dialog = board.get(f"/p/acme-shop/t/{seeded}/dialog")
    assert dialog.status_code == 200 and 'class="dialog-frame"' in dialog.text
    # The report's size S sized the task: version 2.
    assert 'name="csrf_token"' in dialog.text and 'name="version" value="2"' in dialog.text
    # The last report body is rendered as markdown with raw HTML escaped.
    assert "&lt;b&gt;bold&lt;/b&gt;" in dialog.text and "<b>bold</b>" not in dialog.text
    view = board.get(f"/p/acme-shop/t/{seeded}/view")
    assert view.text.lstrip().startswith('<div class="task-view"')
    assert "task-form" not in view.text  # the read-only part, safe to refetch
    form = board.get(f"/p/acme-shop/t/{seeded}/form")
    assert form.text.lstrip().startswith('<div class="task-form-box"')
    assert board.get(f"/p/acme-shop/t/{seeded}/edit").status_code == 404  # inline editor is gone
    assert board.get("/p/acme-shop/t/demo-api-1/card").status_code == 404
    assert board.get("/p/acme-shop/t/demo-api-1/dialog").status_code == 404
    assert board.get("/p/nope").status_code == 404


def test_edit_card_goes_through_the_service(board: TestClient, seeded: str, token: str) -> None:
    form = {
        "title": "Checkout page v2",
        "stage": "test",
        "waiting_on": "reviewer",
        "next_step": "",
        "branch": "feat/x",
        "description": "Build the **checkout** page.\r\nCards only.",
        "progress": "- cart\r\n- totals",
        "version": "2",
        "csrf_token": csrf_token(token),
    }
    response = board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert response.status_code == 200
    assert 'class="form-ok"' in response.text and 'name="version" value="3"' in response.text

    service: Service = board.app.state.service
    task = run_in_app(board, service.get_task, seeded)
    assert (task.title, task.stage, task.waiting_on, task.version) == (
        "Checkout page v2",
        "test",
        "reviewer",
        3,
    )
    assert task.description == "Build the **checkout** page.\nCards only."
    assert task.progress == "- cart\n- totals"

    # Saving the same values again changes nothing (textarea CRLF is normalised).
    form["version"] = "3"
    board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert run_in_app(board, service.get_task, seeded).version == 3
    events = run_in_app(board, service.events_since, 0)
    assert {e.kind for e in events} >= {"task.stage_changed", "task.updated"}
    assert events[-1].actor == "board"


def test_edit_conflict_returns_the_form_with_an_error(
    board: TestClient, seeded: str, token: str
) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.update_task, seeded, {"waiting_on": "owner"})
    form = {"title": "Mine", "stage": "dev", "version": "2", "csrf_token": csrf_token(token)}
    response = board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div class="task-form-box"')
    assert "Someone else changed this task while you were editing" in response.text
    assert "task_get" not in response.text  # MCP wording stays out of the board
    assert 'class="action form-reload">Discard my edits and reload' in response.text
    assert 'value="Mine"' in response.text  # the user's input is kept
    # Marked dirty, so the next live event does not replace the kept input.
    assert '<form class="task-form" data-dirty="1"' in response.text
    assert 'name="version" value="3"' in response.text  # Save again overwrites

    invalid = dict(form, version="3", title="  ")
    response = board.post(f"/p/acme-shop/t/{seeded}", data=invalid, headers=HTMX)
    assert "title must not be empty" in response.text and 'data-dirty="1"' in response.text
    assert "Someone else changed" not in response.text

    fresh = board.get(f"/p/acme-shop/t/{seeded}/form").text
    assert "data-dirty" not in fresh


def test_stage_labels_from_config(tmp_path) -> None:
    (tmp_path / "config.toml").write_text(
        '[stages.labels]\ndev = "in progress"\n', encoding="utf-8"
    )
    settings = load_settings(home=tmp_path, environ={})
    with TestClient(create_app(settings), base_url=BASE) as client:
        token = settings.token_path.read_text(encoding="utf-8").strip()
        client.get(f"/login?token={token}")
        run_in_app(client, client.app.state.service.upsert_project, "acme-shop")
        page = client.get("/p/acme-shop")
    assert "in progress" in page.text


DESKTOP = "local_00000000-0000-0000-0000-000000000001"


def test_card_and_task_page_show_the_desktop_session(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.bind_session, seeded, "first-session")
    run_in_app(board, service.bind_session, seeded, DESKTOP, session_title="Checkout work")

    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    link = f"claude://claude.ai/epitaxy/{DESKTOP}"
    # The card has only an icon link; the session name is in its tooltip.
    assert f'<a class="open-desktop icon" href="{link}"' in card
    assert 'title="Open in desktop: Checkout work"' in card
    assert 'aria-label="Open Checkout work in the Claude desktop app"' in card
    assert card.index("open-desktop") < card.index("</div>")  # in the top row
    assert "copy-link" not in card and "badge session" not in card
    assert f'href="/t/{seeded}"' in card

    page = board.get(f"/t/{seeded}")
    assert page.status_code == 200
    text = page.text
    assert "Checkout work" in text and DESKTOP in text and f'href="{link}"' in text
    assert f'data-link="{link}"' in text and "copy-link" in text  # Copy link stays here
    history = text.index("History")  # the picker above it offers first-session again
    assert text.index("Checkout work", history) < text.index("first-session", history)
    assert "· current" in text and "· until" in text
    # Report bodies are markdown with raw HTML escaped.
    assert "&lt;b&gt;bold&lt;/b&gt;" in text and "<b>bold</b>" not in text
    assert board.get("/t/acme-shop-99").status_code == 404


def test_plain_session_has_no_desktop_link(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.bind_session, seeded, "checkout-dev")
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert "checkout-dev" not in card and "open-desktop" not in card
    assert "checkout-dev" in board.get(f"/t/{seeded}").text


def test_pages_read_the_event_id_before_the_data(
    board: TestClient, seeded: str, monkeypatch
) -> None:
    """An event committed while a page renders must be replayed, so the id is read first."""
    service: Service = board.app.state.service
    calls: list[str] = []

    def spy(name: str):
        original = getattr(service, name)

        async def wrapper(*args, **kwargs):
            calls.append(name)
            return await original(*args, **kwargs)

        monkeypatch.setattr(service, name, wrapper)

    for name in ("last_event_id", "list_projects", "get_project", "task_detail"):
        spy(name)
    for url, data_call in (
        ("/", "list_projects"),
        ("/p/acme-shop", "get_project"),
        (f"/t/{seeded}", "task_detail"),
        (f"/p/acme-shop?task={seeded}", "task_detail"),
    ):
        calls.clear()
        assert board.get(url).status_code == 200
        assert calls.index("last_event_id") < calls.index(data_call), (url, calls)


def test_report_state_and_size_are_badges(board: TestClient, seeded: str) -> None:
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert "badge-state" not in card and "card-report" not in card  # not on the card
    task = board.get(f"/t/{seeded}").text
    assert 'class="badge-state state-changed" title="Changed locally, not verified yet"' in task
    assert ">Changed</span>" in task
    assert 'class="badge-size" title="Medium"' not in task
    assert 'class="badge-size" title="Small">S</span>' in task
    dialog = board.get(f"/p/acme-shop/t/{seeded}/dialog").text
    assert "badge-state state-changed" in dialog


def test_card_shows_only_essentials(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(
        board,
        service.update_task,
        seeded,
        {
            "description": "Build the page <script>x</script>",
            "progress": "- **cart** done",
            "waiting_on": "owner",
            "next_step": "Write the cart",
        },
    )
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert "no-session" not in card  # no session at all: nothing about sessions
    assert "card-desc" not in card and "Build the page" not in card
    assert "card-progress" not in card and "Done:" not in card
    assert "card-report" not in card and "first pass" not in card
    assert 'class="chip waiting" title="Waiting on owner">waiting on owner</span>' in card
    assert '<code class="branch" title="Branch">feat/x</code>' in card
    assert '<p class="card-next clamp clamp-2"><span class="muted">Next:</span>' in card

    dialog = board.get(f"/p/acme-shop/t/{seeded}/dialog").text
    assert "&lt;script&gt;" in dialog and "<script>x" not in dialog
    assert "<strong>cart</strong> done" in dialog and "first pass" in dialog
    assert "No session is bound to this task." in dialog


def test_task_query_renders_the_dialog_open(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.update_task, seeded, {"description": "Why: _speed_"})
    plain = board.get("/p/acme-shop").text
    assert 'id="task-dialog"' in plain and "data-open=" not in plain
    assert 'class="dialog-frame"' not in plain

    page = board.get(f"/p/acme-shop?task={seeded}")
    assert page.status_code == 200
    assert f'data-open="{seeded}"' in page.text and 'class="dialog-frame"' in page.text
    assert "Why: <em>speed</em>" in page.text and 'id="col-dev"' in page.text  # board stays

    assert board.get("/p/acme-shop?task=acme-shop-99").status_code == 404


def test_task_page_shows_description_progress_and_unbind(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.update_task, seeded, {"description": "D1", "progress": "P1"})
    page = board.get(f"/t/{seeded}").text
    assert "<h2>Description</h2>" in page and "<p>D1</p>" in page and "<p>P1</p>" in page
    assert "Unbind session" not in page and "No session is bound" in page

    run_in_app(board, service.bind_session, seeded, DESKTOP, session_title="Checkout work")
    page = board.get(f"/t/{seeded}").text
    assert f'action="/p/acme-shop/t/{seeded}/unbind"' in page and "Unbind session" in page
    assert f'name="session" value="{DESKTOP}"' in page and "hx-confirm=" in page


def test_unbind_from_the_board(board: TestClient, seeded: str, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.bind_session, seeded, "checkout-dev")
    url = f"/p/acme-shop/t/{seeded}/unbind"

    refused = board.post(url, data={"session": "checkout-dev"}, headers=HTMX)
    assert refused.status_code == 403
    assert run_in_app(board, service.get_task, seeded).session_name == "checkout-dev"

    form = {"session": "someone-else", "csrf_token": csrf_token(token)}
    stale = board.post(url, data=form, headers=HTMX)
    assert stale.status_code == 200 and "form-error" in stale.text
    assert "This task is now bound to checkout-dev; nothing changed." in stale.text
    assert "task_get" not in stale.text
    assert run_in_app(board, service.get_task, seeded).session_name == "checkout-dev"
    plain_stale = board.post(url, data=form, headers={"Origin": BASE}, follow_redirects=False)
    assert plain_stale.status_code == 409
    assert plain_stale.text == "This task is now bound to checkout-dev; nothing changed."

    form["session"] = "checkout-dev"
    done = board.post(url, data=form, headers=HTMX)
    assert done.status_code == 200 and done.text.lstrip().startswith('<div class="task-view"')
    assert "No session is bound" in done.text and "· until" in done.text
    assert run_in_app(board, service.get_task, seeded).session_id is None
    events = run_in_app(board, service.events_since, 0)
    assert events[-1].kind == "task.session_unbound" and events[-1].actor == "board"

    # A plain form post (no JavaScript) returns to the task page.
    run_in_app(board, service.bind_session, seeded, "checkout-dev")
    plain = board.post(url, data=form, headers={"Origin": BASE}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == f"/t/{seeded}"


def test_bind_a_session_from_the_board(board: TestClient, seeded: str, token: str) -> None:
    service: Service = board.app.state.service
    page = board.get(f"/t/{seeded}").text
    assert "bind-picker" not in page  # no session seen yet: nothing to pick
    run_in_app(board, service.upsert_project, "demo-api")
    other = run_in_app(board, service.create_task, "demo-api", "Other", session=DESKTOP)
    run_in_app(board, service.unbind_session, other.key)
    [desktop] = [r.session for r in run_in_app(board, service.list_sessions)]

    page = board.get(f"/t/{seeded}").text
    assert "Bind a session" in page and f'name="session_id" value="{desktop.id}"' in page
    assert "Untitled desktop session" in page and f"desktop · {DESKTOP}" in page
    assert "other project" in page
    url = f"/p/acme-shop/t/{seeded}/bind"
    refused = board.post(url, data={"session_id": str(desktop.id)}, headers=HTMX)
    assert refused.status_code == 403
    form = {"session_id": str(desktop.id), "csrf_token": csrf_token(token)}
    done = board.post(url, data=form, headers=HTMX)
    assert done.status_code == 200 and done.text.lstrip().startswith('<div class="task-view"')
    assert "Unbind session" in done.text and "· current" in done.text
    assert run_in_app(board, service.get_task, seeded).session_external_id == DESKTOP
    events = run_in_app(board, service.events_since, 0)
    assert events[-1].kind == "task.session_bound" and events[-1].actor == "board"

    missing = board.post(url, data=form | {"session_id": "9999"}, headers=HTMX)
    assert missing.status_code == 200 and "Session #9999 not found." in missing.text
    plain = board.post(url, data=form, headers={"Origin": BASE}, follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == f"/t/{seeded}"


def test_unbind_needs_same_origin(board: TestClient, seeded: str, token: str) -> None:
    form = {"csrf_token": csrf_token(token)}
    foreign = {"HX-Request": "true", "Origin": "http://evil.example"}
    response = board.post(f"/p/acme-shop/t/{seeded}/unbind", data=form, headers=foreign)
    assert response.status_code == 403


def test_stale_next_step_is_marked(board: TestClient, seeded: str, monkeypatch) -> None:
    service: Service = board.app.state.service
    # Later than the seeded task's real creation time, and one tick per write.
    ticks = iter(range(1, 100))
    monkeypatch.setattr(
        "relaybay.service.now_iso", lambda: f"2999-01-01T00:00:{next(ticks):02d}.000+00:00"
    )
    run_in_app(board, service.update_task, seeded, {"next_step": "Write tests"})
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert '<p class="card-next clamp clamp-2">' in card and "stale-mark" not in card

    run_in_app(board, service.set_stage, seeded, "test")
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert '<p class="card-next clamp clamp-2 stale">' in card
    assert 'title="Set before the last stage change">stale</span>' in card
    view = board.get(f"/t/{seeded}").text
    assert '<dd class="muted">Write tests <span class="stale-mark"' in view

    run_in_app(board, service.set_stage, seeded, "done")
    assert "card-next" not in board.get(f"/p/acme-shop/t/{seeded}/card").text  # done hides it
    view = board.get(f"/t/{seeded}").text
    assert '<dd class="muted">Write tests</dd>' in view  # history: muted, no marker


def test_board_save_confirms_next_step_only_when_edited(
    board: TestClient, seeded: str, token: str, monkeypatch
) -> None:
    service: Service = board.app.state.service
    ticks = iter(range(1, 100))
    monkeypatch.setattr(
        "relaybay.service.now_iso", lambda: f"2999-01-01T00:00:{next(ticks):02d}.000+00:00"
    )
    run_in_app(board, service.update_task, seeded, {"next_step": "Write tests"})

    def save(**fields: str) -> None:
        task = run_in_app(board, service.get_task, seeded)
        form = {
            "title": task.title,
            "stage": task.stage,
            "next_step": task.next_step or "",
            "version": str(task.version),
            "csrf_token": csrf_token(token),
            **fields,
        }
        assert board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX).status_code == 200

    # A stage change from the board with the next step left as it is marks it stale.
    save(stage="test")
    assert run_in_app(board, service.get_task, seeded).next_step_stale
    # Editing another field keeps it stale.
    save(title="Checkout page, renamed")
    assert run_in_app(board, service.get_task, seeded).next_step_stale
    # Editing the next step itself confirms it.
    save(next_step="Review the tests")
    assert not run_in_app(board, service.get_task, seeded).next_step_stale


def test_card_chip_shows_who_and_the_title_the_full_text(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    text = "owner: accept the PR and merge"
    run_in_app(board, service.update_task, seeded, {"waiting_on": text})
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert f'title="Waiting on {text}">waiting on owner</span>' in card
    assert f'<span class="chip waiting">{text}</span>' in board.get(f"/t/{seeded}").text
    # Nothing before the colon: the text stays as it is.
    run_in_app(board, service.update_task, seeded, {"waiting_on": ": odd"})
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert ">waiting on : odd</span>" in card


def test_older_reports_are_an_accordion(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    older = run_in_app(board, service.add_report, seeded, "verified · M · second\n\nthe details")
    newest = run_in_app(board, service.add_report, seeded, "committed · S · third\n\nall of it")
    for url in (f"/t/{seeded}", f"/p/acme-shop/t/{seeded}/dialog", f"/p/acme-shop/t/{seeded}/view"):
        page = board.get(url).text
        assert "<p>all of it</p>" in page
        assert f'data-report="{newest[1].id}"' not in page  # the newest one is in full
        assert f'<details class="report report-older" data-report="{older[1].id}">' in page
        assert '<span class="report-gist">second</span>' in page
        assert "<p>the details</p>" in page
        assert page.index("all of it") < page.index("report-older")


def test_compact_rules_touch_only_task_cards(board: TestClient, seeded: str) -> None:
    css = board.get("/static/app.css").text
    selectors = [line for line in css.splitlines() if ":root[data-compact]" in line]
    assert len(selectors) >= 4
    assert all(".card[data-key]" in line for line in selectors), selectors
    assert ".answer-mark" not in " ".join(selectors)  # a fresh answer stays visible
    # Task cards carry the attribute, backlog cards (also `.card`, `.card-meta`) do not.
    assert 'class="card" id="card-' in board.get(f"/p/acme-shop/t/{seeded}/card").text
    service: Service = board.app.state.service
    run_in_app(board, service.add_backlog_item, "acme-shop", "A finding")
    page = board.get("/p/acme-shop/backlog").text
    start = page.index('class="card backlog-card')
    assert "data-key" not in page[start : page.index(">", start)]


def test_every_block_of_the_task_view_is_a_fold(board: TestClient, seeded: str) -> None:
    templates = Path(relaybay.__file__).parent / "web" / "templates"
    for name in ("_task_view.html", "_comments.html"):
        text = (templates / name).read_text(encoding="utf-8")
        assert '<section class="panel' not in text, name
        assert '<details class="panel"' not in text, name
    run_in_app(
        board,
        board.app.state.service.update_task,
        seeded,
        {"description": "D1", "progress": "P1"},
    )
    always = ("session", "description", "progress", "reports", "comments")
    for url in (f"/t/{seeded}", f"/p/acme-shop/t/{seeded}/dialog"):
        page = board.get(url).text
        for key in always:
            assert re.search(rf'<details class="panel fold[^"]*" data-fold="{key}" open', page), key
        assert "<summary><h2>Description</h2></summary>" in page
    dialog = board.get(f"/p/acme-shop/t/{seeded}/dialog").text
    assert '<details class="panel fold dialog-edit" data-fold="edit">' in dialog
    assert "<summary><h2>Edit task</h2></summary>" in dialog
    # The task page's Plan and Review stay plain panels, as the tabs do.
    task_page = board.get(f"/t/{seeded}").text
    assert '<section class="panel">\n    <h2>Plan</h2>' in task_page
    assert 'data-fold="plan"' not in dialog and 'data-fold="review"' not in dialog


def test_the_reports_accordion_lives_inside_its_fold(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.add_report, seeded, "verified · M · second")
    page = board.get(f"/t/{seeded}").text
    fold = page.split('data-fold="reports" open>', 1)[1].split('data-fold="', 1)[0]
    assert "<summary><h2>Last report</h2></summary>" in fold
    assert '<details class="report report-older" data-report="' in fold


def test_compact_controls_are_real_buttons_and_no_server_state(
    board: TestClient, seeded: str
) -> None:
    page = board.get("/p/acme-shop").text
    assert 'class="btn board-compact" aria-pressed="false"' in page
    assert (
        f'class="card-expand" aria-expanded="false" aria-label="Expand {seeded}"'
        in board.get(f"/p/acme-shop/t/{seeded}/card").text
    )
