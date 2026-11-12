"""Tray notifications: what becomes a notice, the API, and the notifier with a fake server, icon
and clock. No display and no real program."""

import json
import logging
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from starlette.testclient import TestClient

from relaybay.desktop import app
from relaybay.desktop.app import DesktopController
from relaybay.desktop.autostart import Autostart
from relaybay.desktop.notifier import (
    STATE_FILE,
    TEXT_MAX,
    TITLE_MAX,
    WINDOW,
    Item,
    Notifier,
    Page,
    fetch_page,
    fit,
    summary,
)
from relaybay.desktop.settings import (
    DesktopSettings,
    load_desktop_settings,
    save_desktop_settings,
    settings_path,
)
from relaybay.desktop.tray import MenuState, build_menu
from relaybay.domain.models import Event
from relaybay.domain.notifications import DEFAULT_KINDS, KINDS, classify
from relaybay.service import Service

OWNERS = {"owner", "me"}


def event(kind: str, actor: str = "agent-1", task_id: int | None = 7, **payload: Any) -> Event:
    payload = {"key": "SHOP-1", "project": "acme-shop", **payload}
    return Event(1, "2026-11-06T10:00:00Z", kind, 1, task_id, None, actor, payload)


# --- classify ---------------------------------------------------------------


def test_a_task_that_now_waits_on_the_owner() -> None:
    changes = {"waiting_on": " Owner ", "next_step": "look"}
    notice = classify(event("task.updated", changes=changes), OWNERS, {7: "Fix the login"})
    assert notice is not None
    assert notice.kind == "waiting" and notice.key == "SHOP-1"
    assert notice.title == "Waiting on you: SHOP-1" and notice.text == "Fix the login"
    assert notice.url == "/p/acme-shop?task=SHOP-1"


@pytest.mark.parametrize("changes", [{"waiting_on": "the client"}, {"next_step": "x"}, {}])
def test_other_task_updates_are_no_notice(changes: dict[str, str]) -> None:
    assert classify(event("task.updated", changes=changes), OWNERS) is None


@pytest.mark.parametrize(
    ("to", "expected"), [("review", True), ("test", True), ("dev", False), ("done", False)]
)
def test_a_move_to_acceptance(to: str, expected: bool) -> None:
    notice = classify(event("task.stage_changed", to=to, title="Fix the login"), OWNERS)
    assert (notice is not None) is expected
    if notice:
        assert notice.kind == "review" and notice.text == "Fix the login"
        assert notice.title == "Waiting for your acceptance: SHOP-1"
        assert notice.url == "/p/acme-shop?task=SHOP-1&tab=review"


def test_only_an_agents_comment_is_a_reply() -> None:
    notice = classify(event("comment.added", author_kind="agent", excerpt="Done it"), OWNERS)
    assert notice is not None
    assert notice.kind == "reply" and notice.text == "Done it"
    assert notice.title == "Reply on SHOP-1 from agent-1"
    for author_kind in ("owner", "external"):
        assert classify(event("comment.added", author_kind=author_kind), OWNERS) is None


def test_an_answer_names_its_source() -> None:
    notice = classify(event("intake.answer_received", source="client-chat"), OWNERS, {7: "Login"})
    assert notice is not None
    assert notice.kind == "answer" and notice.title == "Answer received: SHOP-1"
    assert notice.text == "From client-chat: Login"
    bare = classify(event("intake.answer_received", source="mail"), OWNERS)
    assert bare is not None and bare.text == "From mail"


def test_a_session_that_starts_waiting() -> None:
    waiting = {"session": "agent-1", "status": "waiting", "activity": "needs permission"}
    notice = classify(event("session.status_changed", previous="working", **waiting), OWNERS)
    assert notice is not None
    assert notice.kind == "permission" and notice.key == "agent-1"
    assert notice.text == "needs permission" and notice.url == "/sessions"
    assert classify(event("session.status_changed", **waiting), OWNERS) is None  # still waiting
    idle = {**waiting, "status": "idle"}
    assert classify(event("session.status_changed", previous="working", **idle), OWNERS) is None


@pytest.mark.parametrize("actor", ["board", "owner", "Me"])
def test_the_owners_own_writes_are_no_news(actor: str) -> None:
    moved = event("task.stage_changed", actor=actor, to="review")
    assert classify(moved, OWNERS) is None


def test_unknown_events_and_old_payloads_are_no_notice() -> None:
    assert classify(event("report.added"), OWNERS) is None
    old = Event(1, "t", "comment.added", 1, 7, None, "agent-1", {"author_kind": "agent"})
    assert classify(old, OWNERS) is None


def test_permission_is_available_but_off() -> None:
    assert set(DEFAULT_KINDS) == set(KINDS) - {"permission"}


def test_a_brake_by_an_agent_is_a_notice_and_a_budgets_own_is_not() -> None:
    brake = event("brake.set", target="project acme-shop", reason="runaway", source="coordinator")
    notice = classify(brake, OWNERS)
    assert notice is not None and notice.kind == "brake"
    assert notice.title == "Brake on project acme-shop" and notice.text == "runaway"
    assert classify(event("brake.set", target="all", source="budget"), OWNERS) is None
    assert classify(event("brake.set", actor="board", source="owner"), OWNERS) is None


@pytest.mark.parametrize(
    ("kind", "action", "title"),
    [
        ("budget.warned", "warn", "Budget nearly spent: all"),
        ("budget.exceeded", "warn", "Budget spent: all"),
        ("budget.exceeded", "stop", "Budget spent, agents stopped: all"),
    ],
)
def test_budgets_are_notices(kind: str, action: str, title: str) -> None:
    budget = event(kind, target="all", used=90, max_tokens=100, action=action, actor="relaybay")
    notice = classify(budget, OWNERS)
    assert notice is not None and notice.kind == "budget"
    assert (notice.title, notice.text) == (title, "90 of 100 tokens")


def test_a_budgets_two_thresholds_do_not_collapse() -> None:
    warned = classify(event("budget.warned", target="all"), OWNERS)
    exceeded = classify(event("budget.exceeded", target="all"), OWNERS)
    assert warned is not None and exceeded is not None and warned.key != exceeded.key


# --- the API ----------------------------------------------------------------


async def _seed(service: Service) -> None:
    await service.upsert_project("acme-shop", name="Acme shop")
    await service.create_task("acme-shop", "Fix the login", actor="agent-1")


async def test_service_starts_from_the_newest_event_without_a_cursor(service: Service) -> None:
    await _seed(service)
    last, items = await service.notifications(None)
    assert last == await service.last_event_id() and last > 0 and items == []


async def test_service_tells_what_happened_after_the_cursor(service: Service) -> None:
    await _seed(service)
    start, _ = await service.notifications(None)
    key = await _key(service)
    await service.set_stage(key, "review", waiting_on="owner", actor="agent-1")
    last, items = await service.notifications(start)
    assert last == await service.last_event_id() and last > start
    assert [notice.kind for _id, notice in items] == ["review", "waiting"]
    assert [event_id for event_id, _n in items] == sorted(event_id for event_id, _n in items)
    assert items[1][1].text == "Fix the login"  # the event has no title, the task does
    assert await service.notifications(last) == (last, [])


async def _key(service: Service) -> str:
    return (await service.list_tasks(project="acme-shop"))[0].key


async def test_service_pages_through_events_and_never_skips_one(service: Service) -> None:
    await _seed(service)
    start, _ = await service.notifications(None)
    key = await _key(service)
    for stage in ("review", "dev", "test"):
        await service.set_stage(key, stage, actor="agent-1")
    first, page = await service.notifications(start, limit=1)
    assert [notice.kind for _id, notice in page] == ["review"]
    assert first < await service.last_event_id()
    second, page = await service.notifications(first, limit=1)
    assert page == [] and second > first  # the move to dev reads as no notice, the cursor goes on
    third, page = await service.notifications(second, limit=1)
    assert [notice.kind for _id, notice in page] == ["review"] and third > second


async def test_a_cursor_past_the_newest_event_moves_back(service: Service) -> None:
    await _seed(service)
    last, _ = await service.notifications(None)
    assert await service.notifications(last + 1000) == (last, [])


def test_api_answers_with_the_token_and_only_with_it(client: TestClient, token: str) -> None:
    assert client.get("/api/notifications").status_code == 401
    client.get(f"/login?token={token}", follow_redirects=False)
    assert client.get("/api/notifications").status_code == 401  # the cookie is for the board
    bearer = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/notifications", headers=bearer)
    assert response.status_code == 200
    assert set(response.json()) == {"last_id", "items"} and response.json()["items"] == []
    foreign = {**bearer, "Origin": "http://evil.example"}
    assert client.get("/api/notifications", headers=foreign).status_code == 403
    assert client.get("/api/notifications?after=x", headers=bearer).status_code == 422


# --- the notifier -----------------------------------------------------------


def item(kind: str = "review", key: str = "SHOP-1", text: str = "Fix the login") -> Item:
    return Item(kind, f"{kind}: {key}", text, key)


class Stand:
    """A fake server, icon and clock around one notifier."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.now = 1000.0
        self.last_id = 10
        self.queue: list[Item] = []
        self.down: Exception | None = None
        self.fetches: list[int | None] = []
        self.toasts: list[tuple[str, str]] = []
        self.toast_error: Exception | None = None
        self.dots: list[bool] = []
        self.notifier = self.fresh()

    def fresh(self, **settings: Any) -> Notifier:
        notifier = Notifier(
            self.folder, fetch=self.fetch, toast=self.toast, dot=self.dots.append, clock=self.clock
        )
        options = {"enabled": True, "quiet": False, "kinds": KINDS, "server": "s1", **settings}
        notifier.configure(**options)
        return notifier

    def clock(self) -> float:
        return self.now

    def toast(self, text: str, title: str) -> None:
        if self.toast_error:
            raise self.toast_error
        self.toasts.append((title, text))

    def fetch(self, after: int | None) -> Page:
        self.fetches.append(after)
        if self.down:
            raise self.down
        if after is None:
            return Page(self.last_id, ())
        items, self.queue = tuple(self.queue[:2]), self.queue[2:]
        self.last_id += len(items)
        return Page(self.last_id, items)

    def tick(self, *new: Item) -> None:
        self.queue += new
        self.notifier.tick()


@pytest.fixture
def stand(tmp_path: Path) -> Stand:
    stand = Stand(tmp_path)
    stand.tick()  # the first poll only learns where the events are
    assert stand.notifier.cursor == 10 and stand.toasts == []
    return stand


def test_the_first_poll_tells_nothing_of_the_past(stand: Stand) -> None:
    assert stand.fetches == [None]
    stand.tick()
    assert stand.fetches == [None, 10] and stand.toasts == []


def test_one_item_shows_its_full_text(stand: Stand) -> None:
    stand.tick(item())
    assert stand.toasts == [("review: SHOP-1", "Fix the login")]


def test_an_item_without_text_shows_its_title(stand: Stand) -> None:
    stand.tick(item(text=""))
    assert stand.toasts == [("review: SHOP-1", "review: SHOP-1")]


def test_several_items_in_one_window_are_one_summary(stand: Stand) -> None:
    stand.tick(item())  # the first goes at once
    stand.now += 5
    stand.tick(item(key="SHOP-2"), item(key="SHOP-3"), item("reply", "SHOP-4"))
    stand.now += 5
    assert len(stand.toasts) == 1  # the window is still open
    stand.now += WINDOW
    stand.tick()
    assert stand.toasts[1] == ("relaybay", "3 need you: 2 reviews, 1 reply")


def test_same_task_and_kind_collapse_in_the_window(stand: Stand) -> None:
    stand.tick(item())
    stand.now += 5
    stand.tick(item())  # shown already in this window
    stand.now += WINDOW
    stand.tick()
    assert len(stand.toasts) == 1
    stand.tick(item("reply", text="first"), item("reply", text="second"))
    assert stand.toasts[1] == ("reply: SHOP-1", "second")  # one toast, the newest wording


def test_a_later_window_tells_the_same_task_again(stand: Stand) -> None:
    stand.tick(item())
    stand.now += WINDOW
    stand.tick(item())
    assert len(stand.toasts) == 2


def test_a_move_to_review_that_also_sets_waiting_on_is_one_thing(stand: Stand) -> None:
    stand.tick(item("review"), item("waiting"))
    assert stand.toasts == [("review: SHOP-1", "Fix the login")]
    stand.now += WINDOW
    stand.tick(item("waiting", "SHOP-2"), item("review", "SHOP-2"))
    assert stand.toasts[1][0] == "review: SHOP-2"


def test_a_blocking_question_that_also_sets_waiting_on_is_one_thing(stand: Stand) -> None:
    stand.tick(item("question"), item("waiting"))
    assert stand.toasts == [("question: SHOP-1", "Fix the login")]
    stand.now += WINDOW
    stand.tick(item("waiting", "SHOP-2"), item("question", "SHOP-2"))
    assert stand.toasts[1][0] == "question: SHOP-2"


def test_a_long_downtime_ends_in_one_summary(stand: Stand) -> None:
    stand.notifier._save()
    later = stand.fresh()  # the program started again
    stand.notifier = later
    stand.tick(*(item(key=f"SHOP-{n}") for n in range(7)))  # pages of two
    assert stand.toasts == [("relaybay", "7 need you: 7 reviews")]
    assert later.cursor == 17


def test_kinds_that_are_off_are_dropped(tmp_path: Path) -> None:
    stand = Stand(tmp_path)
    stand.notifier = stand.fresh(kinds=("reply",))
    stand.tick()
    stand.tick(item("review"), item("reply"))
    assert stand.toasts == [("reply: SHOP-1", "Fix the login")]


def test_a_server_that_fails_keeps_the_cursor_and_logs_once(
    stand: Stand, caplog: pytest.LogCaptureFixture
) -> None:
    stand.down = URLError("refused")
    with caplog.at_level(logging.INFO, logger="relaybay.desktop"):
        stand.tick()
        stand.tick()
        assert stand.notifier.cursor == 10 and stand.toasts == []
        assert caplog.text.count("tray notifications paused") == 1
        stand.down = None
        stand.tick(item())
    assert "tray notifications resumed" in caplog.text
    assert len(stand.toasts) == 1


def test_a_failure_in_the_middle_keeps_what_was_read(stand: Stand) -> None:
    stand.queue += [item(key=f"SHOP-{n}") for n in range(2)]
    real = stand.fetch

    def second_page_fails(after: int | None) -> Page:
        if len(stand.fetches) == 2:
            stand.down = URLError("gone")
        return real(after)

    stand.notifier._fetch = second_page_fails
    stand.notifier.tick()
    assert stand.notifier.cursor == 12
    assert stand.toasts == [("relaybay", "2 need you: 2 reviews")]


def test_a_tray_that_refuses_keeps_the_items_and_asks_a_window_later(stand: Stand) -> None:
    stand.toast_error = OSError("no tray yet")
    with pytest.raises(OSError, match="no tray yet"):
        stand.tick(item())
    assert stand.notifier.pending
    stand.toast_error = None
    stand.tick()
    assert stand.toasts == []  # not on every poll
    stand.now += WINDOW
    stand.tick()
    assert stand.toasts == [("review: SHOP-1", "Fix the login")]


def test_quiet_mode_shows_a_dot_and_one_summary_on_leaving(stand: Stand) -> None:
    stand.notifier.set_quiet(True)
    stand.tick(item(), item("reply"))
    stand.now += 10 * WINDOW
    stand.tick(item(key="SHOP-2"))
    assert stand.toasts == []
    assert stand.dots == [True]
    stand.notifier.set_quiet(False)
    assert stand.toasts == [("relaybay", "3 need you: 2 reviews, 1 reply")]
    assert stand.dots == [True, False]
    stand.now += WINDOW
    stand.tick()
    assert len(stand.toasts) == 1


def test_leaving_quiet_with_nothing_held_says_nothing(stand: Stand) -> None:
    stand.notifier.set_quiet(True)
    stand.tick()
    stand.notifier.set_quiet(False)
    assert stand.toasts == [] and stand.dots == []


def test_quiet_mode_survives_a_restart_with_what_it_held(stand: Stand) -> None:
    stand.notifier.set_quiet(True)
    stand.tick(item(), item("reply"))
    stand.notifier = stand.fresh(quiet=True)
    assert stand.notifier.cursor == 12
    stand.tick()
    assert stand.dots == [True, True]  # a new program draws its dot again
    stand.notifier.set_quiet(False)
    assert stand.toasts == [("relaybay", "2 need you: 1 review, 1 reply")]


def test_another_server_starts_from_its_newest_event(stand: Stand) -> None:
    stand.notifier.set_quiet(True)
    stand.tick(item())
    stand.notifier.configure(enabled=True, quiet=False, kinds=KINDS, server="s2")
    assert stand.notifier.cursor is None and not stand.notifier.pending
    stand.tick()
    assert stand.toasts == []


def test_off_means_no_requests_and_on_again_starts_from_the_newest(stand: Stand) -> None:
    stand.notifier.configure(enabled=False, quiet=False, kinds=KINDS, server="s1")
    asked = len(stand.fetches)
    stand.queue.append(item())
    stand.notifier.tick()
    stand.notifier.local("server", "gone", "gone")
    assert len(stand.fetches) == asked and stand.toasts == [] and stand.notifier.cursor is None
    stand.queue.clear()
    stand.notifier.configure(enabled=True, quiet=False, kinds=KINDS, server="s1")
    stand.tick()
    assert stand.fetches[-1] is None and stand.toasts == []


def test_a_local_notice_goes_the_same_way(stand: Stand) -> None:
    stand.notifier.local("server", "Unreachable", "x does not answer.")
    assert stand.toasts == [("Unreachable", "x does not answer.")]
    stand.tick(item())
    stand.notifier.local("server", "Unreachable", "x does not answer.")  # in the window, shown
    stand.now += WINDOW
    stand.tick()
    assert stand.toasts[1:] == [("review: SHOP-1", "Fix the login")]


def test_a_corrupt_state_file_starts_anew(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / STATE_FILE).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="relaybay.desktop"):
        stand = Stand(tmp_path)
    assert "could not be read" in caplog.text and stand.notifier.cursor is None


def test_the_state_file_is_written_only_on_a_change(stand: Stand, monkeypatch) -> None:
    state = json.loads((stand.folder / STATE_FILE).read_text(encoding="utf-8"))
    assert state == {"server": "s1", "cursor": 10, "pending": []}
    writes = []
    monkeypatch.setattr("relaybay.desktop.notifier._write_private", lambda *a: writes.append(a))
    stand.tick()
    assert writes == []
    stand.tick(item())
    assert len(writes) == 1


def test_a_state_file_that_cannot_be_written_is_only_logged(
    stand: Stand, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*args: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr("relaybay.desktop.notifier._write_private", refuse)
    with caplog.at_level(logging.WARNING, logger="relaybay.desktop"):
        stand.tick(item())
    assert stand.toasts and "was not saved: in use" in caplog.text


def test_summary_counts_in_the_order_of_the_kinds() -> None:
    items = [item("reply", "A"), item("review", "B"), item("review", "C")]
    assert summary(items) == "3 need you: 2 reviews, 1 reply"
    assert summary([item("waiting", "A"), item("answer", "B")]) == "2 need you: 1 waiting, 1 answer"


def test_fit_stays_inside_the_balloon_in_utf16_units() -> None:
    assert fit("short", TITLE_MAX) == "short"
    assert fit("x" * 300, TEXT_MAX) == "x" * (TEXT_MAX - 3) + "..."
    fitted = fit("\U0001f600" * 200, TEXT_MAX)  # two UTF-16 units each
    assert len(fitted.encode("utf-16-le")) // 2 <= TEXT_MAX and fitted.endswith("...")


def test_every_toast_fits(stand: Stand) -> None:
    stand.tick(Item("review", "t" * 200, "x" * 1000, "SHOP-1"))
    title, text = stand.toasts[0]
    assert len(title) == TITLE_MAX and len(text) == TEXT_MAX


# --- fetch_page against a real server ----------------------------------------


def test_fetch_page_reads_the_real_endpoint(live_server) -> None:
    server = live_server()
    first = fetch_page(server.url, server.token, None)
    assert first.items == () and first.last_id >= 0
    assert fetch_page(server.url, server.token, first.last_id) == first
    with pytest.raises(URLError):
        fetch_page(server.url, "wrong", None)


# --- the program ------------------------------------------------------------


class FakeIcon:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []
        self.icon: Any = None
        self.title = "relaybay"

    def notify(self, message: str, title: str) -> None:
        self.notes.append((title, message))


@pytest.fixture
def ctl(
    tmp_path: Path, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> DesktopController:
    monkeypatch.setenv("RELAYBAY_HOME", str(tmp_path / "data"))
    controller = DesktopController(desktop_config_dir)
    controller.log_config = lambda: None
    controller.autostart = Autostart("linux", tmp_path / "home")
    controller.load()
    controller.icon = FakeIcon()
    controller.refresh_menu = lambda: None
    controller._redraw_icon = lambda: None
    return controller


def test_the_menu_has_open_inbox_and_the_quiet_checkbox() -> None:
    state = MenuState(server_mode="embedded", autostart=False, has_window=True, quiet=True)
    entries = build_menu(state)
    assert [e.text for e in entries[:3]] == ["Open board", "Open in browser", "Open Inbox"]
    quiet = next(e for e in entries if e.text == "Quiet notifications")
    assert quiet.action == "toggle_quiet" and quiet.checked is True
    off = MenuState(server_mode="embedded", autostart=False, has_window=True, notify=False)
    assert "Quiet notifications" not in [e.text for e in build_menu(off)]


def test_toggle_quiet_is_kept_in_the_settings_file(
    ctl: DesktopController, desktop_config_dir: Path
) -> None:
    ctl.toggle_quiet()
    assert ctl.notifier.quiet and load_desktop_settings(desktop_config_dir).notify_quiet
    ctl.toggle_quiet()
    assert not ctl.notifier.quiet and not load_desktop_settings(desktop_config_dir).notify_quiet


def test_load_passes_the_settings_on(ctl: DesktopController, desktop_config_dir: Path) -> None:
    saved = DesktopSettings(notify=False, notify_quiet=True, notify_kinds=("reply",))
    save_desktop_settings(desktop_config_dir, saved)
    ctl.load()
    assert not ctl.notifier.enabled and ctl.notifier.quiet and ctl.notifier.kinds == ("reply",)


def test_open_inbox_signs_in_and_goes_to_the_inbox(
    ctl: DesktopController, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(app.webbrowser, "open", opened.append)
    ctl.open_inbox()  # no window: the browser
    assert opened[0].endswith("&next=/inbox") and "/login?token=" in opened[0]

    class Window:
        url = ""

        def load_url(self, url: str) -> None:
            self.url = url

    shown: list[bool] = []
    ctl.window = Window()
    monkeypatch.setattr(ctl, "show_window", lambda: shown.append(True))
    ctl.open_inbox()
    assert ctl.window.url.endswith("&next=/inbox") and shown == [True]


def test_external_server_going_away_notifies_once(
    ctl: DesktopController, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_desktop_settings(
        desktop_config_dir, DesktopSettings(server_mode="external", server_url="http://b.lan:8765")
    )
    ctl.load()
    health: list[dict[str, str] | None] = [None]
    monkeypatch.setattr(app, "healthz", lambda *a, **k: health[0])
    ctl.notifier._last_toast = None
    for _ in range(3):
        ctl.watch_once()
    assert ctl.icon.notes == [
        ("The relaybay server is unreachable", "http://b.lan:8765 does not answer.")
    ]
    health[0] = {"status": "ok"}
    ctl.watch_once()
    assert len(ctl.icon.notes) == 1


def test_a_backend_without_notifications_only_logs(
    ctl: DesktopController, caplog: pytest.LogCaptureFixture
) -> None:
    ctl.icon.HAS_NOTIFICATION = False
    with caplog.at_level(logging.INFO, logger="relaybay.desktop"):
        ctl._toast("the text", "the title")
    assert ctl.icon.notes == [] and "notification: the title: the text" in caplog.text


def test_the_pending_dot_is_drawn_apart_from_the_update_dot() -> None:
    pytest.importorskip("PIL", reason="the desktop extra")
    from relaybay.desktop.tray import icon_image

    plain = icon_image().convert("RGBA")
    both = icon_image(update=True, pending=True)
    pending = icon_image(pending=True)
    assert pending.tobytes() != plain.tobytes()
    assert pending.tobytes() != both.tobytes() != icon_image(update=True).tobytes()
    # The upper right corner holds the dot, the lower right one stays as it was.
    box = (plain.width * 3 // 4, plain.height * 3 // 4, plain.width, plain.height)
    assert pending.crop(box).tobytes() == plain.crop(box).tobytes()


# --- settings ---------------------------------------------------------------


def test_the_new_keys_stay_out_of_the_file_at_their_defaults(tmp_path: Path) -> None:
    save_desktop_settings(tmp_path, DesktopSettings())
    assert "notify" not in settings_path(tmp_path).read_text(encoding="utf-8")


def test_the_new_keys_round_trip(tmp_path: Path) -> None:
    saved = DesktopSettings(notify=False, notify_quiet=True, notify_kinds=("reply", "permission"))
    save_desktop_settings(tmp_path, saved)
    assert load_desktop_settings(tmp_path) == saved
    save_desktop_settings(tmp_path, DesktopSettings(notify_kinds=()))
    assert load_desktop_settings(tmp_path).notify_kinds == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('notify = "yes"', "notify must be true or false"),
        ("notify_quiet = 1", "notify_quiet must be true or false"),
        ('notify_kinds = "reply"', "notify_kinds must be a list"),
        ("notify_kinds = [1]", "notify_kinds must be a list"),
        ('notify_kinds = ["from-the-future"]', "unknown kind.*from-the-future"),
    ],
)
def test_bad_values_name_the_key(tmp_path: Path, text: str, message: str) -> None:
    settings_path(tmp_path).write_text(text + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_desktop_settings(tmp_path)


def test_a_server_skips_kinds_of_a_newer_version(tmp_path: Path) -> None:
    settings_path(tmp_path).write_text(
        'notify_kinds = ["reply", "from-the-future"]\n', encoding="utf-8"
    )
    assert load_desktop_settings(tmp_path, ignore_unknown=True).notify_kinds == ("reply",)


def test_the_settings_form_keeps_the_keys_it_does_not_show(tmp_path: Path) -> None:
    from relaybay.desktop.settings_window import apply_form

    saved = DesktopSettings(notify=False, notify_quiet=True, notify_kinds=("reply",))
    save_desktop_settings(tmp_path, saved)
    _before, after = apply_form(tmp_path, {"server_mode": "embedded"})
    assert (after.notify, after.notify_quiet, after.notify_kinds) == (False, True, ("reply",))
