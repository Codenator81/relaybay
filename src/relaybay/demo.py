"""`relaybay demo`: a neutral demo workspace for screenshots, the README GIF and manual testing."""

import asyncio
import base64
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from relaybay.service import Service

# A fixed, made-up desktop session id, so screenshots stay the same between runs.
DEMO_DESKTOP_SESSION = "local_0d3e4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b"
DEMO_HOOK_SESSION = "demo-hook-session"
ACME_REPO = "/home/demo/src/acme-shop"
DEMO_REVIEW_HOOK = "demo-review-hook-session"


def mock_screen(width: int, height: int, accent: tuple[int, int, int], rows: int) -> bytes:
    """A made-up page as a PNG, drawn with the standard library: a header bar in `accent`,
    `rows` grey content blocks and a button. Stands in for a real screenshot in the demo."""
    white, grey, line = (255, 255, 255), (222, 226, 230), (241, 243, 245)
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        for x in range(width):
            if y < 56:
                color = accent
            elif 80 <= y < 80 + rows * 44 and (y - 80) % 44 < 28 and 40 <= x < width - 40:
                color = grey if x < width * 0.7 else line
            elif height - 90 <= y < height - 50 and width - 220 <= x < width - 40:
                color = accent
            else:
                color = white
            raw += bytes(color)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


class DemoError(Exception):
    """The data folder already holds a board."""


@dataclass(frozen=True, slots=True)
class DemoResult:
    db_path: Path
    backup: Path | None
    projects: int
    tasks: int


async def _has_board(db_path: Path) -> bool:
    service = Service(db_path)
    await service.start()
    try:
        return bool(await service.list_projects()) or await service.last_event_id() > 0
    finally:
        await service.close()


def _move_aside(db_path: Path, now: datetime) -> Path:
    """Rename the database and its WAL files to `<name>.before-demo-<stamp>`; nothing is lost."""
    stamp = now.strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.before-demo-{stamp}")
    n = 1
    while backup.exists():
        n += 1
        backup = db_path.with_name(f"{db_path.name}.before-demo-{stamp}-{n}")
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            side.replace(backup.with_name(backup.name + suffix))
    db_path.replace(backup)
    return backup


async def seed(service: Service) -> int:
    """Two projects with tasks in every stage; returns the number of tasks created."""
    await service.upsert_project(
        "acme-shop",
        name="Acme Shop",
        repo_path=ACME_REPO,
        description="Storefront and checkout for a made-up shop.",
    )
    await service.upsert_project(
        "demo-api", name="Demo API", description="Public REST API behind the shop."
    )
    count = 0

    async def task(project: str, title: str, **fields) -> str:
        nonlocal count
        count += 1
        return (await service.create_task(project, title, actor="demo", **fields)).key

    # acme-shop: one task per stage, the dev one bound to a (made-up) desktop session.
    await task(
        "acme-shop",
        "Gift cards at checkout",
        description="Let shoppers pay part of an order with a gift card. Needs a decision on"
        " partial refunds.",
        waiting_on="owner",
        next_step="Decide how partial refunds of gift-card orders work.",
        tracker_ref="SHOP-51",
        tracker_name="Tracker",
        tracker_url="https://tracker.example.com/SHOP-51",
    )
    checkout = await task(
        "acme-shop",
        "One-page checkout",
        stage="dev",
        branch="feat/one-page-checkout",
        description="Merge address, delivery and payment into one page with inline validation.",
        progress="- Address and delivery steps merged\n- Inline validation for the address form",
        waiting_on="agent",
        next_step="Move the payment step onto the page and keep the order summary sticky.",
        tracker_ref="SHOP-42",
        tracker_name="Tracker",
        tracker_url="https://tracker.example.com/SHOP-42",
        session=DEMO_DESKTOP_SESSION,
        session_title="acme-shop · checkout",
    )
    await service.add_report(
        checkout,
        "changed · M · address and delivery on one page\n\nValidation runs on blur; the payment"
        " step is still separate.",
        session=DEMO_DESKTOP_SESSION,
    )
    search = await task(
        "acme-shop",
        "Search suggestions",
        stage="test",
        branch="feat/search-suggest",
        description="Suggest products while typing in the search box.",
        progress="- Suggestions endpoint with a 150 ms debounce\n- Keyboard navigation",
        waiting_on="owner",
        next_step="Try the suggestions on a phone and accept or send back.",
        session="search-agent",
    )
    await service.add_report(
        search,
        "verified · S · suggestions work on desktop and phone\n\nTests pass; checked by hand on"
        " the local stand.",
        session="search-agent",
    )
    await service.add_decision(
        "acme-shop",
        "Suggestions show at most 8 products; categories come later.",
        task=search,
        decided_by="owner",
    )
    wishlist = await task(
        "acme-shop",
        "Wishlist on the product page",
        stage="review",
        branch="feat/wishlist",
        description="A heart button on the product page saves the product to a wishlist.",
        progress="- Heart button and wishlist page\n- Saved items survive a sign-out",
        waiting_on="owner",
        next_step="Accept the wishlist step by step in the Review tab.",
        session="wishlist-agent",
        hook_session=DEMO_REVIEW_HOOK,
    )
    await service.set_review_steps(
        wishlist,
        [
            {
                "title": "Heart button on the product page",
                "details": "**What:** the heart next to *Add to cart*.\n\n**How:** open any"
                " product and press the heart.\n\n**Why:** it is the only way in.",
            },
            {
                "title": "Wishlist page lists the saved product",
                "details": "Open *Account → Wishlist*; the product shows with its price.",
            },
            {
                "title": "Saved items survive a sign-out",
                "details": "Sign out and in again with the demo account; the list is the same.",
            },
        ],
        session="wishlist-agent",
    )
    for step, accent, rows, caption in (
        (1, (59, 91, 219), 4, "Product page with the heart button"),
        (2, (112, 72, 232), 6, "Wishlist page with one saved product"),
    ):
        image = mock_screen(640, 400, accent, rows)
        await service.attach_file(
            wishlist,
            content_base64=base64.b64encode(image).decode(),
            filename=f"step-{step}.png",
            step=step,
            caption=caption,
            session="wishlist-agent",
        )
    banner = await task(
        "acme-shop",
        "Holiday banner",
        stage="live",
        progress="- Banner component\n- Shown from December 1 to January 6",
        next_step="Remove the banner after January 6.",
    )
    await service.add_report(banner, "pushed · S · banner is live", session="banner-agent")
    receipts = await task(
        "acme-shop",
        "Order receipts as PDF",
        stage="dev",
        progress="- PDF layout\n- Download link on the order page",
        tracker_ref="SHOP-37",
        tracker_name="Tracker",
        tracker_url="https://tracker.example.com/SHOP-37",
    )
    await service.add_report(receipts, "verified · M · receipts render", session="receipts-agent")
    await service.set_stage(receipts, "done", actor="demo")
    # Logged last: a report after the mark would clear it again.
    await service.update_task(receipts, {"tracker_logged": True}, actor="demo")

    # demo-api: a stale next step (set before the last stage change) and a blocked task.
    rate = await task(
        "demo-api",
        "Rate limiting per API key",
        branch="feat/rate-limit",
        description="Limit each API key to 100 requests per minute, with a Retry-After header.",
        next_step="Pick the storage for the counters.",
        waiting_on="agent",
    )
    await asyncio.sleep(0.01)  # the stage change must be later than the next step
    await service.set_stage(rate, "dev", actor="demo")
    await service.add_report(
        rate,
        "blocked · S · counters need a shared store\n\nWaiting for a decision between the"
        " database and an in-memory store.",
        session="api-agent",
        waiting_on="owner",
    )
    docs = await task(
        "demo-api",
        "OpenAPI examples for orders",
        stage="test",
        progress="- Examples for create, list and cancel",
        next_step="Review the examples in the rendered docs.",
        waiting_on="owner",
    )
    await service.add_report(docs, "changed · S · examples added", session="docs-agent")
    await task(
        "demo-api",
        "Remove the v0 endpoints",
        description="v0 has had no traffic for 90 days; remove it and its tests.",
        next_step="Announce the removal date.",
    )

    # The desktop session of the checkout task is "working": as if its hook had reported. One
    # event only, so no coalescing timer outlives the service.
    await service.record_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": DEMO_HOOK_SESSION,
            "cwd": ACME_REPO,
            "tool_name": "Edit",
        }
    )
    return count


async def _seed_new(db_path: Path) -> tuple[int, int]:
    service = Service(db_path)
    await service.start()
    try:
        tasks = await seed(service)
        projects = len(await service.list_projects())
    finally:
        await service.close()
    return projects, tasks


def create_demo(db_path: Path, *, force: bool = False) -> DemoResult:
    """Seed the demo into `db_path`. A database that already holds a board is refused, or with
    `force` renamed aside first."""
    backup = None
    if db_path.is_file() and asyncio.run(_has_board(db_path)):
        if not force:
            raise DemoError(
                f"{db_path} already holds a board; use another --home, or --force to move it"
                " aside and start over"
            )
        backup = _move_aside(db_path, datetime.now())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    projects, tasks = asyncio.run(_seed_new(db_path))
    return DemoResult(db_path=db_path, backup=backup, projects=projects, tasks=tasks)
