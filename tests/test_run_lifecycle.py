"""Limits before each turn (slots, free memory, the brake), Start anyway, a card's close (the run
ends, its worktrees go or are kept with a question, its traces are deleted) and the retention of
run lines. The service runs with the stub runner of test_runs_service: no process starts."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from relaybay.db import repo
from relaybay.domain.errors import ValidationError
from relaybay.domain.run_events import StreamEvent, pack
from relaybay.runner import memory, transcripts
from relaybay.runner.layers import LayerState
from relaybay.runner.stream import read_line
from relaybay.service import Service
from tests.e2e.conftest import git, make_git_repo
from tests.test_runs_service import CC_A, CC_B, StubRuns, stub

CC_C = "abcdefab-1234-5678-9abc-def012345678"
SECRET = "ghp_" + "Q1w2E3r4T5y6" * 3
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    ok = LayerState("command", True, "command hooks from your user settings", "registered")
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: ok)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, tmp_path)  # type: ignore[assignment]
    folder = tmp_path / "acme-shop"
    folder.mkdir()
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    try:
        yield service
    finally:
        await service.close()


PROJECTS = ("acme-shop", "demo-api", "demo-web", "demo-docs")


async def cards(svc: Service, count: int) -> list[str]:
    """acme-shop-1 exists; a card in a project of its own (one open run per folder) up to
    `count`."""
    root = Path(svc.db_path).parent
    for slug in PROJECTS[1:count]:
        (root / slug).mkdir()
        await svc.upsert_project(slug, repo_path=str(root / slug))
        await svc.create_task(slug, "Card", stage="dev")
    return [f"{slug}-1" for slug in PROJECTS[:count]]


async def events(svc: Service, kind: str) -> list[dict[str, Any]]:
    async with svc._r.execute(
        "SELECT payload FROM event WHERE kind = ? ORDER BY id", (kind,)
    ) as cur:
        return [json.loads(r[0]) for r in await cur.fetchall()]


async def line_count(svc: Service, run_id: int) -> int:
    async with svc._r.execute(
        "SELECT COUNT(*) FROM run_event e JOIN run_turn t ON t.id = e.turn_id WHERE t.run_id = ?",
        (run_id,),
    ) as cur:
        return (await cur.fetchone())[0]


# --- slots and memory ----------------------------------------------------------------------


async def test_two_runs_hold_both_slots_a_third_waits_and_starts_when_one_ends(
    svc: Service,
) -> None:
    one, two, three = await cards(svc, 3)
    first = await svc.start_run(one)
    second = await svc.start_run(two)
    third = await svc.start_run(three)
    assert (first.status, second.status) == ("running", "running")
    assert third.status == "queued"
    assert third.status_reason == "waiting for a slot: 2 of 2 running"
    assert [r for r, _, _ in stub(svc).started] == [first.id, second.id]
    inbox = await svc.inbox(["owner"])
    assert [r.id for r in inbox.queued] == [third.id] and inbox.count >= 1
    queued = await events(svc, "run.turn_queued")
    assert queued[-1]["reason"] == "waiting for a slot: 2 of 2 running"
    await stub(svc).finish(first.id, cc_id=CC_A)
    started = await svc.get_run(third.id)
    assert started.status == "running" and started.status_reason is None
    assert [r for r, _, _ in stub(svc).started] == [first.id, second.id, third.id]
    assert (await svc.inbox(["owner"])).queued == []


async def test_the_queue_is_first_in_first_out_over_all_runs(svc: Service) -> None:
    await svc.set_run_limits(1, 0)
    one, two, three = await cards(svc, 3)
    first = await svc.start_run(one)
    second = await svc.start_run(two)
    third = await svc.start_run(three)
    assert (second.status, third.status) == ("queued", "queued")
    assert second.status_reason == "waiting for a slot: 1 of 1 running"
    await stub(svc).finish(first.id, cc_id=CC_A)
    assert (await svc.get_run(second.id)).status == "running"
    assert (await svc.get_run(third.id)).status == "queued"
    # A text of the first run now queues behind the third, which was queued before it.
    await svc.send_to_run(first.id, "Go on")
    assert (await svc.get_run(first.id)).status == "queued"
    await stub(svc).finish(second.id, cc_id=CC_B)
    assert (await svc.get_run(third.id)).status == "running"
    assert (await svc.get_run(first.id)).status == "queued"


async def test_too_little_memory_queues_with_its_reason_and_start_anyway_starts_it(
    svc: Service,
) -> None:
    svc.memory_reader = lambda: 1200
    run = await svc.start_run("acme-shop-1")
    assert run.status == "queued"
    assert run.status_reason == "waiting for memory: 1.2 GB free, 1.5 GB needed"
    assert stub(svc).started == []
    # The periodic re-check changes nothing while nothing changed, and writes no event.
    before = len(await events(svc, "run.turn_queued"))
    assert await svc.dispatch_queued() == 0
    assert len(await events(svc, "run.turn_queued")) == before
    # Less memory now: the reason follows, still without an event of its own.
    svc.memory_reader = lambda: 1100
    assert await svc.dispatch_queued() == 0
    moved = await svc.get_run(run.id)
    assert moved.status_reason == "waiting for memory: 1.1 GB free, 1.5 GB needed"
    assert len(await events(svc, "run.turn_queued")) == before
    svc.memory_reader = lambda: 1200
    started = await svc.start_anyway(run.id, actor="owner")
    assert started.status == "running" and len(stub(svc).started) == 1
    [anyway] = await events(svc, "run.start_anyway")
    assert anyway["reason"] == "waiting for memory: 1.1 GB free, 1.5 GB needed"
    assert (anyway["free_mb"], anyway["min_free_memory_mb"]) == (1200, 1500)
    with pytest.raises(ValidationError, match="no turn of it waits"):
        await svc.start_anyway(run.id)


async def test_memory_that_recovers_starts_the_turn_at_the_next_check(svc: Service) -> None:
    free = [1000]
    svc.memory_reader = lambda: free[0]
    run = await svc.start_run("acme-shop-1")
    assert run.status == "queued"
    free[0] = 4000
    assert await svc.dispatch_queued() == 1
    assert (await svc.get_run(run.id)).status == "running"


async def test_unreadable_memory_does_not_hold_a_turn(svc: Service) -> None:
    svc.memory_reader = lambda: None
    run = await svc.start_run("acme-shop-1")
    assert run.status == "running"


async def test_start_anyway_goes_past_the_slot_limit_too(svc: Service) -> None:
    one, two, three = await cards(svc, 3)
    await svc.start_run(one)
    await svc.start_run(two)
    third = await svc.start_run(three)
    assert third.status == "queued"
    assert (await svc.start_anyway(third.id)).status == "running"
    assert await repo.count_running_turns(svc._r) == 3


async def test_the_owners_limits_win_over_the_config_and_start_what_waits(svc: Service) -> None:
    one, two, three = await cards(svc, 3)
    await svc.start_run(one)
    await svc.start_run(two)
    third = await svc.start_run(three)
    assert third.status == "queued"
    assert await svc.set_run_limits("3", "0") == (3, 0)
    assert (await svc.get_run(third.id)).status == "running"
    assert await svc.run_limits() == (3, 0)
    with pytest.raises(ValidationError, match="Turns at once"):
        await svc.set_run_limits(0, 0)


async def test_a_queued_turn_survives_a_restart(tmp_path: Path, svc: Service) -> None:
    one, two, three = await cards(svc, 3)
    await svc.start_run(one)
    await svc.start_run(two)
    third = await svc.start_run(three)
    await svc.close()
    again = Service(tmp_path / "relaybay.sqlite")
    await again.start()
    try:
        kept = await again.get_run(third.id)
        assert kept.status == "queued"
        assert await repo.next_queued_turn(again._r, third.id) is not None
    finally:
        await again.close()
    await svc.start()  # the fixture closes it again


# --- the brake ------------------------------------------------------------------------------


def pre_tool(run_id: int, cwd: str) -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": CC_C,
        "cwd": cwd,
        "tool_name": "Bash",
        "tool_input": {"command": "make build"},
        "relaybay_run": str(run_id),
    }


async def test_a_project_brake_kills_the_running_turn_and_holds_new_ones(
    svc: Service, tmp_path: Path
) -> None:
    run = await svc.start_run("acme-shop-1")
    brake = await svc.set_board_brake("project", project="acme-shop", reason="stop now")
    answer = await svc.brake_stop(pre_tool(run.id, str(tmp_path / "acme-shop")))
    assert answer is not None and answer["continue"] is False
    while svc._brake_stops:  # the kill runs in the background
        await next(iter(svc._brake_stops))
    stopped = await svc.get_run(run.id)
    assert stopped.status == "stopped"
    assert stopped.status_reason == "braked (project acme-shop): stop now"
    await svc.send_to_run(run.id, "Carry on")
    held = await svc.get_run(run.id)
    assert held.status == "queued" and held.status_reason.startswith("braked (project acme-shop)")
    with pytest.raises(ValidationError, match="does not go past a brake"):
        await svc.start_anyway(run.id)
    assert held.id in [r.id for r in (await svc.inbox(["owner"])).queued]
    await svc.release_brake(brake.id)
    assert await svc.dispatch_queued() == 1
    assert (await svc.get_run(run.id)).status == "running"


async def test_a_refused_call_of_another_run_id_kills_nothing_unbraked(
    svc: Service, tmp_path: Path
) -> None:
    await svc.create_task("acme-shop", "Other", stage="dev")
    run = await svc.start_run("acme-shop-1")
    await svc.set_board_brake("project", project="acme-shop")
    other_project = tmp_path / "demo-api"
    other_project.mkdir()
    await svc.upsert_project("demo-api", repo_path=str(other_project))
    await svc.create_task("demo-api", "Api", stage="dev")
    api_run = await svc.start_run("demo-api-1")
    # The hook claims the other project's run: that run is not braked, so it keeps running.
    await svc.brake_stop(pre_tool(api_run.id, str(tmp_path / "acme-shop")))
    while svc._brake_stops:
        await next(iter(svc._brake_stops))
    assert (await svc.get_run(api_run.id)).status == "running"
    assert (await svc.get_run(run.id)).status == "running"  # no refused call of its own yet


# --- a card's close ---------------------------------------------------------------------------


async def three_repo_run(svc: Service, tmp_path: Path) -> tuple[Any, dict[str, Path]]:
    folder = tmp_path / "acme"
    sources = {name: make_git_repo(folder / name) for name in ("api", "web", "docs")}
    await svc.upsert_project(
        "acme-shop", repos=[{"name": n, "path": str(p)} for n, p in sources.items()]
    )
    await svc.set_run_defaults("acme-shop", {"folder": str(folder)})
    project = await svc.get_project("acme-shop")
    repos = await repo.list_project_repos(svc._r, project.id)
    run = await svc.start_run("acme-shop-1", cwd="folder", repos=[(r.id, True) for r in repos])
    return run, sources


def on_disk(*parts: str) -> bool:
    return Path(*parts).exists()


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def worktree_list(source: Path) -> str:
    return git(source, "worktree", "list", "--porcelain").replace("\\", "/")


@needs_git
async def test_a_card_to_done_ends_the_run_removes_its_worktrees_and_asks_about_work_left(
    svc: Service, tmp_path: Path
) -> None:
    run, sources = await three_repo_run(svc, tmp_path)
    folders = {w.repo_name: w for w in await repo.list_run_worktrees(svc._r, run.id)}
    assert all(w.created_by_run for w in folders.values())
    (Path(folders["docs"].path) / "notes.md").write_text("draft\n", encoding="utf-8")
    run_folder = Path(folders["api"].path).parent
    (run_folder / "keep-me.txt").write_text("not the run's\n", encoding="utf-8")
    sibling = tmp_path / "sibling-of-api"
    git(sources["api"], "worktree", "add", "-q", "-b", "mine", str(sibling))
    assert (await svc.get_run(run.id)).status == "running"

    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()

    ended = await svc.get_run(run.id)
    assert ended.status == "ended" and ended.status_reason == "the card went to done"
    after = {w.repo_name: w for w in await repo.list_run_worktrees(svc._r, run.id)}
    for name in ("api", "web"):
        assert after[name].removed_at is not None and not on_disk(after[name].path)
        assert after[name].path.replace("\\", "/") not in worktree_list(sources[name])
        assert git(sources[name], "branch", "--list", after[name].branch).strip()  # branch stays
    docs = after["docs"]
    assert docs.removed_at is None and on_disk(docs.path, "notes.md")
    assert docs.kept_reason == "uncommitted changes (notes.md)"
    q = await svc.get_question(docs.question_id)
    assert (q.status, q.options, q.default, q.asked_by) == (
        "open",
        ["remove", "keep"],
        "keep",
        "relaybay",
    )
    assert docs.path in q.text
    # Nothing the run did not create is touched.
    assert (run_folder / "keep-me.txt").is_file() and sibling.is_dir()
    assert str(sibling).replace("\\", "/") in worktree_list(sources["api"])
    assert all(p.is_dir() for p in sources.values())
    kinds = [e["path"] for e in await events(svc, "run.worktree_removed")]
    assert len(kinds) == 2

    await svc.answer_question(q.id, choice="remove")
    gone = await repo.get_run_worktree(svc._r, docs.id)
    assert gone.removed_at is not None and not on_disk(docs.path)
    assert (run_folder / "keep-me.txt").is_file() and sibling.is_dir()
    forced = (await events(svc, "run.worktree_removed"))[-1]
    assert forced["forced"] is True and forced["path"] == docs.path
    assert not await repo.list_comments(svc._r, (await svc.get_task("acme-shop-1")).id)


@needs_git
async def test_a_commit_on_no_remote_keeps_the_worktree_and_keep_is_the_answer(
    svc: Service, tmp_path: Path
) -> None:
    run, _ = await three_repo_run(svc, tmp_path)
    await stub(svc).finish(run.id, cc_id=CC_A)
    web = next(w for w in await repo.list_run_worktrees(svc._r, run.id) if w.repo_name == "web")
    write(Path(web.path) / "page.html", "<p>hi</p>\n")
    git(Path(web.path), "add", "page.html")
    git(Path(web.path), "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", "page")
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    kept = await repo.get_run_worktree(svc._r, web.id)
    assert kept.kept_reason == "1 commit not on any remote"
    await svc.answer_question(kept.question_id, choice="keep")
    kept = await repo.get_run_worktree(svc._r, web.id)
    assert kept.kept_at is not None and kept.removed_at is None and on_disk(web.path)
    # The sweep leaves a kept one alone.
    assert await repo.done_cards_to_close(svc._r) == []


@needs_git
async def test_the_copied_claude_files_do_not_keep_a_worktree(svc: Service, tmp_path: Path) -> None:
    folder = tmp_path / "acme"
    api = make_git_repo(folder / "api", {"CLAUDE.md": "rules\n", ".claude/settings.json": "{}"})
    await svc.upsert_project("acme-shop", repos=[{"name": "api", "path": str(api)}])
    [api_repo] = await repo.list_project_repos(svc._r, (await svc.get_project("acme-shop")).id)
    run = await svc.start_run("acme-shop-1", repos=[(api_repo.id, True)])
    [w] = await repo.list_run_worktrees(svc._r, run.id)
    assert sorted(w.copied) == [".claude/settings.json", "CLAUDE.md"]
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    assert (await repo.get_run_worktree(svc._r, w.id)).removed_at is not None
    assert (api / "CLAUDE.md").is_file() and (api / ".claude" / "settings.json").is_file()


# --- traces: secrets, live, retention -------------------------------------------------------


@pytest.fixture
def short_claude_dir(svc: Service) -> Any:
    """A Claude data folder with a short path: the encoded cwd of a pytest folder is long, and
    Windows refuses paths past 260 characters."""
    folder = Path(tempfile.mkdtemp(prefix="cc"))
    stub(svc).config.claude_dir = folder
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


async def test_a_run_that_saw_a_secret_loses_its_lines_and_transcript_when_the_card_closes(
    svc: Service, short_claude_dir: Path
) -> None:
    run = await svc.start_run("acme-shop-1")
    turn_id = stub(svc).active.pop(run.id)
    await svc.turn_init(turn_id, CC_A)
    data = transcripts.data_folder(stub(svc).config.claude_dir)
    path = data / "projects" / transcripts.encode_cwd(run.cwd) / f"{CC_A}.jsonl"
    assert (await svc.get_run(run.id)).transcript_path == str(path)
    path.parent.mkdir(parents=True)
    path.write_text('{"type": "user"}\n', encoding="utf-8")
    other = path.with_name(f"{CC_B}.jsonl")
    other.write_text("{}\n", encoding="utf-8")
    note = path.with_name("notes.txt")
    note.write_text("mine\n", encoding="utf-8")
    session_dirs = [
        path.with_name(CC_A) / "subagents",
        data / "file-history" / CC_A,
        data / "session-env" / CC_A,
    ]
    for folder in session_dirs:
        folder.mkdir(parents=True)
        (folder / "x.jsonl").write_text("{}\n", encoding="utf-8")
    kept_dirs = [data / "file-history" / CC_B, data / "todos"]
    for folder in kept_dirs:
        folder.mkdir(parents=True)
    plain = read_line(json.dumps({"type": "assistant", "text": "hello"}), 1)[0]
    leaked = read_line(json.dumps({"type": "user", "text": f"token {SECRET}"}), 2)[0]
    assert (plain.secret, leaked.secret) == (False, True)
    await svc.turn_events(turn_id, [plain, leaked])
    assert (await svc.get_run(run.id)).secret_seen_at is not None
    assert len(await events(svc, "run.secret_seen")) == 1
    await svc.turn_events(turn_id, [read_line(json.dumps({"type": "x", "t": SECRET}), 3)[0]])
    assert len(await events(svc, "run.secret_seen")) == 1  # once
    await svc.turn_done(turn_id, "success", None, 0, None)

    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()

    cleaned = await svc.get_run(run.id)
    assert cleaned.deleted_at is not None and await line_count(svc, run.id) == 0
    assert not path.exists() and other.is_file() and note.is_file()
    [deleted] = await events(svc, "run.traces_deleted")
    assert deleted["reason"] == "saw a secret" and deleted["lines"] == 3
    gone = [str(path), str(path.with_name(CC_A)), *(str(d) for d in session_dirs[1:])]
    assert sorted(deleted["transcripts"]) == sorted(gone) and deleted["failed"] == []
    assert not path.with_name(CC_A).exists()
    assert not any(d.exists() for d in session_dirs) and all(d.is_dir() for d in kept_dirs)
    assert await repo.done_cards_to_close(svc._r) == []


async def test_a_run_that_touched_live_loses_its_traces_and_a_plain_one_keeps_them(
    svc: Service,
) -> None:
    live_key, plain_key = await cards(svc, 2)
    live = await svc.start_run(live_key)
    plain = await svc.start_run(plain_key)
    for run, cc in ((live, CC_A), (plain, CC_B)):
        turn_id = stub(svc).active.pop(run.id)
        await svc.turn_init(turn_id, cc)
        await svc.turn_events(turn_id, [read_line(json.dumps({"type": "x"}), 1)[0]])
        await svc.turn_done(turn_id, "success", None, 0, None)
    async with svc._tx() as tx:
        assert await repo.mark_run(tx.conn, live.id, "touched_live_at", tx.now)
    for key in (live_key, plain_key):
        await svc.set_stage(key, "done", actor="owner")
    await svc.settle_closings()
    assert await line_count(svc, live.id) == 0
    assert await line_count(svc, plain.id) == 1
    assert (await svc.get_run(plain.id)).deleted_at is None
    [deleted] = await events(svc, "run.traces_deleted")
    assert deleted["run_id"] == live.id and deleted["reason"] == "touched live"


def test_a_transcript_is_deleted_only_under_projects_and_by_its_own_name(tmp_path: Path) -> None:
    data = tmp_path / "claude"
    own = data / "projects" / "-x" / f"{CC_A}.jsonl"
    own.parent.mkdir(parents=True)
    own.write_text("{}", encoding="utf-8")
    outside = tmp_path / f"{CC_A}.jsonl"
    outside.write_text("{}", encoding="utf-8")
    deeper = data / "projects" / "-x" / "sub" / f"{CC_A}.jsonl"
    deeper.parent.mkdir()
    deeper.write_text("{}", encoding="utf-8")
    found = transcripts.find(data, CC_A, [str(outside), str(deeper), None])
    assert found == [own]
    assert transcripts.find(data, "../../etc", [str(own)]) == []
    deleted, failed = transcripts.delete(found)
    assert deleted == [str(own)] and failed == []
    assert outside.is_file() and deeper.is_file()


def stored(seq: int, ts: str) -> StreamEvent:
    return StreamEvent(seq, "assistant", None, None, ts, 2, False, pack({"n": seq}))


async def test_retention_deletes_old_lines_and_off_stores_none(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    turn_id = stub(svc).active[run.id]
    old, new = "2026-01-01T00:00:00.000+00:00", "2026-03-01T00:00:00.000+00:00"
    await svc.turn_events(turn_id, [stored(1, old), stored(2, new)])
    from datetime import UTC, datetime

    now = datetime(2026, 3, 5, tzinfo=UTC)
    assert await svc.retention_pass(now) == 1
    assert await line_count(svc, run.id) == 1
    [expired] = await events(svc, "run.events_expired")
    assert (expired["lines"], expired["retention"]) == (1, "30")
    saved = await svc.set_run_defaults("acme-shop", {"retention": "off"})
    assert saved.retention == "off"
    await svc.turn_events(turn_id, [stored(3, new)])
    assert await line_count(svc, run.id) == 1  # nothing new stored
    assert await svc.retention_pass(now) == 1 and await line_count(svc, run.id) == 0
    with pytest.raises(ValidationError, match="retention"):
        await svc.set_run_defaults("acme-shop", {"retention": "forever"})
    assert (await svc.run_defaults("acme-shop")).retention == "off"


# --- the memory readers --------------------------------------------------------------------


def test_meminfo_and_vm_stat_are_read_as_documented() -> None:
    meminfo = "MemTotal:       16314128 kB\nMemFree:  1000 kB\nMemAvailable:    2048000 kB\n"
    assert memory.parse_meminfo(meminfo) == 2000
    assert memory.parse_meminfo("MemFree: 1 kB\n") is None
    vm_stat = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               12800.\n"
        "Pages active:                            300000.\n"
        "Pages inactive:                           51200.\n"
        "Pages speculative:                         0.\n"
    )
    assert memory.parse_vm_stat(vm_stat) == 1000
    assert memory.parse_vm_stat("nothing here") is None

    def fake_vm_stat(*_: Any, **__: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["vm_stat"], 0, vm_stat, "")

    assert memory.macos_free_mb(fake_vm_stat) == 1000


def test_linux_reader_reads_a_meminfo_file(tmp_path: Path) -> None:
    info = tmp_path / "meminfo"
    info.write_text("MemAvailable:    3145728 kB\n", encoding="ascii")
    assert memory.linux_free_mb(info) == 3072
    assert memory.linux_free_mb(tmp_path / "missing") is None


@pytest.mark.skipif(os.name != "nt", reason="GlobalMemoryStatusEx is Windows'")
def test_windows_reader_answers_on_windows() -> None:
    free = memory.windows_free_mb()
    assert isinstance(free, int) and free > 0


# --- review fixes: work left in ignored files, git refusing, the server stopping, traces ------


def global_excludes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner keeps `.claude/` and `CLAUDE.local.md` out of git with a global excludes file,
    so the copies are ignored files in the worktree."""
    excludes = tmp_path / "excludes"
    excludes.write_text(".claude/\nCLAUDE.local.md\n", encoding="utf-8")
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\texcludesFile = {excludes.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


async def ignored_copy_run(svc: Service, tmp_path: Path) -> tuple[Any, Path, Any]:
    api = make_git_repo(
        tmp_path / "acme" / "api", {".claude/STATUS.md": "owner notes\n", "CLAUDE.local.md": "x\n"}
    )
    await svc.upsert_project("acme-shop", repos=[{"name": "api", "path": str(api)}])
    [api_repo] = await repo.list_project_repos(svc._r, (await svc.get_project("acme-shop")).id)
    run = await svc.start_run("acme-shop-1", repos=[(api_repo.id, True)])
    [w] = await repo.list_run_worktrees(svc._r, run.id)
    assert sorted(w.copied) == [".claude/STATUS.md", "CLAUDE.local.md"]
    return run, api, w


@needs_git
async def test_an_edited_or_new_ignored_file_keeps_the_worktree(
    svc: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_excludes(tmp_path, monkeypatch)
    _, _, w = await ignored_copy_run(svc, tmp_path)
    write(Path(w.path) / ".claude" / "STATUS.md", "AGENT WORK: where we stopped\n")
    write(Path(w.path) / ".claude" / "notes.md", "AGENT NEW NOTE\n")
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    kept = await repo.get_run_worktree(svc._r, w.id)
    assert kept.removed_at is None
    assert kept.kept_reason == "uncommitted changes (.claude/STATUS.md, .claude/notes.md)"
    assert on_disk(w.path, ".claude", "notes.md") and on_disk(w.path, "CLAUDE.local.md")


@needs_git
async def test_ignored_build_output_goes_with_the_worktree_an_untracked_file_keeps_it(
    svc: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_excludes(tmp_path, monkeypatch)
    _, api, w = await ignored_copy_run(svc, tmp_path)
    await svc.create_task("acme-shop", "Second", stage="dev")
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    assert (await repo.get_run_worktree(svc._r, w.id)).removed_at is not None
    assert on_disk(str(api), ".claude", "STATUS.md")  # the source keeps its own
    # A second run whose worktree holds ignored build output: it goes with the worktree.
    write(api / ".gitignore", "dist/\nnode_modules/\n")
    git(api, "add", ".gitignore")
    git(api, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", "ignore dist")
    [api_repo] = await repo.list_project_repos(svc._r, (await svc.get_project("acme-shop")).id)
    second = await svc.start_run("acme-shop-2", repos=[(api_repo.id, True)])
    [w2] = await repo.list_run_worktrees(svc._r, second.id)
    (Path(w2.path) / "dist").mkdir()
    write(Path(w2.path) / "dist" / "app.js", "built\n")
    (Path(w2.path) / "node_modules" / "x").mkdir(parents=True)
    write(Path(w2.path) / "node_modules" / "x" / "index.js", "dep\n")
    await svc.set_stage("acme-shop-2", "done", actor="owner")
    await svc.settle_closings()
    gone = await repo.get_run_worktree(svc._r, w2.id)
    assert gone.removed_at is not None and gone.kept_reason is None
    assert not on_disk(w2.path)
    # A third run with an untracked file git does not ignore: kept, and the owner is asked.
    await svc.create_task("acme-shop", "Third", stage="dev")
    third = await svc.start_run("acme-shop-3", repos=[(api_repo.id, True)])
    [w3] = await repo.list_run_worktrees(svc._r, third.id)
    write(Path(w3.path) / "new_module.py", "x = 1\n")
    await svc.set_stage("acme-shop-3", "done", actor="owner")
    await svc.settle_closings()
    kept = await repo.get_run_worktree(svc._r, w3.id)
    assert kept.removed_at is None and kept.kept_reason == "uncommitted changes (new_module.py)"


@needs_git
async def test_copies_come_back_when_git_refuses_the_removal(svc: Service, tmp_path: Path) -> None:
    api = make_git_repo(tmp_path / "acme" / "api", {"CLAUDE.md": "rules\n", ".mcp.json": "{}"})
    await svc.upsert_project("acme-shop", repos=[{"name": "api", "path": str(api)}])
    [api_repo] = await repo.list_project_repos(svc._r, (await svc.get_project("acme-shop")).id)
    run = await svc.start_run("acme-shop-1", repos=[(api_repo.id, True)])
    [w] = await repo.list_run_worktrees(svc._r, run.id)
    git(api, "worktree", "lock", w.path)  # git refuses to remove a locked worktree
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    kept = await repo.get_run_worktree(svc._r, w.id)
    assert kept.removed_at is None and kept.kept_reason.startswith("git worktree remove refused")
    assert on_disk(w.path, "CLAUDE.md") and on_disk(w.path, ".mcp.json")
    assert not on_disk(str(stub(svc).config.home), "runs", str(run.id), f"copies-{w.id}")


@needs_git
async def test_remove_is_not_done_for_a_card_that_left_done(svc: Service, tmp_path: Path) -> None:
    run, _ = await three_repo_run(svc, tmp_path)
    docs = next(w for w in await repo.list_run_worktrees(svc._r, run.id) if w.repo_name == "docs")
    write(Path(docs.path) / "draft.md", "draft\n")
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    kept = await repo.get_run_worktree(svc._r, docs.id)
    await svc.set_stage("acme-shop-1", "dev", actor="owner")
    assert (await svc.get_question(kept.question_id)).status == "withdrawn"
    with pytest.raises(ValidationError, match="withdrawn"):
        await svc.answer_question(kept.question_id, choice="remove")
    await svc._remove_on_answer(docs.id, "owner")  # straight past the question: still refused
    assert on_disk(docs.path, "draft.md")
    refused = (await events(svc, "run.worktree_kept"))[-1]
    assert refused["reason"] == "not removed: acme-shop-1 is no longer done"
    # Done again: asked anew.
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    again = await repo.get_run_worktree(svc._r, docs.id)
    assert (await svc.get_question(again.question_id)).status == "open"


class ClosingStub(StubRuns):
    async def close(self) -> None:
        # RunManager.close: closing first, then every running turn is stopped (turn_done runs).
        self.closing = True
        for run_id in list(self.active):
            await self.stop(run_id, "server stopped")


async def test_a_server_stop_keeps_the_queued_turn_for_the_next_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = LayerState("command", True, "command hooks from your user settings", "registered")
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: ok)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = ClosingStub(service, tmp_path)  # type: ignore[assignment]
    await service.set_run_limits(1, 0)
    for slug in ("acme-shop", "demo-api"):
        (tmp_path / slug).mkdir()
        await service.upsert_project(slug, repo_path=str(tmp_path / slug))
        await service.create_task(slug, "Card", stage="dev")
    first = await service.start_run("acme-shop-1")
    second = await service.start_run("demo-api-1")
    assert (first.status, second.status) == ("running", "queued")
    await service.close()
    again = Service(tmp_path / "relaybay.sqlite")
    await again.start()
    try:
        kept = await again.get_run(second.id)
        assert kept.status == "queued" and kept.status_reason.startswith("waiting for a slot")
        assert await repo.next_queued_turn(again._r, second.id) is not None
        assert (await again.get_run(first.id)).status == "stopped"
    finally:
        await again.close()


async def test_one_memory_reading_does_not_start_more_than_it_holds(svc: Service) -> None:
    await svc.set_run_limits(1, 0)
    keys = await cards(svc, 4)
    runs = [await svc.start_run(key) for key in keys]
    assert [r.status for r in runs] == ["running", "queued", "queued", "queued"]
    svc.memory_reader = lambda: 3200
    # One pass, one reading: each start takes 1500 MB off it, so the fourth waits.
    await svc.set_run_limits(4, 1500)
    after = [(await svc.get_run(r.id)).status for r in runs]
    assert after == ["running", "running", "running", "queued"]
    held = await svc.get_run(runs[3].id)
    assert held.status_reason == "waiting for memory: 0.2 GB free, 1.5 GB needed"


async def test_a_failed_trace_delete_is_tried_again(
    svc: Service, short_claude_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await svc.start_run("acme-shop-1")
    turn_id = stub(svc).active.pop(run.id)
    await svc.turn_init(turn_id, CC_A)
    await svc.turn_done(turn_id, "success", None, 0, None)
    async with svc._tx() as tx:
        await repo.mark_run(tx.conn, run.id, "secret_seen_at", tx.now)
    path = Path(run.cwd)  # any path: the delete below fails for it
    monkeypatch.setattr(transcripts, "find", lambda *_: [path])
    monkeypatch.setattr(transcripts, "delete", lambda paths: ([], [f"{path}: busy"]))
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    assert (await svc.get_run(run.id)).deleted_at is None
    await svc.close_done_cards()  # the sweep: the same failure, no second event
    assert len(await events(svc, "run.traces_deleted")) == 1
    monkeypatch.setattr(transcripts, "delete", lambda paths: ([str(path)], []))
    await svc.close_done_cards()
    assert (await svc.get_run(run.id)).deleted_at is not None
    assert len(await events(svc, "run.traces_deleted")) == 2


def test_a_session_folder_reached_through_a_link_is_not_deleted(tmp_path: Path) -> None:
    data = tmp_path / "claude"
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / CC_A).mkdir(parents=True)
    (data / "projects").mkdir(parents=True)
    if os.name == "nt":  # a junction: a symlink needs a privilege on Windows
        import _winapi

        _winapi.CreateJunction(str(elsewhere), str(data / "file-history"))
    else:
        (data / "file-history").symlink_to(elsewhere, target_is_directory=True)
    assert transcripts.find(data, CC_A, []) == []
    crafted = data / "projects" / ".." / f"{CC_A}.jsonl"
    write(data / f"{CC_A}.jsonl", "{}")
    assert not transcripts.own_transcript(crafted, data, CC_A)
    assert (elsewhere / CC_A).is_dir()


@needs_git
async def test_new_ignored_files_in_a_claude_folder_with_a_tracked_file_keep_the_worktree(
    svc: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ignore rules per file: `.claude/` holds a tracked settings.json, so git lists each
    # ignored file in it on its own, not the folder.
    excludes = tmp_path / "excludes"
    excludes.write_text(".claude/settings.local.json\n.claude/notes.md\n.mcp.json\n", "utf-8")
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\texcludesFile = {excludes.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    api = make_git_repo(tmp_path / "acme" / "api", {".claude/settings.json": "{}\n"})
    await svc.upsert_project("acme-shop", repos=[{"name": "api", "path": str(api)}])
    [api_repo] = await repo.list_project_repos(svc._r, (await svc.get_project("acme-shop")).id)
    run = await svc.start_run("acme-shop-1", repos=[(api_repo.id, True)])
    [w] = await repo.list_run_worktrees(svc._r, run.id)
    write(Path(w.path) / ".claude" / "settings.local.json", '{"permissions": {}}\n')
    write(Path(w.path) / ".mcp.json", "{}\n")
    await svc.set_stage("acme-shop-1", "done", actor="owner")
    await svc.settle_closings()
    kept = await repo.get_run_worktree(svc._r, w.id)
    assert kept.removed_at is None
    assert kept.kept_reason == "uncommitted changes (.claude/settings.local.json, .mcp.json)"
