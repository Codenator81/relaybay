"""Usage: the transcript reader, the per-day totals, the page and the MCP tool."""

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import load_settings
from relaybay.db import repo
from relaybay.db.connection import connect
from relaybay.domain import usage
from relaybay.domain.usage import UNKNOWN, UNKNOWN_LINE, UsageLine, parse_line, transcript_file
from relaybay.mcp_server import format as fmt
from relaybay.security import load_or_create_token
from relaybay.service import Service
from tests.conftest import LiveServer, run_in_app
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "PROMPT-TEXT-THAT-MUST-NOT-BE-KEPT"
TODAY = datetime.now(UTC)


def usage_line(
    message_id: str = "msg-1",
    *,
    model: str = "demo-model",
    day: datetime = TODAY,
    tokens: int = 10,
    text: str = SECRET,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": day.isoformat().replace("+00:00", "Z"),
            "message": {
                "id": message_id,
                "model": model,
                "content": [{"type": "text", "text": text}],
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": tokens * 2,
                    "cache_read_input_tokens": tokens * 3,
                    "cache_creation_input_tokens": tokens * 4,
                },
            },
        }
    )


def write_lines(path: Path, *lines: str) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


async def report_session(service: Service, path: Path, cc: str = "cc-1", cwd: str = "/d/acme"):
    return await service.record_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": cc,
            "cwd": cwd,
            "transcript_path": str(path),
        }
    )


def dump_database(db_path: Path) -> str:
    """Everything in the database as text."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


# --- the line parser --------------------------------------------------------


def test_parse_line_keeps_numbers_and_model_only() -> None:
    parsed = parse_line(usage_line().encode())
    assert isinstance(parsed, UsageLine)
    assert (parsed.message_id, parsed.model) == ("msg-1", "demo-model")
    assert (parsed.input, parsed.output, parsed.cache_read, parsed.cache_write) == (10, 20, 30, 40)
    assert SECRET not in repr(parsed)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        b'{"type": "a-line-type-of-the-future"}',
        b'{"type": "assistant", "message": {"id": "m"}}',
        b'{"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {"id": "m",'
        b' "usage": {"input_tokens": "many"}}}',
        b'{"type": "assistant", "timestamp": "yesterday", "message": {"id": "m",'
        b' "usage": {"input_tokens": 1}}}',
        b'{"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message":'
        b' {"usage": {"input_tokens": 1}}}',
    ],
)
def test_parse_line_unknown_is_never_guessed(raw: bytes) -> None:
    assert parse_line(raw) is UNKNOWN_LINE


@pytest.mark.parametrize("number", [2**53 + 1, 10**20, 1.5, -1, True, "1"])
def test_parse_line_impossible_numbers_are_unknown(number: object) -> None:
    line = json.loads(usage_line())
    line["message"]["usage"]["input_tokens"] = number
    assert parse_line(json.dumps(line).encode()) is UNKNOWN_LINE


@pytest.mark.parametrize("message_id", ["\ud800x", "a b", "a/b", "é", "", "x" * 201, 7])
def test_parse_line_odd_ids_are_unknown(message_id: object) -> None:
    line = json.loads(usage_line())
    line["message"]["id"] = message_id
    assert parse_line(json.dumps(line).encode()) is UNKNOWN_LINE


@pytest.mark.parametrize(
    "stamp",
    [
        "0001-01-01T00:00:00+23:00",
        "9999-12-31T23:59:59-23:00",
        "9999-12-31T00:00:00Z",
        "0001-01-02T00:00:00Z",
        "2019-12-31T23:59:59Z",
        (TODAY + timedelta(days=3)).isoformat(),
    ],
)
def test_parse_line_day_outside_the_range_is_unknown(stamp: str) -> None:
    line = json.loads(usage_line())
    line["timestamp"] = stamp
    assert parse_line(json.dumps(line).encode()) is UNKNOWN_LINE


def test_parse_line_without_usage_or_tokens_is_not_unknown() -> None:
    assert parse_line(b'{"type": "user", "message": {"content": "hello"}}') is None
    assert parse_line(usage_line(tokens=0).encode()) is None


@pytest.mark.parametrize("model", [None, 7, "<script>", "two words", "x" * 200])
def test_parse_line_odd_model_shows_unknown(model: object) -> None:
    line = json.loads(usage_line())
    line["message"]["model"] = model
    parsed = parse_line(json.dumps(line).encode())
    assert isinstance(parsed, UsageLine) and parsed.model == UNKNOWN


def test_parse_line_day_is_utc() -> None:
    line = json.loads(usage_line())
    line["timestamp"] = "2026-03-01T01:00:00+03:00"
    parsed = parse_line(json.dumps(line).encode())
    assert isinstance(parsed, UsageLine) and parsed.day == "2026-02-28"


# --- which file may be read -------------------------------------------------


def test_transcript_file_rules(tmp_path: Path) -> None:
    good = tmp_path / "a.jsonl"
    good.write_text("", encoding="utf-8")
    assert transcript_file(str(good)) == good.resolve()
    assert transcript_file(str(tmp_path / "not-written-yet.jsonl")) is not None
    assert transcript_file(str(tmp_path / "a.txt")) is None
    assert transcript_file("relative/a.jsonl") is None
    assert transcript_file("\\\\server\\share\\a.jsonl") is None
    assert transcript_file("//server/share/a.jsonl") is None
    assert transcript_file("\\/server/share/a.jsonl") is None
    assert transcript_file("/\\server\\share\\a.jsonl") is None
    assert transcript_file("a\x00.jsonl") is None


@pytest.mark.parametrize(
    "value",
    [
        "\\\\server\\share\\a.jsonl",
        "//server/share/a.jsonl",
        "\\/server/share/a.jsonl",
        "/\\server\\share\\a.jsonl",
    ],
)
def test_network_paths_are_refused_before_any_file_call(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_call(*_: object, **__: object) -> None:
        raise AssertionError("touched the file system")

    monkeypatch.setattr(Path, "resolve", no_call)
    monkeypatch.setattr(Path, "stat", no_call)
    assert transcript_file(value) is None


def test_mapped_network_drive_is_refused_before_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_call(*_: object, **__: object) -> None:
        raise AssertionError("touched the file system")

    monkeypatch.setattr(usage, "_remote_drive", lambda _drive: True)
    monkeypatch.setattr(Path, "resolve", no_call)
    assert transcript_file(str(tmp_path / "a.jsonl")) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows names")
def test_windows_stream_and_device_names_are_refused(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert transcript_file(str(tmp_path / "a.txt") + ":s.jsonl") is None
    for name in ("NUL.jsonl", "con.jsonl", "COM1.jsonl", "lpt2.x.jsonl"):
        assert transcript_file(str(tmp_path / name)) is None


async def test_path_check_runs_off_the_event_loop(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads = []
    real = usage.transcript_file

    def spy(value: str) -> Path | None:
        threads.append(threading.get_ident())
        return real(value)

    monkeypatch.setattr(usage, "transcript_file", spy)
    await report_session(service, tmp_path / "t.jsonl")
    await report_session(service, tmp_path / "t.jsonl")  # remembered: not asked again
    assert len(threads) == 1 and threads[0] != threading.get_ident()


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this machine does not allow symbolic links")


def test_link_leaving_its_folder_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "other.jsonl"
    secret.write_text("", encoding="utf-8")
    inside = tmp_path / "inside"
    inside.mkdir()
    _symlink(inside / "away.jsonl", secret)
    assert transcript_file(str(inside / "away.jsonl")) is None
    _symlink(inside / "near.jsonl", inside / "real.jsonl")
    assert transcript_file(str(inside / "near.jsonl")) is not None


# --- the reader -------------------------------------------------------------


async def test_transcript_path_is_stored_only_when_allowed(
    service: Service, tmp_path: Path
) -> None:
    good = tmp_path / "t.jsonl"
    session = await report_session(service, good)
    assert session.transcript_path == str(good)
    other = await report_session(service, tmp_path / "t.txt", cc="cc-2", cwd="/d/other")
    assert other.transcript_path is None
    far = await service.record_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": "cc-3",
            "cwd": "/d/third",
            "transcript_path": "\\\\server\\share\\t.jsonl",
        }
    )
    assert far.transcript_path is None


async def test_nothing_is_read_on_the_hook_path(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line())
    await report_session(service, path)
    assert (await service.usage_report()).rows == []
    assert (await service.usage_pass())[0] == 1
    assert (await service.usage_report()).total.input == 10


async def test_incremental_reading_and_partial_last_line(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line("m1"))
    await report_session(service, path)
    assert (await service.usage_pass())[0] == 1

    partial = usage_line("m2")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[:20])  # the writer is mid-line
    assert (await service.usage_pass())[0] == 0
    assert (await service.usage_report()).total.messages == 1

    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[20:] + "\n")
    assert (await service.usage_pass())[0] == 1
    assert (await service.usage_report()).total.messages == 2
    assert (await service.usage_pass())[0] == 0  # nothing new, nothing counted twice


async def test_duplicate_message_ids_count_once_also_across_files(
    service: Service, tmp_path: Path
) -> None:
    first = tmp_path / "first.jsonl"
    write_lines(first, usage_line("m1"), usage_line("m1"), usage_line("m2"))
    await report_session(service, first)
    assert (await service.usage_pass())[0] == 2

    # /resume: a new file repeats the old lines, then goes on.
    resumed = tmp_path / "resumed.jsonl"
    write_lines(resumed, usage_line("m1"), usage_line("m2"), usage_line("m3"))
    await report_session(service, resumed)
    assert (await service.usage_pass())[0] == 1
    report = await service.usage_report()
    assert report.total.messages == 3 and report.total.input == 30


async def test_file_that_shrank_replaces_its_own_rows(service: Service, tmp_path: Path) -> None:
    keep = tmp_path / "keep.jsonl"
    write_lines(keep, usage_line("k1", tokens=1))
    await report_session(service, keep, cc="cc-keep", cwd="/d/keep")
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line("m1"), usage_line("m2"), "not json")
    await report_session(service, path)
    await service.usage_pass()
    report = await service.usage_report()
    assert (report.total.messages, report.unknown_lines) == (3, 1)

    write_lines(path, usage_line("m3", tokens=5))
    assert (await service.usage_pass())[0] == 1
    report = await service.usage_report()
    assert report.total.messages == 2  # m3 and the other file's k1
    assert report.total.input == 6
    assert report.unknown_lines == 0

    # The ids it gave up can be counted again.
    write_lines(path, usage_line("m1", tokens=7, text=""))
    await service.usage_pass()
    assert (await service.usage_report()).total.input == 8


async def test_pass_is_bounded_and_catches_up(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [usage_line(f"m{n}") for n in range(50)]
    write_lines(path, *lines)
    await report_session(service, path)
    chunk = len(lines[0]) * 5
    counted, behind = await service.usage_pass(budget=chunk, chunk_bytes=chunk)
    assert 0 < counted < 50 and behind
    total = counted
    for _ in range(40):
        counted, behind = await service.usage_pass(budget=chunk, chunk_bytes=chunk)
        total += counted
        if not behind:
            break
    assert not behind and total == 50


async def test_line_too_long_is_skipped_and_counted(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    long_line = json.dumps({"type": "user", "padding": "x" * 5000})
    write_lines(path, usage_line("m1"), long_line, usage_line("m2"))
    await report_session(service, path)
    for _ in range(10):
        await service.usage_pass(chunk_bytes=1024)
    report = await service.usage_report()
    assert report.total.messages == 2 and report.unknown_lines == 1


async def test_unreadable_file_keeps_rows_and_says_so(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line())
    await report_session(service, path)
    await service.usage_pass()

    path.unlink()
    path.mkdir()  # no longer a regular file
    await service.usage_pass()
    report = await service.usage_report()
    assert report.total.messages == 1 and report.unread_files == 1

    path.rmdir()  # removed altogether: not an error, the rows stay
    write_lines(path, usage_line("m2"))
    await service.usage_pass()
    assert (await service.usage_report()).unread_files == 0


@pytest.mark.skipif(sys.platform == "win32", reason="named pipes of this kind are POSIX")
async def test_named_pipe_does_not_hang(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    os.mkfifo(path)
    await report_session(service, path)
    await service.usage_pass()
    assert (await service.usage_report()).unread_files == 1


async def test_no_text_is_stored_or_logged(
    service: Service, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(
        path,
        json.dumps({"type": "user", "message": {"content": SECRET}}),
        usage_line("m1"),
        json.dumps({"type": "a-line-type-of-the-future", "text": SECRET}),
        f"broken {SECRET}",
    )
    await report_session(service, path)
    with caplog.at_level("DEBUG"):
        await service.usage_pass()
    assert (await service.usage_report()).total.messages == 1
    assert SECRET not in caplog.text
    assert SECRET not in dump_database(service.db_path)


async def test_fixture_transcript_with_its_subagent(service: Service) -> None:
    await report_session(service, FIXTURES / "transcript.jsonl")
    await service.usage_pass()
    report = await service.usage_report(days=0)
    # Three in the transcript, two in the subagent's file; msg_demo_1 repeated there counts once.
    assert report.total.messages == 5
    assert report.unknown_lines == 2
    assert [m for m, _ in report.by_model] == ["demo-model-large", "demo-model-small", UNKNOWN]
    assert [s for s, _ in report.by_session] == ["acme"]
    assert report.by_model[0][1].input == 120 + 50 + 40


def subagent_folder(path: Path) -> Path:
    folder = path.parent / path.stem / "subagents"
    folder.mkdir(parents=True)
    return folder


async def test_subagents_are_found_later_and_follow_the_session(
    service: Service, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line("m1"))
    await report_session(service, path, cc="cc-9")
    await service.usage_pass()
    assert (await service.usage_report()).total.messages == 1

    # A subagent starts long after the hook reported the transcript.
    folder = subagent_folder(path)
    write_lines(folder / "agent-1.jsonl", usage_line("s1"))
    (folder / "notes.txt").write_text(usage_line("not-counted"), encoding="utf-8")
    await service.usage_pass()
    report = await service.usage_report()
    assert report.total.messages == 2 and [s for s, _ in report.by_session] == ["acme"]

    # New lines of a known file and a new file are both read.
    write_lines(folder / "agent-1.jsonl", usage_line("s1"), usage_line("s2"))
    write_lines(folder / "agent-2.jsonl", usage_line("s3"))
    await service.usage_pass()
    assert (await service.usage_report()).total.messages == 4

    # Linking the hook to a desktop session moves the subagents' rows too.
    await service.upsert_project("acme-shop")
    await service.create_task(
        "acme-shop",
        "Checkout",
        session="local_00000000-0000-0000-0000-00000000000b",
        session_title="checkout",
        hook_session="cc-9",
    )
    report = await service.usage_report()
    assert [s for s, _ in report.by_session] == ["checkout"]


async def test_subagent_files_move_with_a_transcript_reported_by_another_session(
    service: Service, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line("m1"))
    write_lines(subagent_folder(path) / "agent-1.jsonl", usage_line("s1"))
    await report_session(service, path, cc="cc-a", cwd="/d/a")
    await service.usage_pass()
    await report_session(service, path, cc="cc-b", cwd="/d/b")
    assert [s for s, _ in (await service.usage_report()).by_session] == ["b"]


async def test_subagent_files_have_a_cap_per_session(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage, "SUBAGENT_FILES_MAX", 2)
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line("m1"))
    folder = subagent_folder(path)
    for n in range(5):
        write_lines(folder / f"agent-{n}.jsonl", usage_line(f"s{n}"))
    await report_session(service, path)
    await service.usage_pass()
    await service.usage_pass()
    report = await service.usage_report()
    assert report.total.messages == 3 and report.capped_files == 3
    assert "3 subagent transcript(s) over the limit" in fmt.usage_summary(report, True)


def test_subagent_links_leaving_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write_lines(outside / "other.jsonl", usage_line("x"))
    main = tmp_path / "home" / "t.jsonl"
    main.parent.mkdir()
    write_lines(main, usage_line("m"))

    (main.parent / "t").mkdir()
    _symlink(main.parent / "t" / "subagents", outside)  # the folder itself is a link
    assert usage.subagent_files(str(main), set(), 10) == ([], 0)

    (main.parent / "t" / "subagents").unlink()
    folder = main.parent / "t" / "subagents"
    folder.mkdir()
    _symlink(folder / "away.jsonl", outside / "other.jsonl")  # a file in it is a link
    write_lines(folder / "real.jsonl", usage_line("s"))
    found, over = usage.subagent_files(str(main), set(), 10)
    assert [Path(p).name for p in found] == ["real.jsonl"] and over == 0


async def test_a_hostile_line_never_stops_the_reader(service: Service, tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl"
    write_lines(good, usage_line("g1"))
    bad = tmp_path / "bad.jsonl"
    huge = json.loads(usage_line("p1"))
    huge["message"]["usage"]["input_tokens"] = 10**20
    surrogate = usage_line("PLACEHOLDER").replace("PLACEHOLDER", "\\ud800x")
    far = usage_line("p2").replace(TODAY.isoformat().replace("+00:00", "Z"), "9999-12-31T00:00:00Z")
    write_lines(bad, json.dumps(huge), surrogate, far)
    await report_session(service, bad, cc="cc-bad", cwd="/d/bad")
    await report_session(service, good, cc="cc-good", cwd="/d/good")
    await service.usage_pass()
    for days in (0, 30):
        report = await service.usage_report(days=days)
        assert report.total.messages == 1 and report.unknown_lines == 3
        assert len(report.periods) <= usage.PERIODS_MAX


async def test_a_failing_file_stops_only_itself(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    bad = tmp_path / "bad.jsonl"
    write_lines(bad, usage_line("b1", text=SECRET))
    good = tmp_path / "good.jsonl"
    write_lines(good, usage_line("g1"))
    await report_session(service, bad, cc="cc-bad", cwd="/d/bad")
    await report_session(service, good, cc="cc-good", cwd="/d/good")
    real = usage.read_file

    def failing(value: str, *args: object) -> object:
        if value == str(bad):
            raise ValueError(f"a message quoting {SECRET}")
        return real(value, *args)

    monkeypatch.setattr(usage, "read_file", failing)
    await service.usage_pass()
    report = await service.usage_report()
    assert report.total.messages == 1 and report.unread_files == 1
    assert SECRET not in caplog.text and SECRET not in dump_database(service.db_path)
    monkeypatch.setattr(usage, "read_file", real)
    await service.usage_pass()
    report = await service.usage_report()
    assert report.total.messages == 2 and report.unread_files == 0


async def test_apply_failure_stops_only_its_file(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad.jsonl"
    write_lines(bad, usage_line("b1"))
    good = tmp_path / "good.jsonl"
    write_lines(good, usage_line("g1"))
    await report_session(service, bad, cc="cc-bad", cwd="/d/bad")
    await report_session(service, good, cc="cc-good", cwd="/d/good")
    real = repo.apply_usage_chunk
    calls = 0

    async def failing(*args: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.IntegrityError("no")
        return await real(*args)

    monkeypatch.setattr(repo, "apply_usage_chunk", failing)
    await service.usage_pass()
    report = await service.usage_report()
    assert report.total.messages == 1 and report.unread_files == 1


async def test_report_survives_totals_too_big_for_sum(service: Service, tmp_path: Path) -> None:
    await report_session(service, tmp_path / "t.jsonl")
    conn = sqlite3.connect(service.db_path)
    try:
        for n, day in enumerate(("2026-01-01", "2026-01-02")):
            conn.execute(
                "INSERT INTO usage_day (file_id, day, model, input, messages)"
                " VALUES (1, ?, ?, ?, 1)",
                (day, f"m{n}", 2**63 - 1),
            )
        conn.commit()
    finally:
        conn.close()
    assert (await service.usage_report(days=0)).total.input > 2**63


def test_chart_periods_are_capped() -> None:
    far = usage.UsageDayRow("0001-01-01", 1, "s", "p", "m", 1, 1, 1, 1, 1)
    future = usage.UsageDayRow("9999-12-31", 1, "s", "p", "m", 1, 1, 1, 1, 1)
    for group in ("day", "week"):
        report = usage.build_report(
            [far, future],
            group=group,
            days=0,
            today=TODAY.date(),
            files=1,
            unread_files=0,
            unknown_lines=0,
        )
        assert len(report.periods) <= usage.PERIODS_MAX
        assert report.total.messages == 2


async def test_stuck_partial_line_does_not_starve_other_files(
    service: Service, tmp_path: Path
) -> None:
    for n in range(4):
        path = tmp_path / f"partial{n}.jsonl"
        path.write_text("x" * 900, encoding="utf-8")
        await report_session(service, path, cc=f"cc-{n}", cwd=f"/d/{n}")
    good = tmp_path / "good.jsonl"
    write_lines(good, usage_line("g1"))
    await report_session(service, good, cc="cc-good", cwd="/d/good")
    counted, behind = await service.usage_pass(budget=2000, chunk_bytes=1000)
    assert counted == 1 and not behind
    assert (await service.usage_pass(budget=2000, chunk_bytes=1000)) == (0, False)


async def test_lines_per_transaction_are_capped(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, *[usage_line(f"m{n}") for n in range(25)])
    await report_session(service, path)
    sizes = []
    real = repo.apply_usage_chunk

    async def spy(conn: object, file_id: int, chunk: usage.Chunk, now: str) -> int:
        sizes.append((len(chunk.lines), chunk.offset))
        return await real(conn, file_id, chunk, now)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repo, "apply_usage_chunk", spy)
        assert await service.usage_pass(max_lines=10) == (25, False)
    assert [n for n, _ in sizes] == [10, 10, 5]
    assert sizes[-1][1] == path.stat().st_size
    assert sizes[0][1] < sizes[1][1] < sizes[2][1]
    assert (await service.usage_report()).total.messages == 25


async def test_usage_off_keeps_no_path(tmp_path: Path) -> None:
    service = Service(tmp_path / "relaybay.sqlite", usage_enabled=False)
    await service.start()
    try:
        session = await report_session(service, tmp_path / "t.jsonl")
        assert session.transcript_path is None
    finally:
        await service.close()


async def test_link_moves_usage_to_the_desktop_session(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line())
    await report_session(service, path, cc="cc-9")
    await service.usage_pass()
    await service.upsert_project("acme-shop")
    desktop = "local_00000000-0000-0000-0000-000000000009"
    await service.create_task(
        "acme-shop", "Checkout", session=desktop, session_title="checkout", hook_session="cc-9"
    )
    report = await service.usage_report()
    assert [(r.session, r.project) for r in report.rows] == [("checkout", "acme-shop")]
    session = (await service.list_sessions("acme-shop"))[0].session
    assert session.transcript_path == str(path)


# --- report -----------------------------------------------------------------


async def test_filters_week_and_unknown_project(service: Service, tmp_path: Path) -> None:
    await service.upsert_project("acme-shop", repo_path="/d/acme")
    await service.upsert_project("demo-api")
    old = TODAY - timedelta(days=40)
    first = tmp_path / "a.jsonl"
    write_lines(first, usage_line("a1"), usage_line("a2", model="other-model", tokens=1))
    second = tmp_path / "b.jsonl"
    write_lines(second, usage_line("b1", day=old))
    await report_session(service, first, cc="cc-a", cwd="/d/acme")
    await report_session(service, second, cc="cc-b", cwd="/somewhere/else")
    await service.usage_pass()

    assert (await service.usage_report(days=30)).total.messages == 2
    assert (await service.usage_report(days=0)).total.messages == 3
    only = await service.usage_report(project="acme-shop", days=0)
    assert only.total.messages == 2 and [p for p, _ in only.by_project] == ["acme-shop"]
    assert (await service.usage_report(project="demo-api", days=0)).rows == []
    assert (await service.usage_report(model="other-model", days=0)).total.input == 1
    everything = await service.usage_report(days=0)
    assert UNKNOWN in [p for p, _ in everything.by_project]
    # No gap in the chart between the old message and today.
    assert len(everything.periods) == 41
    weekly = await service.usage_report(days=0, group="week")
    assert all(datetime.fromisoformat(p).weekday() == 0 for p, _ in weekly.periods)
    assert sum(t.messages for _, t in weekly.periods) == 3


# --- page and tool ----------------------------------------------------------


def test_usage_page(board: TestClient, tmp_path: Path) -> None:
    service: Service = board.app.state.service
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line(), "broken")
    session = run_in_app(board, report_session, service, path)
    run_in_app(board, service.usage_pass)

    page = board.get("/usage")
    assert page.status_code == 200
    assert "demo-model" in page.text and "<svg" in page.text
    assert "not understood" in page.text
    # Tokens only.
    lowered = page.text.lower()
    assert "$" not in page.text and "%" not in page.text.split("</head>")[1]
    assert "cost" not in lowered

    filtered = board.get(f"/usage?session={session.id}&model=demo-model&days=7&group=week")
    assert filtered.status_code == 200 and "Week of" in filtered.text
    # The filter form sends empty values.
    assert board.get("/usage?project=&session=&model=&days=30&group=day").status_code == 200
    assert board.get("/usage?session=abc").status_code == 400
    assert board.get("/usage?session=" + "9" * 5000).status_code == 400
    assert board.get("/usage?session=" + "9" * 19).status_code == 400
    assert board.get("/usage?session=" + "\u0663").status_code == 400
    assert "Subagents count to their session" in page.text
    assert board.get("/usage?group=year").status_code == 400
    assert board.get("/usage?project=nope").status_code == 404
    assert 'href="/usage"' in board.get("/inbox").text


def test_usage_page_empty_and_off(tmp_path: Path) -> None:
    settings = load_settings(home=tmp_path, environ={"RELAYBAY_USAGE_ENABLED": "false"})
    assert settings.usage_enabled is False
    token = load_or_create_token(settings.token_path)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        client.get(f"/login?token={token}")
        page = client.get("/usage")
        assert "Reading is off" in page.text and "No usage for this filter" in page.text


def test_session_row_links_to_usage(board: TestClient, tmp_path: Path) -> None:
    service: Service = board.app.state.service
    session = run_in_app(board, report_session, service, tmp_path / "t.jsonl")
    assert f'href="/usage?session={session.id}"' in board.get("/sessions").text


def test_usage_page_needs_the_cookie(client: TestClient) -> None:
    assert client.get("/usage", headers={"Accept": "application/json"}).status_code == 401


def test_config_interval(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).usage_interval_seconds == 300
    env = {"RELAYBAY_USAGE_INTERVAL_SECONDS": "60"}
    assert load_settings(home=tmp_path, environ=env).usage_interval_seconds == 60
    with pytest.raises(ValueError, match="usage_interval_seconds"):
        load_settings(home=tmp_path, environ={"RELAYBAY_USAGE_INTERVAL_SECONDS": "1"})


async def test_background_reader_goes_on_after_a_failure(
    service: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line())
    await report_session(service, path)
    real_pass = service.usage_pass
    calls = 0

    async def flaky() -> tuple[int, bool]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("no")
        return await real_pass()

    monkeypatch.setattr(service, "usage_pass", flaky)
    reader = asyncio.create_task(service.usage_loop(0.01))
    try:
        for _ in range(200):
            if (await service.usage_report()).total.messages:
                break
            await asyncio.sleep(0.01)
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
    assert (await service.usage_report()).total.messages == 1


def test_reader_starts_and_stops_with_the_app(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    write_lines(transcript, usage_line())
    settings = load_settings(home=tmp_path, environ={})
    token = load_or_create_token(settings.token_path)

    async def seed() -> None:
        first = Service(settings.db_path)
        await first.start()
        try:
            await report_session(first, transcript)
        finally:
            await first.close()

    asyncio.run(seed())
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as client:
        client.get(f"/login?token={token}")
        for _ in range(100):  # the first pass runs as soon as the server is up
            if "demo-model" in client.get("/usage").text:
                break
            time.sleep(0.02)
        else:
            pytest.fail("the background reader did not count the transcript")


async def test_usage_summary_tool(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        text = await call(client, "usage_summary", days=7)
        assert "No usage recorded" in text and "no prices or quotas" in text
        result = await client.call_tool("usage_summary", {"session": "nobody"})
        assert result.is_error


async def test_usage_summary_format(service: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(path, usage_line(), "broken")
    await report_session(service, path)
    await service.usage_pass()
    ids = await service.usage_session_ids("cc-1")
    text = fmt.usage_summary(await service.usage_report(session_ids=ids, days=7), True)
    assert "demo-model" in text and "100 tokens" in text
    assert "1 transcript line(s) not understood" in text
    assert "$" not in text


# --- migration --------------------------------------------------------------


async def test_upgrade_from_0016_keeps_data(tmp_path: Path) -> None:
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(p.name for p in folder.iterdir() if p.name.endswith(".sql"))
    names = [n for n in names if int(n[:4]) <= 16]
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    db = tmp_path / "relaybay.sqlite"
    now = "2026-01-01T00:00:00.000+00:00"
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 16;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 1, ?, ?)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO task (project_id, key, title, stage, created_at, updated_at)"
            " VALUES (1, 'acme-shop-1', 'Checkout page', 'dev', ?, ?)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO session (name, cc_session_id, last_seen_at, created_at)"
            " VALUES ('acme-shop', 'cc-old', ?, ?)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO event (ts, kind, actor) VALUES (?, 'session.seen', 'acme-shop')", (now,)
        )
    finally:
        await conn.close()

    service = Service(db)
    await service.start()
    try:
        assert (await service.get_task("acme-shop-1")).title == "Checkout page"
        rows = await service.list_sessions()
        assert rows[0].session.name == "acme-shop" and rows[0].session.transcript_path is None
        assert await service.last_event_id() == 1
        path = tmp_path / "t.jsonl"
        write_lines(path, usage_line())
        session = await service.record_hook(
            {"hook_event_name": "Stop", "session_id": "cc-old", "transcript_path": str(path)}
        )
        assert session.transcript_path == str(path)
        await service.usage_pass()
        assert (await service.usage_report()).total.messages == 1
    finally:
        await service.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 17
    finally:
        await conn.close()
