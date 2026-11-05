"""`relaybay demo`: a neutral demo workspace for screenshots, the README GIF and manual testing."""

import asyncio
import base64
import contextlib
import json
import sqlite3
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from relaybay.domain.annotations import COLOURS
from relaybay.service import Service

# A fixed, made-up desktop session id, so screenshots stay the same between runs.
DEMO_DESKTOP_SESSION = "local_0d3e4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b"
DEMO_HOOK_SESSION = "demo-hook-session"
ACME_REPO = "/home/demo/src/acme-shop"
DEMO_REVIEW_HOOK = "demo-review-hook-session"
DEMO_TODO_HOOK = "demo-todo-hook-session"


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


def _has_rows(db_path: Path) -> bool:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            for table in ("project", "event")
            if table in tables
        )


async def _has_board(db_path: Path) -> bool:
    """Read-only: a refused demo leaves the database as it found it, no migration, no rule sync."""
    return await asyncio.to_thread(_has_rows, db_path)


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
        " partial refunds: relaybay://doc/acme-shop/spec#gift-cards",
        doc_path="relaybay://doc/acme-shop/spec#gift-cards",
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
    shots = []
    for step, accent, rows, caption in (
        (1, (59, 91, 219), 4, "Product page with the heart button"),
        (2, (112, 72, 232), 6, "Wishlist page with one saved product"),
    ):
        image = mock_screen(640, 400, accent, rows)
        shot, _ = await service.attach_file(
            wishlist,
            content_base64=base64.b64encode(image).decode(),
            filename=f"step-{step}.png",
            step=step,
            caption=caption,
            session="wishlist-agent",
        )
        shots.append(shot.id)
    await seed_annotations(service, shots[0])
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

    # A plan with checkpoints: one step as planned, one with a deviation, one in progress.
    addresses = await task(
        "acme-shop",
        "Saved addresses",
        stage="dev",
        branch="feat/saved-addresses",
        description="Signed-in shoppers pick a saved address at checkout instead of typing it.",
        waiting_on="agent",
        next_step="Show the saved addresses as a picker on the checkout page.",
        session="addresses-agent",
    )
    await service.update_task(addresses, {"size": "M"}, actor="demo")
    await service.set_plan(
        addresses,
        [
            {"title": "Address table and model", "details": "Store addresses per account."},
            {
                "title": "Address book page",
                "details": "Shoppers add, edit and remove addresses in their account.",
            },
            {
                "title": "Picker at checkout",
                "details": "Pick a saved address instead of typing it; typing still works.",
            },
            {"title": "Tests and review steps", "details": "So the owner can accept it by eye."},
        ],
        reason="from the task description",
        session="addresses-agent",
    )
    await service.accept_plan(addresses, actor="owner")
    _, first = await service.add_report(
        addresses,
        "changed · M · address table and model\n\nMigration and model with tests.",
        session="addresses-agent",
    )
    await service.update_plan_step(
        addresses,
        1,
        "done",
        outcome="Migration and model with tests",
        link=first.id,
        conformance="as planned",
        session="addresses-agent",
    )
    await service.update_plan_step(
        addresses,
        2,
        "done",
        outcome="Add and remove only",
        conformance="deviation: editing waits for the shared form component",
        session="addresses-agent",
    )
    await service.update_plan_step(addresses, 3, "in_progress", session="addresses-agent")

    # A small task without a plan: the agent's own todo stands in, marked not accepted.
    errors = await task(
        "demo-api",
        "JSON body for 404 errors",
        stage="dev",
        description="Unknown routes answer with the same JSON error body as other errors.",
        session="errors-agent",
        hook_session=DEMO_TODO_HOOK,
    )
    await service.update_task(errors, {"size": "S"}, actor="demo")
    await service.record_hook(
        {
            "hook_event_name": "PostToolUse",
            "session_id": DEMO_TODO_HOOK,
            "tool_name": "TodoWrite",
            "tool_input": {
                "todos": [
                    {
                        "content": "Find where 404s are answered",
                        "status": "completed",
                        "activeForm": "Finding where 404s are answered",
                    },
                    {
                        "content": "Return the JSON error body",
                        "status": "in_progress",
                        "activeForm": "Returning the JSON error body",
                    },
                    {
                        "content": "Add a test for an unknown route",
                        "status": "pending",
                        "activeForm": "Adding a test for an unknown route",
                    },
                ]
            },
        }
    )

    await seed_memory(service)
    await seed_map_and_usage(service)

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


def demo_transcript(path: Path, seed_number: int, today: datetime) -> None:
    """A small made-up transcript: two weeks of usage lines, a user line around them. The numbers
    come from a formula, so screenshots stay the same between runs."""
    lines = []
    for back in range(14):
        moment = (today - timedelta(days=back)).isoformat()
        lines.append({"type": "user", "timestamp": moment, "message": {"role": "user"}})
        for n in range(1 + (back + seed_number) % 3):
            model = "demo-model-large" if (back + n) % 4 else "demo-model-small"
            usage = {
                "input_tokens": 120 + 40 * ((back * 7 + seed_number) % 11),
                "output_tokens": 900 + 150 * ((back * 5 + n) % 13),
                "cache_read_input_tokens": 4000 + 700 * ((back * 3 + seed_number) % 9),
                "cache_creation_input_tokens": 300 + 90 * (n + back % 5),
            }
            message = {"id": f"demo-{seed_number}-{back}-{n}", "model": model, "usage": usage}
            lines.append({"type": "assistant", "timestamp": moment, "message": message})
    text = "".join(json.dumps(line) + "\n" for line in lines)
    path.write_text(text, encoding="utf-8")


async def seed_map_and_usage(service: Service) -> None:
    """Two environments and a few messages between sessions for the Map, a small usage history for
    Usage: both through the real paths, a hook's tool call and transcript files in the data
    folder."""
    await service.save_environment("acme-shop", {"name": "test", "kind": "test"}, actor="demo")
    await service.save_environment(
        "acme-shop", {"name": "live", "kind": "live"}, confirm_live=True, actor="demo"
    )
    folder = service.db_path.parent / "demo-transcripts"
    folder.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC)
    for number, hook in enumerate((DEMO_HOOK_SESSION, DEMO_TODO_HOOK, DEMO_REVIEW_HOOK), 1):
        path = (folder / f"{hook}.jsonl").resolve()
        demo_transcript(path, number, today)
        await service.record_hook(
            {
                "hook_event_name": "Stop",
                "session_id": hook,
                "cwd": ACME_REPO,
                "transcript_path": str(path),
            }
        )
    await service.usage_pass()
    for sender, recipient in (
        (DEMO_TODO_HOOK, DEMO_DESKTOP_SESSION),
        (DEMO_TODO_HOOK, "wishlist-agent"),
        (DEMO_REVIEW_HOOK, "docs-agent"),
    ):
        await service.record_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": sender,
                "tool_name": "SendMessage",
                "tool_input": {"to": recipient},
            }
        )


async def seed_annotations(service: Service, shot: int) -> None:
    """The owner's markup on a screenshot: a rectangle, an arrow and two pins, one answered."""
    red, blue = COLOURS[0], COLOURS[3]
    await service.add_annotation(
        shot,
        {"kind": "rect", "colour": red, "geometry": {"x": 0.62, "y": 0.72, "w": 0.33, "h": 0.15}},
    )
    await service.add_annotation(
        shot,
        {
            "kind": "arrow",
            "colour": blue,
            "geometry": {"x1": 0.4, "y1": 0.55, "x2": 0.62, "y2": 0.75},
        },
    )
    first = await service.add_annotation(
        shot,
        {
            "kind": "pin",
            "colour": red,
            "geometry": {"x": 0.78, "y": 0.68},
            "body": "The heart should sit next to this button.",
        },
    )
    await service.add_annotation(
        shot,
        {
            "kind": "pin",
            "colour": blue,
            "geometry": {"x": 0.3, "y": 0.3},
            "body": "Is this the final product title?",
        },
    )
    await service.reply_as_agent(
        first.id, "Moved it next to Add to cart; look again.", session="wishlist-agent"
    )


DEMO_SPEC = """# Acme Shop specification

## Checkout
One page: address, delivery and payment, validated inline.

## Gift cards
A gift card pays part of an order; the rest goes on the card.

## Search
Suggestions while typing, at most 8 products.
"""


async def seed_memory(service: Service) -> None:
    """Docs & rules of acme-shop: a spec with two versions, a runbook that lives in the
    repository, a superseded decision, two notes and a project block."""
    await service.update_document(
        "acme-shop", "spec", DEMO_SPEC, "First draft", 0, kind="spec", author="demo"
    )
    await service.update_document(
        "acme-shop",
        "spec",
        "A gift card pays part of an order; the rest goes on the card. Refunds go back to the"
        " gift card first.",
        "Partial refunds decided",
        1,
        section="gift-cards",
        author="demo",
    )
    await service.update_document(
        "acme-shop",
        "deploy",
        "-",
        "Kept in the repository next to the code",
        0,
        title="Deploy runbook",
        kind="runbook",
        source_url="https://git.example.com/acme-shop/blob/main/docs/deploy.md",
        author="demo",
    )
    first = await service.add_decision(
        "acme-shop",
        "Partial refunds of gift-card orders go back to the card.",
        decided_by="owner",
        source="message:demo-chat-1 owner in chat",
    )
    await service.add_decision(
        "acme-shop",
        "Partial refunds go back to the gift card first, the rest to the card.",
        decided_by="owner",
        source="message:demo-chat-2 owner in chat",
        supersedes=first.id,
    )
    await service.add_note(
        "global",
        "user",
        "The owner reads reports on a phone",
        "Keep the first line of a report short.\nWhy: the owner reads the board on a phone.\n"
        "How: one line of state, size and gist; details below.",
        author="demo",
    )
    await service.add_note(
        "project",
        "reference",
        "Payment provider test cards",
        "The payment provider's published test card numbers are in its docs; never real"
        " cards.\nHow: use them on the local stand only.",
        project="acme-shop",
        tags="payments, testing",
        author="demo",
    )
    await service.set_block(
        "conventions",
        "Prices are stored in cents. Every checkout change gets a review step for the owner.",
        0,
        project="acme-shop",
        description="What every session of this project should know",
        author="demo",
    )
    await service.set_pulse(
        "acme-shop",
        "Checkout is in dev, search suggestions wait for the owner's test.\n"
        "Gift cards wait for a refund decision.",
        actor="demo",
    )
    # Written weeks ago: the board and the digests show it as stale.
    weeks_ago = datetime.now(UTC) - timedelta(days=21)
    await service.set_pulse(
        "demo-api",
        "The public API is stable; error bodies are being unified.",
        actor="demo",
        at=weeks_ago.isoformat(timespec="milliseconds"),
    )


async def seed_rules_and_filters(service: Service) -> None:
    """A second global rules version, one open proposal and one saved filter."""
    accepted = await service.propose_rules(
        "global",
        {"guidance": "Report at the end of every stage. Keep progress and next step current."},
        "Shorter guidance: the owner reads it on a phone.",
        session="receipts-agent",
    )
    await service.decide_rule_proposal(accepted.id, True, actor="owner")
    await service.propose_rules(
        "acme-shop",
        {"stage_requirements": {"review": [{"kind": "report_since_stage_change"}]}},
        "Reviews went out without a report twice; ask for one before review too.",
        session="checkout-agent",
    )
    await service.save_filter("acme-shop", "Waiting on me", waiting_on="owner", actor="owner")


async def seed_intake_and_backlog(service: Service) -> None:
    """Two intake sources (support-chat trusted, team-mail not), acme-shop with autonomy on, an
    answer that woke the agent of the bound checkout task, one that only notified, a message
    waiting in the Inbox, and the backlog: a session finding, an intake proposal, a declined item
    and a planned one."""
    checkout = next(
        t for t in await service.list_tasks(project="acme-shop") if t.title == "One-page checkout"
    )
    gift = next(
        t for t in await service.list_tasks(project="acme-shop") if t.title.startswith("Gift")
    )
    await service.set_project_autonomy("acme-shop", True, actor="demo")
    # The checkout agent links its hook session itself: only an exact link can be woken.
    await service.add_backlog_item(
        "acme-shop",
        "Cart badge does not update after a removal",
        body="Found while working on the checkout: the header badge keeps the old count.",
        area="cart",
        task=checkout.key,
        session=DEMO_DESKTOP_SESSION,
        hook_session=DEMO_HOOK_SESSION,
    )
    await service.update_task(checkout.key, {"waiting_on": "client"}, actor="demo")
    hello = await service.intake_post(
        "support-chat", "demo-1", "Hello, Dana from the client here.", author="Dana", kind="chat"
    )
    await service.intake_dismiss(hello.message.id, actor="owner")
    await service.set_source_trust("support-chat", True, actor="owner")
    await service.intake_post(
        "support-chat",
        "demo-2",
        f"About {checkout.key}: yes, keep the order summary sticky on phones too.",
        author="Dana",
        thread="chat-7",
    )
    await service.intake_post(
        "team-mail",
        "demo-mail-1",
        f"Re {gift.key}: refunds go back to the gift card first.",
        author="Sam Lee",
        thread="<refunds@example.com>",
        kind="email",
    )
    proposal = await service.intake_post(
        "team-mail",
        "demo-mail-2",
        "Could the receipts PDF show the shop's VAT number?",
        author="Sam Lee",
        thread="<vat@example.com>",
    )
    await service.intake_post(
        "team-mail",
        "demo-mail-3",
        "Is the holiday banner coming back next year?",
        author="Alex Kim",
        thread="<banner@example.com>",
    )
    await service.intake_propose(
        proposal.message.id, "acme-shop", "VAT number on receipts", actor="demo"
    )
    declined = await service.add_backlog_item(
        "demo-api", "XML responses", body="One client asked for XML.", area="api", by_owner=True
    )
    await service.decide_backlog(
        declined.id, "decline", "JSON only: no client needs XML any more", actor="owner"
    )
    planned = await service.add_backlog_item(
        "demo-api", "Pagination links in list responses", area="api", by_owner=True
    )
    await service.decide_backlog(planned.id, "plan", actor="owner")


async def _seed_new(db_path: Path) -> tuple[int, int]:
    service = Service(db_path)
    await service.start()
    try:
        await seed(service)
        await seed_rules_and_filters(service)
        await seed_intake_and_backlog(service)
        # "To plan" in the backlog creates a task too: count what the board holds.
        tasks = len(await service.list_tasks(include_done=True))
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
