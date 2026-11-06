"""Screenshot annotations: validation, soft delete and pin numbers, the flattened upload, the
comment to the agent, MCP image output, replies, the board's CSRF, escaping and the upgrade."""

import base64
import json
import math
import random
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from starlette.testclient import TestClient

from relaybay.db.connection import connect
from relaybay.domain import annotations as ann
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.mcp_server.format import annotation_lines, review_text
from relaybay.security import COOKIE_NAME, csrf_token, port_cookie, session_cookie_value
from relaybay.service import Service
from relaybay.web.routes import static_versions
from relaybay.web.sse import format_event
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_comments import context_of, fire, linked_task
from tests.test_hooks import CC_A
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import LATEST, NOW, make_old_db
from tests.test_review import STEPS, board_task, png

RED = ann.COLOURS[0]
RECT = {"kind": "rect", "colour": RED, "geometry": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}}
ARROW = {
    "kind": "arrow",
    "colour": RED,
    "geometry": {"x1": 0.1, "y1": 0.2, "x2": 0.5, "y2": 0.6},
}


def pin(x: float = 0.42, y: float = 0.18, body: str | None = "Button text is cut off") -> dict:
    return {"kind": "pin", "colour": RED, "geometry": {"x": x, "y": y}, "body": body}


async def shot_on_step(svc: Service, step: int = 2) -> tuple[str, int]:
    """A bound task with a checklist and one screenshot on `step`."""
    task = await linked_task(svc)
    await svc.set_review_steps(task.key, STEPS)
    item, _ = await svc.attach_file(
        task.key, content_base64=base64.b64encode(png()).decode(), step=step
    )
    return task.key, item.id


# --- validation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values, message",
    [
        ({**RECT, "kind": "circle"}, "kind must be one of"),
        ({**RECT, "colour": "#123456"}, "colour must be one of"),
        ({**RECT, "colour": "url(x)"}, "colour must be one of"),
        ({**RECT, "stroke_width": 0}, "stroke_width"),
        ({**RECT, "stroke_width": "wide"}, "stroke_width"),
        ({**RECT, "stroke_width": 0.5}, "stroke_width"),
        ({**RECT, "geometry": {"x": 0.1, "y": 0.2, "w": 0.3}}, "exactly"),
        ({**RECT, "geometry": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4, "z": 1}}, "exactly"),
        ({**RECT, "geometry": {"x": "0.1", "y": 0.2, "w": 0.3, "h": 0.4}}, "x must be a number"),
        ({**RECT, "geometry": {"x": True, "y": 0.2, "w": 0.3, "h": 0.4}}, "x must be a number"),
        ({**RECT, "geometry": {"x": 1.5, "y": 0.2, "w": 0.3, "h": 0.4}}, "between 0 and 1"),
        ({**RECT, "geometry": {"x": -0.5, "y": 0.2, "w": 0.3, "h": 0.4}}, "between 0 and 1"),
        ({**RECT, "geometry": {"x": float("nan"), "y": 0, "w": 0.3, "h": 0.4}}, "between 0 and 1"),
        ({**RECT, "geometry": {"x": float("inf"), "y": 0, "w": 0.3, "h": 0.4}}, "between 0 and 1"),
        ({**RECT, "geometry": {"x": 0.9, "y": 0.2, "w": 0.5, "h": 0.4}}, "stay on the image"),
        ({**RECT, "geometry": {"x": 0.1, "y": 0.2, "w": 0, "h": 0.4}}, "width and a height"),
        ({**ARROW, "geometry": {"x1": 0.1, "y1": 0.2, "x2": 0.1, "y2": 0.2}}, "different ends"),
        ({"kind": "freehand", "colour": RED, "geometry": {"points": [[0.1, 0.2]]}}, "at least two"),
        ({"kind": "freehand", "colour": RED, "geometry": {"points": [[0.1], [0.2]]}}, "pair"),
        ({"kind": "freehand", "colour": RED, "geometry": "M0 0"}, "at least two"),
        ({**RECT, "body": "A note"}, "Only a pin"),
        ({**RECT, "extra": 1}, "Unknown annotation field"),
        ({**pin(), "body": "x" * (ann.BODY_MAX + 1)}, "longer than"),
    ],
)
async def test_bad_annotations_are_refused(service: Service, values: dict, message: str) -> None:
    _, shot = await shot_on_step(service)
    with pytest.raises(ValidationError, match=message):
        await service.add_annotation(shot, values)
    assert (await service.annotations(shot))[1] == []


async def test_coordinates_past_the_edge_within_the_tolerance_are_held_to_the_image(
    service: Service,
) -> None:
    _, shot = await shot_on_step(service)
    added = await service.add_annotation(
        shot, {**RECT, "geometry": {"x": -0.005, "y": 0.5, "w": 0.5, "h": 0.505}}
    )
    assert added.geometry == {"x": 0.0, "y": 0.5, "w": 0.5, "h": 0.5}
    point = await service.add_annotation(shot, pin(1.004, 0.5))
    assert point.geometry == {"x": 1.0, "y": 0.5}


async def test_a_pen_line_is_simplified_and_capped_on_save(service: Service) -> None:
    _, shot = await shot_on_step(service)
    straight = [[i / 1000, i / 1000] for i in range(1000)]
    saved = await service.add_annotation(
        shot, {"kind": "freehand", "colour": RED, "geometry": {"points": straight}}
    )
    assert saved.geometry["points"] == [[0.0, 0.0], [0.999, 0.999]]

    zigzag = [[i / 4000, (i % 2) * 0.5] for i in range(4000)]
    capped = await service.add_annotation(
        shot, {"kind": "freehand", "colour": RED, "geometry": {"points": zigzag}}
    )
    assert 2 <= len(capped.geometry["points"]) <= ann.FREEHAND_POINTS_MAX

    too_many = [[0.5, 0.5]] * (ann.FREEHAND_POINTS_IN_MAX + 1)
    with pytest.raises(ValidationError, match="at most"):
        await service.add_annotation(
            shot, {"kind": "freehand", "colour": RED, "geometry": {"points": too_many}}
        )


async def test_a_secret_in_a_pin_or_a_reply_is_refused(service: Service) -> None:
    _, shot = await shot_on_step(service)
    secret = "password=hunter2hunter2hunter2"
    with pytest.raises(ValidationError, match="secret"):
        await service.add_annotation(shot, pin(body=secret))
    added = await service.add_annotation(shot, pin())
    with pytest.raises(ValidationError, match="secret"):
        await service.update_annotation(shot, added.id, secret)
    with pytest.raises(ValidationError, match="secret"):
        await service.reply_as_owner(shot, added.id, secret)
    with pytest.raises(ValidationError, match="secret"):
        await service.reply_as_agent(added.id, secret, session="checkout-dev")


# --- soft delete and numbering ---------------------------------------------------------------


async def test_pin_numbers_are_per_screenshot_and_never_reused(service: Service) -> None:
    key, shot = await shot_on_step(service)
    first = await service.add_annotation(shot, pin())
    second = await service.add_annotation(shot, pin(0.5, 0.5))
    shape = await service.add_annotation(shot, RECT)
    assert (first.pin_number, second.pin_number, shape.pin_number) == (1, 2, None)

    await service.remove_annotation(shot, second.id)
    third = await service.add_annotation(shot, pin(0.6, 0.6))
    assert third.pin_number == 3
    assert [a.id for a in (await service.annotations(shot))[1]] == [first.id, shape.id, third.id]

    other, _ = await service.attach_file(key, content_base64=base64.b64encode(png(seed=9)).decode())
    assert (await service.add_annotation(other.id, pin())).pin_number == 1


async def test_soft_delete_keeps_the_row_and_undo_restores_it(service: Service) -> None:
    _, shot = await shot_on_step(service)
    added = await service.add_annotation(shot, pin())
    await service.reply_as_owner(shot, added.id, "Still broken")
    with service.bus.subscribe() as sub:
        removed = await service.remove_annotation(shot, added.id)
        again = await service.remove_annotation(shot, added.id)  # nothing more happens
        assert sub.queue.get_nowait().kind == "annotation.removed"
        assert sub.queue.empty()
    assert removed.deleted_at and again.deleted_at == removed.deleted_at
    with pytest.raises(NotFoundError):
        await service.reply_as_owner(shot, added.id, "Too late")
    with pytest.raises(NotFoundError):
        await service.update_annotation(shot, added.id, "Too late")
    restored = await service.restore_annotation(shot, added.id)
    assert restored.deleted_at is None and restored.pin_number == 1
    assert [r.body_md for r in restored.replies] == ["Still broken"]


async def test_events_name_the_task_and_the_screenshot(service: Service) -> None:
    key, shot = await shot_on_step(service)
    with service.bus.subscribe() as sub:
        added = await service.add_annotation(shot, pin())
        await service.update_annotation(shot, added.id, "New text")
        await service.reply_as_agent(added.id, "Fixed", session="checkout-dev")
        kinds = []
        while not sub.queue.empty():
            event = sub.queue.get_nowait()
            kinds.append(event.kind)
            if event.kind.startswith("annotation."):
                assert event.payload["key"] == key and event.payload["attachment_id"] == shot
    assert kinds[:2] == ["annotation.added", "annotation.updated"]
    assert "annotation.replied" in kinds


async def test_a_live_event_names_the_screenshot_for_the_open_viewer(service: Service) -> None:
    _, shot = await shot_on_step(service)
    with service.bus.subscribe() as sub:
        await service.add_annotation(shot, pin())
        event = sub.queue.get_nowait()
    assert f'"attachment_id":{shot}' in format_event(event)


async def test_the_limit_of_annotations_per_screenshot(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, shot = await shot_on_step(service)
    monkeypatch.setattr(ann, "ANNOTATIONS_MAX", 1)
    await service.add_annotation(shot, pin())
    with pytest.raises(ValidationError, match="at most 1 annotations"):
        await service.add_annotation(shot, pin())


# --- the flattened file ----------------------------------------------------------------------


async def test_annotated_upload_keeps_the_original_and_replaces_the_previous(
    service: Service,
) -> None:
    key, shot = await shot_on_step(service)
    original, path = await service.attachment_file(shot)
    before = path.read_bytes()
    first = await service.store_annotated(shot, png(seed=1))
    assert first.annotated_name and first.annotated_name.endswith(".png")
    assert first.annotated_name != first.stored_name
    _, first_path = await service.annotated_file(shot)
    assert first_path.read_bytes() == png(seed=1) and first_path.parent == path.parent

    second = await service.store_annotated(shot, png(seed=2))
    _, second_path = await service.annotated_file(shot)
    assert second.annotated_name != first.annotated_name
    assert not first_path.exists() and second_path.read_bytes() == png(seed=2)
    assert path.read_bytes() == before
    item, _ = await service.attachment_file(shot)
    assert (item.sha256, item.size) == (original.sha256, original.size)

    await service.remove_attachment(key, shot)
    assert not second_path.exists() and not path.exists()


async def test_annotated_upload_refuses_other_types_and_oversize(service: Service) -> None:
    _, shot = await shot_on_step(service)
    for data in (b"<html>not an image</html>", b"\xff\xd8\xff" + b"0" * 32, b"GIF89a" + b"0" * 32):
        with pytest.raises(ValidationError, match="must be a PNG"):
            await service.store_annotated(shot, data)
    service.attachment_max_bytes = 1024
    with pytest.raises(ValidationError, match="larger than"):
        await service.store_annotated(shot, png() + b"\x00" * 2048)
    with pytest.raises(NotFoundError):
        await service.store_annotated(9999, png())
    item, _ = await service.attachment_file(shot)
    assert item.annotated_name is None
    assert sorted(p.name for p in service._file_path(item).parent.iterdir()) == [item.stored_name]


async def test_the_flattened_file_counts_in_the_tasks_limit(service: Service) -> None:
    key, shot = await shot_on_step(service)
    item, _ = await service.attachment_file(shot)
    big = png() + b"\x00" * 1000
    service.attachment_task_max_bytes = item.size + len(big)
    await service.store_annotated(shot, big)
    await service.store_annotated(shot, big)  # the one it replaces does not count
    with pytest.raises(ValidationError, match="per task"):
        await service.store_annotated(shot, big + b"\x00")
    with pytest.raises(ValidationError, match="per task"):
        await service.attach_file(key, content_base64=base64.b64encode(png(seed=7)).decode())
    assert sorted(p.name for p in service._file_path(item).parent.iterdir()) == sorted(
        [item.stored_name, (await service.attachment_file(shot))[0].annotated_name]
    )


async def test_the_startup_sweep_keeps_the_flattened_file(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite")
    await svc.start()
    try:
        _, shot = await shot_on_step(svc)
        await svc.store_annotated(shot, png(seed=1))
        _, path = await svc.annotated_file(shot)
    finally:
        await svc.close()
    orphan = path.with_name("orphan.png")
    orphan.write_bytes(png())
    svc = Service(tmp_path / "relaybay.sqlite")
    await svc.start()
    try:
        assert path.is_file() and not orphan.exists()
    finally:
        await svc.close()


# --- the comment to the agent ----------------------------------------------------------------


async def test_send_posts_one_owner_comment_tied_to_the_step_and_delivered(
    service: Service,
) -> None:
    key, shot = await shot_on_step(service)
    await service.add_annotation(shot, pin())
    await service.add_annotation(shot, pin(0.7, 0.65, "Wrong\ncurrency"))
    await service.add_annotation(shot, RECT)
    await service.store_annotated(shot, png(seed=1))
    comment = await service.send_annotations(shot)
    assert comment is not None and comment.from_owner
    _, review, _ = await service.review(key)
    assert comment.step_id == review.steps[1].id and comment.attachment_id == shot
    first, second = (await service.annotations(shot))[1][:2]
    assert comment.body_md == (
        f"Screenshot 1 of step 2 — 2 pins: #1 (x 0.42, y 0.18, annotation {first.id})"
        f" 'Button text is cut off'; #2 (x 0.70, y 0.65, annotation {second.id}) 'Wrong currency'."
        " Also marked: rectangle (x 0.10, y 0.20, w 0.30, h 0.40)."
        f" Annotated image: relaybay://attachments/{shot}/annotated."
        " Answer a pin with annotation_reply."
    )

    answer = await fire(service, "PostToolUse", CC_A)
    text = context_of(answer)
    assert "Button text is cut off" in text and f"relaybay://attachments/{shot}/annotated" in text

    assert await service.send_annotations(shot) is None  # a double click writes nothing
    await service.add_annotation(shot, pin(0.1, 0.1, "One more"))
    assert await service.send_annotations(shot) is not None


async def test_send_needs_markup_and_the_image(service: Service) -> None:
    _, shot = await shot_on_step(service)
    with pytest.raises(ValidationError, match="no markup"):
        await service.send_annotations(shot)
    await service.add_annotation(shot, RECT)
    with pytest.raises(ValidationError, match="annotated image first"):
        await service.send_annotations(shot)
    await service.store_annotated(shot, png(seed=1))
    comment = await service.send_annotations(shot)
    assert comment is not None and comment.body_md.startswith("Screenshot 1 of step 2 — no pins.")


def test_the_comment_stays_within_a_comments_size() -> None:
    pins = [
        ann.Annotation(
            id=i,
            attachment_id=1,
            kind="pin",
            colour=RED,
            stroke_width=0.004,
            geometry={"x": 0.5, "y": 0.5},
            pin_number=i,
            body_md="y" * 1000,
            created_by="owner",
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
        )
        for i in range(1, 60)
    ]
    text = ann.send_text("Screenshot 1 of step 1", 7, pins)
    assert len(text) <= ann.COMMENT_MAX
    assert "more: see review_get" in text and "Answer a pin with annotation_reply." in text
    assert "relaybay://attachments/7/annotated" in text


async def test_a_loose_screenshot_says_so(service: Service) -> None:
    task = await linked_task(service)
    item, _ = await service.attach_file(task.key, content_base64=base64.b64encode(png()).decode())
    await service.add_annotation(item.id, pin())
    await service.store_annotated(item.id, png(seed=1))
    comment = await service.send_annotations(item.id)
    assert comment is not None
    assert comment.body_md.startswith(f"Screenshot 1 of {task.key} (no step) — 1 pin: #1")


# --- replies ---------------------------------------------------------------------------------


async def test_replies_reach_the_other_side_through_the_comment_path(service: Service) -> None:
    key, shot = await shot_on_step(service)
    added = await service.add_annotation(shot, pin())
    await service.store_annotated(shot, png(seed=1))
    sent = await service.send_annotations(shot)
    assert sent is not None

    item, answered = await service.reply_as_agent(added.id, "Fixed", session="checkout-dev")
    assert item.id == shot
    assert [(r.author_kind, r.author, r.body_md) for r in answered.replies] == [
        ("agent", "checkout-dev", "Fixed")
    ]
    _, comments = await service.list_comments(key)
    reply = comments[-1]
    assert not reply.from_owner and reply.body_md == f"Pin #1 of screenshot #{shot}: Fixed"
    assert next(c for c in comments if c.id == sent.id).read_at  # the comment is answered

    both = await service.reply_as_owner(shot, added.id, "Thanks, one more thing")
    assert [r.author_kind for r in both.replies] == ["agent", "owner"]
    _, comments = await service.list_comments(key)
    assert comments[-1].from_owner and "Thanks, one more thing" in comments[-1].body_md
    assert comments[-1].attachment_id == shot
    with pytest.raises(ValidationError, match="must not be empty"):
        await service.reply_as_owner(shot, added.id, "   ")


async def test_a_reply_goes_to_a_live_pin_only(service: Service) -> None:
    _, shot = await shot_on_step(service)
    shape = await service.add_annotation(shot, RECT)
    with pytest.raises(NotFoundError):
        await service.reply_as_agent(shape.id, "Fixed")
    with pytest.raises(NotFoundError):
        await service.reply_as_agent(9999, "Fixed")


# --- MCP -------------------------------------------------------------------------------------


async def test_mcp_returns_images_and_replies(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop")
        await call(client, "task_create", project="acme-shop", title="Checkout", stage="dev")
        await call(client, "review_steps_set", task="acme-shop-1", steps=[{"title": "Sign in"}])
        await call(
            client,
            "attach_file",
            task="acme-shop-1",
            content_base64=base64.b64encode(png()).decode(),
            step=1,
        )
        none = await client.call_tool("attachment_get", {"id": 1, "annotated": True})
        assert none.is_error and "no annotated image yet" in none.content[0].text
        plain = await client.call_tool("review_get", {"task": "acme-shop-1"})
        assert [c.type for c in plain.content] == ["text"]

        browser = {
            "Origin": live.url,
            "X-Relaybay-CSRF": csrf_token(live.token),
            "Cookie": (f"{port_cookie(COOKIE_NAME, live.url)}={session_cookie_value(live.token)}"),
        }
        async with httpx2.AsyncClient(base_url=live.url, headers=browser) as http:
            added = (await http.post("/files/1/annotations", json=pin())).json()
            stored = await http.post("/files/1/annotated", content=png(seed=3))
            assert stored.status_code == 201, stored.text

        result = await client.call_tool("review_get", {"task": "acme-shop-1"})
        assert [c.type for c in result.content] == ["text", "image"]
        assert f"pin #1 (x 0.42, y 0.18, annotation {added['id']})" in result.content[0].text
        assert "annotated image of #1: attached to this answer" in result.content[0].text
        assert result.content[1].mime_type == "image/png"
        assert base64.b64decode(result.content[1].data) == png(seed=3)

        one = await client.call_tool("attachment_get", {"id": 1, "annotated": True})
        assert [c.type for c in one.content] == ["text", "image"]
        assert "Annotated image of screenshot #1" in one.content[0].text
        assert base64.b64decode(one.content[1].data) == png(seed=3)
        original = await client.call_tool("attachment_get", {"id": 1})
        assert base64.b64decode(original.content[1].data) == png()

        resource = await client.read_resource("relaybay://attachments/1/annotated")
        assert base64.b64decode(resource.contents[0].blob) == png(seed=3)

        said = await call(
            client, "annotation_reply", annotation=added["id"], body="Fixed", session="checkout-dev"
        )
        assert "Replied to pin #1 of screenshot #1 on **acme-shop-1**" in said
        again = await client.call_tool("review_get", {"task": "acme-shop-1"})
        assert "checkout-dev: Fixed" in again.content[0].text

        missing = await client.call_tool("annotation_reply", {"annotation": 999, "body": "x"})
        assert missing.is_error and "not found" in missing.content[0].text


async def test_a_huge_image_is_refused_with_a_clear_message(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, shot = await shot_on_step(service)
    await service.store_annotated(shot, png(seed=1))
    monkeypatch.setattr(ann, "IMAGE_TOOL_MAX_BYTES", 8)
    with pytest.raises(ValidationError, match="too large for a tool answer"):
        await service.attachment_image(shot, annotated=True)
    with pytest.raises(ValidationError, match="too large for a tool answer"):
        await service.attachment_image(shot, annotated=False)
    task, review, _ = await service.review(key)
    _, images, notes = await service.review_markup(task, review)
    assert images == {} and "too large for a tool answer" in notes[shot]


async def test_review_get_holds_a_limited_total_of_images(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    key, shot = await shot_on_step(service)
    other, _ = await service.attach_file(
        key, content_base64=base64.b64encode(png(seed=5)).decode(), step=2
    )
    await service.store_annotated(shot, png(seed=1))
    await service.store_annotated(other.id, png(seed=2))
    monkeypatch.setattr(ann, "REVIEW_IMAGES_MAX_BYTES", len(png(seed=1)))
    task, review, _ = await service.review(key)
    _, images, notes = await service.review_markup(task, review)
    assert list(images) == [shot]
    assert f"attachment_get({other.id}, annotated=true)" in notes[other.id]


# --- the board -------------------------------------------------------------------------------


def viewer_headers(token: str) -> dict[str, str]:
    return {"Origin": BASE, "X-Relaybay-CSRF": csrf_token(token)}


def test_board_writes_need_the_cookie_and_the_csrf_token(
    client: TestClient, board: TestClient, token: str
) -> None:
    board_task(board)
    origin = {"Origin": BASE}
    paths = (
        ("/files/1/annotations", {"json": RECT}),
        ("/files/1/annotated", {"content": png(seed=1)}),
        ("/files/1/send-annotations", {}),
        ("/files/1/annotations/1", {"json": {"body": "x"}}),
        ("/files/1/annotations/1/delete", {}),
        ("/files/1/annotations/1/restore", {}),
        ("/files/1/annotations/1/replies", {"json": {"body": "x"}}),
    )
    for path, kwargs in paths:
        assert board.post(path, headers=origin, **kwargs).status_code == 403, path
    bearer = {**viewer_headers(token), "Authorization": f"Bearer {token}"}
    assert board.post("/files/1/annotations", headers=bearer, json=RECT).status_code == 403
    service: Service = board.app.state.service
    assert run_in_app(board, service.annotations, 1)[1] == []
    board.cookies.clear()
    assert board.get("/files/1/annotations").status_code == 401
    assert board.get("/files/1/annotated").status_code == 401
    assert board.post(
        "/files/1/annotations", json=RECT, headers=viewer_headers(token)
    ).status_code in (
        401,
        403,
    )


def test_the_viewer_flow_over_http(board: TestClient, token: str) -> None:
    key, _ = board_task(board)
    headers = viewer_headers(token)
    empty = board.get("/files/1/annotations")
    assert empty.json() == {"attachment": 1, "annotated": False, "annotations": []}
    assert board.get("/files/1/annotated").status_code == 404

    added = board.post("/files/1/annotations", json=pin(), headers=headers)
    assert added.status_code == 201, added.text
    pin_id = added.json()["id"]
    assert added.json()["pin_number"] == 1 and added.json()["body"] == "Button text is cut off"

    bad = board.post("/files/1/annotations", json={**RECT, "colour": "red"}, headers=headers)
    assert bad.status_code == 400 and "colour must be one of" in bad.text
    broken = board.post("/files/1/annotations", content=b"{nope", headers=headers)
    assert broken.status_code == 400 and "not valid JSON" in broken.text
    huge = board.post("/files/1/annotations", content=b"x" * (300 * 1024), headers=headers)
    assert huge.status_code == 413

    saved = board.post(f"/files/1/annotations/{pin_id}", json={"body": "New"}, headers=headers)
    assert saved.json()["body"] == "New"
    replied = board.post(
        f"/files/1/annotations/{pin_id}/replies", json={"body": "Hello"}, headers=headers
    )
    assert replied.status_code == 201 and replied.json()["replies"][0]["author_kind"] == "owner"

    not_png = board.post("/files/1/annotated", content=b"<svg/>", headers=headers)
    assert not_png.status_code == 400 and "must be a PNG" in not_png.text
    service: Service = board.app.state.service
    limit = service.attachment_max_bytes
    service.attachment_max_bytes = 1024
    oversize = board.post("/files/1/annotated", content=png() + b"\x00" * 2048, headers=headers)
    assert oversize.status_code == 413
    service.attachment_max_bytes = limit
    assert board.post("/files/1/send-annotations", headers=headers).status_code == 400

    stored = board.post(
        "/files/1/annotated",
        content=png(seed=4),
        headers={**headers, "Content-Disposition": 'attachment; filename="../../evil.html"'},
    )
    assert stored.status_code == 201, stored.text
    served = board.get("/files/1/annotated")
    assert served.content == png(seed=4) and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "no-cache" in served.headers["cache-control"]
    assert "evil" not in served.headers["content-disposition"]
    assert board.get("/files/1").content == png()  # the original is untouched

    sent = board.post("/files/1/send-annotations", headers=headers)
    assert sent.status_code == 200 and sent.json()["comment"]
    assert board.post("/files/1/send-annotations", headers=headers).json() == {"comment": None}
    _, comments = run_in_app(board, service.list_comments, key)
    assert comments[-1].body_md.startswith("Screenshot 1 of step 1 — 1 pin: #1")

    assert board.post(f"/files/1/annotations/{pin_id}/delete", headers=headers).status_code == 200
    assert board.get("/files/1/annotations").json()["annotations"] == []
    assert board.post(f"/files/1/annotations/{pin_id}/restore", headers=headers).status_code == 200
    assert len(board.get("/files/1/annotations").json()["annotations"]) == 1


def test_an_annotation_of_another_screenshot_or_task_is_not_found(
    board: TestClient, token: str
) -> None:
    board_task(board)
    service: Service = board.app.state.service
    other_task = run_in_app(board, service.create_task, "acme-shop", "Search", stage="review")
    other, _ = run_in_app(
        board,
        service.attach_file,
        other_task.key,
        content_base64=base64.b64encode(png(seed=8)).decode(),
    )
    headers = viewer_headers(token)
    mine = board.post("/files/1/annotations", json=pin(), headers=headers).json()["id"]
    base = f"/files/{other.id}/annotations/{mine}"
    assert board.post(base, json={"body": "x"}, headers=headers).status_code == 404
    assert board.post(f"{base}/delete", headers=headers).status_code == 404
    assert board.post(f"{base}/restore", headers=headers).status_code == 404
    assert board.post(f"{base}/replies", json={"body": "x"}, headers=headers).status_code == 404
    assert board.post("/files/99/annotated", content=png(), headers=headers).status_code == 404
    assert board.post("/files/99/annotations", json=RECT, headers=headers).status_code == 404
    assert board.get(f"/files/{other.id}/annotated").status_code == 404
    assert run_in_app(board, service.annotations, 1)[1][0].body_md == "Button text is cut off"


def test_the_viewer_has_the_tools_and_no_inline_script(board: TestClient) -> None:
    key, _ = board_task(board)
    page = board.get(f"/p/acme-shop?task={key}&tab=review")
    assert page.status_code == 200
    text = page.text
    for tool in ("select", "rect", "arrow", "freehand", "pin"):
        assert f'data-tool="{tool}"' in text
    for colour in ann.COLOURS:
        assert f'data-colour="{colour}"' in text
    for action in ("undo", "delete", "hide", "send"):
        assert f'data-action="{action}"' in text
    for zoom in ("out", "in", "fit", "actual"):
        assert f'data-zoom="{zoom}"' in text
    assert 'class="viewer-zoom-level"' in text
    # One transform on this box zooms the image and the markup together; the thread is outside it.
    stage = text.split('<div class="viewer-zoom">', 1)[1]
    assert stage.index('class="viewer-svg"') < stage.index("</div>") < stage.index("viewer-thread")
    assert 'data-attachment="1"' in text
    assert "annotate.js" in text
    assert "<script>" not in text.replace('<script src="', "")
    assert "script-src" not in page.headers["content-security-policy"]
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "annotate.js" in static_versions()


def test_agent_text_stays_text(board: TestClient) -> None:
    key, _ = board_task(board)
    service: Service = board.app.state.service
    added = run_in_app(board, service.add_annotation, 1, pin(body="<b>owner</b>"))
    evil = "<img src=x onerror=alert(1)>"
    run_in_app(board, service.reply_as_agent, added.id, evil, session="<script>alert(2)</script>")

    # The API hands it over as JSON; the script only ever sets textContent.
    answer = board.get("/files/1/annotations")
    assert answer.headers["content-type"].startswith("application/json")
    assert answer.json()["annotations"][0]["replies"][0]["body"] == evil
    source = Path(__file__).parent.parent / "src/relaybay/web/static/annotate.js"
    code = source.read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in code

    # The same words in the task's thread are rendered by the board's markdown, escaped.
    thread = board.get(f"/p/acme-shop/t/{key}/comments")
    assert thread.status_code == 200 and "<img src=x" not in thread.text
    assert "<script>alert(2)" not in thread.text


# --- upgrade ---------------------------------------------------------------------------------


async def test_upgrade_from_0015_keeps_screenshots_and_adds_markup(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=15)
    folder = tmp_path / "attachments" / "acme-shop" / "acme-shop-1"
    folder.mkdir(parents=True)
    (folder / "abc.png").write_bytes(png())
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO review_step (task_id, position, title, status, created_by, created_at,"
            " updated_at) VALUES (1, 1, 'Sign in', 'accepted', 'agent', ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO attachment (task_id, step_id, stored_name, mime, size, sha256,"
            " created_by, created_at) VALUES (1, 1, 'abc.png', 'image/png', ?, 'h', 'agent', ?)",
            (len(png()), NOW),
        )
        await conn.execute(
            "INSERT INTO comment (task_id, author_kind, author, body_md, created_at, attachment_id)"
            " VALUES (1, 'owner', 'owner', 'Look', ?, 1)",
            (NOW,),
        )
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        item, path = await svc.attachment_file(1)
        assert (item.stored_name, item.annotated_name, item.annotated_size) == (
            "abc.png",
            None,
            None,
        )
        assert path.is_file()  # the sweep keeps it
        _, review, comments = await svc.review("acme-shop-1")
        assert review.steps[0].status == "accepted" and comments[0].attachment_id == 1
        added = await svc.add_annotation(1, pin())
        assert added.pin_number == 1
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == LATEST
        async with conn.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []
    finally:
        await conn.close()


def test_geometry_is_stored_as_compact_json() -> None:
    assert json.loads(ann.geometry_json({"x": 0.5, "y": 0.25})) == {"x": 0.5, "y": 0.25}
    assert " " not in ann.geometry_json({"points": [[0.1, 0.2], [0.3, 0.4]]})


# --- review findings -------------------------------------------------------------------------


async def test_only_the_comment_cuts_a_pin_and_it_says_where_the_rest_is(service: Service) -> None:
    key, shot = await shot_on_step(service)
    long_text = "Line one\nline two " + "y" * 1500 + " the very end"
    added = await service.add_annotation(shot, pin(body=long_text))
    reply_text = "z" * 1500 + " reply end"
    await service.reply_as_owner(shot, added.id, reply_text)
    await service.store_annotated(shot, png(seed=1))

    task, review, comments = await service.review(key)
    marks, images, notes = await service.review_markup(task, review)
    text = review_text(task, review, comments, marks, images, notes)
    assert long_text in text and reply_text in text
    lines = "\n".join(annotation_lines((await service.annotations(shot))[1]))
    assert long_text in lines and reply_text in lines

    sent = await service.send_annotations(shot)
    assert sent is not None and "the very end" not in sent.body_md
    assert "Long pin texts are cut here: review_get has them whole." in sent.body_md


def test_a_short_pin_text_is_not_flagged_as_cut() -> None:
    short_pin = ann.Annotation(
        id=1,
        attachment_id=1,
        kind="pin",
        colour=RED,
        stroke_width=0.004,
        geometry={"x": 0.5, "y": 0.5},
        pin_number=1,
        body_md="y" * ann.COMMENT_PIN_TEXT_MAX,
        created_by="owner",
        created_at=NOW,
        updated_at=NOW,
        deleted_at=None,
    )
    text = ann.send_text("Screenshot 1 of step 1", 7, [short_pin])
    assert "cut here" not in text and "…" not in text


async def test_a_held_previous_file_does_not_fail_the_upload_and_the_sweep_takes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    svc = Service(tmp_path / "relaybay.sqlite")
    await svc.start()
    try:
        _, shot = await shot_on_step(svc)
        await svc.store_annotated(shot, png(seed=1))
        _, held = await svc.annotated_file(shot)
        real_unlink = Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            if self == held:
                raise PermissionError(13, "The file is in use", str(self))
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", unlink)
        updated = await svc.store_annotated(shot, png(seed=2))
        monkeypatch.undo()
        assert updated.annotated_size == len(png(seed=2))
        _, current = await svc.annotated_file(shot)
        assert current != held and current.read_bytes() == png(seed=2)
        assert held.is_file() and "the next start removes it" in caplog.text
    finally:
        await svc.close()
    svc = Service(tmp_path / "relaybay.sqlite")
    await svc.start()
    try:
        assert not held.exists() and current.is_file()
    finally:
        await svc.close()


def test_the_route_answers_success_when_the_previous_file_is_held(
    board: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    board_task(board)
    headers = viewer_headers(token)
    assert board.post("/files/1/annotated", content=png(seed=1), headers=headers).status_code == 201
    service: Service = board.app.state.service
    _, held = run_in_app(board, service.annotated_file, 1)
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == held:
            raise PermissionError(13, "The file is in use", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    again = board.post("/files/1/annotated", content=png(seed=2), headers=headers)
    assert again.status_code == 201, again.text
    assert board.get("/files/1/annotated").content == png(seed=2)


def _pin_json(x: str = "0.1", extra: str = "") -> str:
    return f'{{"kind":"pin","colour":"{RED}",{extra}"geometry":{{"x":{x},"y":0.1}}}}'


@pytest.mark.parametrize(
    "raw",
    [
        _pin_json("1" + "0" * 400),
        _pin_json(extra=f'"stroke_width":1{"0" * 400},'),
        _pin_json("NaN"),
        _pin_json("Infinity"),
        _pin_json("1e999"),
        _pin_json("-1e999"),
        _pin_json("1" + "0" * 5000),
        "[" * 100_000,
    ],
)
def test_numbers_and_nesting_that_python_chokes_on_are_a_400(
    board: TestClient, token: str, raw: str
) -> None:
    board_task(board)
    response = board.post("/files/1/annotations", content=raw, headers=viewer_headers(token))
    assert response.status_code == 400, response.text
    assert board.get("/files/1/annotations").json()["annotations"] == []


@pytest.mark.parametrize("value", [10**400, -(10**400)])
def test_an_integer_too_large_for_a_float_is_a_validation_error(value: int) -> None:
    with pytest.raises(ValidationError):
        ann.clean_geometry("pin", {"x": value, "y": 0.1})
    with pytest.raises(ValidationError):
        ann.clean_stroke(value)


def test_a_pin_body_is_a_string_or_null_and_an_update_must_name_it(
    board: TestClient, token: str
) -> None:
    board_task(board)
    headers = viewer_headers(token)
    for body in ({"evil": 1}, 12, ["x"], True):
        refused = board.post("/files/1/annotations", json={**pin(), "body": body}, headers=headers)
        assert refused.status_code == 400 and "string or null" in refused.text
    added = board.post("/files/1/annotations", json=pin(), headers=headers).json()
    url = f"/files/1/annotations/{added['id']}"

    def stored() -> str | None:
        return board.get("/files/1/annotations").json()["annotations"][0]["body"]

    for bad in ({}, {"text": "x"}, {"body": 5}, {"body": {"a": 1}}, []):
        assert board.post(url, json=bad, headers=headers).status_code == 400, bad
        assert stored() == "Button text is cut off"
    reply = board.post(f"{url}/replies", json={"body": 5}, headers=headers)
    assert reply.status_code == 400
    assert board.post(f"{url}/replies", json={}, headers=headers).status_code == 400
    assert board.post(url, json={"body": None}, headers=headers).status_code == 200
    assert stored() is None


def _worst_lines() -> dict[str, list[list[float]]]:
    n = ann.FREEHAND_POINTS_IN_MAX
    generator = random.Random(1)
    return {
        "zigzag": [[i / n, float(i % 2)] for i in range(n)],
        "random": [[generator.random(), generator.random()] for _ in range(n)],
        "curve": [[i / n, (i / n) ** 12] for i in range(n)],
        "spiral": [
            [
                0.5 + 0.5 * (i / n) * math.cos(i * 2.4),
                0.5 + 0.5 * (i / n) * math.sin(i * 2.4),
            ]
            for i in range(n)
        ],
    }


@pytest.mark.parametrize("name", ["zigzag", "random", "curve", "spiral"])
def test_the_worst_pen_line_stays_fast_and_keeps_its_shape(name: str) -> None:
    points = _worst_lines()[name]
    timings = []
    for _ in range(3):  # the best of three: a busy machine says nothing about the code
        started = time.perf_counter()
        saved = ann.clean_geometry("freehand", {"points": points})["points"]
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.1, timings
    assert 2 < len(saved) <= ann.FREEHAND_POINTS_MAX
    assert saved[0] == [round(v, 5) for v in points[0]]
    assert saved[-1] == [round(v, 5) for v in points[-1]]
    for axis in (0, 1):
        wanted = max(p[axis] for p in points) - min(p[axis] for p in points)
        kept = max(p[axis] for p in saved) - min(p[axis] for p in saved)
        assert kept >= wanted * 0.95, (name, axis, kept, wanted)


def test_a_fine_zigzag_does_not_flatten_when_it_is_thinned() -> None:
    n = 2000
    zigzag = [[i / n, float(i % 2)] for i in range(n)]
    kept = ann.thin(zigzag, 500)
    assert len(kept) <= 500 and {p[1] for p in kept} == {0.0, 1.0}
    assert kept[0] == zigzag[0] and kept[-1] == zigzag[-1]
    assert kept == sorted(kept, key=lambda p: p[0])  # still in drawing order


def test_a_straight_line_may_end_with_two_points() -> None:
    straight = [[i / 1000, i / 1000] for i in range(1000)]
    assert len(ann.clean_geometry("freehand", {"points": straight})["points"]) == 2


async def test_geometry_is_cleaned_off_the_event_loop(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, shot = await shot_on_step(service)
    seen = []
    real = ann.clean_geometry

    def spy(kind: str, value: object) -> dict:
        seen.append(threading.current_thread())
        return real(kind, value)

    monkeypatch.setattr(ann, "clean_geometry", spy)
    await service.add_annotation(shot, pin())
    assert seen and seen[0] is not threading.current_thread()


async def test_a_changed_image_with_the_same_text_is_sent_again(service: Service) -> None:
    key, shot = await shot_on_step(service)
    await service.add_annotation(shot, RECT)
    await service.store_annotated(shot, png(seed=1))
    first = await service.send_annotations(shot)
    assert first is not None

    await service.store_annotated(shot, png(seed=1))  # flattened again, the very same picture
    assert await service.send_annotations(shot) is None

    await service.store_annotated(shot, png(seed=2))  # a new colour: the text stays alike
    second = await service.send_annotations(shot)
    assert second is not None and second.body_md == first.body_md
    assert await service.send_annotations(shot) is None
    assert len([c for c in (await service.list_comments(key))[1] if c.from_owner]) == 2


async def test_an_image_too_tall_or_wide_is_replaced_by_the_same_note(service: Service) -> None:
    key, shot = await shot_on_step(service)
    await service.store_annotated(shot, png(width=ann.IMAGE_TOOL_MAX_SIDE, height=1, seed=1))
    _, data, _ = await service.attachment_image(shot, annotated=True)
    assert ann.png_size(data) == (ann.IMAGE_TOOL_MAX_SIDE, 1)

    await service.store_annotated(shot, png(width=1, height=ann.IMAGE_TOOL_MAX_SIDE + 1, seed=2))
    with pytest.raises(ValidationError, match="too large for a tool answer"):
        await service.attachment_image(shot, annotated=True)
    task, review, comments = await service.review(key)
    marks, images, notes = await service.review_markup(task, review)
    assert images == {} and f"1x{ann.IMAGE_TOOL_MAX_SIDE + 1} px" in notes[shot]
    text = review_text(task, review, comments, marks, images, notes)
    assert "too large for a tool answer" in text


def test_png_size_needs_a_real_header() -> None:
    assert ann.png_size(png(width=3, height=2)) == (3, 2)
    assert ann.png_size(b"\x89PNG\r\n\x1a\n") is None
    assert ann.png_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40) is None


def test_every_viewer_request_names_the_screenshot_fixed_when_it_was_queued() -> None:
    # The behaviour itself needs a browser; this keeps the screen's id out of the request URLs.
    source = Path(__file__).parent.parent / "src/relaybay/web/static/annotate.js"
    code = source.read_text(encoding="utf-8")
    assert '"/files/" + attachment' not in code
    assert re.findall(r'[^.]call\("', code) == []
