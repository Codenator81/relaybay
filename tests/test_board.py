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
    form = board.get(f"/p/acme-shop/t/{seeded}/edit")
    assert 'name="csrf_token"' in form.text and 'name="version" value="1"' in form.text
    # The last report body is rendered as markdown with raw HTML escaped.
    assert "&lt;b&gt;bold&lt;/b&gt;" in form.text
    assert board.get("/p/acme-shop/t/demo-api-1/card").status_code == 404
    assert board.get("/p/nope").status_code == 404


def test_edit_card_goes_through_the_service(board: TestClient, seeded: str, token: str) -> None:
    form = {
        "title": "Checkout page v2",
        "stage": "test",
        "waiting_on": "reviewer",
        "next_step": "",
        "branch": "feat/x",
        "version": "1",
        "csrf_token": csrf_token(token),
    }
    response = board.post(f"/p/acme-shop/t/{seeded}", data=form, headers=HTMX)
    assert response.status_code == 200
    assert 'id="col-test"' in response.text and "Checkout page v2" in response.text

    service: Service = board.app.state.service
    task = run_in_app(board, service.get_task, seeded)
    assert (task.title, task.stage, task.waiting_on, task.version) == (
        "Checkout page v2",
        "test",
        "reviewer",
        2,
    )
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
    assert response.headers["HX-Retarget"] == f"#card-{seeded}"
    assert "form-error" in response.text and "version 2" in response.text


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
