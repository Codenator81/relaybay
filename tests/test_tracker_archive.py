"""External tracker mark, the done-column window with its archive, search and clearing fields."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import load_settings
from relaybay.domain.errors import ValidationError
from relaybay.mcp_server.format import tracker_label
from relaybay.security import csrf_token
from relaybay.service import Service
from relaybay.web import routes
from tests.conftest import BASE, run_in_app

HTMX = {"HX-Request": "true", "Origin": BASE}


def card(board: TestClient, key: str) -> str:
    return board.get(f"/p/acme-shop/t/{key}/card").text


@pytest.fixture
def strict(tmp_path: Path) -> Iterator[TestClient]:
    """A signed-in board with `require_tracker_log = true` and an empty acme-shop project."""
    (tmp_path / "config.toml").write_text("require_tracker_log = true\n", encoding="utf-8")
    settings = load_settings(home=tmp_path, environ={})
    with TestClient(create_app(settings), base_url=BASE) as client:
        token = settings.token_path.read_text(encoding="utf-8").strip()
        client.get(f"/login?token={token}")
        run_in_app(client, client.app.state.service.upsert_project, "acme-shop")
        yield client


async def test_tracker_fields_are_validated_and_versioned(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task(
        "acme-shop", "Checkout", tracker_ref="ENG-12", tracker_name="Linear"
    )
    assert (task.tracker_ref, task.tracker_name, task.tracker_url, task.tracker_logged) == (
        "ENG-12",
        "Linear",
        None,
        False,
    )
    with pytest.raises(ValidationError, match="http"):
        await service.update_task(task.key, {"tracker_url": "javascript:alert(1)"})
    with pytest.raises(ValidationError, match="longer than 100"):
        await service.update_task(task.key, {"tracker_ref": "X" * 101})
    with pytest.raises(ValidationError, match="true or false"):
        await service.update_task(task.key, {"tracker_logged": "yes"})

    updated = await service.update_task(
        task.key, {"tracker_url": "https://tracker.example/ENG-12", "tracker_logged": True}
    )
    assert updated.version == task.version + 1 and updated.tracker_logged
    event = (await service.events_since(0))[-1]
    assert event.kind == "task.updated"
    assert event.payload["changes"] == {
        "tracker_url": "https://tracker.example/ENG-12",
        "tracker_logged": True,
    }


def test_card_tracker_badges(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    text = card(board, seeded)
    assert "badge tracker" not in text  # open task without a key: nothing

    run_in_app(board, service.update_task, seeded, {"tracker_ref": "ENG-12"})
    text = card(board, seeded)
    assert '<span class="badge tracker" title="Issue in the tracker">ENG-12</span>' in text
    top = text[text.index('class="card-top"') : text.index("</div>")]
    assert "card-tracker" in top and "ENG-12" in top  # in the top row, next to the key

    changes = {
        "tracker_name": "Linear",
        "tracker_url": "https://tracker.example/ENG-12?a=1&b=2",
        "tracker_logged": True,
    }
    run_in_app(board, service.update_task, seeded, changes)
    text = card(board, seeded)
    assert (
        '<a class="badge tracker logged" href="https://tracker.example/ENG-12?a=1&amp;b=2"'
        ' target="_blank" rel="noopener noreferrer" title="Work log entered in the tracker">'
        "✓ Linear ENG-12</a>"
    ) in text
    assert "not-logged" not in text

    # Without require_tracker_log a done task is not nagged about the work log.
    run_in_app(board, service.update_task, seeded, {"stage": "done", "tracker_logged": False})
    text = card(board, seeded)
    assert "not-logged" not in text and ">Linear ENG-12</a>" in text
    view = board.get(f"/p/acme-shop/t/{seeded}/view").text
    assert "not-logged" not in view

    run_in_app(board, service.update_task, seeded, {"tracker_ref": None, "tracker_logged": True})
    assert (
        '<span class="badge tracker logged" title="Work log entered in the tracker">✓ logged'
        in (card(board, seeded))
    )


def test_not_logged_badge_with_require_tracker_log(strict: TestClient) -> None:
    service: Service = strict.app.state.service
    task = run_in_app(strict, service.create_task, "acme-shop", "Ship", stage="done")
    text = card(strict, task.key)
    assert "badge tracker not-logged" in text  # no tracker key at all: still "not logged"
    assert "badge tracker not-logged" in strict.get(f"/p/acme-shop/t/{task.key}/view").text

    run_in_app(strict, service.update_task, task.key, {"tracker_ref": "ENG-12"})
    text = card(strict, task.key)
    assert "badge tracker not-logged" in text and ">ENG-12</span>" in text
    run_in_app(strict, service.update_task, task.key, {"tracker_logged": True})
    text = card(strict, task.key)
    assert "not-logged" not in text and "✓ ENG-12" in text

    # An open task without the work log is not marked, even in this mode.
    run_in_app(strict, service.update_task, task.key, {"stage": "dev", "tracker_logged": False})
    assert "not-logged" not in card(strict, task.key)


def test_tracker_label_for_mcp(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, service.update_task, seeded, {"stage": "done"})
    assert tracker_label(task) == ""
    assert tracker_label(task, require_log=True) == "not logged"
    task = run_in_app(board, service.update_task, seeded, {"tracker_ref": "ENG-12"})
    assert tracker_label(task) == "ENG-12 · work log not entered"
    assert tracker_label(task, require_log=True) == "ENG-12 · not logged"


def test_tracker_text_is_escaped(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(
        board,
        service.update_task,
        seeded,
        {"tracker_ref": "<b>X-1</b>", "tracker_name": '"><script>'},
    )
    text = card(board, seeded)
    assert "<b>X-1</b>" not in text and "&lt;b&gt;X-1&lt;/b&gt;" in text
    assert "<script>" not in text


def test_done_card_hides_waiting_and_next(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.update_task, seeded, {"waiting_on": "owner", "next_step": "Ship it"})
    text = card(board, seeded)
    assert "waiting on owner" in text and "Ship it" in text
    run_in_app(board, service.set_stage, seeded, "done")
    text = card(board, seeded)
    assert "waiting on owner" not in text and "Ship it" not in text
    view = board.get(f"/p/acme-shop/t/{seeded}/view").text
    assert '<span class="muted">owner</span>' in view
    assert '<dd class="muted">Ship it</dd>' in view


def test_form_edits_and_clears_fields(board: TestClient, seeded: str, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.update_task, seeded, {"waiting_on": "owner", "next_step": "Ship"})
    task = run_in_app(board, service.get_task, seeded)
    form = board.get(f"/p/acme-shop/t/{seeded}/form").text
    for name in ("tracker_ref", "tracker_name", "tracker_url"):
        assert f'name="{name}"' in form
    assert '<input type="hidden" name="tracker_logged" value="0">' in form

    data = {
        "csrf_token": csrf_token(token),
        "version": str(task.version),
        "waiting_on": "",  # present but empty: clears
        "tracker_ref": "ENG-12",
        "tracker_name": "Linear",
        "tracker_url": "https://tracker.example/ENG-12",
        "tracker_logged": ["0", "1"],  # hidden field, then the ticked checkbox
    }
    response = board.post(f"/p/acme-shop/t/{seeded}", data=data, headers=HTMX)
    assert 'class="form-ok"' in response.text
    saved = run_in_app(board, service.get_task, seeded)
    assert saved.waiting_on is None and saved.next_step == "Ship"  # absent field is kept
    assert (saved.tracker_ref, saved.tracker_name, saved.tracker_logged) == (
        "ENG-12",
        "Linear",
        True,
    )
    assert "checked" in response.text.split('name="tracker_logged" value="1"')[1][:20]

    data.update(version=str(saved.version), tracker_logged="0", tracker_url="ftp://x")
    response = board.post(f"/p/acme-shop/t/{seeded}", data=data, headers=HTMX)
    assert "tracker_url must be an http" in response.text
    assert run_in_app(board, service.get_task, seeded).tracker_logged is True

    data.update(tracker_url="")
    board.post(f"/p/acme-shop/t/{seeded}", data=data, headers=HTMX)
    cleared = run_in_app(board, service.get_task, seeded)
    assert cleared.tracker_logged is False and cleared.tracker_url is None


def test_done_window_archive_and_search(board: TestClient, seeded: str, monkeypatch) -> None:
    service: Service = board.app.state.service
    logged = run_in_app(board, service.create_task, "acme-shop", "Old logged", stage="done")
    run_in_app(board, service.update_task, logged.key, {"tracker_logged": True})
    old = run_in_app(board, service.create_task, "acme-shop", "Old no tracker", stage="done")
    fresh = run_in_app(board, service.create_task, "acme-shop", "Fresh", stage="done")

    page = board.get("/p/acme-shop").text  # within the 7-day window: all visible
    assert all(f'id="card-{k}"' in page for k in (logged.key, old.key, fresh.key))
    assert "Show archived</a>" not in page

    later = datetime.now(UTC) + timedelta(days=8)
    monkeypatch.setattr(routes, "utcnow", lambda: later)
    page = board.get("/p/acme-shop").text
    # Default: tracker fields do not matter, every old done task is archived.
    assert not any(f'id="card-{k}"' in page for k in (logged.key, old.key, fresh.key))
    assert "3 closed more than 7 days ago" in page
    assert 'href="/p/acme-shop?archived=1">Show archived</a>' in page
    assert 'hx-get="/p/acme-shop/col/done"' in page

    shown = board.get("/p/acme-shop?archived=1").text
    assert all(f'id="card-{k}"' in shown for k in (logged.key, old.key, fresh.key))
    assert 'name="archived" value="1" checked' in shown
    assert 'hx-get="/p/acme-shop/col/done?archived=1"' in shown  # live refresh keeps the view
    column = board.get("/p/acme-shop/col/done?archived=1").text
    assert f'id="card-{logged.key}"' in column
    assert f'id="card-{logged.key}"' not in board.get("/p/acme-shop/col/done").text

    # Search: key or title, case-insensitive, across the archive too; other cards are filtered.
    found = board.get("/p/acme-shop?q=old LOGGED").text
    assert f'id="card-{logged.key}"' in found and f'id="card-{seeded}"' not in found
    assert f'id="card-{old.key}"' not in found
    assert 'hx-get="/p/acme-shop/col/done?q=old+LOGGED"' in found
    assert 'value="old LOGGED"' in found
    by_key = board.get(f"/p/acme-shop?q={seeded}").text
    assert f'id="card-{seeded}"' in by_key and f'id="card-{old.key}"' not in by_key


def test_unlogged_done_tasks_stay_with_require_tracker_log(strict: TestClient, monkeypatch) -> None:
    service: Service = strict.app.state.service
    logged = run_in_app(strict, service.create_task, "acme-shop", "Old logged", stage="done")
    run_in_app(strict, service.update_task, logged.key, {"tracker_logged": True})
    unlogged = run_in_app(strict, service.create_task, "acme-shop", "Old unlogged", stage="done")

    later = datetime.now(UTC) + timedelta(days=8)
    monkeypatch.setattr(routes, "utcnow", lambda: later)
    page = strict.get("/p/acme-shop").text
    assert f'id="card-{logged.key}"' not in page
    assert f'id="card-{unlogged.key}"' in page  # not logged: never archived
    assert "1 closed more than 7 days ago" in page


def test_done_window_from_config(tmp_path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text("done_window_days = 30\n", encoding="utf-8")
    settings = load_settings(home=tmp_path, environ={})
    with TestClient(create_app(settings), base_url=BASE) as client:
        token = settings.token_path.read_text(encoding="utf-8").strip()
        client.get(f"/login?token={token}")
        service: Service = client.app.state.service
        run_in_app(client, service.upsert_project, "acme-shop")
        task = run_in_app(client, service.create_task, "acme-shop", "Old", stage="done")
        run_in_app(client, service.update_task, task.key, {"tracker_logged": True})
        monkeypatch.setattr(routes, "utcnow", lambda: datetime.now(UTC) + timedelta(days=8))
        assert f'id="card-{task.key}"' in client.get("/p/acme-shop").text
        monkeypatch.setattr(routes, "utcnow", lambda: datetime.now(UTC) + timedelta(days=31))
        assert f'id="card-{task.key}"' not in client.get("/p/acme-shop").text
