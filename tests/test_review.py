"""Review: the review stage, the checklist, screenshots and their file safety, decisions to the
bound session, the board's Review tab, paste, the Review queue and the paged board."""

import base64
import os
import struct
import sys
import tempfile
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.db import connection
from relaybay.db.connection import FOREIGN_KEYS_OFF, connect
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.domain.review import MB, read_source, resolve_source, sniff_image
from relaybay.mcp_server.format import attachment_saved
from relaybay.security import csrf_token
from relaybay.service import Service, collect_notes
from relaybay.web.routes import static_versions
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_comments import context_of, fire, linked_task
from tests.test_hooks import CC_A
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import NOW, make_old_db

HTMX = {"HX-Request": "true", "Origin": BASE}
TMP_ROOT = Path(tempfile.gettempdir()).resolve()


def png(width: int = 2, height: int = 2, seed: int = 0) -> bytes:
    """A valid PNG made with the standard library; `seed` changes its pixels (and hash)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes((seed + i) % 256 for i in range(width * 3))
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


STEPS = [
    {"title": "Sign in", "details": "Open /login and sign in with the demo account."},
    {"title": "Add to cart", "details": "Add two products; the badge shows 2."},
    {"title": "Pay", "details": "Pay with the test card; the receipt opens."},
]


async def task_with_steps(svc: Service, **kwargs) -> str:
    task = await linked_task(svc, **kwargs)
    await svc.set_review_steps(task.key, STEPS)
    return task.key


def shots(tmp_path: Path) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir(exist_ok=True)
    return folder


async def repo_project(svc: Service, tmp_path: Path) -> str:
    """acme-shop whose repository is under tmp_path, with a task in dev."""
    await svc.upsert_project("acme-shop", repo_path=str(shots(tmp_path)))
    return (await svc.create_task("acme-shop", "Checkout", stage="dev")).key


# --- stage and migration -------------------------------------------------------------------


async def test_review_stage_sits_between_dev_and_test(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="review")
    assert task.stage == "review"
    with collect_notes() as notes:
        await service.set_stage(task.key, "test")
    assert any("review checklist of" in n and "review_steps_set" in n for n in notes)
    await service.set_review_steps(task.key, ["Look at it"])
    with collect_notes() as notes:
        await service.set_stage(task.key, "review")
    assert notes == []
    assert (await service.snapshot("acme-shop")).tasks[0].review_total == 1


async def test_upgrade_from_0010_keeps_tasks_and_their_rows(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=10)
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO report (task_id, author, summary, body_md, created_at)"
            " VALUES (1, 'agent', 'first', 'first', ?)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO comment (task_id, author_kind, author, body_md, created_at)"
            " VALUES (1, 'owner', 'owner', 'hello', ?)",
            (NOW,),
        )
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        detail = await svc.task_detail("acme-shop-1")
        assert (detail.task.title, detail.task.stage, detail.task.next_step) == (
            "Checkout page",
            "dev",
            "Review the cart",
        )
        assert [r.summary for r in detail.reports] == ["first"]
        assert [c.body_md for c in detail.comments] == ["hello"]
        moved = await svc.set_stage(detail.task.key, "review")
        assert moved.stage == "review"
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA foreign_keys") as cur:
            assert (await cur.fetchone())[0] == 1
        async with conn.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []
        async with conn.execute("SELECT sql FROM sqlite_master WHERE name = 'task'") as cur:
            assert "'review'" in (await cur.fetchone())[0]
    finally:
        await conn.close()


# --- checklist and decisions ----------------------------------------------------------------


async def test_steps_set_keeps_decisions_of_unchanged_steps(service: Service) -> None:
    key = await task_with_steps(service)
    _, review, _ = await service.review(key)
    first, second, third = review.steps
    await service.decide_review_step(key, first.id, "accept")
    await service.decide_review_step(key, second.id, "reject", "The badge shows 1")
    changed = [STEPS[0], {**STEPS[1], "details": "Add two products; the badge shows 2 now."}]
    task, review = await service.set_review_steps(key, changed)
    assert [(s.id, s.status) for s in review.steps] == [(first.id, "accepted"), (second.id, "open")]
    assert (task.review_total, task.review_accepted) == (2, 1)
    with pytest.raises(ValidationError, match="only set status 'open'"):
        await service.update_review_step(key, 1, status="accepted")
    reopened = await service.update_review_step(key, 1, status="open")
    assert reopened.status == "open" and reopened.decided_by is None
    with pytest.raises(NotFoundError):
        await service.update_review_step(key, 9, title="x")
    with pytest.raises(ValidationError):
        await service.set_review_steps(key, [{"title": " "}])
    assert third.id not in [s.id for s in review.steps]


async def test_decisions_reach_the_bound_session_as_comments(service: Service) -> None:
    key = await task_with_steps(service)
    _, review, _ = await service.review(key)
    first, second, _ = review.steps
    await service.decide_review_step(key, first.id, "accept")
    text = context_of(await fire(service, "PostToolUse", CC_A))
    assert "Step 1 'Sign in' accepted — continue with step 2" in text
    await service.decide_review_step(key, second.id, "reject", "The badge shows 1")
    text = context_of(await fire(service, "PreToolUse", CC_A))
    assert "Step 2 'Add to cart' rejected: The badge shows 1" in text
    _, _, comments = await service.review(key)
    assert [c.step_id for c in comments] == [first.id, second.id]
    with pytest.raises(ValidationError):
        await service.decide_review_step(key, first.id, "comment", " ")
    with pytest.raises(ValidationError):
        await service.decide_review_step(key, first.id, "approve")


async def test_last_accept_says_all_steps_are_accepted(service: Service) -> None:
    key = await task_with_steps(service)
    _, review, _ = await service.review(key)
    for step in review.steps[:2]:
        await service.decide_review_step(key, step.id, "accept")
    last = await service.decide_review_step(key, review.steps[2].id, "accept")
    assert last is not None
    assert last.body_md == "Step 3 'Pay' accepted — all 3 steps accepted"


# --- files: types, limits, folders ----------------------------------------------------------


async def test_attach_by_path_copies_and_dedupes(service: Service, tmp_path: Path) -> None:
    key = await repo_project(service, tmp_path)
    await service.set_review_steps(key, STEPS)
    source = shots(tmp_path) / "shot.png"
    source.write_bytes(png())
    item, new = await service.attach_file(key, path=str(source), step=2, caption="Cart *badge*")
    assert new and (item.mime, item.step_id is not None, item.caption_md) == (
        "image/png",
        True,
        "Cart *badge*",
    )
    _, stored = await service.attachment_file(item.id)
    assert stored.read_bytes() == source.read_bytes() and source.exists()  # copied, not moved
    assert stored.parent == service.attachments_dir / "acme-shop" / key
    assert stored.name != "shot.png" and stored.suffix == ".png"
    again, new = await service.attach_file(key, path=str(source))
    assert (again.id, new) == (item.id, False)
    caption = await service.set_attachment_caption(item.id, None)
    assert caption.caption_md is None
    _, review, _ = await service.review(key)
    assert [a.id for a in review.steps[1].attachments] == [item.id]


async def test_attach_refuses_files_that_are_not_images(service: Service, tmp_path: Path) -> None:
    key = await repo_project(service, tmp_path)
    folder = shots(tmp_path)
    fakes = {
        "logo.svg": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        "page.png": b"<!doctype html><script>alert(1)</script>",
        "svg-as.png": b"<?xml version='1.0'?><svg xmlns='http://www.w3.org/2000/svg'/>",
        "empty.png": b"",
    }
    for name, data in fakes.items():
        (folder / name).write_bytes(data)
        with pytest.raises(ValidationError):
            await service.attach_file(key, path=str(folder / name))
    for data in (b"<svg/>", b"not base64!"):
        encoded = base64.b64encode(data).decode() if data.startswith(b"<") else data.decode()
        with pytest.raises(ValidationError):
            await service.attach_file(key, content_base64=encoded)
    assert not any(service.attachments_dir.rglob("*.*"))
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert (
        sniff_image(b"GIF89a") == "image/gif" and sniff_image(b"\xff\xd8\xff\xe0") == "image/jpeg"
    )


async def test_attach_refuses_paths_outside_the_allowed_folders(
    service: Service, tmp_path: Path
) -> None:
    key = await repo_project(service, tmp_path)
    repo_dir = shots(tmp_path)
    outside = Path.home() / ".relaybay-test-outside.png"
    with pytest.raises(ValidationError, match="must be absolute"):
        await service.attach_file(key, path="shot.png")
    with pytest.raises(ValidationError, match="not found"):
        await service.attach_file(key, path=str(repo_dir / ".." / "missing.png"))
    with pytest.raises(ValidationError, match="not a regular file"):
        await service.attach_file(key, path=str(repo_dir))
    # A path that walks out of the repository with ".." is checked after resolving it.
    service.attachment_roots = []
    if not outside.resolve().is_relative_to(TMP_ROOT):
        outside.write_bytes(png())
        try:
            sneaky = repo_dir / ".." / ".." / ".." / ".." / ".." / ".." / ".." / ".."
            target = Path(str(sneaky) + str(outside.resolve())[2:])
            with pytest.raises(ValidationError, match="outside the folders"):
                await service.attach_file(key, path=str(outside))
            with pytest.raises(ValidationError):
                await service.attach_file(key, path=str(target))
        finally:
            outside.unlink()
    # relaybay's own data folder is never a source.
    (service.db_path.parent / "inside.png").write_bytes(png())
    with pytest.raises(ValidationError, match="data folder"):
        await service.attach_file(key, path=str(service.db_path.parent / "inside.png"))


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
async def test_attach_refuses_a_symlink_that_escapes(service: Service, tmp_path: Path) -> None:
    key = await repo_project(service, tmp_path)
    secret_dir = Path.home() / ".relaybay-test-secret"
    secret_dir.mkdir(exist_ok=True)
    secret = secret_dir / "shot.png"
    secret.write_bytes(png())
    link = shots(tmp_path) / "link.png"
    try:
        link.symlink_to(secret)
        if secret.resolve().is_relative_to(TMP_ROOT):
            pytest.skip("home is inside the temp folder")
        with pytest.raises(ValidationError, match="outside the folders"):
            await service.attach_file(key, path=str(link))
    finally:
        link.unlink(missing_ok=True)
        secret.unlink()
        secret_dir.rmdir()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
async def test_attach_refuses_a_device_or_pipe(service: Service, tmp_path: Path) -> None:
    key = await repo_project(service, tmp_path)
    pipe = shots(tmp_path) / "pipe.png"
    os.mkfifo(pipe)
    with pytest.raises(ValidationError, match="not a regular file"):
        await service.attach_file(key, path=str(pipe))


async def test_size_limits_per_file_and_per_task(service: Service, tmp_path: Path) -> None:
    key = await repo_project(service, tmp_path)
    service.attachment_max_bytes = 1 * MB
    service.attachment_task_max_bytes = 1 * MB
    big = png() + b"\x00" * MB
    with pytest.raises(ValidationError, match="larger than 1 MB"):
        await service.attach_file(key, content_base64=base64.b64encode(big).decode())
    (shots(tmp_path) / "big.png").write_bytes(big)
    with pytest.raises(ValidationError, match="larger than 1 MB"):
        await service.attach_file(key, path=str(shots(tmp_path) / "big.png"))
    half = png() + b"\x00" * (MB // 2)
    await service.attach_file(key, content_base64=base64.b64encode(half).decode())
    other = png(seed=7) + b"\x00" * (MB // 2)
    with pytest.raises(ValidationError, match="per task"):
        await service.attach_file(key, content_base64=base64.b64encode(other).decode())
    folder = service.attachments_dir / "acme-shop" / key
    assert len(list(folder.iterdir())) == 1  # the refused file was removed again


async def test_remove_and_orphan_sweep(tmp_path: Path) -> None:
    db = tmp_path / "data" / "relaybay.sqlite"
    svc = Service(db)
    await svc.start()
    try:
        key = await repo_project(svc, tmp_path)
        item, _ = await svc.attach_file(key, content_base64=base64.b64encode(png()).decode())
        keep, _ = await svc.attach_file(key, content_base64=base64.b64encode(png(seed=3)).decode())
        _, path = await svc.attachment_file(item.id)
        await svc.remove_attachment(key, item.id)
        assert not path.exists()
        with pytest.raises(NotFoundError):
            await svc.attachment_file(item.id)
        _, kept = await svc.attachment_file(keep.id)
    finally:
        await svc.close()
    orphan = kept.parent / "0123456789abcdef.png"
    orphan.write_bytes(png())
    stray = svc.attachments_dir / "gone-project" / "gone-project-1" / "x.png"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(png())
    svc = Service(db)
    await svc.start()
    try:
        assert kept.exists() and not orphan.exists() and not stray.exists()
        assert not stray.parent.parent.exists()
    finally:
        await svc.close()


# --- MCP -------------------------------------------------------------------------------------


async def test_mcp_review_tools(live_server: Callable[[], LiveServer], tmp_path: Path) -> None:
    live = live_server()
    source = shots(tmp_path) / "cart.png"
    source.write_bytes(png())
    async with mcp_connect(live) as session:
        await call(session, "project_upsert", slug="acme-shop", repo_path=str(shots(tmp_path)))
        created = await call(session, "task_create", project="acme-shop", title="Checkout")
        key = "acme-shop-1"
        assert key in created
        text = await call(session, "task_set_stage", task=key, stage="review")
        assert "review checklist of" in text
        text = await call(session, "review_steps_set", task=key, steps=STEPS)
        assert "3 step(s)" in text
        text = await call(
            session, "attach_file", task=key, path=str(source), step=2, caption="Badge"
        )
        assert "Attached screenshot #" in text
        text = await call(session, "review_get", task=key)
        assert "2. [open] Add to cart" in text and "Badge" in text
        text = await call(session, "review_step_update", task=key, step=3, title="Pay by card")
        assert "Step 3 'Pay by card' is open." in text
        text = await call(session, "attachment_caption", attachment_id=1, caption="-")
        assert "saved" in text
        text = await call(session, "project_snapshot", project="acme-shop")
        assert "review 0/3" in text and "## review" in text


# --- board ---------------------------------------------------------------------------------


def board_task(board: TestClient) -> tuple[str, list[int]]:
    """acme-shop with a task in review, a 3-step checklist and one screenshot on step 1."""
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Checkout", stage="review")
    run_in_app(board, service.set_review_steps, task.key, STEPS)
    run_in_app(
        board,
        service.attach_file,
        task.key,
        content_base64=base64.b64encode(png()).decode(),
        step=1,
        caption="The login page",
    )
    _, review, _ = run_in_app(board, service.review, task.key)
    return task.key, [s.id for s in review.steps]


def test_board_pages_and_review_column(board: TestClient) -> None:
    key, _ = board_task(board)
    html = board.get("/p/acme-shop").text
    assert 'id="board" class="board" data-page="1"' in html
    assert 'id="col-review"' in html and 'id="tab-review"' in html
    assert 'href="/p/acme-shop?page=2" data-page="2"' in html
    assert "Review 0/3" in html and f"?task={key}&amp;tab=review" in html
    assert 'data-page="2"' in board.get("/p/acme-shop?page=2").text.split('class="board"')[1][:20]
    assert 'data-page="2"' in board.get("/p/acme-shop?page=9").text.split('class="board"')[1][:20]
    kept = board.get("/p/acme-shop?q=check&page=2").text
    assert 'href="/p/acme-shop?q=check" data-page="1"' in kept


def test_review_tab_in_the_dialog(board: TestClient) -> None:
    key, _steps = board_task(board)
    html = board.get(f"/p/acme-shop?task={key}&tab=review").text
    assert 'data-tab="review">Review <span class="col-count">0/3</span>' in html
    assert f'id="panel-review-{key}"' in html and f'id="panel-overview-{key}"' in html
    assert 'aria-selected="true" data-tab="review"' in html
    assert "What, how and why" in html and "Open /login and sign in" in html
    assert 'data-caption="The login page"' in html and 'src="/files/1"' in html
    assert 'id="viewer"' in html
    overview = board.get(f"/p/acme-shop/t/{key}/dialog").text
    assert f'id="panel-review-{key}" aria-labelledby="tab-review-{key}" hidden' in overview


def test_board_decision_needs_cookie_and_csrf(board: TestClient, token: str) -> None:
    key, steps = board_task(board)
    url = f"/p/acme-shop/t/{key}/review/{steps[0]}"
    form = {"decision": "accept", "csrf_token": csrf_token(token)}
    assert board.post(url, data={"decision": "accept"}, headers=HTMX).status_code == 403
    bearer = {**HTMX, "Authorization": f"Bearer {token}"}
    assert board.post(url, data=form, headers=bearer).status_code == 403
    response = board.post(url, data=form, headers=HTMX)
    assert response.status_code == 200 and "1 of 3 accepted" in response.text
    response = board.post(
        f"/p/acme-shop/t/{key}/review/{steps[1]}",
        data={**form, "decision": "reject", "comment": "Badge shows 1"},
        headers=HTMX,
    )
    assert "1 rejected" in response.text
    service: Service = board.app.state.service
    _, comments = run_in_app(board, service.list_comments, key)
    assert [c.body_md for c in comments] == [
        "Step 1 'Sign in' accepted — continue with step 2",
        "Step 2 'Add to cart' rejected: Badge shows 1",
    ]
    plain = board.post(
        url, data={**form, "decision": "reopen"}, headers={"Origin": BASE}, follow_redirects=False
    )
    assert plain.status_code == 303 and plain.headers["location"].endswith("tab=review")


def test_paste_upload_into_a_step(board: TestClient, token: str) -> None:
    key, steps = board_task(board)
    url = f"/p/acme-shop/t/{key}/review/{steps[1]}/paste"
    headers = {"Origin": BASE, "Content-Type": "image/png"}
    assert board.post(url, content=png(seed=5), headers=headers).status_code == 403
    headers["X-Relaybay-CSRF"] = csrf_token(token)
    response = board.post(url, content=png(seed=5), headers=headers)
    assert response.status_code == 201, response.text
    assert board.post(url, content=png(seed=5), headers=headers).status_code == 200  # dedupe
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    refused = board.post(url, content=svg, headers={**headers, "Content-Type": "image/svg+xml"})
    assert refused.status_code == 400 and "SVG" in refused.text
    service: Service = board.app.state.service
    service.attachment_max_bytes = 1024
    big = board.post(url, content=png() + b"\x00" * 2048, headers=headers)
    assert big.status_code == 413
    _, review, _ = run_in_app(board, service.review, key)
    assert len(review.steps[1].attachments) == 1


def test_files_are_served_behind_the_cookie_with_their_type(
    client: TestClient, board: TestClient, token: str
) -> None:
    key, _ = board_task(board)
    response = board.get("/files/1")
    assert response.status_code == 200 and response.content == png()
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("inline")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert board.get("/files/99").status_code == 404
    board.cookies.clear()
    assert board.get("/files/1").status_code == 401
    form = {"csrf_token": csrf_token(token)}
    board.get(f"/login?token={token}")
    removed = board.post(f"/p/acme-shop/t/{key}/attachments/1/delete", data=form, headers=HTMX)
    assert removed.status_code == 200 and "/files/1" not in removed.text
    assert board.get("/files/1").status_code == 404


def test_inbox_review_queue(board: TestClient) -> None:
    key, _ = board_task(board)
    service: Service = board.app.state.service
    run_in_app(board, service.create_task, "acme-shop", "No checklist", stage="test")
    html = board.get("/inbox").text
    assert 'id="inbox-review"' in html and f"?task={key}&amp;tab=review" in html
    assert (
        "0/3 accepted" in html and "No checklist" not in html.split('id="inbox-review"')[1][:2000]
    )
    run_in_app(board, service.set_stage, key, "live")
    assert 'id="inbox-review"' not in board.get("/inbox").text


def test_notification_permission_only_from_the_settings_button(board: TestClient) -> None:
    html = board.get("/settings/notifications").text
    assert "data-notify-enable" in html and "Waiting for your acceptance" in html
    script = (Path(__file__).parents[1] / "src/relaybay/web/static/app.js").read_text("utf-8")
    # requestPermission is called in one place, the button's handler; never on load.
    assert script.count("requestPermission") == 1
    handler = script.split("function askNotifications")[1]
    assert "requestPermission" in handler.split("}")[1] or "requestPermission" in handler[:200]
    assert 'closest("[data-notify-enable]")' in script


def test_page_keys_are_not_taken_from_inputs_and_dialogs() -> None:
    script = (Path(__file__).parents[1] / "src/relaybay/web/static/app.js").read_text("utf-8")
    guard = script.split("function arrowsBelongElsewhere")[1].split("\n  }\n")[0]
    for needed in ("input, textarea, select, [contenteditable], dialog", "dialog[open]"):
        assert needed in guard
    assert "ctrlKey" in guard and "altKey" in guard


# --- review fixes: migration check, path prefixes, stat, no-op decisions, dedupe, assets ----


async def _fk_db(path: Path) -> None:
    conn = await connect(path)
    try:
        await conn.executescript(
            "PRAGMA foreign_keys=OFF;"
            "BEGIN;"
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (id INTEGER PRIMARY KEY,"
            " parent_id INTEGER REFERENCES parent(id));"
            "INSERT INTO parent (id) VALUES (1);"
            "INSERT INTO child (id, parent_id) VALUES (1, 1), (2, 99);"
            "PRAGMA user_version = 1;"
            "COMMIT;"
        )
    finally:
        await conn.close()


REBUILD_OK = (
    FOREIGN_KEYS_OFF + "\nCREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT);"
    "\nINSERT INTO parent_new (id) SELECT id FROM parent;\nDROP TABLE parent;"
    "\nALTER TABLE parent_new RENAME TO parent;"
)
REBUILD_BAD = (
    FOREIGN_KEYS_OFF + "\nCREATE TABLE parent_new (id INTEGER PRIMARY KEY);"
    "\nDROP TABLE parent;\nALTER TABLE parent_new RENAME TO parent;"
)


async def test_rebuild_keeps_old_orphans_and_refuses_new_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "fk.sqlite"
    await _fk_db(db)
    monkeypatch.setattr(connection, "_migrations", lambda: [(2, REBUILD_OK)])
    conn = await connect(db)
    try:
        with caplog.at_level("WARNING", logger="relaybay"):
            assert await connection.migrate(conn) == 2
        assert "dangling foreign key (child)" in caplog.text
    finally:
        await conn.close()

    db2 = tmp_path / "fk2.sqlite"
    await _fk_db(db2)
    monkeypatch.setattr(connection, "_migrations", lambda: [(2, REBUILD_BAD)])
    conn = await connect(db2)
    try:
        with pytest.raises(RuntimeError, match=r"1 broken foreign key\(s\) in child"):
            await connection.migrate(conn)
        async with conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 1  # rolled back
        async with conn.execute("SELECT COUNT(*) FROM parent") as cur:
            assert (await cur.fetchone())[0] == 1
        async with conn.execute("PRAGMA foreign_keys") as cur:
            assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "raw",
    [
        r"\\fileserver\share\shot.png",
        "//fileserver/share/shot.png",
        r"\\?\C:\shots\shot.png",
        r"\\.\pipe\relaybay",
        r"\\.\PhysicalDrive0",
    ],
)
def test_network_and_device_paths_are_refused_without_touching_the_disk(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_disk(*_args, **_kwargs):
        raise AssertionError("the disk was touched")

    monkeypatch.setattr(Path, "resolve", no_disk)
    monkeypatch.setattr(Path, "stat", no_disk)
    monkeypatch.setattr(os, "stat", no_disk)
    with pytest.raises(ValidationError, match="network share or a device path"):
        resolve_source(raw, [Path(r"C:\work")], [])


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are a Windows form")
def test_a_configured_share_passes_the_prefix_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def past_the_prefix(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "resolve", past_the_prefix)
    with pytest.raises(ValidationError, match="not found"):
        resolve_source(r"\\nas\shots\a.png", [Path(r"\\nas\shots")], [])
    with pytest.raises(ValidationError, match="network share"):
        resolve_source(r"\\nas\shotsx\a.png", [Path(r"\\nas\shots")], [])


def test_the_opened_file_must_be_the_checked_one(tmp_path: Path) -> None:
    first = tmp_path / "a.png"
    first.write_bytes(png())
    other = tmp_path / "b.png"
    other.write_bytes(png(seed=1))
    real, checked = resolve_source(str(first), [tmp_path], [])
    assert read_source(real, checked, MB) == png()
    # The path now holds another file than the one resolve_source checked.
    first.unlink()
    other.rename(first)
    with pytest.raises(ValidationError, match="changed while it was being read"):
        read_source(real, checked, MB)


async def test_repeated_decision_without_comment_is_a_no_op(service: Service) -> None:
    key = await task_with_steps(service)
    _, review, _ = await service.review(key)
    step = review.steps[0]
    assert await service.decide_review_step(key, step.id, "accept") is not None
    last = await service.last_event_id()
    assert await service.decide_review_step(key, step.id, "accept") is None  # double click
    assert await service.decide_review_step(key, step.id, "accept", "  ") is None
    assert await service.last_event_id() == last
    _, comments = await service.list_comments(key)
    assert len(comments) == 1
    text = context_of(await fire(service, "PostToolUse", CC_A))
    assert text.count("accepted") == 1
    noted = await service.decide_review_step(key, step.id, "accept", "Looks right")
    assert noted is not None and noted.body_md.endswith("Looks right")
    assert await service.decide_review_step(key, step.id, "reopen") is not None


async def test_dedupe_names_the_step_it_reused(service: Service, tmp_path: Path) -> None:
    key = await repo_project(service, tmp_path)
    await service.set_review_steps(key, STEPS)
    data = base64.b64encode(png()).decode()
    item, _ = await service.attach_file(key, content_base64=data, step=1)
    again, new = await service.attach_file(key, content_base64=data, step=3)
    assert (again.id, new, again.step_position) == (item.id, False, 1)
    text = attachment_saved(again, new)
    assert f"#{item.id}, on step 1; reused" in text and "{" not in text


def test_static_links_carry_a_content_hash(board: TestClient) -> None:
    html = board.get("/inbox").text
    versions = static_versions()
    for name in ("app.css", "nav.js", "htmx.min.js", "sse.js", "app.js"):
        assert f'"/static/{name}?v={versions[name]}"' in html
        assert len(versions[name]) == 12
    assert board.get(f"/static/app.js?v={versions['app.js']}").status_code == 200
