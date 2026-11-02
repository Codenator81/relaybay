from starlette.testclient import TestClient

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
    assert "first pass" in page.text
    # Agent text is escaped, never rendered as HTML.
    assert "<b>bold</b>" not in page.text
    assert 'sse-connect="/events?project=acme-shop"' in page.text


def test_fragments(board: TestClient, seeded: str) -> None:
    column = board.get("/p/acme-shop/col/dev")
    assert column.status_code == 200 and f'id="card-{seeded}"' in column.text
    card = board.get(f"/p/acme-shop/t/{seeded}/card")
    assert card.text.lstrip().startswith("<article")
    assert f'href="/p/acme-shop?task={seeded}"' in card.text  # the card opens the task dialog
    dialog = board.get(f"/p/acme-shop/t/{seeded}/dialog")
    assert dialog.status_code == 200 and 'class="dialog-frame"' in dialog.text
    assert 'name="csrf_token"' in dialog.text and 'name="version" value="1"' in dialog.text
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
        "version": "1",
        "csrf_token": csrf_token(token),
    }
    response = board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert response.status_code == 200
    assert 'class="form-ok"' in response.text and 'name="version" value="2"' in response.text

    service: Service = board.app.state.service
    task = run_in_app(board, service.get_task, seeded)
    assert (task.title, task.stage, task.waiting_on, task.version) == (
        "Checkout page v2",
        "test",
        "reviewer",
        2,
    )
    assert task.description == "Build the **checkout** page.\nCards only."
    assert task.progress == "- cart\n- totals"

    # Saving the same values again changes nothing (textarea CRLF is normalised).
    form["version"] = "2"
    board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert run_in_app(board, service.get_task, seeded).version == 2
    events = run_in_app(board, service.events_since, 0)
    assert {e.kind for e in events} >= {"task.stage_changed", "task.updated"}
    assert events[-1].actor == "board"


def test_edit_conflict_returns_the_form_with_an_error(
    board: TestClient, seeded: str, token: str
) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.update_task, seeded, {"waiting_on": "owner"})
    form = {"title": "Mine", "stage": "dev", "version": "1", "csrf_token": csrf_token(token)}
    response = board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<div class="task-form-box"')
    assert "Someone else changed this task while you were editing" in response.text
    assert "task_get" not in response.text  # MCP wording stays out of the board
    assert 'class="action form-reload">Discard my edits and reload' in response.text
    assert 'value="Mine"' in response.text  # the user's input is kept
    # Marked dirty, so the next live event does not replace the kept input.
    assert '<form class="task-form" data-dirty="1"' in response.text
    assert 'name="version" value="2"' in response.text  # Save again overwrites

    invalid = dict(form, version="2", title="  ")
    response = board.post(f"/p/acme-shop/t/{seeded}", data=invalid, headers=HTMX)
    assert "title must not be empty" in response.text and 'data-dirty="1"' in response.text
    assert "Someone else changed" not in response.text

    fresh = board.get(f"/p/acme-shop/t/{seeded}/form").text
    assert "data-dirty" not in fresh


def test_stage_labels_from_config(tmp_path) -> None:
    (tmp_path / "config.toml").write_text('[stages.labels]\ndev = "разработка"\n', encoding="utf-8")
    settings = load_settings(home=tmp_path, environ={})
    with TestClient(create_app(settings), base_url=BASE) as client:
        token = settings.token_path.read_text(encoding="utf-8").strip()
        client.get(f"/login?token={token}")
        run_in_app(client, client.app.state.service.upsert_project, "acme-shop")
        page = client.get("/p/acme-shop")
    assert "разработка" in page.text


DESKTOP = "local_00000000-0000-0000-0000-000000000001"


def test_card_and_task_page_show_the_desktop_session(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.bind_session, seeded, "first-session")
    run_in_app(board, service.bind_session, seeded, DESKTOP, session_title="Checkout work")

    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    link = f"claude://claude.ai/epitaxy/{DESKTOP}"
    assert "Checkout work" in card
    assert f'href="{link}"' in card and "Open in desktop" in card
    assert f'data-link="{link}"' in card and "copy-link" in card
    assert "paste the link into the Claude desktop app" in card
    assert f'href="/t/{seeded}"' in card
    assert "session-status" not in card  # status unknown: no status shown

    page = board.get(f"/t/{seeded}")
    assert page.status_code == 200
    text = page.text
    assert "Checkout work" in text and DESKTOP in text and f'href="{link}"' in text
    assert text.index("Checkout work", text.index("History")) < text.index("first-session")
    assert "· current" in text and "· until" in text
    # Report bodies are markdown with raw HTML escaped.
    assert "&lt;b&gt;bold&lt;/b&gt;" in text and "<b>bold</b>" not in text
    assert board.get("/t/acme-shop-99").status_code == 404


def test_plain_session_has_no_desktop_link(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.bind_session, seeded, "checkout-dev")
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert "checkout-dev" in card and "Open in desktop" not in card


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
    assert 'class="badge-state state-changed" title="Changed locally, not verified yet"' in card
    assert ">Changed</span>" in card
    assert 'class="badge-size" title="Medium"' not in card
    assert 'class="badge-size" title="Small">S</span>' in card
    # The gist sits on its own line, without the "state · size" prefix.
    assert '<p class="summary">first pass' in card
    task = board.get(f"/t/{seeded}").text
    assert "badge-state state-changed" in task


def test_card_shows_description_progress_and_no_session(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert 'class="chip no-session"' in card
    assert "card-desc" not in card and "card-progress" not in card  # empty fields: no blocks

    run_in_app(
        board,
        service.update_task,
        seeded,
        {"description": "Build the page <script>x</script>", "progress": "- **cart** done"},
    )
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert 'class="card-desc clamp clamp-3 markdown"' in card
    assert "&lt;script&gt;" in card and "<script>" not in card
    assert '<span class="card-label">Done:</span>' in card
    assert '<div class="clamp clamp-3 markdown"><ul>\n<li><strong>cart</strong> done' in card

    run_in_app(board, service.bind_session, seeded, "checkout-dev")
    card = board.get(f"/p/acme-shop/t/{seeded}/card").text
    assert "no-session" not in card and "checkout-dev" in card


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


def test_unbind_needs_same_origin(board: TestClient, seeded: str, token: str) -> None:
    form = {"csrf_token": csrf_token(token)}
    foreign = {"HX-Request": "true", "Origin": "http://evil.example"}
    response = board.post(f"/p/acme-shop/t/{seeded}/unbind", data=form, headers=foreign)
    assert response.status_code == 403
