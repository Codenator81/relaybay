"""SQL for every table. Functions take an open connection; transactions belong to the service."""

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from relaybay.domain.annotations import Annotation, AnnotationReply, geometry_json
from relaybay.domain.backlog import UNASSIGNED, BacklogItem
from relaybay.domain.environments import (
    AccessEntry,
    Environment,
    PolicyProposal,
    Proposal,
    split_test_secrets,
)
from relaybay.domain.intake import IntakeMessage, IntakeSource
from relaybay.domain.map_graph import CommentRow, MessageRow
from relaybay.domain.memory import (
    Document,
    DocumentVersion,
    MemoryBlock,
    MemoryNote,
    Source,
    stem,
    stem_text,
)
from relaybay.domain.models import (
    Binding,
    Comment,
    Decision,
    Event,
    ImportItem,
    Project,
    Report,
    SavedFilter,
    Session,
    Task,
    TrackerEntry,
)
from relaybay.domain.plan import PlanRevision, PlanStep, TodoItem
from relaybay.domain.review import Attachment, ReviewStep
from relaybay.domain.rules import RuleProposal, RuleVersion
from relaybay.domain.stages import ACCEPTANCE_STAGES, Stage
from relaybay.domain.usage import Chunk, UsageDayRow, UsageFile

logger = logging.getLogger("relaybay")

_TASK_SELECT = """
SELECT t.*, p.slug AS project_slug, p.autonomy AS project_autonomy,
       s.name AS session_name, s.external_id AS session_external_id,
       s.status AS session_status, s.desktop_link AS session_link,
       s.activity AS session_activity, s.activity_at AS session_activity_at,
       (SELECT COUNT(*) FROM review_step r WHERE r.task_id = t.id) AS review_total,
       (SELECT COUNT(*) FROM review_step r WHERE r.task_id = t.id AND r.status = 'accepted')
           AS review_accepted,
       (SELECT COUNT(*) FROM review_step r WHERE r.task_id = t.id AND r.status = 'rejected')
           AS review_rejected,
       (SELECT COUNT(*) FROM plan_step ps WHERE ps.task_id = t.id) AS plan_total,
       (SELECT COUNT(*) FROM plan_step ps WHERE ps.task_id = t.id AND ps.status = 'done')
           AS plan_done,
       (SELECT COUNT(*) FROM plan_step ps WHERE ps.task_id = t.id
           AND ps.conformance LIKE 'deviation:%') AS plan_deviations,
       (SELECT ps.title FROM plan_step ps WHERE ps.task_id = t.id AND ps.status = 'in_progress'
           ORDER BY ps.position LIMIT 1) AS plan_now,
       (SELECT ps.position FROM plan_step ps WHERE ps.task_id = t.id
           AND ps.status = 'in_progress' ORDER BY ps.position LIMIT 1) AS plan_now_position,
       (SELECT MAX(pr.created_at) FROM plan_revision pr WHERE pr.task_id = t.id)
           AS plan_revised_at
FROM task t
JOIN project p ON p.id = t.project_id
LEFT JOIN session s ON s.id = t.session_id
"""


def _project(row: aiosqlite.Row) -> Project:
    return Project(
        id=row["id"],
        slug=row["slug"],
        name=row["name"],
        repo_path=row["repo_path"],
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
        pulse=row["pulse"],
        pulse_at=row["pulse_at"],
        pulse_by=row["pulse_by"],
        autonomy=bool(row["autonomy"]),
    )


def _task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        project_id=row["project_id"],
        key=row["key"],
        title=row["title"],
        stage=Stage(row["stage"]),
        stage_note=row["stage_note"],
        waiting_on=row["waiting_on"],
        next_step=row["next_step"],
        branch=row["branch"],
        doc_path=row["doc_path"],
        description=row["description"],
        progress=row["progress"],
        session_id=row["session_id"],
        priority=row["priority"],
        tracker_ref=row["tracker_ref"],
        tracker_name=row["tracker_name"],
        tracker_url=row["tracker_url"],
        tracker_logged=bool(row["tracker_logged"]),
        next_step_at=row["next_step_at"],
        stage_changed_at=row["stage_changed_at"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        closed_at=row["closed_at"],
        project_slug=row["project_slug"],
        session_name=row["session_name"],
        session_external_id=row["session_external_id"],
        session_status=row["session_status"],
        session_link=row["session_link"],
        session_activity=row["session_activity"],
        session_activity_at=row["session_activity_at"],
        review_total=row["review_total"],
        review_accepted=row["review_accepted"],
        review_rejected=row["review_rejected"],
        size=row["size"],
        plan_status=row["plan_status"],
        plan_accepted_at=row["plan_accepted_at"],
        plan_accepted_by=row["plan_accepted_by"],
        plan_total=row["plan_total"],
        plan_done=row["plan_done"],
        plan_deviations=row["plan_deviations"],
        plan_now=row["plan_now"],
        plan_now_position=row["plan_now_position"],
        plan_revised_at=row["plan_revised_at"],
        autonomy=row["autonomy"],
        project_autonomy=bool(row["project_autonomy"]),
        answer_received_at=row["answer_received_at"],
        answer_message_id=row["answer_message_id"],
    )


def _report(row: aiosqlite.Row) -> Report:
    return Report(
        id=row["id"],
        task_id=row["task_id"],
        session_id=row["session_id"],
        author=row["author"],
        state=row["state"],
        size=row["size"],
        summary=row["summary"],
        body_md=row["body_md"],
        created_at=row["created_at"],
    )


def _event(row: aiosqlite.Row) -> Event:
    return Event(
        id=row["id"],
        ts=row["ts"],
        kind=row["kind"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        session_id=row["session_id"],
        actor=row["actor"],
        payload=json.loads(row["payload"]),
    )


async def _one(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> Any:
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()


async def _all(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    async with conn.execute(sql, params) as cur:
        return list(await cur.fetchall())


# --- project ---------------------------------------------------------------


async def get_project(conn: aiosqlite.Connection, slug: str) -> Project | None:
    row = await _one(conn, "SELECT * FROM project WHERE slug = ?", (slug,))
    return _project(row) if row else None


async def list_projects(conn: aiosqlite.Connection) -> list[Project]:
    rows = await _all(conn, "SELECT * FROM project WHERE archived_at IS NULL ORDER BY slug")
    return [_project(r) for r in rows]


async def insert_project(
    conn: aiosqlite.Connection,
    slug: str,
    name: str,
    repo_path: str | None,
    description: str | None,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO project (slug, name, repo_path, description, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (slug, name, repo_path, description, now, now),
    )
    return int(cur.lastrowid or 0)


async def update_project(
    conn: aiosqlite.Connection, project_id: int, changes: dict[str, Any], now: str
) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(
        f"UPDATE project SET {sets}, updated_at = ? WHERE id = ?",
        (*changes.values(), now, project_id),
    )


async def get_project_by_id(conn: aiosqlite.Connection, project_id: int) -> Project | None:
    row = await _one(conn, "SELECT * FROM project WHERE id = ?", (project_id,))
    return _project(row) if row else None


async def next_task_number(conn: aiosqlite.Connection, project_id: int) -> int:
    await conn.execute("UPDATE project SET task_seq = task_seq + 1 WHERE id = ?", (project_id,))
    row = await _one(conn, "SELECT task_seq FROM project WHERE id = ?", (project_id,))
    return int(row[0])


async def stage_counts(conn: aiosqlite.Connection) -> dict[int, dict[str, int]]:
    rows = await _all(
        conn, "SELECT project_id, stage, COUNT(*) AS n FROM task GROUP BY project_id, stage"
    )
    counts: dict[int, dict[str, int]] = {}
    for r in rows:
        counts.setdefault(r["project_id"], {})[r["stage"]] = r["n"]
    return counts


# --- session ---------------------------------------------------------------


def _session(row: aiosqlite.Row) -> Session:
    return Session(
        id=row["id"],
        name=row["name"],
        external_id=row["external_id"],
        kind=row["kind"],
        desktop_link=row["desktop_link"],
        status=row["status"],
        last_seen_at=row["last_seen_at"],
        created_at=row["created_at"],
        project_id=row["project_id"],
        cwd=row["cwd"],
        activity=row["activity"],
        activity_at=row["activity_at"],
        cc_session_id=row["cc_session_id"],
        unlinked_reason=row["unlinked_reason"],
        cc_exact=bool(row["cc_exact"]),
        transcript_path=row["transcript_path"],
        no_live_at=row["no_live_at"],
        no_live_by=row["no_live_by"],
    )


async def get_session_by_name(conn: aiosqlite.Connection, name: str) -> Session | None:
    """A plain-name session (one without an external id)."""
    row = await _one(conn, "SELECT * FROM session WHERE name = ? AND external_id IS NULL", (name,))
    return _session(row) if row else None


async def get_session_by_id(conn: aiosqlite.Connection, session_id: int) -> Session | None:
    row = await _one(conn, "SELECT * FROM session WHERE id = ?", (session_id,))
    return _session(row) if row else None


async def get_session_by_external_id(
    conn: aiosqlite.Connection, external_id: str
) -> Session | None:
    row = await _one(conn, "SELECT * FROM session WHERE external_id = ?", (external_id,))
    return _session(row) if row else None


async def find_sessions(conn: aiosqlite.Connection, ref: str) -> list[Session]:
    """Sessions matching an external id, a name (desktop titles may repeat) or an alias."""
    rows = await _all(
        conn,
        "SELECT * FROM session WHERE external_id = ? OR name = ?"
        " OR id IN (SELECT session_id FROM session_alias WHERE name = ?) ORDER BY id",
        (ref, ref, ref),
    )
    return [_session(r) for r in rows]


async def get_session_by_alias(conn: aiosqlite.Connection, name: str) -> Session | None:
    row = await _one(
        conn,
        "SELECT s.* FROM session_alias a JOIN session s ON s.id = a.session_id WHERE a.name = ?",
        (name,),
    )
    return _session(row) if row else None


async def set_session_alias(
    conn: aiosqlite.Connection, name: str, session_id: int, now: str
) -> None:
    """Point `name` at `session_id`, replacing an alias of another session."""
    await conn.execute(
        "INSERT INTO session_alias (name, session_id, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET session_id = excluded.session_id,"
        " created_at = excluded.created_at",
        (name, session_id, now),
    )


async def insert_session(
    conn: aiosqlite.Connection,
    name: str,
    now: str,
    *,
    external_id: str | None = None,
    kind: str | None = None,
    desktop_link: str | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO session (name, external_id, kind, desktop_link, last_seen_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (name, external_id, kind, desktop_link, now, now),
    )
    return int(cur.lastrowid or 0)


async def touch_session(
    conn: aiosqlite.Connection, session_id: int, now: str, changes: dict[str, Any] | None = None
) -> None:
    changes = changes or {}
    sets = "".join(f", {k} = ?" for k in changes)
    await conn.execute(
        f"UPDATE session SET last_seen_at = ?{sets} WHERE id = ?",
        (now, *changes.values(), session_id),
    )


async def set_session_no_live(
    conn: aiosqlite.Connection, session_id: int, at: str | None, by: str | None
) -> None:
    """The owner's switch; not the session's own activity, so `last_seen_at` stays."""
    await conn.execute(
        "UPDATE session SET no_live_at = ?, no_live_by = ? WHERE id = ?", (at, by, session_id)
    )


async def get_session_by_cc(conn: aiosqlite.Connection, cc_session_id: str) -> Session | None:
    row = await _one(conn, "SELECT * FROM session WHERE cc_session_id = ?", (cc_session_id,))
    return _session(row) if row else None


async def projects_with_repo_path(conn: aiosqlite.Connection) -> list[Project]:
    rows = await _all(
        conn, "SELECT * FROM project WHERE archived_at IS NULL AND repo_path IS NOT NULL"
    )
    return [_project(r) for r in rows]


async def bound_desktop_sessions(conn: aiosqlite.Connection, project_id: int) -> list[Session]:
    """Desktop sessions with an open binding to a task of the project, most recent first."""
    rows = await _all(
        conn,
        "SELECT s.* FROM session s JOIN task_session b ON b.session_id = s.id"
        " JOIN task t ON t.id = b.task_id"
        " WHERE t.project_id = ? AND b.unbound_at IS NULL AND s.external_id IS NOT NULL"
        " GROUP BY s.id ORDER BY MAX(b.bound_at) DESC, s.id DESC",
        (project_id,),
    )
    return [_session(r) for r in rows]


async def open_tasks_of_session(conn: aiosqlite.Connection, session_id: int) -> list[Task]:
    """Tasks the session is bound to now."""
    rows = await _all(
        conn,
        _TASK_SELECT + " WHERE t.id IN (SELECT task_id FROM task_session"
        " WHERE session_id = ? AND unbound_at IS NULL) ORDER BY t.id",
        (session_id,),
    )
    return [_task(r) for r in rows]


async def bound_task_ids(conn: aiosqlite.Connection, session_id: int) -> list[int]:
    """The tasks the session is bound to now, most recently bound first."""
    rows = await _all(
        conn,
        "SELECT task_id FROM task_session WHERE session_id = ? AND unbound_at IS NULL"
        " ORDER BY id DESC",
        (session_id,),
    )
    return [r["task_id"] for r in rows]


async def has_bindings(conn: aiosqlite.Connection, session_id: int) -> bool:
    row = await _one(conn, "SELECT 1 FROM task_session WHERE session_id = ? LIMIT 1", (session_id,))
    return row is not None


async def delete_session(conn: aiosqlite.Connection, session_id: int) -> None:
    await conn.execute("DELETE FROM session WHERE id = ?", (session_id,))


async def list_unlinked_hook_sessions(conn: aiosqlite.Connection, limit: int) -> list[Session]:
    rows = await _all(
        conn,
        "SELECT * FROM session WHERE unlinked_reason IS NOT NULL"
        " ORDER BY last_seen_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [_session(r) for r in rows]


async def list_sessions(
    conn: aiosqlite.Connection, project_id: int | None, limit: int
) -> list[Session]:
    """Sessions, most recently seen first. With `project_id`: those its hooks placed in the
    project and those bound to one of its tasks now."""
    if project_id is None:
        rows = await _all(
            conn, "SELECT * FROM session ORDER BY last_seen_at DESC, id DESC LIMIT ?", (limit,)
        )
    else:
        rows = await _all(
            conn,
            "SELECT * FROM session WHERE project_id = ? OR id IN (SELECT b.session_id"
            " FROM task_session b JOIN task t ON t.id = b.task_id"
            " WHERE t.project_id = ? AND b.unbound_at IS NULL)"
            " ORDER BY last_seen_at DESC, id DESC LIMIT ?",
            (project_id, project_id, limit),
        )
    return [_session(r) for r in rows]


async def open_binding(conn: aiosqlite.Connection, task_id: int) -> int | None:
    row = await _one(
        conn,
        "SELECT session_id FROM task_session WHERE task_id = ? AND unbound_at IS NULL",
        (task_id,),
    )
    return row[0] if row else None


async def rebind(
    conn: aiosqlite.Connection, task_id: int, session_id: int, actor: str, now: str
) -> None:
    """Close the task's open binding (if any) and open one for `session_id`."""
    await conn.execute(
        "UPDATE task_session SET unbound_at = ? WHERE task_id = ? AND unbound_at IS NULL",
        (now, task_id),
    )
    await conn.execute(
        "INSERT INTO task_session (task_id, session_id, bound_at, bound_by) VALUES (?, ?, ?, ?)",
        (task_id, session_id, now, actor),
    )


async def unbind(conn: aiosqlite.Connection, task_id: int, now: str) -> None:
    await conn.execute(
        "UPDATE task_session SET unbound_at = ? WHERE task_id = ? AND unbound_at IS NULL",
        (now, task_id),
    )


async def list_bindings(conn: aiosqlite.Connection, task_id: int, limit: int = 50) -> list[Binding]:
    rows = await _all(
        conn,
        "SELECT b.*, s.name AS session_name, s.external_id AS session_external_id,"
        " s.desktop_link AS session_link"
        " FROM task_session b JOIN session s ON s.id = b.session_id"
        " WHERE b.task_id = ? ORDER BY b.id DESC LIMIT ?",
        (task_id, limit),
    )
    return [
        Binding(
            id=r["id"],
            task_id=r["task_id"],
            session_id=r["session_id"],
            session_name=r["session_name"],
            session_external_id=r["session_external_id"],
            session_link=r["session_link"],
            bound_at=r["bound_at"],
            unbound_at=r["unbound_at"],
            bound_by=r["bound_by"],
        )
        for r in rows
    ]


# --- task ------------------------------------------------------------------


async def get_task_by_key(conn: aiosqlite.Connection, key: str) -> Task | None:
    row = await _one(conn, _TASK_SELECT + " WHERE t.key = ?", (key,))
    return _task(row) if row else None


async def get_task_by_id(conn: aiosqlite.Connection, task_id: int) -> Task | None:
    row = await _one(conn, _TASK_SELECT + " WHERE t.id = ?", (task_id,))
    return _task(row) if row else None


async def list_tasks(
    conn: aiosqlite.Connection,
    *,
    project_id: int | None = None,
    stages: list[str] | None = None,
    waiting_on: str | None = None,
    session_ids: list[int] | None = None,
) -> list[Task]:
    where, params = [], []
    if project_id is not None:
        where.append("t.project_id = ?")
        params.append(project_id)
    if stages:
        where.append(f"t.stage IN ({', '.join('?' for _ in stages)})")
        params.extend(stages)
    if waiting_on is not None:
        where.append("t.waiting_on = ? COLLATE NOCASE")
        params.append(waiting_on)
    if session_ids is not None:
        where.append(f"t.session_id IN ({', '.join('?' for _ in session_ids) or 'NULL'})")
        params.extend(session_ids)
    sql = _TASK_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.priority DESC, t.updated_at DESC, t.id DESC"
    return [_task(r) for r in await _all(conn, sql, tuple(params))]


async def insert_task(
    conn: aiosqlite.Connection,
    project_id: int,
    key: str,
    fields: dict[str, Any],
    session_id: int | None,
    now: str,
) -> int:
    cols = ["project_id", "key", *fields.keys(), "session_id", "created_at", "updated_at"]
    cols += ["next_step_at", "stage_changed_at"]
    values = [project_id, key, *fields.values(), session_id, now, now, now, now]
    if fields.get("stage") == Stage.DONE:
        cols.append("closed_at")
        values.append(now)
    cur = await conn.execute(
        f"INSERT INTO task ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        tuple(values),
    )
    return int(cur.lastrowid or 0)


async def update_task(
    conn: aiosqlite.Connection, task_id: int, changes: dict[str, Any], now: str
) -> None:
    sets = [f"{k} = ?" for k in changes]
    params = list(changes.values())
    if "stage" in changes:
        sets.append("closed_at = ?")
        params.append(now if changes["stage"] == Stage.DONE else None)
        sets.append("stage_changed_at = ?")
        params.append(now)
    if "next_step" in changes:
        sets.append("next_step_at = ?")
        params.append(now)
    if "waiting_on" in changes and "answer_received_at" not in changes:
        # The "answer received" mark is about who was waited on: a new `waiting_on` clears it.
        sets += ["answer_received_at = NULL", "answer_message_id = NULL"]
    sets += ["version = version + 1", "updated_at = ?"]
    await conn.execute(f"UPDATE task SET {', '.join(sets)} WHERE id = ?", (*params, now, task_id))


async def distinct_waiting_on(conn: aiosqlite.Connection, limit: int = 50) -> list[str]:
    rows = await _all(
        conn,
        "SELECT waiting_on FROM task WHERE waiting_on IS NOT NULL"
        " GROUP BY waiting_on ORDER BY MAX(updated_at) DESC LIMIT ?",
        (limit,),
    )
    return [r[0] for r in rows]


# --- tracker history -------------------------------------------------------


def _tracker_entry(row: aiosqlite.Row) -> TrackerEntry:
    return TrackerEntry(
        id=row["id"],
        task_id=row["task_id"],
        tracker_name=row["tracker_name"],
        tracker_ref=row["tracker_ref"],
        tracker_url=row["tracker_url"],
        logged_at=row["logged_at"],
        unlogged_at=row["unlogged_at"],
        created_at=row["created_at"],
    )


async def insert_tracker_entry(
    conn: aiosqlite.Connection,
    task_id: int,
    tracker_name: str | None,
    tracker_ref: str,
    tracker_url: str | None,
    logged_at: str | None,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO tracker_entry (task_id, tracker_name, tracker_ref, tracker_url, logged_at,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, tracker_name, tracker_ref, tracker_url, logged_at, now),
    )
    return int(cur.lastrowid or 0)


async def latest_tracker_entry(
    conn: aiosqlite.Connection, task_id: int, tracker_ref: str
) -> TrackerEntry | None:
    row = await _one(
        conn,
        "SELECT * FROM tracker_entry WHERE task_id = ? AND tracker_ref = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, tracker_ref),
    )
    return _tracker_entry(row) if row else None


async def update_tracker_entry(
    conn: aiosqlite.Connection, entry_id: int, changes: dict[str, Any]
) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(
        f"UPDATE tracker_entry SET {sets} WHERE id = ?", (*changes.values(), entry_id)
    )


async def list_tracker_entries(
    conn: aiosqlite.Connection, task_id: int, limit: int = 50
) -> list[TrackerEntry]:
    rows = await _all(
        conn,
        "SELECT * FROM tracker_entry WHERE task_id = ? ORDER BY id DESC LIMIT ?",
        (task_id, limit),
    )
    return [_tracker_entry(r) for r in rows]


# --- report / decision -----------------------------------------------------


async def insert_report(
    conn: aiosqlite.Connection,
    task_id: int,
    session_id: int | None,
    author: str,
    state: str | None,
    size: str | None,
    summary: str,
    body_md: str,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO report (task_id, session_id, author, state, size, summary, body_md,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, session_id, author, state, size, summary, body_md, now),
    )
    return int(cur.lastrowid or 0)


async def list_reports(conn: aiosqlite.Connection, task_id: int, limit: int) -> list[Report]:
    rows = await _all(
        conn, "SELECT * FROM report WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)
    )
    return [_report(r) for r in rows]


async def has_report_since(conn: aiosqlite.Connection, task_id: int, since: str | None) -> bool:
    """Any report of the task created at or after `since` (any report at all when None)."""
    row = await _one(
        conn,
        "SELECT 1 FROM report WHERE task_id = ? AND (? IS NULL OR created_at >= ?) LIMIT 1",
        (task_id, since, since),
    )
    return row is not None


async def has_report_after(conn: aiosqlite.Connection, task_id: int, moment: str) -> bool:
    """Any report of the task created strictly after `moment`."""
    row = await _one(
        conn, "SELECT 1 FROM report WHERE task_id = ? AND created_at > ? LIMIT 1", (task_id, moment)
    )
    return row is not None


async def stale_draft_plans(
    conn: aiosqlite.Connection, before: str, project_id: int | None, limit: int
) -> tuple[list[tuple[str, str]], int]:
    """(key, newest plan revision time) of tasks in dev whose plan is a draft not written since
    `before`, oldest first and at most `limit`; and how many there are in all."""
    revised = "(SELECT MAX(pr.created_at) FROM plan_revision pr WHERE pr.task_id = t.id)"
    where = (
        "t.stage = 'dev' AND t.plan_status = 'draft'"
        " AND EXISTS (SELECT 1 FROM plan_step ps WHERE ps.task_id = t.id)"
        f" AND {revised} <= ?"
    )
    params: list[Any] = [before]
    if project_id is not None:
        where += " AND t.project_id = ?"
        params.append(project_id)
    rows = await _all(
        conn,
        f"SELECT t.key, {revised} AS revised FROM task t WHERE {where}"
        " ORDER BY revised, t.id LIMIT ?",
        (*params, limit),
    )
    total = await _one(conn, f"SELECT COUNT(*) FROM task t WHERE {where}", tuple(params))
    return [(r["key"], r["revised"]) for r in rows], int(total[0]) if total else 0


async def last_reports(conn: aiosqlite.Connection, task_ids: list[int]) -> dict[int, Report]:
    if not task_ids:
        return {}
    marks = ", ".join("?" for _ in task_ids)
    rows = await _all(
        conn,
        f"SELECT * FROM report WHERE id IN (SELECT MAX(id) FROM report WHERE task_id IN ({marks})"
        " GROUP BY task_id)",
        tuple(task_ids),
    )
    return {r["task_id"]: _report(r) for r in rows}


async def insert_decision(
    conn: aiosqlite.Connection,
    project_id: int,
    task_id: int | None,
    text: str,
    decided_by: str | None,
    now: str,
    source: Source | None = None,
) -> int:
    source = source or Source()
    cur = await conn.execute(
        "INSERT INTO decision (project_id, task_id, text, decided_by, created_at, scope,"
        " source_kind, source_ref, source_text, valid_from, recorded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            task_id,
            text,
            decided_by,
            now,
            "project" if task_id is None else "task",
            source.kind,
            source.ref,
            source.text,
            now,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


async def supersede_decision(
    conn: aiosqlite.Connection, old_id: int, new_id: int, now: str
) -> None:
    await conn.execute(
        "UPDATE decision SET superseded_at = ?, superseded_by = ?, valid_to = ? WHERE id = ?",
        (now, new_id, now, old_id),
    )


def _decision(r: aiosqlite.Row) -> Decision:
    return Decision(
        id=r["id"],
        task_id=r["task_id"],
        project_id=r["project_id"],
        text=r["text"],
        decided_by=r["decided_by"],
        created_at=r["created_at"],
        task_key=r["task_key"],
        scope=r["scope"],
        source_kind=r["source_kind"],
        source_ref=r["source_ref"],
        source_text=r["source_text"],
        valid_from=r["valid_from"],
        valid_to=r["valid_to"],
        recorded_at=r["recorded_at"],
        superseded_at=r["superseded_at"],
        superseded_by=r["superseded_by"],
        project_slug=r["project_slug"],
    )


async def get_decision(conn: aiosqlite.Connection, decision_id: int) -> Decision | None:
    rows = await list_decisions(conn, ids=[decision_id], include_superseded=True)
    return rows[0] if rows else None


async def list_decisions(
    conn: aiosqlite.Connection,
    *,
    project_id: int | None = None,
    task_id: int | None = None,
    ids: list[int] | None = None,
    include_superseded: bool = False,
    scope: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> list[Decision]:
    """Newest first; superseded decisions only with `include_superseded`."""
    where, params = _decision_filter(project_id, task_id, ids, include_superseded, scope, since)
    sql = (
        "SELECT d.*, t.key AS task_key, p.slug AS project_slug FROM decision d"
        " JOIN project p ON p.id = d.project_id LEFT JOIN task t ON t.id = d.task_id"
    )
    rows = await _all(conn, sql + where + " ORDER BY d.id DESC LIMIT ?", (*params, limit))
    return [_decision(r) for r in rows]


def _decision_filter(
    project_id: int | None,
    task_id: int | None,
    ids: list[int] | None,
    include_superseded: bool,
    scope: str | None,
    since: str | None,
) -> tuple[str, list[Any]]:
    where, params = [], []
    if project_id is not None:
        where.append("d.project_id = ?")
        params.append(project_id)
    if task_id is not None:
        where.append("d.task_id = ?")
        params.append(task_id)
    if ids is not None:
        where.append(f"d.id IN ({', '.join('?' for _ in ids) or 'NULL'})")
        params.extend(ids)
    if not include_superseded:
        where.append("d.superseded_at IS NULL")
    if scope is not None:
        where.append("d.scope = ?")
        params.append(scope)
    if since is not None:
        where.append("d.recorded_at >= ?")
        params.append(since)
    return (" WHERE " + " AND ".join(where) if where else ""), params


async def count_current_decisions(
    conn: aiosqlite.Connection,
    *,
    project_id: int | None = None,
    since: str | None = None,
) -> int:
    where, params = _decision_filter(project_id, None, None, False, None, since)
    row = await _one(conn, "SELECT COUNT(*) FROM decision d" + where, tuple(params))
    return int(row[0])


# --- comment ---------------------------------------------------------------

_COMMENT_SELECT = """
SELECT c.*, t.key AS task_key, d.name AS delivered_to_name
FROM comment c
JOIN task t ON t.id = c.task_id
LEFT JOIN session d ON d.id = c.delivered_to_session_id
"""
# Comments for the agent (owner and external) on the tasks a session is bound to now.
_BOUND_OWNER = (
    " WHERE c.author_kind != 'agent' AND c.read_at IS NULL AND c.task_id IN"
    " (SELECT task_id FROM task_session WHERE session_id = ? AND unbound_at IS NULL)"
)


def _comment(row: aiosqlite.Row) -> Comment:
    return Comment(
        id=row["id"],
        task_id=row["task_id"],
        author_kind=row["author_kind"],
        author=row["author"],
        session_id=row["session_id"],
        body_md=row["body_md"],
        in_reply_to=row["in_reply_to"],
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
        delivered_to_session_id=row["delivered_to_session_id"],
        read_at=row["read_at"],
        stop_blocks=row["stop_blocks"],
        task_key=row["task_key"],
        delivered_to_name=row["delivered_to_name"],
        step_id=row["step_id"],
        attachment_id=row["attachment_id"],
    )


async def insert_comment(
    conn: aiosqlite.Connection,
    *,
    task_id: int,
    author_kind: str,
    author: str,
    session_id: int | None,
    body_md: str,
    in_reply_to: int | None,
    now: str,
    step_id: int | None = None,
    attachment_id: int | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO comment (task_id, author_kind, author, session_id, body_md, in_reply_to,"
        " created_at, step_id, attachment_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            author_kind,
            author,
            session_id,
            body_md,
            in_reply_to,
            now,
            step_id,
            attachment_id,
        ),
    )
    return int(cur.lastrowid or 0)


async def get_comment(conn: aiosqlite.Connection, comment_id: int) -> Comment | None:
    row = await _one(conn, _COMMENT_SELECT + " WHERE c.id = ?", (comment_id,))
    return _comment(row) if row else None


async def list_comments(
    conn: aiosqlite.Connection, task_id: int, *, unread_only: bool = False, limit: int = 200
) -> list[Comment]:
    """The task's comments, oldest first (the newest `limit` of them)."""
    unread = " AND c.read_at IS NULL" if unread_only else ""
    rows = await _all(
        conn,
        f"SELECT * FROM ({_COMMENT_SELECT} WHERE c.task_id = ?{unread}"
        " ORDER BY c.id DESC LIMIT ?) ORDER BY id",
        (task_id, limit),
    )
    return [_comment(r) for r in rows]


async def undelivered_owner_comments(conn: aiosqlite.Connection, session_id: int) -> list[Comment]:
    """Unread owner comments not delivered to this session: never delivered, or delivered to a
    session bound before it (a rebind hands them to the new one)."""
    rows = await _all(
        conn,
        _COMMENT_SELECT
        + _BOUND_OWNER
        + " AND (c.delivered_to_session_id IS NULL OR c.delivered_to_session_id != ?)"
        " ORDER BY c.id",
        (session_id, session_id),
    )
    return [_comment(r) for r in rows]


async def delivered_unread_comments(conn: aiosqlite.Connection, session_id: int) -> list[Comment]:
    """Owner comments delivered to this session that it has not acknowledged yet."""
    rows = await _all(
        conn,
        _COMMENT_SELECT + _BOUND_OWNER + " AND c.delivered_to_session_id = ? ORDER BY c.id",
        (session_id, session_id),
    )
    return [_comment(r) for r in rows]


async def unread_owner_count(conn: aiosqlite.Connection, session_id: int) -> int:
    row = await _one(conn, "SELECT COUNT(*) FROM comment c" + _BOUND_OWNER, (session_id,))
    return int(row[0])


async def mark_comments_delivered(
    conn: aiosqlite.Connection, comment_ids: list[int], session_id: int, now: str
) -> None:
    marks = ", ".join("?" for _ in comment_ids)
    await conn.execute(
        f"UPDATE comment SET delivered_at = ?, delivered_to_session_id = ?, stop_blocks = 0"
        f" WHERE id IN ({marks})",
        (now, session_id, *comment_ids),
    )


async def count_stop_blocks(conn: aiosqlite.Connection, comment_ids: list[int]) -> None:
    marks = ", ".join("?" for _ in comment_ids)
    await conn.execute(
        f"UPDATE comment SET stop_blocks = stop_blocks + 1 WHERE id IN ({marks})",
        tuple(comment_ids),
    )


async def mark_comments_read(conn: aiosqlite.Connection, comment_ids: list[int], now: str) -> None:
    marks = ", ".join("?" for _ in comment_ids)
    await conn.execute(f"UPDATE comment SET read_at = ? WHERE id IN ({marks})", (now, *comment_ids))


async def unread_agent_replies(conn: aiosqlite.Connection, limit: int = 200) -> list[Comment]:
    """Agent replies the owner has not marked read, oldest first."""
    rows = await _all(
        conn,
        _COMMENT_SELECT + " WHERE c.author_kind = 'agent' AND c.read_at IS NULL"
        " ORDER BY c.id LIMIT ?",
        (limit,),
    )
    return [_comment(r) for r in rows]


# --- review ----------------------------------------------------------------


def _step(row: aiosqlite.Row) -> ReviewStep:
    return ReviewStep(
        id=row["id"],
        task_id=row["task_id"],
        position=row["position"],
        title=row["title"],
        details_md=row["details_md"],
        status=row["status"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def list_review_steps(conn: aiosqlite.Connection, task_id: int) -> list[ReviewStep]:
    rows = await _all(
        conn, "SELECT * FROM review_step WHERE task_id = ? ORDER BY position", (task_id,)
    )
    return [_step(r) for r in rows]


async def get_review_step(conn: aiosqlite.Connection, step_id: int) -> ReviewStep | None:
    row = await _one(conn, "SELECT * FROM review_step WHERE id = ?", (step_id,))
    return _step(row) if row else None


async def insert_review_step(
    conn: aiosqlite.Connection,
    task_id: int,
    position: int,
    title: str,
    details_md: str | None,
    created_by: str,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO review_step (task_id, position, title, details_md, created_by, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, position, title, details_md, created_by, now, now),
    )
    return int(cur.lastrowid or 0)


async def update_review_step(
    conn: aiosqlite.Connection, step_id: int, changes: dict[str, Any], now: str
) -> None:
    sets = [f"{k} = ?" for k in changes]
    await conn.execute(
        f"UPDATE review_step SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
        (*changes.values(), now, step_id),
    )


async def delete_review_step(conn: aiosqlite.Connection, step_id: int) -> None:
    await conn.execute("DELETE FROM review_step WHERE id = ?", (step_id,))


# --- plan ------------------------------------------------------------------


def _plan_step(row: aiosqlite.Row) -> PlanStep:
    return PlanStep(
        id=row["id"],
        task_id=row["task_id"],
        position=row["position"],
        title=row["title"],
        details_md=row["details_md"],
        status=row["status"],
        done_at=row["done_at"],
        link=row["link"],
        outcome=row["outcome"],
        conformance=row["conformance"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        added_after_acceptance_at=row["added_after_acceptance_at"],
        added_by=row["added_by"],
        added_reason=row["added_reason"],
    )


async def list_plan_steps(conn: aiosqlite.Connection, task_id: int) -> list[PlanStep]:
    rows = await _all(
        conn, "SELECT * FROM plan_step WHERE task_id = ? ORDER BY position", (task_id,)
    )
    return [_plan_step(r) for r in rows]


async def insert_plan_step(
    conn: aiosqlite.Connection,
    task_id: int,
    position: int,
    title: str,
    details_md: str | None,
    created_by: str,
    now: str,
    added_reason: str | None = None,
) -> None:
    """With `added_reason`, the step is marked as added after the plan's acceptance."""
    added_at = now if added_reason is not None else None
    added_by = created_by if added_reason is not None else None
    await conn.execute(
        "INSERT INTO plan_step (task_id, position, title, details_md, created_by, created_at,"
        " updated_at, added_after_acceptance_at, added_by, added_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            position,
            title,
            details_md,
            created_by,
            now,
            now,
            added_at,
            added_by,
            added_reason,
        ),
    )


async def update_plan_step(
    conn: aiosqlite.Connection, step_id: int, changes: dict[str, Any], now: str
) -> None:
    sets = [f"{k} = ?" for k in changes]
    await conn.execute(
        f"UPDATE plan_step SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
        (*changes.values(), now, step_id),
    )


async def delete_plan_step(conn: aiosqlite.Connection, step_id: int) -> None:
    await conn.execute("DELETE FROM plan_step WHERE id = ?", (step_id,))


async def report_of_task(conn: aiosqlite.Connection, report_id: int, task_id: int) -> bool:
    row = await _one(
        conn, "SELECT 1 FROM report WHERE id = ? AND task_id = ?", (report_id, task_id)
    )
    return row is not None


async def set_plan_status(
    conn: aiosqlite.Connection,
    task_id: int,
    status: str | None,
    accepted_at: str | None,
    accepted_by: str | None,
) -> None:
    """The plan's status lives on the task row; it is not a task field edit (no version bump)."""
    await conn.execute(
        "UPDATE task SET plan_status = ?, plan_accepted_at = ?, plan_accepted_by = ? WHERE id = ?",
        (status, accepted_at, accepted_by, task_id),
    )


async def insert_plan_revision(
    conn: aiosqlite.Connection,
    task_id: int,
    body: dict[str, Any],
    author: str,
    reason: str,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO plan_revision (task_id, revision, body, author, reason, created_at)"
        " VALUES (?, (SELECT COALESCE(MAX(revision), 0) + 1 FROM plan_revision"
        " WHERE task_id = ?), ?, ?, ?, ?)",
        (task_id, task_id, json.dumps(body, ensure_ascii=False), author, reason, now),
    )
    return int(cur.lastrowid or 0)


async def list_plan_revisions(
    conn: aiosqlite.Connection, task_id: int, limit: int = 100
) -> list[PlanRevision]:
    """Newest first."""
    rows = await _all(
        conn,
        "SELECT * FROM plan_revision WHERE task_id = ? ORDER BY revision DESC LIMIT ?",
        (task_id, limit),
    )
    return [
        PlanRevision(
            id=r["id"],
            task_id=r["task_id"],
            revision=r["revision"],
            body=json.loads(r["body"]),
            author=r["author"],
            reason=r["reason"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


# --- session todo (Claude Code's own list, from hooks) ---------------------


async def list_session_todo(
    conn: aiosqlite.Connection, session_id: int, cc_session_id: str | None
) -> list[TodoItem]:
    """The todo of the session's current Claude Code id; an older id's list is not shown."""
    if cc_session_id is None:
        return []
    rows = await _all(
        conn,
        "SELECT * FROM session_todo WHERE session_id = ? AND cc_session_id = ?"
        " ORDER BY position, item_id",
        (session_id, cc_session_id),
    )
    return [
        TodoItem(
            item_id=r["item_id"],
            subject=r["subject"],
            status=r["status"],
            active_form=r["active_form"],
            position=r["position"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def clear_session_todo(
    conn: aiosqlite.Connection, session_id: int, keep_cc: str | None = None
) -> None:
    """Remove the session's todo; with `keep_cc`, only the lists of other Claude Code ids."""
    if keep_cc is None:
        await conn.execute("DELETE FROM session_todo WHERE session_id = ?", (session_id,))
    else:
        await conn.execute(
            "DELETE FROM session_todo WHERE session_id = ? AND cc_session_id != ?",
            (session_id, keep_cc),
        )


async def upsert_session_todo(
    conn: aiosqlite.Connection, session_id: int, cc_session_id: str, item: TodoItem, now: str
) -> None:
    await conn.execute(
        "INSERT INTO session_todo (session_id, cc_session_id, item_id, subject, status,"
        " active_form, position, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (session_id, item_id) DO UPDATE SET cc_session_id = excluded.cc_session_id,"
        " subject = excluded.subject, status = excluded.status,"
        " active_form = excluded.active_form, position = excluded.position,"
        " updated_at = excluded.updated_at",
        (
            session_id,
            cc_session_id,
            item.item_id,
            item.subject,
            item.status,
            item.active_form,
            item.position,
            now,
        ),
    )


async def move_session_todo(
    conn: aiosqlite.Connection, from_session: int, to_session: int, cc_session_id: str
) -> None:
    """Hand the todo of a Claude Code id to the session it is now linked to."""
    await conn.execute("DELETE FROM session_todo WHERE session_id = ?", (to_session,))
    await conn.execute(
        "UPDATE session_todo SET session_id = ? WHERE session_id = ? AND cc_session_id = ?",
        (to_session, from_session, cc_session_id),
    )


async def delete_session_todo_item(
    conn: aiosqlite.Connection, session_id: int, item_id: str
) -> None:
    await conn.execute(
        "DELETE FROM session_todo WHERE session_id = ? AND item_id = ?", (session_id, item_id)
    )


_ATTACHMENT_SELECT = """
SELECT a.*, t.key AS task_key, p.slug AS project_slug, r.position AS step_position
FROM attachment a
JOIN task t ON t.id = a.task_id
JOIN project p ON p.id = t.project_id
LEFT JOIN review_step r ON r.id = a.step_id
"""


def _attachment(row: aiosqlite.Row) -> Attachment:
    return Attachment(
        id=row["id"],
        task_id=row["task_id"],
        step_id=row["step_id"],
        kind=row["kind"],
        stored_name=row["stored_name"],
        original_name=row["original_name"],
        mime=row["mime"],
        size=row["size"],
        sha256=row["sha256"],
        caption_md=row["caption_md"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        task_key=row["task_key"],
        project_slug=row["project_slug"],
        step_position=row["step_position"],
        annotated_name=row["annotated_name"],
        annotated_size=row["annotated_size"],
        annotated_sha256=row["annotated_sha256"],
        annotated_sent_sha256=row["annotated_sent_sha256"],
    )


async def insert_attachment(
    conn: aiosqlite.Connection,
    *,
    task_id: int,
    step_id: int | None,
    stored_name: str,
    original_name: str | None,
    mime: str,
    size: int,
    sha256: str,
    caption_md: str | None,
    created_by: str,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO attachment (task_id, step_id, stored_name, original_name, mime, size,"
        " sha256, caption_md, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            step_id,
            stored_name,
            original_name,
            mime,
            size,
            sha256,
            caption_md,
            created_by,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


async def get_attachment(conn: aiosqlite.Connection, attachment_id: int) -> Attachment | None:
    row = await _one(conn, _ATTACHMENT_SELECT + " WHERE a.id = ?", (attachment_id,))
    return _attachment(row) if row else None


async def list_attachments(conn: aiosqlite.Connection, task_id: int) -> list[Attachment]:
    rows = await _all(conn, _ATTACHMENT_SELECT + " WHERE a.task_id = ? ORDER BY a.id", (task_id,))
    return [_attachment(r) for r in rows]


async def find_attachment(
    conn: aiosqlite.Connection, task_id: int, sha256: str
) -> Attachment | None:
    row = await _one(
        conn, _ATTACHMENT_SELECT + " WHERE a.task_id = ? AND a.sha256 = ?", (task_id, sha256)
    )
    return _attachment(row) if row else None


async def task_attachment_bytes(conn: aiosqlite.Connection, task_id: int) -> int:
    row = await _one(
        conn,
        "SELECT COALESCE(SUM(size + COALESCE(annotated_size, 0)), 0) FROM attachment"
        " WHERE task_id = ?",
        (task_id,),
    )
    return int(row[0])


async def update_attachment(
    conn: aiosqlite.Connection, attachment_id: int, changes: dict[str, Any]
) -> None:
    sets = [f"{k} = ?" for k in changes]
    await conn.execute(
        f"UPDATE attachment SET {', '.join(sets)} WHERE id = ?",
        (*changes.values(), attachment_id),
    )


async def delete_attachment(conn: aiosqlite.Connection, attachment_id: int) -> None:
    await conn.execute("DELETE FROM attachment WHERE id = ?", (attachment_id,))


async def stored_files(conn: aiosqlite.Connection) -> set[tuple[str, str, str]]:
    """(project slug, task key, stored name) of every attachment and flattened image: the
    files that must exist."""
    rows = await _all(
        conn,
        "SELECT p.slug, t.key, a.stored_name, a.annotated_name FROM attachment a"
        " JOIN task t ON t.id = a.task_id JOIN project p ON p.id = t.project_id",
    )
    return {(r[0], r[1], name) for r in rows for name in (r[2], r[3]) if name}


def _annotation(row: aiosqlite.Row) -> Annotation:
    return Annotation(
        id=row["id"],
        attachment_id=row["attachment_id"],
        kind=row["kind"],
        colour=row["colour"],
        stroke_width=row["stroke_width"],
        geometry=json.loads(row["geometry"]),
        pin_number=row["pin_number"],
        body_md=row["body_md"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


async def _with_replies(
    conn: aiosqlite.Connection, annotations: list[Annotation]
) -> list[Annotation]:
    by_id = {a.id: a for a in annotations}
    if not by_id:
        return annotations
    marks = ", ".join("?" for _ in by_id)
    rows = await _all(
        conn,
        f"SELECT * FROM annotation_reply WHERE annotation_id IN ({marks}) ORDER BY id",
        tuple(by_id),
    )
    for r in rows:
        by_id[r["annotation_id"]].replies.append(
            AnnotationReply(
                id=r["id"],
                annotation_id=r["annotation_id"],
                author_kind=r["author_kind"],
                author=r["author"],
                body_md=r["body_md"],
                created_at=r["created_at"],
            )
        )
    return annotations


async def get_annotation(conn: aiosqlite.Connection, annotation_id: int) -> Annotation | None:
    row = await _one(conn, "SELECT * FROM annotation WHERE id = ?", (annotation_id,))
    return (await _with_replies(conn, [_annotation(row)]))[0] if row else None


async def list_annotations(conn: aiosqlite.Connection, attachment_id: int) -> list[Annotation]:
    """The live annotations of a screenshot, oldest first."""
    rows = await _all(
        conn,
        "SELECT * FROM annotation WHERE attachment_id = ? AND deleted_at IS NULL ORDER BY id",
        (attachment_id,),
    )
    return await _with_replies(conn, [_annotation(r) for r in rows])


async def list_task_annotations(conn: aiosqlite.Connection, task_id: int) -> list[Annotation]:
    rows = await _all(
        conn,
        "SELECT n.* FROM annotation n JOIN attachment a ON a.id = n.attachment_id"
        " WHERE a.task_id = ? AND n.deleted_at IS NULL ORDER BY n.id",
        (task_id,),
    )
    return await _with_replies(conn, [_annotation(r) for r in rows])


async def next_pin_number(conn: aiosqlite.Connection, attachment_id: int) -> int:
    """Counts removed pins too: their numbers are never given out again."""
    row = await _one(
        conn,
        "SELECT COALESCE(MAX(pin_number), 0) + 1 FROM annotation WHERE attachment_id = ?",
        (attachment_id,),
    )
    return int(row[0])


async def insert_annotation(
    conn: aiosqlite.Connection,
    *,
    attachment_id: int,
    kind: str,
    colour: str,
    stroke_width: float,
    geometry: dict[str, Any],
    pin_number: int | None,
    body_md: str | None,
    created_by: str,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO annotation (attachment_id, kind, colour, stroke_width, geometry, pin_number,"
        " body_md, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attachment_id,
            kind,
            colour,
            stroke_width,
            geometry_json(geometry),
            pin_number,
            body_md,
            created_by,
            now,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


async def update_annotation(
    conn: aiosqlite.Connection, annotation_id: int, changes: dict[str, Any], now: str
) -> None:
    values = {**changes, "updated_at": now}
    sets = [f"{k} = ?" for k in values]
    await conn.execute(
        f"UPDATE annotation SET {', '.join(sets)} WHERE id = ?",
        (*values.values(), annotation_id),
    )


async def insert_annotation_reply(
    conn: aiosqlite.Connection,
    *,
    annotation_id: int,
    author_kind: str,
    author: str,
    session_id: int | None,
    body_md: str,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO annotation_reply (annotation_id, author_kind, author, session_id, body_md,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (annotation_id, author_kind, author, session_id, body_md, now),
    )
    return int(cur.lastrowid or 0)


async def review_queue(conn: aiosqlite.Connection) -> list[Task]:
    """Tasks in review or test with a checklist, across projects, the longest waiting first."""
    marks = ", ".join("?" for _ in ACCEPTANCE_STAGES)
    rows = await _all(
        conn,
        f"SELECT * FROM ({_TASK_SELECT} WHERE t.stage IN ({marks}))"
        " WHERE review_total > 0 ORDER BY COALESCE(stage_changed_at, updated_at), id",
        tuple(s.value for s in ACCEPTANCE_STAGES),
    )
    return [_task(r) for r in rows]


# --- event -----------------------------------------------------------------


async def insert_event(
    conn: aiosqlite.Connection,
    *,
    ts: str,
    kind: str,
    actor: str,
    project_id: int | None,
    task_id: int | None,
    session_id: int | None,
    payload: dict[str, Any],
) -> Event:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    cur = await conn.execute(
        "INSERT INTO event (ts, kind, project_id, task_id, session_id, actor, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, kind, project_id, task_id, session_id, actor, data),
    )
    return Event(
        id=int(cur.lastrowid or 0),
        ts=ts,
        kind=kind,
        project_id=project_id,
        task_id=task_id,
        session_id=session_id,
        actor=actor,
        payload=payload,
    )


async def events_after(
    conn: aiosqlite.Connection, after_id: int, project_id: int | None, limit: int
) -> list[Event]:
    if project_id is None:
        rows = await _all(
            conn, "SELECT * FROM event WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        )
    else:
        rows = await _all(
            conn,
            "SELECT * FROM event WHERE id > ? AND project_id = ? ORDER BY id LIMIT ?",
            (after_id, project_id, limit),
        )
    return [_event(r) for r in rows]


async def events_of_kinds_after(
    conn: aiosqlite.Connection, after_id: int, kinds: tuple[str, ...], limit: int
) -> list[Event]:
    marks = ", ".join("?" for _ in kinds)
    rows = await _all(
        conn,
        f"SELECT * FROM event WHERE id > ? AND kind IN ({marks}) ORDER BY id LIMIT ?",
        (after_id, *kinds, limit),
    )
    return [_event(r) for r in rows]


async def task_titles(conn: aiosqlite.Connection, task_ids: set[int]) -> dict[int, str]:
    marks = ", ".join("?" for _ in task_ids)
    rows = await _all(conn, f"SELECT id, title FROM task WHERE id IN ({marks})", tuple(task_ids))
    return {r["id"]: r["title"] for r in rows}


async def events_page(
    conn: aiosqlite.Connection,
    *,
    before_id: int | None,
    after_id: int | None,
    project_id: int | None,
    kind: str | None,
    exclude_kinds: tuple[str, ...],
    limit: int,
) -> list[Event]:
    """Events newest first, below `before_id` and above `after_id`; `kind` selects one kind,
    otherwise `exclude_kinds` are left out."""
    where, params = [], []
    if before_id is not None:
        where.append("id < ?")
        params.append(before_id)
    if after_id is not None:
        where.append("id > ?")
        params.append(after_id)
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    elif exclude_kinds:
        where.append(f"kind NOT IN ({', '.join('?' for _ in exclude_kinds)})")
        params.extend(exclude_kinds)
    sql = "SELECT * FROM event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    return [_event(r) for r in await _all(conn, sql, (*params, limit))]


async def audit_page(
    conn: aiosqlite.Connection,
    *,
    before_id: int | None,
    after_id: int | None,
    project_id: int | None,
    environment: str | None,
    session_ids: list[int] | None,
    decision: str | None,
    since: str | None,
    kinds: tuple[str, ...],
    limit: int,
) -> list[Event]:
    """Events of `kinds` newest first; `environment` and `decision` look into the payload."""
    where = [f"kind IN ({', '.join('?' for _ in kinds)})"]
    params: list[Any] = list(kinds)
    if before_id is not None:
        where.append("id < ?")
        params.append(before_id)
    if after_id is not None:
        where.append("id > ?")
        params.append(after_id)
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    if environment is not None:
        where.append("json_extract(payload, '$.environment') = ?")
        params.append(environment)
    if session_ids is not None:
        where.append(f"session_id IN ({', '.join('?' for _ in session_ids)})")
        params.extend(session_ids)
    if decision is not None:
        where.append("json_extract(payload, '$.decision') = ?")
        params.append(decision)
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    sql = f"SELECT * FROM event WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
    return [_event(row) for row in await _all(conn, sql, (*params, limit))]


async def latest_stage_changes(conn: aiosqlite.Connection) -> dict[int, Event]:
    """The newest `task.stage_changed` event of every task, by task id."""
    rows = await _all(
        conn,
        "SELECT * FROM event WHERE id IN (SELECT MAX(id) FROM event"
        " WHERE kind = 'task.stage_changed' AND task_id IS NOT NULL GROUP BY task_id)",
    )
    return {r["task_id"]: _event(r) for r in rows}


async def last_event_id(conn: aiosqlite.Connection) -> int:
    row = await _one(conn, "SELECT COALESCE(MAX(id), 0) FROM event", ())
    return int(row[0])


async def last_event_before(conn: aiosqlite.Connection, ts: str) -> int:
    """The newest event older than `ts`, or 0. Walks back from the newest event, so it reads only
    the events after `ts`; `ts` has no index."""
    row = await _one(conn, "SELECT id FROM event WHERE ts < ? ORDER BY id DESC LIMIT 1", (ts,))
    return int(row[0]) if row else 0


async def event_counts(
    conn: aiosqlite.Connection,
    after_id: int,
    upto_id: int,
    *,
    exclude_kinds: tuple[str, ...],
    project_id: int | None = None,
    task_id: int | None = None,
    own_session_id: int | None = None,
    own_kinds: tuple[str, ...] = (),
) -> list[tuple[int | None, str | None, str | None, str, int, int]]:
    """Events in (after_id, upto_id] counted per task or project and kind: (task id, task key or
    project slug, task title, kind, count, last id). `own_kinds` are left out for one session."""
    where = [
        "e.id > ?",
        "e.id <= ?",
        f"e.kind NOT IN ({', '.join('?' for _ in exclude_kinds)})",
    ]
    params: list[Any] = [after_id, upto_id, *exclude_kinds]
    if project_id is not None:
        where.append("e.project_id = ?")
        params.append(project_id)
    if task_id is not None:
        where.append("e.task_id = ?")
        params.append(task_id)
    if own_session_id is not None and own_kinds:
        where.append(f"NOT (e.session_id IS ? AND e.kind IN ({', '.join('?' for _ in own_kinds)}))")
        params.extend([own_session_id, *own_kinds])
    rows = await _all(
        conn,
        "SELECT e.task_id, COALESCE(t.key, p.slug) AS subject, t.title, e.kind,"
        " COUNT(*) AS n, MAX(e.id) AS last_id FROM event e"
        " LEFT JOIN task t ON t.id = e.task_id LEFT JOIN project p ON p.id = e.project_id"
        f" WHERE {' AND '.join(where)} GROUP BY e.task_id, e.project_id, e.kind",
        tuple(params),
    )
    return [(r["task_id"], r["subject"], r["title"], r["kind"], r["n"], r["last_id"]) for r in rows]


# --- reader cursors --------------------------------------------------------


async def reader_cursor(
    conn: aiosqlite.Connection, reader: str, level: str, ref_id: int
) -> int | None:
    row = await _one(
        conn,
        "SELECT last_event_id FROM reader_cursor WHERE reader = ? AND level = ? AND ref_id = ?",
        (reader, level, ref_id),
    )
    return int(row[0]) if row else None


async def set_reader_cursor(
    conn: aiosqlite.Connection, reader: str, level: str, ref_id: int, event_id: int, now: str
) -> None:
    """Never moves back: two reads at once leave the newer position."""
    await conn.execute(
        "INSERT INTO reader_cursor (reader, level, ref_id, last_event_id, read_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (reader, level, ref_id) DO UPDATE SET"
        " last_event_id = MAX(last_event_id, excluded.last_event_id), read_at = excluded.read_at",
        (reader, level, ref_id, event_id, now),
    )


# --- project rules ---------------------------------------------------------


async def get_project_rules(conn: aiosqlite.Connection, project_id: int) -> str | None:
    row = await _one(conn, "SELECT body FROM project_rules WHERE project_id = ?", (project_id,))
    return row["body"] if row else None


async def list_project_rules(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """(project slug, rules body JSON) for every project with its own rules."""
    rows = await _all(
        conn,
        "SELECT p.slug, r.body FROM project_rules r JOIN project p ON p.id = r.project_id"
        " ORDER BY p.slug",
    )
    return [(r["slug"], r["body"]) for r in rows]


async def set_project_rules(
    conn: aiosqlite.Connection, project_id: int, body: str, now: str
) -> None:
    await conn.execute(
        "INSERT INTO project_rules (project_id, body, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT (project_id) DO UPDATE SET body = excluded.body,"
        " updated_at = excluded.updated_at",
        (project_id, body, now),
    )


# --- rule versions ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleSetRow:
    id: int
    scope: str
    project_id: int | None
    current_version: int
    config_hash: str | None


def _rule_set(row: aiosqlite.Row) -> RuleSetRow:
    return RuleSetRow(
        row["id"], row["scope"], row["project_id"], row["current_version"], row["config_hash"]
    )


async def get_rule_set(conn: aiosqlite.Connection, project_id: int | None) -> RuleSetRow | None:
    """A project's set, or the global one with `project_id` None; never by name, a project may
    be called `global`."""
    where = "project_id IS NULL" if project_id is None else "project_id = ?"
    params = () if project_id is None else (project_id,)
    row = await _one(conn, f"SELECT * FROM rule_set WHERE {where}", params)
    return _rule_set(row) if row else None


async def list_project_rule_sets(conn: aiosqlite.Connection) -> list[RuleSetRow]:
    rows = await _all(conn, "SELECT * FROM rule_set WHERE project_id IS NOT NULL ORDER BY scope")
    return [_rule_set(r) for r in rows]


async def rule_set_version(conn: aiosqlite.Connection, project_id: int | None) -> int:
    found = await get_rule_set(conn, project_id)
    return found.current_version if found else 0


async def insert_rule_set(
    conn: aiosqlite.Connection, scope: str, project_id: int | None, now: str
) -> RuleSetRow:
    await conn.execute(
        "INSERT INTO rule_set (scope, project_id, created_at) VALUES (?, ?, ?)",
        (scope, project_id, now),
    )
    found = await get_rule_set(conn, project_id)
    assert found is not None
    return found


async def set_rules_config_hash(conn: aiosqlite.Connection, set_id: int, value: str) -> None:
    await conn.execute("UPDATE rule_set SET config_hash = ? WHERE id = ?", (value, set_id))


def _rule_version(row: aiosqlite.Row) -> RuleVersion:
    """A stored version; a body that is not JSON (edited by hand) is reported, never raised."""
    body, error = {}, None
    try:
        body = json.loads(row["body"])
    except ValueError as exc:
        error = f"not valid JSON ({exc})"
    return RuleVersion(
        id=row["id"],
        scope=row["scope"],
        number=row["number"],
        body=body,
        rendered=row["rendered"],
        author=row["author"],
        session_id=row["session_id"],
        reason=row["reason"],
        diff=row["diff"],
        created_at=row["created_at"],
        notice_open=bool(row["notice_open"]),
        body_error=error,
    )


_RULE_VERSION_SELECT = (
    "SELECT v.*, s.scope FROM rule_version v JOIN rule_set s ON s.id = v.rule_set_id"
)


async def insert_rule_version(
    conn: aiosqlite.Connection,
    rule_set: RuleSetRow,
    body: dict[str, Any],
    rendered: str,
    author: str,
    session_id: int | None,
    reason: str,
    diff: str,
    now: str,
    *,
    notice: bool = False,
) -> int:
    """The next version of the set, made current; returns its number. The number comes from the
    rows, so a `current_version` edited by hand cannot make two versions collide."""
    row = await _one(
        conn,
        "SELECT COALESCE(MAX(number), 0) AS top FROM rule_version WHERE rule_set_id = ?",
        (rule_set.id,),
    )
    number = row["top"] + 1
    await conn.execute(
        "INSERT INTO rule_version (rule_set_id, number, body, rendered, author, session_id,"
        " reason, diff, notice_open, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rule_set.id,
            number,
            json.dumps(body, ensure_ascii=False),
            rendered,
            author,
            session_id,
            reason,
            diff,
            int(notice),
            now,
        ),
    )
    await conn.execute(
        "UPDATE rule_set SET current_version = ? WHERE id = ?", (number, rule_set.id)
    )
    return number


async def get_rule_version(
    conn: aiosqlite.Connection, set_id: int, number: int
) -> RuleVersion | None:
    row = await _one(
        conn, f"{_RULE_VERSION_SELECT} WHERE v.rule_set_id = ? AND v.number = ?", (set_id, number)
    )
    return _rule_version(row) if row else None


async def get_rule_version_by_id(conn: aiosqlite.Connection, version_id: int) -> RuleVersion | None:
    row = await _one(conn, f"{_RULE_VERSION_SELECT} WHERE v.id = ?", (version_id,))
    return _rule_version(row) if row else None


async def list_rule_versions(
    conn: aiosqlite.Connection, set_id: int, limit: int = 100
) -> list[RuleVersion]:
    rows = await _all(
        conn,
        f"{_RULE_VERSION_SELECT} WHERE v.rule_set_id = ? ORDER BY v.number DESC LIMIT ?",
        (set_id, limit),
    )
    return [_rule_version(r) for r in rows]


async def list_rule_notices(conn: aiosqlite.Connection) -> list[RuleVersion]:
    rows = await _all(conn, f"{_RULE_VERSION_SELECT} WHERE v.notice_open = 1 ORDER BY v.id")
    return [_rule_version(r) for r in rows]


async def close_rule_notice(conn: aiosqlite.Connection, version_id: int) -> None:
    await conn.execute("UPDATE rule_version SET notice_open = 0 WHERE id = ?", (version_id,))


def _rule_proposal(row: aiosqlite.Row) -> RuleProposal:
    return RuleProposal(
        id=row["id"],
        scope=row["scope"],
        project_id=row["project_id"],
        patch=json.loads(row["patch"]),
        reason=row["reason"],
        proposed_by=row["proposed_by"],
        session_id=row["session_id"],
        status=row["status"],
        version=row["version"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
    )


async def insert_rule_proposal(
    conn: aiosqlite.Connection,
    scope: str,
    project_id: int | None,
    patch: dict[str, Any],
    reason: str,
    proposed_by: str,
    session_id: int | None,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO rule_proposal (scope, project_id, patch, reason, proposed_by, session_id,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            scope,
            project_id,
            json.dumps(patch, ensure_ascii=False),
            reason,
            proposed_by,
            session_id,
            now,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


async def get_rule_proposal(conn: aiosqlite.Connection, proposal_id: int) -> RuleProposal | None:
    row = await _one(conn, "SELECT * FROM rule_proposal WHERE id = ?", (proposal_id,))
    return _rule_proposal(row) if row else None


async def list_rule_proposals(conn: aiosqlite.Connection, limit: int = 200) -> list[RuleProposal]:
    rows = await _all(
        conn, "SELECT * FROM rule_proposal WHERE status = 'open' ORDER BY id LIMIT ?", (limit,)
    )
    return [_rule_proposal(r) for r in rows]


async def decided_rule_proposals(
    conn: aiosqlite.Connection, scope: str, limit: int = 100
) -> list[RuleProposal]:
    rows = await _all(
        conn,
        "SELECT * FROM rule_proposal WHERE scope = ? AND status <> 'open' ORDER BY id DESC LIMIT ?",
        (scope, limit),
    )
    return [_rule_proposal(r) for r in rows]


async def decide_rule_proposal(
    conn: aiosqlite.Connection,
    proposal_id: int,
    status: str,
    version: int | None,
    decided_by: str,
    now: str,
) -> None:
    await conn.execute(
        "UPDATE rule_proposal SET status = ?, version = ?, decided_at = ?, decided_by = ?"
        " WHERE id = ?",
        (status, version, now, decided_by, proposal_id),
    )


async def rule_seen(conn: aiosqlite.Connection, session_id: int, project_id: int) -> int | None:
    row = await _one(
        conn,
        "SELECT version FROM rule_seen WHERE session_id = ? AND project_id = ?",
        (session_id, project_id),
    )
    return row["version"] if row else None


async def set_rule_seen(
    conn: aiosqlite.Connection, session_id: int, project_id: int, version: int
) -> None:
    await conn.execute(
        "INSERT INTO rule_seen (session_id, project_id, version) VALUES (?, ?, ?)"
        " ON CONFLICT (session_id, project_id) DO UPDATE SET version = excluded.version",
        (session_id, project_id, version),
    )


# --- saved filters ---------------------------------------------------------


def _saved_filter(row: aiosqlite.Row) -> SavedFilter:
    return SavedFilter(
        id=row["id"],
        project_id=row["project_id"],
        project_slug=row["slug"],
        name=row["name"],
        stage=row["stage"],
        waiting_on=row["waiting_on"],
        session=row["session"],
        q=row["q"],
        created_at=row["created_at"],
    )


_SAVED_FILTER_SELECT = (
    "SELECT f.*, p.slug FROM saved_filter f JOIN project p ON p.id = f.project_id"
)


async def list_saved_filters(
    conn: aiosqlite.Connection, project_id: int | None = None
) -> list[SavedFilter]:
    where, params = ("", ()) if project_id is None else (" WHERE f.project_id = ?", (project_id,))
    rows = await _all(conn, f"{_SAVED_FILTER_SELECT}{where} ORDER BY f.name COLLATE NOCASE", params)
    return [_saved_filter(r) for r in rows]


async def get_saved_filter(conn: aiosqlite.Connection, filter_id: int) -> SavedFilter | None:
    row = await _one(conn, f"{_SAVED_FILTER_SELECT} WHERE f.id = ?", (filter_id,))
    return _saved_filter(row) if row else None


async def saved_filter_named(
    conn: aiosqlite.Connection, project_id: int, name: str
) -> SavedFilter | None:
    row = await _one(
        conn, f"{_SAVED_FILTER_SELECT} WHERE f.project_id = ? AND f.name = ?", (project_id, name)
    )
    return _saved_filter(row) if row else None


async def insert_saved_filter(
    conn: aiosqlite.Connection, project_id: int, name: str, query: dict[str, str], now: str
) -> int:
    cur = await conn.execute(
        "INSERT INTO saved_filter (project_id, name, stage, waiting_on, session, q, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            name,
            query.get("stage"),
            query.get("waiting_on"),
            query.get("session"),
            query.get("q"),
            now,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


async def delete_saved_filter(conn: aiosqlite.Connection, filter_id: int) -> None:
    await conn.execute("DELETE FROM saved_filter WHERE id = ?", (filter_id,))


# --- quick switcher --------------------------------------------------------


async def switcher_tasks(conn: aiosqlite.Connection, limit: int) -> list[tuple[str, str, str, str]]:
    """(key, title, stage, project slug) of the most recently changed tasks."""
    rows = await _all(
        conn,
        "SELECT t.key, t.title, t.stage, p.slug FROM task t JOIN project p ON p.id = t.project_id"
        " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?",
        (limit,),
    )
    return [(r["key"], r["title"], r["stage"], r["slug"]) for r in rows]


# --- environments ----------------------------------------------------------

_ENV_SELECT = (
    "SELECT e.*, p.slug AS project_slug FROM environment e JOIN project p ON p.id = e.project_id"
)


def _environment(row: aiosqlite.Row) -> Environment:
    return Environment(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        kind=row["kind"],
        base_url=row["base_url"],
        sign_in_allowed=bool(row["sign_in_allowed"]),
        login_state_path=row["login_state_path"],
        notes=row["notes"],
        level=row["level"],
        overrides=json.loads(row["overrides"] or "{}"),
        position=row["position"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
        project_slug=row["project_slug"],
    )


def _access(row: aiosqlite.Row) -> AccessEntry:
    fields, tests = split_test_secrets(json.loads(row["fields"] or "{}"))
    return AccessEntry(
        id=row["id"],
        environment_id=row["environment_id"],
        kind=row["kind"],
        fields=fields,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
        test_secrets=tests,
    )


def _access_json(fields: dict[str, str], test_secrets: dict[str, str] | None) -> str:
    stored: dict[str, Any] = dict(fields)
    stored.update({k: {"value": v, "test": True} for k, v in (test_secrets or {}).items()})
    return json.dumps(stored, ensure_ascii=False)


async def list_environments(
    conn: aiosqlite.Connection, project_ids: list[int] | None = None, *, or_live: bool = False
) -> list[Environment]:
    """Environments with their access entries, by project and position; `or_live`: the live ones
    of every other project too."""
    sql, params = _ENV_SELECT, ()
    if project_ids is not None:
        where = []
        if project_ids:
            where.append(f"e.project_id IN ({', '.join('?' for _ in project_ids)})")
            params = tuple(project_ids)
        if or_live:
            where.append("e.kind = 'live'")
        if not where:
            return []
        sql += " WHERE " + " OR ".join(where)
    sql += " ORDER BY p.slug, e.position, e.id"
    envs = [_environment(r) for r in await _all(conn, sql, params)]
    if not envs:
        return envs
    by_id = {e.id: e for e in envs}
    marks = ", ".join("?" for _ in by_id)
    rows = await _all(
        conn,
        f"SELECT * FROM environment_access WHERE environment_id IN ({marks}) ORDER BY id",
        tuple(by_id),
    )
    for row in rows:
        by_id[row["environment_id"]].access.append(_access(row))
    return envs


async def has_environments(conn: aiosqlite.Connection) -> bool:
    return await _one(conn, "SELECT 1 FROM environment LIMIT 1", ()) is not None


async def get_environment(conn: aiosqlite.Connection, env_id: int) -> Environment | None:
    row = await _one(conn, _ENV_SELECT + " WHERE e.id = ?", (env_id,))
    if row is None:
        return None
    env = _environment(row)
    rows = await _all(
        conn, "SELECT * FROM environment_access WHERE environment_id = ? ORDER BY id", (env_id,)
    )
    env.access = [_access(r) for r in rows]
    return env


async def find_environment(
    conn: aiosqlite.Connection, project_id: int, name: str
) -> Environment | None:
    row = await _one(
        conn,
        "SELECT id FROM environment WHERE project_id = ? AND name = ? COLLATE NOCASE",
        (project_id, name),
    )
    return await get_environment(conn, row["id"]) if row else None


async def insert_environment(
    conn: aiosqlite.Connection, project_id: int, values: dict[str, Any], actor: str, now: str
) -> int:
    row = await _one(
        conn,
        "SELECT COALESCE(MAX(position), 0) + 1 FROM environment WHERE project_id = ?",
        (project_id,),
    )
    cur = await conn.execute(
        "INSERT INTO environment (project_id, name, kind, base_url, sign_in_allowed,"
        " login_state_path, notes, level, overrides, position, created_at, updated_at,"
        " updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            values["name"],
            values["kind"],
            values.get("base_url"),
            int(values.get("sign_in_allowed", False)),
            values.get("login_state_path"),
            values.get("notes"),
            values.get("level"),
            json.dumps(values.get("overrides", {})),
            int(row[0]),
            now,
            now,
            actor,
        ),
    )
    return int(cur.lastrowid or 0)


async def update_environment(
    conn: aiosqlite.Connection, env_id: int, changes: dict[str, Any], actor: str, now: str
) -> None:
    values = {
        k: (json.dumps(v) if k == "overrides" else int(v) if k == "sign_in_allowed" else v)
        for k, v in changes.items()
    }
    sets = "".join(f"{k} = ?, " for k in values)
    await conn.execute(
        f"UPDATE environment SET {sets}updated_at = ?, updated_by = ? WHERE id = ?",
        (*values.values(), now, actor, env_id),
    )


async def delete_environment(conn: aiosqlite.Connection, env_id: int) -> None:
    await conn.execute("DELETE FROM environment WHERE id = ?", (env_id,))


async def insert_access(
    conn: aiosqlite.Connection,
    env_id: int,
    kind: str,
    fields: dict[str, str],
    actor: str,
    now: str,
    test_secrets: dict[str, str] | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO environment_access (environment_id, kind, fields, created_at, updated_at,"
        " updated_by) VALUES (?, ?, ?, ?, ?, ?)",
        (env_id, kind, _access_json(fields, test_secrets), now, now, actor),
    )
    return int(cur.lastrowid or 0)


async def update_access(
    conn: aiosqlite.Connection,
    access_id: int,
    kind: str,
    fields: dict[str, str],
    actor: str,
    now: str,
    test_secrets: dict[str, str] | None = None,
) -> None:
    await conn.execute(
        "UPDATE environment_access SET kind = ?, fields = ?, updated_at = ?, updated_by = ?"
        " WHERE id = ?",
        (kind, _access_json(fields, test_secrets), now, actor, access_id),
    )


async def get_access(conn: aiosqlite.Connection, access_id: int) -> AccessEntry | None:
    row = await _one(conn, "SELECT * FROM environment_access WHERE id = ?", (access_id,))
    return _access(row) if row else None


async def delete_access(conn: aiosqlite.Connection, access_id: int) -> None:
    await conn.execute("DELETE FROM environment_access WHERE id = ?", (access_id,))


# --- environment proposals -------------------------------------------------

_PROPOSAL_SELECT = (
    "SELECT x.*, p.slug AS project_slug FROM environment_proposal x"
    " JOIN project p ON p.id = x.project_id"
)


def _proposal(row: aiosqlite.Row) -> Proposal:
    return Proposal(
        id=row["id"],
        project_id=row["project_id"],
        project_slug=row["project_slug"],
        change=json.loads(row["change"]),
        reason=row["reason"],
        proposed_by=row["proposed_by"],
        session_id=row["session_id"],
        status=row["status"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
    )


async def insert_proposal(
    conn: aiosqlite.Connection,
    project_id: int,
    change: dict[str, Any],
    reason: str,
    proposed_by: str,
    session_id: int | None,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO environment_proposal (project_id, change, reason, proposed_by, session_id,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, json.dumps(change, ensure_ascii=False), reason, proposed_by, session_id, now),
    )
    return int(cur.lastrowid or 0)


async def get_proposal(conn: aiosqlite.Connection, proposal_id: int) -> Proposal | None:
    row = await _one(conn, _PROPOSAL_SELECT + " WHERE x.id = ?", (proposal_id,))
    return _proposal(row) if row else None


async def list_proposals(
    conn: aiosqlite.Connection, project_id: int | None = None, limit: int = 100
) -> list[Proposal]:
    """Pending proposals, oldest first."""
    sql, params = _PROPOSAL_SELECT + " WHERE x.status = 'pending'", ()
    if project_id is not None:
        sql += " AND x.project_id = ?"
        params = (project_id,)
    rows = await _all(conn, sql + " ORDER BY x.id LIMIT ?", (*params, limit))
    return [_proposal(r) for r in rows]


async def decide_proposal(
    conn: aiosqlite.Connection, proposal_id: int, status: str, actor: str, now: str
) -> None:
    await conn.execute(
        "UPDATE environment_proposal SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
        (status, now, actor, proposal_id),
    )


# --- policy layers and proposals --------------------------------------------


@dataclass(slots=True)
class PolicyRow:
    project_id: int
    project_slug: str
    environment_id: int | None
    environment_name: str | None
    body: str
    updated_at: str
    updated_by: str


async def list_policy_layers(
    conn: aiosqlite.Connection, project_ids: list[int] | None = None
) -> list[PolicyRow]:
    """Project and environment policy layers (project layers first)."""
    sql = (
        "SELECT l.*, p.slug, e.name AS env_name FROM policy_layer l"
        " JOIN project p ON p.id = l.project_id"
        " LEFT JOIN environment e ON e.id = l.environment_id"
    )
    params: tuple[Any, ...] = ()
    if project_ids is not None:
        if not project_ids:
            return []
        sql += f" WHERE l.project_id IN ({', '.join('?' for _ in project_ids)})"
        params = tuple(project_ids)
    rows = await _all(conn, sql + " ORDER BY p.slug, l.environment_id IS NOT NULL, e.name", params)
    return [
        PolicyRow(
            r["project_id"],
            r["slug"],
            r["environment_id"],
            r["env_name"],
            r["body"],
            r["updated_at"],
            r["updated_by"],
        )
        for r in rows
    ]


async def set_policy_layer(
    conn: aiosqlite.Connection,
    project_id: int,
    environment_id: int | None,
    body: str | None,
    actor: str,
    now: str,
) -> None:
    """Store a layer, or remove it (`body` None)."""
    await conn.execute(
        "DELETE FROM policy_layer WHERE project_id = ? AND COALESCE(environment_id, 0) = ?",
        (project_id, environment_id or 0),
    )
    if body is not None:
        await conn.execute(
            "INSERT INTO policy_layer (project_id, environment_id, body, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (project_id, environment_id, body, now, actor),
        )


def _policy_proposal(row: aiosqlite.Row) -> PolicyProposal:
    return PolicyProposal(
        id=row["id"],
        scope=row["scope"],
        project_id=row["project_id"],
        patch=json.loads(row["patch"]),
        reason=row["reason"],
        proposed_by=row["proposed_by"],
        session_id=row["session_id"],
        status=row["status"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
    )


async def insert_policy_proposal(
    conn: aiosqlite.Connection,
    scope: str,
    project_id: int | None,
    patch: dict[str, Any],
    reason: str,
    proposed_by: str,
    session_id: int | None,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO policy_proposal (scope, project_id, patch, reason, proposed_by, session_id,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            scope,
            project_id,
            json.dumps(patch, ensure_ascii=False),
            reason,
            proposed_by,
            session_id,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


async def get_policy_proposal(
    conn: aiosqlite.Connection, proposal_id: int
) -> PolicyProposal | None:
    row = await _one(conn, "SELECT * FROM policy_proposal WHERE id = ?", (proposal_id,))
    return _policy_proposal(row) if row else None


async def list_policy_proposals(
    conn: aiosqlite.Connection, limit: int = 100
) -> list[PolicyProposal]:
    """Pending policy proposals, oldest first."""
    rows = await _all(
        conn, "SELECT * FROM policy_proposal WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
    )
    return [_policy_proposal(r) for r in rows]


async def decide_policy_proposal(
    conn: aiosqlite.Connection, proposal_id: int, status: str, actor: str, now: str
) -> None:
    await conn.execute(
        "UPDATE policy_proposal SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
        (status, now, actor, proposal_id),
    )


async def reopen_policy_proposal(conn: aiosqlite.Connection, proposal_id: int) -> None:
    await conn.execute(
        "UPDATE policy_proposal SET status = 'pending', decided_at = NULL, decided_by = NULL"
        " WHERE id = ?",
        (proposal_id,),
    )


async def task_events(
    conn: aiosqlite.Connection, task_id: int, kind: str, limit: int = 50
) -> list[Event]:
    """A task's events of one kind, newest first."""
    rows = await _all(
        conn,
        "SELECT * FROM event WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT ?",
        (task_id, kind, limit),
    )
    return [_event(r) for r in rows]


# --- documents -------------------------------------------------------------

_DOCUMENT_SELECT = """
SELECT d.*, p.slug AS project_slug FROM document d JOIN project p ON p.id = d.project_id
"""


def _document(row: aiosqlite.Row) -> Document:
    return Document(
        id=row["id"],
        project_id=row["project_id"],
        project_slug=row["project_slug"],
        slug=row["slug"],
        title=row["title"],
        kind=row["kind"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_url=row["source_url"],
    )


def _document_version(row: aiosqlite.Row, with_body: bool) -> DocumentVersion:
    return DocumentVersion(
        version=row["version"],
        author=row["author"],
        reason=row["reason"],
        base_version=row["base_version"],
        section=row["section"],
        created_at=row["created_at"],
        size=row["size"],
        body=row["body"] if with_body else "",
    )


async def get_document(conn: aiosqlite.Connection, project_id: int, slug: str) -> Document | None:
    row = await _one(
        conn, _DOCUMENT_SELECT + " WHERE d.project_id = ? AND d.slug = ?", (project_id, slug)
    )
    return _document(row) if row else None


async def list_documents(conn: aiosqlite.Connection, project_id: int) -> list[Document]:
    rows = await _all(
        conn, _DOCUMENT_SELECT + " WHERE d.project_id = ? ORDER BY d.slug", (project_id,)
    )
    return [_document(r) for r in rows]


async def get_document_version(
    conn: aiosqlite.Connection, document_id: int, version: int
) -> DocumentVersion | None:
    row = await _one(
        conn,
        "SELECT *, length(body) AS size FROM document_version"
        " WHERE document_id = ? AND version = ?",
        (document_id, version),
    )
    return _document_version(row, True) if row else None


async def list_document_versions(
    conn: aiosqlite.Connection, document_id: int
) -> list[DocumentVersion]:
    """Newest first, without the texts."""
    rows = await _all(
        conn,
        "SELECT version, author, reason, base_version, section, created_at, length(body) AS size"
        " FROM document_version WHERE document_id = ? ORDER BY version DESC",
        (document_id,),
    )
    return [_document_version(r, False) for r in rows]


async def insert_document(
    conn: aiosqlite.Connection,
    project_id: int,
    slug: str,
    title: str,
    kind: str,
    now: str,
    source_url: str | None = None,
) -> int:
    """A document without versions yet (version 0); `add_document_version` gives it text."""
    cur = await conn.execute(
        "INSERT INTO document (project_id, slug, title, kind, version, created_at, updated_at,"
        " source_url) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
        (project_id, slug, title, kind, now, now, source_url),
    )
    return int(cur.lastrowid or 0)


async def add_document_version(
    conn: aiosqlite.Connection,
    document_id: int,
    *,
    version: int,
    body: str,
    author: str,
    reason: str,
    base_version: int,
    section: str | None,
    title: str,
    kind: str,
    source_url: str | None,
    now: str,
) -> None:
    await conn.execute(
        "INSERT INTO document_version (document_id, version, body, author, reason, base_version,"
        " section, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (document_id, version, body, author, reason, base_version, section, now),
    )
    await conn.execute(
        "UPDATE document SET version = ?, title = ?, kind = ?, source_url = ?, updated_at = ?"
        " WHERE id = ?",
        (version, title, kind, source_url, now, document_id),
    )


# --- memory notes and blocks -----------------------------------------------

_NOTE_SELECT = """
SELECT m.*, p.slug AS project_slug FROM memory m LEFT JOIN project p ON p.id = m.project_id
"""
NOTE_FIELDS = ("title", "body", "tags", "links", "review_at")


def _note(row: aiosqlite.Row) -> MemoryNote:
    return MemoryNote(
        id=row["id"],
        scope=row["scope"],
        project_id=row["project_id"],
        project_slug=row["project_slug"],
        role=row["role"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        tags=row["tags"],
        source_kind=row["source_kind"],
        source_ref=row["source_ref"],
        source_text=row["source_text"],
        links=row["links"],
        review_at=row["review_at"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        updated_at=row["updated_at"],
        superseded_at=row["superseded_at"],
        superseded_by=row["superseded_by"],
        last_read_at=row["last_read_at"],
        read_count=row["read_count"],
        version=row["version"],
        author=row["author"],
    )


async def insert_note(
    conn: aiosqlite.Connection,
    *,
    scope: str,
    project_id: int | None,
    role: str | None,
    kind: str,
    title: str,
    body: str,
    tags: str | None,
    source: Source,
    review_at: str | None,
    author: str,
    now: str,
    links: str | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO memory (scope, project_id, role, kind, title, body, tags, source_kind,"
        " source_ref, source_text, links, review_at, valid_from, recorded_at, updated_at, author)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope,
            project_id,
            role,
            kind,
            title,
            body,
            tags,
            source.kind,
            source.ref,
            source.text,
            links,
            review_at,
            now,
            now,
            now,
            author,
        ),
    )
    return int(cur.lastrowid or 0)


async def get_note(conn: aiosqlite.Connection, note_id: int) -> MemoryNote | None:
    row = await _one(conn, _NOTE_SELECT + " WHERE m.id = ?", (note_id,))
    return _note(row) if row else None


async def get_notes(conn: aiosqlite.Connection, ids: list[int]) -> list[MemoryNote]:
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = await _all(conn, _NOTE_SELECT + f" WHERE m.id IN ({marks})", tuple(ids))
    return [_note(r) for r in rows]


async def update_note(
    conn: aiosqlite.Connection, note_id: int, changes: dict[str, Any], now: str
) -> None:
    """Change NOTE_FIELDS; the version grows by one."""
    assert changes and set(changes) <= set(NOTE_FIELDS)
    sets = ", ".join(f"{name} = ?" for name in changes)
    await conn.execute(
        f"UPDATE memory SET {sets}, version = version + 1, updated_at = ? WHERE id = ?",
        (*changes.values(), now, note_id),
    )


async def supersede_note(conn: aiosqlite.Connection, old_id: int, new_id: int, now: str) -> None:
    await conn.execute(
        "UPDATE memory SET superseded_at = ?, superseded_by = ?, valid_to = ?,"
        " version = version + 1, updated_at = ? WHERE id = ?",
        (now, new_id, now, now, old_id),
    )


async def mark_note_read(conn: aiosqlite.Connection, note_id: int, now: str) -> None:
    await conn.execute(
        "UPDATE memory SET last_read_at = ?, read_count = read_count + 1 WHERE id = ?",
        (now, note_id),
    )


async def list_notes(
    conn: aiosqlite.Connection,
    project_id: int | None,
    *,
    include_superseded: bool = False,
    limit: int = 200,
) -> list[MemoryNote]:
    """Notes of a project plus the global and role notes (only those when `project_id` is
    None), newest first."""
    sql = _NOTE_SELECT + " WHERE (m.project_id IS NULL OR m.project_id = ?)"
    if not include_superseded:
        sql += " AND m.superseded_at IS NULL"
    rows = await _all(conn, sql + " ORDER BY m.id DESC LIMIT ?", (project_id, limit))
    return [_note(r) for r in rows]


async def list_all_notes(conn: aiosqlite.Connection) -> list[MemoryNote]:
    rows = await _all(conn, _NOTE_SELECT + " WHERE m.superseded_at IS NULL ORDER BY m.id")
    return [_note(r) for r in rows]


async def list_import_items(conn: aiosqlite.Connection) -> list[ImportItem]:
    rows = await _all(conn, "SELECT * FROM import_item ORDER BY kind, item")
    return [ImportItem(**dict(r)) for r in rows]


async def save_import_item(conn: aiosqlite.Connection, item: ImportItem) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO import_item (kind, item, source_path, target_id, content_hash,"
        " target_hash, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            item.kind,
            item.item,
            item.source_path,
            item.target_id,
            item.content_hash,
            item.target_hash,
            item.imported_at,
        ),
    )


async def delete_import_item(conn: aiosqlite.Connection, kind: str, item: str) -> None:
    await conn.execute("DELETE FROM import_item WHERE kind = ? AND item = ?", (kind, item))


_BLOCK_SELECT = """
SELECT b.*, p.slug AS project_slug FROM memory_block b LEFT JOIN project p ON p.id = b.project_id
"""


def _block(row: aiosqlite.Row) -> MemoryBlock:
    return MemoryBlock(
        id=row["id"],
        scope=row["scope"],
        project_id=row["project_id"],
        project_slug=row["project_slug"],
        label=row["label"],
        description=row["description"],
        body=row["body"],
        char_limit=row["char_limit"],
        owner_only=bool(row["owner_only"]),
        version=row["version"],
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
        created_at=row["created_at"],
    )


async def get_block(
    conn: aiosqlite.Connection, project_id: int | None, label: str
) -> MemoryBlock | None:
    row = await _one(
        conn, _BLOCK_SELECT + " WHERE b.project_id IS ? AND b.label = ?", (project_id, label)
    )
    return _block(row) if row else None


async def list_blocks(conn: aiosqlite.Connection, project_id: int | None) -> list[MemoryBlock]:
    """The blocks of one scope: a project's, or the global ones when `project_id` is None."""
    rows = await _all(
        conn, _BLOCK_SELECT + " WHERE b.project_id IS ? ORDER BY b.label", (project_id,)
    )
    return [_block(r) for r in rows]


async def insert_block(
    conn: aiosqlite.Connection,
    *,
    project_id: int | None,
    label: str,
    description: str | None,
    body: str,
    char_limit: int,
    owner_only: bool,
    actor: str,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO memory_block (scope, project_id, label, description, body, char_limit,"
        " owner_only, updated_by, updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "global" if project_id is None else "project",
            project_id,
            label,
            description,
            body,
            char_limit,
            int(owner_only),
            actor,
            now,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


async def update_block(
    conn: aiosqlite.Connection,
    block_id: int,
    *,
    description: str | None,
    body: str,
    char_limit: int,
    owner_only: bool,
    actor: str,
    now: str,
) -> None:
    await conn.execute(
        "UPDATE memory_block SET description = ?, body = ?, char_limit = ?, owner_only = ?,"
        " version = version + 1, updated_by = ?, updated_at = ? WHERE id = ?",
        (description, body, char_limit, int(owner_only), actor, now, block_id),
    )


# --- search ----------------------------------------------------------------

# Everything search covers, one uniform row per item: kind, id, project_id, project_slug,
# task_key, name (a document's slug), title, body, superseded_at, note_scope, note_kind.
_SEARCH_SOURCES = {
    "doc": (
        "SELECT 'doc' AS kind, d.id AS id, d.project_id AS project_id, p.slug AS project_slug,"
        " NULL AS task_key, d.slug AS name, d.title AS title, v.body AS body,"
        " NULL AS superseded_at, NULL AS note_scope, NULL AS note_kind"
        " FROM document d JOIN project p ON p.id = d.project_id"
        " JOIN document_version v ON v.document_id = d.id AND v.version = d.version"
    ),
    "decision": (
        "SELECT 'decision' AS kind, x.id AS id, x.project_id AS project_id,"
        " p.slug AS project_slug, t.key AS task_key, NULL AS name, '' AS title, x.text AS body,"
        " x.superseded_at AS superseded_at, NULL AS note_scope, NULL AS note_kind"
        " FROM decision x JOIN project p ON p.id = x.project_id"
        " LEFT JOIN task t ON t.id = x.task_id"
    ),
    "note": (
        "SELECT 'note' AS kind, m.id AS id, m.project_id AS project_id, p.slug AS project_slug,"
        " NULL AS task_key, NULL AS name, m.title AS title, m.body AS body,"
        " m.superseded_at AS superseded_at, m.scope AS note_scope, m.kind AS note_kind"
        " FROM memory m LEFT JOIN project p ON p.id = m.project_id"
    ),
    "report": (
        "SELECT 'report' AS kind, r.id AS id, t.project_id AS project_id,"
        " p.slug AS project_slug, t.key AS task_key, NULL AS name, r.summary AS title,"
        " r.body_md AS body, NULL AS superseded_at, NULL AS note_scope, NULL AS note_kind"
        " FROM report r JOIN task t ON t.id = r.task_id JOIN project p ON p.id = t.project_id"
    ),
    "comment": (
        "SELECT 'comment' AS kind, c.id AS id, t.project_id AS project_id,"
        " p.slug AS project_slug, t.key AS task_key, NULL AS name, '' AS title,"
        " c.body_md AS body, NULL AS superseded_at, NULL AS note_scope, NULL AS note_kind"
        " FROM comment c JOIN task t ON t.id = c.task_id JOIN project p ON p.id = t.project_id"
    ),
}
_KIND_CODE = {kind: n for n, kind in enumerate(_SEARCH_SOURCES, 1)}
_SEARCH_TABLE = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(kind UNINDEXED, ref UNINDEXED,"
    " project_id UNINDEXED, title, body, stems, tokenize = 'unicode61 remove_diacritics 2')"
)
# bm25 weights per column (kind, ref, project_id, title, body, stems): a title hit counts most.
_RANK = "bm25(search_index, 0, 0, 0, 10.0, 1.0, 0.5)"


def _search_rowid(kind: str, item_id: int) -> int:
    return item_id * 8 + _KIND_CODE[kind]


async def ensure_search_index(conn: aiosqlite.Connection) -> bool:
    """Create the FTS5 index if this SQLite has FTS5; False when it does not (search then
    falls back to LIKE)."""
    try:
        await conn.execute(_SEARCH_TABLE)
        # An index made by an SQLite with FTS5, opened by one without: CREATE ... IF NOT EXISTS
        # passes, the first read fails.
        await conn.execute("SELECT 1 FROM search_index LIMIT 0")
    except sqlite3.OperationalError as exc:
        logger.warning("Full-text search is off (%s); search uses LIKE.", exc)
        return False
    return True


async def search_rows(
    conn: aiosqlite.Connection, kind: str, ids: list[int] | None = None
) -> list[aiosqlite.Row]:
    sql = f"SELECT * FROM ({_SEARCH_SOURCES[kind]})"
    if ids is None:
        return await _all(conn, sql)
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    return await _all(conn, sql + f" WHERE id IN ({marks})", tuple(ids))


async def _index(conn: aiosqlite.Connection, rows: list[aiosqlite.Row]) -> None:
    await conn.executemany(
        "INSERT OR REPLACE INTO search_index (rowid, kind, ref, project_id, title, body, stems)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                _search_rowid(r["kind"], r["id"]),
                r["kind"],
                r["id"],
                r["project_id"],
                r["title"] or "",
                r["body"] or "",
                stem_text(f"{r['title'] or ''} {r['body'] or ''}"),
            )
            for r in rows
        ],
    )


async def index_item(conn: aiosqlite.Connection, kind: str, item_id: int) -> None:
    """(Re)index one item in the caller's transaction."""
    await conn.execute("DELETE FROM search_index WHERE rowid = ?", (_search_rowid(kind, item_id),))
    await _index(conn, await search_rows(conn, kind, [item_id]))


async def search_index_stale(conn: aiosqlite.Connection) -> bool:
    """The index does not hold one row per searchable item (a new index, an upgrade, rows
    written while full-text search was off)."""
    total = 0
    for sql in _SEARCH_SOURCES.values():
        row = await _one(conn, f"SELECT count(*) FROM ({sql})", ())
        total += row[0]
    row = await _one(conn, "SELECT count(*) FROM search_index", ())
    return bool(row[0] != total)


async def rebuild_search_index(conn: aiosqlite.Connection) -> int:
    """Refill the index from the tables, in the caller's transaction; returns the row count."""
    await conn.execute("DELETE FROM search_index")
    count = 0
    for kind in _SEARCH_SOURCES:
        rows = await search_rows(conn, kind)
        await _index(conn, rows)
        count += len(rows)
    return count


def _project_filter(project_id: int | None) -> tuple[str, tuple[Any, ...]]:
    """Items of the project plus notes without one (global and role notes)."""
    if project_id is None:
        return "", ()
    return " AND (project_id = ? OR (kind = 'note' AND project_id IS NULL))", (project_id,)


async def fts_search(
    conn: aiosqlite.Connection,
    match: str,
    kinds: tuple[str, ...],
    project_id: int | None,
    limit: int,
) -> list[tuple[str, int]]:
    """(kind, id) of the best matches, best first."""
    marks = ", ".join("?" for _ in kinds)
    where, params = _project_filter(project_id)
    rows = await _all(
        conn,
        f"SELECT kind, ref FROM search_index WHERE search_index MATCH ? AND kind IN ({marks})"
        f"{where} ORDER BY {_RANK} LIMIT ?",
        (match, *kinds, *params, limit),
    )
    return [(r["kind"], int(r["ref"])) for r in rows]


def _like(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def like_search(
    conn: aiosqlite.Connection,
    words: list[str],
    kinds: tuple[str, ...],
    project_id: int | None,
    limit: int,
    any_word: bool = False,
) -> list[tuple[str, int]]:
    """The fallback without FTS5: every word (any word, with `any_word`) or its stem is a
    substring of the title or the body (case-insensitive for ASCII only); ranked by title
    hits, then newest."""
    needles = [(w, stem(w)) for w in words]
    one = "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
    cond = (" OR " if any_word else " AND ").join(f"({one} OR {one})" for _ in words)
    params = tuple(p for pair in needles for n in pair for p in (_like(n), _like(n)))
    where, extra = _project_filter(project_id)
    scored: list[tuple[int, int, str]] = []
    for kind in kinds:
        rows = await _all(
            conn,
            f"SELECT kind, id, title, body FROM ({_SEARCH_SOURCES[kind]}) WHERE ({cond}){where}"
            " ORDER BY id DESC LIMIT ?",
            (*params, *extra, limit),
        )
        for r in rows:
            title = (r["title"] or "").casefold()
            body = (r["body"] or "").casefold()
            hits = sum(
                10 * (w in title or s in title) + (w in body or s in body) for w, s in needles
            )
            scored.append((hits, r["id"], r["kind"]))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [(kind, item_id) for _, item_id, kind in scored[:limit]]


# --- map --------------------------------------------------------------------


async def map_comments(conn: aiosqlite.Connection) -> list[CommentRow]:
    rows = await _all(
        conn,
        "SELECT task_id, SUM(author_kind = 'owner') AS owner_comments,"
        " MAX(CASE WHEN author_kind = 'owner' THEN created_at END) AS last_owner_at,"
        " SUM(author_kind != 'agent' AND read_at IS NULL) AS unread_by_agent,"
        " SUM(author_kind = 'agent' AND read_at IS NULL) AS unread_by_owner"
        " FROM comment GROUP BY task_id",
        (),
    )
    return [
        CommentRow(
            r["task_id"],
            r["owner_comments"],
            r["last_owner_at"],
            r["unread_by_agent"],
            r["unread_by_owner"],
        )
        for r in rows
    ]


async def map_messages(
    conn: aiosqlite.Connection, since: str, recent_since: str, limit: int
) -> list[MessageRow]:
    """`session.message` events from `since` on, one row per sender and recipient."""
    rows = await _all(
        conn,
        "SELECT session_id, json_extract(payload, '$.to') AS recipient, COUNT(*) AS n,"
        " MAX(ts) AS last_at FROM event"
        " WHERE kind = 'session.message' AND ts >= ? AND session_id IS NOT NULL"
        " GROUP BY session_id, recipient ORDER BY last_at DESC LIMIT ?",
        (since, limit),
    )
    return [
        MessageRow(
            r["session_id"], r["recipient"], r["n"], r["last_at"], r["last_at"] >= recent_since
        )
        for r in rows
        if isinstance(r["recipient"], str)
    ]


async def map_recent_comment_tasks(conn: aiosqlite.Connection, since: str) -> set[int]:
    rows = await _all(
        conn,
        "SELECT DISTINCT task_id FROM comment WHERE author_kind = 'owner' AND created_at >= ?",
        (since,),
    )
    return {r[0] for r in rows}


async def map_recent_events(conn: aiosqlite.Connection, since: str, limit: int) -> list[Event]:
    """What sessions did to tasks and live environments from `since` on, newest first."""
    rows = await _all(
        conn,
        "SELECT * FROM event WHERE session_id IS NOT NULL AND ts >= ?"
        " AND kind IN ('task.created', 'task.updated', 'task.stage_changed', 'report.added',"
        " 'task.session_bound', 'plan.set', 'plan.step_added', 'plan.step_updated',"
        " 'live.action')"
        " ORDER BY id DESC LIMIT ?",
        (since, limit),
    )
    return [_event(r) for r in rows]


# --- usage ------------------------------------------------------------------


async def track_usage_file(
    conn: aiosqlite.Connection, session_id: int, path: str, now: str
) -> None:
    """Start reading `path` for the session. A file already tracked moves to the session that
    reports it now, with the rows read so far."""
    await conn.execute(
        "INSERT INTO usage_file (path, session_id, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(path) DO UPDATE SET session_id = excluded.session_id",
        (path, session_id, now),
    )
    await conn.execute(
        "UPDATE usage_day SET session_id = ? WHERE session_id IS NOT ? AND file_id ="
        " (SELECT id FROM usage_file WHERE path = ?)",
        (session_id, session_id, path),
    )
    # Its subagents' files follow it.
    await conn.execute(
        "UPDATE usage_file SET session_id = ? WHERE parent_id ="
        " (SELECT id FROM usage_file WHERE path = ?)",
        (session_id, path),
    )
    await conn.execute(
        "UPDATE usage_day SET session_id = ? WHERE file_id IN (SELECT c.id FROM usage_file c"
        " JOIN usage_file m ON m.id = c.parent_id WHERE m.path = ?)",
        (session_id, path),
    )


async def add_subagent_files(
    conn: aiosqlite.Connection, parent: UsageFile, paths: list[str], now: str
) -> None:
    for path in paths:
        await conn.execute(
            "INSERT OR IGNORE INTO usage_file (path, session_id, parent_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (path, parent.session_id, parent.id, now),
        )


async def touch_usage_file(conn: aiosqlite.Connection, file_id: int, now: str) -> None:
    """A read that got nowhere: the file goes to the back of the queue."""
    await conn.execute("UPDATE usage_file SET read_at = ? WHERE id = ?", (now, file_id))


async def move_usage(conn: aiosqlite.Connection, from_session: int, to_session: int) -> None:
    await conn.execute(
        "UPDATE usage_file SET session_id = ? WHERE session_id = ?", (to_session, from_session)
    )
    await conn.execute(
        "UPDATE usage_day SET session_id = ? WHERE session_id = ?", (to_session, from_session)
    )


async def list_usage_files(conn: aiosqlite.Connection) -> list[UsageFile]:
    """Least recently read first, so a long file cannot keep the others waiting."""
    rows = await _all(
        conn, "SELECT * FROM usage_file ORDER BY read_at IS NOT NULL, read_at, id", ()
    )
    return [
        UsageFile(
            r["id"],
            r["path"],
            r["session_id"],
            r["read_offset"],
            bool(r["skipping"]),
            r["error"],
            r["parent_id"],
        )
        for r in rows
    ]


async def apply_usage_chunk(
    conn: aiosqlite.Connection, file_id: int, chunk: Chunk, now: str
) -> int:
    """Store one read of a file; returns how many messages were new. A file that shrank first
    gives up its own rows and message ids. Ids another file counted stay with that file."""
    if chunk.shrunk:
        await conn.execute("DELETE FROM usage_day WHERE file_id = ?", (file_id,))
        await conn.execute("DELETE FROM usage_message WHERE file_id = ?", (file_id,))
    counted = 0
    for line in chunk.lines:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO usage_message (message_id, file_id) VALUES (?, ?)",
            (line.message_id, file_id),
        )
        if cur.rowcount != 1:
            continue
        counted += 1
        await conn.execute(
            "INSERT INTO usage_day (file_id, day, session_id, model, input, output, cache_read,"
            " cache_write, messages) VALUES (?, ?, (SELECT session_id FROM usage_file"
            " WHERE id = ?), ?, ?, ?, ?, ?, 1) ON CONFLICT(file_id, day, model) DO UPDATE SET"
            " input = input + excluded.input, output = output + excluded.output,"
            " cache_read = cache_read + excluded.cache_read,"
            " cache_write = cache_write + excluded.cache_write, messages = messages + 1",
            (
                file_id,
                line.day,
                file_id,
                line.model,
                line.input,
                line.output,
                line.cache_read,
                line.cache_write,
            ),
        )
    unknown = "?" if chunk.shrunk else "unknown_lines + ?"
    await conn.execute(
        f"UPDATE usage_file SET read_offset = ?, skipping = ?, unknown_lines = {unknown},"
        " error = NULL, read_at = ? WHERE id = ?",
        (chunk.offset, int(chunk.skipping), chunk.unknown, now, file_id),
    )
    return counted


async def set_usage_error(conn: aiosqlite.Connection, file_id: int, error: str, now: str) -> None:
    await conn.execute(
        "UPDATE usage_file SET error = ?, read_at = ? WHERE id = ?", (error, now, file_id)
    )


_USAGE_PROJECT = (
    "COALESCE(s.project_id, (SELECT t.project_id FROM task_session b JOIN task t"
    " ON t.id = b.task_id WHERE b.session_id = s.id ORDER BY b.bound_at DESC LIMIT 1))"
)


async def usage_day_rows(
    conn: aiosqlite.Connection,
    *,
    since: str | None,
    project_id: int | None,
    session_ids: list[int] | None,
    model: str | None,
) -> list[UsageDayRow]:
    """Daily totals per session and model. A session counts in one project: its own, else that
    of the task it was bound to last."""
    where, params = [], []
    if since is not None:
        where.append("d.day >= ?")
        params.append(since)
    if project_id is not None:
        where.append(f"{_USAGE_PROJECT} = ?")
        params.append(project_id)
    if session_ids is not None:
        where.append(f"d.session_id IN ({', '.join('?' for _ in session_ids) or 'NULL'})")
        params.extend(session_ids)
    if model is not None:
        where.append("d.model = ?")
        params.append(model)
    rows = await _all(
        conn,
        "SELECT d.day, d.session_id, s.name AS session,"
        f" (SELECT slug FROM project WHERE id = {_USAGE_PROJECT}) AS project, d.model,"
        # TOTAL: unlike SUM it cannot fail with an integer overflow.
        " TOTAL(d.input) AS input, TOTAL(d.output) AS output, TOTAL(d.cache_read) AS cache_read,"
        " TOTAL(d.cache_write) AS cache_write, TOTAL(d.messages) AS messages"
        " FROM usage_day d LEFT JOIN session s ON s.id = d.session_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " GROUP BY d.day, d.session_id, d.model",
        tuple(params),
    )
    return [
        UsageDayRow(
            r["day"],
            r["session_id"],
            r["session"],
            r["project"],
            r["model"],
            int(r["input"]),
            int(r["output"]),
            int(r["cache_read"]),
            int(r["cache_write"]),
            int(r["messages"]),
        )
        for r in rows
    ]


async def usage_file_stats(conn: aiosqlite.Connection) -> tuple[int, int, int]:
    """Files tracked, files that could not be read at the last try, lines not understood."""
    row = await _one(
        conn,
        "SELECT COUNT(*), COALESCE(SUM(error IS NOT NULL), 0), COALESCE(SUM(unknown_lines), 0)"
        " FROM usage_file",
        (),
    )
    return (row[0], row[1], row[2]) if row else (0, 0, 0)


async def usage_sessions(conn: aiosqlite.Connection) -> list[tuple[int, str]]:
    """Sessions that have usage, for the filter."""
    rows = await _all(
        conn,
        "SELECT DISTINCT s.id, s.name FROM usage_day d JOIN session s ON s.id = d.session_id"
        " ORDER BY s.name, s.id",
        (),
    )
    return [(r[0], r[1]) for r in rows]


async def usage_models(conn: aiosqlite.Connection) -> list[str]:
    rows = await _all(conn, "SELECT DISTINCT model FROM usage_day ORDER BY model", ())
    return [r[0] for r in rows]


# --- intake ----------------------------------------------------------------


def _source(row: aiosqlite.Row) -> IntakeSource:
    keys = row.keys()
    return IntakeSource(
        id=row["id"],
        slug=row["slug"],
        kind=row["kind"],
        name=row["name"],
        may_wake=bool(row["may_wake"]),
        created_at=row["created_at"],
        last_message_at=row["last_message_at"],
        messages=row["messages"] if "messages" in keys else 0,
        new=row["new"] if "new" in keys else 0,
        first_message_id=row["first_message_id"] if "first_message_id" in keys else None,
    )


async def get_source(conn: aiosqlite.Connection, slug: str) -> IntakeSource | None:
    row = await _one(conn, "SELECT * FROM intake_source WHERE slug = ?", (slug,))
    return _source(row) if row else None


async def get_source_by_id(conn: aiosqlite.Connection, source_id: int) -> IntakeSource | None:
    row = await _one(conn, "SELECT * FROM intake_source WHERE id = ?", (source_id,))
    return _source(row) if row else None


async def insert_source(
    conn: aiosqlite.Connection, slug: str, kind: str, name: str, now: str
) -> int:
    cur = await conn.execute(
        "INSERT INTO intake_source (slug, kind, name, created_at) VALUES (?, ?, ?, ?)",
        (slug, kind, name, now),
    )
    return int(cur.lastrowid or 0)


async def list_sources(conn: aiosqlite.Connection) -> list[IntakeSource]:
    rows = await _all(
        conn,
        "SELECT s.*, (SELECT COUNT(*) FROM intake_message m WHERE m.source_id = s.id) AS messages,"
        " (SELECT COUNT(*) FROM intake_message m WHERE m.source_id = s.id AND m.status = 'new')"
        " AS new,"
        " (SELECT MIN(m.id) FROM intake_message m WHERE m.source_id = s.id) AS first_message_id"
        " FROM intake_source s ORDER BY s.slug",
    )
    return [_source(r) for r in rows]


async def set_source_may_wake(conn: aiosqlite.Connection, source_id: int, value: bool) -> None:
    await conn.execute(
        "UPDATE intake_source SET may_wake = ? WHERE id = ?", (int(value), source_id)
    )


async def touch_source(conn: aiosqlite.Connection, source_id: int, now: str) -> None:
    await conn.execute(
        "UPDATE intake_source SET last_message_at = ? WHERE id = ?", (now, source_id)
    )


async def count_messages_since(conn: aiosqlite.Connection, source_id: int, since: str) -> int:
    row = await _one(
        conn,
        "SELECT COUNT(*) FROM intake_message WHERE source_id = ? AND created_at >= ?"
        " AND status != 'refused'",
        (source_id, since),
    )
    return int(row[0])


_MESSAGE_SELECT = """
SELECT m.*, s.slug AS source_slug, s.may_wake AS source_may_wake, t.key AS task_key,
       (SELECT MIN(f.id) FROM intake_message f WHERE f.source_id = m.source_id)
           AS source_first_message_id
FROM intake_message m
JOIN intake_source s ON s.id = m.source_id
LEFT JOIN task t ON t.id = m.task_id
"""


def _message(row: aiosqlite.Row) -> IntakeMessage:
    return IntakeMessage(
        id=row["id"],
        source_id=row["source_id"],
        source_slug=row["source_slug"],
        source_may_wake=bool(row["source_may_wake"]),
        external_id=row["external_id"],
        thread=row["thread"],
        author=row["author"],
        text=row["text"],
        received_at=row["received_at"],
        status=row["status"],
        refused_reason=row["refused_reason"],
        task_id=row["task_id"],
        task_key=row["task_key"],
        outcome=row["outcome"],
        created_at=row["created_at"],
        source_first_message_id=row["source_first_message_id"],
    )


async def get_message(conn: aiosqlite.Connection, message_id: int) -> IntakeMessage | None:
    row = await _one(conn, _MESSAGE_SELECT + " WHERE m.id = ?", (message_id,))
    return _message(row) if row else None


async def find_message(
    conn: aiosqlite.Connection, source_id: int, external_id: str
) -> IntakeMessage | None:
    row = await _one(
        conn,
        _MESSAGE_SELECT + " WHERE m.source_id = ? AND m.external_id = ?",
        (source_id, external_id),
    )
    return _message(row) if row else None


async def insert_message(
    conn: aiosqlite.Connection,
    *,
    source_id: int,
    external_id: str,
    thread: str | None,
    author: str | None,
    text: str | None,
    received_at: str,
    status: str,
    refused_reason: str | None,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO intake_message (source_id, external_id, thread, author, text, received_at,"
        " status, refused_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, external_id, thread, author, text, received_at, status, refused_reason, now),
    )
    return int(cur.lastrowid or 0)


async def update_message(
    conn: aiosqlite.Connection, message_id: int, changes: dict[str, Any]
) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(
        f"UPDATE intake_message SET {sets} WHERE id = ?", (*changes.values(), message_id)
    )


async def list_messages(
    conn: aiosqlite.Connection,
    *,
    source_id: int | None = None,
    status: str | None = None,
    task_id: int | None = None,
    limit: int = 50,
) -> list[IntakeMessage]:
    """Newest first."""
    where, params = [], []
    for column, value in (("m.source_id", source_id), ("m.status", status), ("m.task_id", task_id)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    sql = _MESSAGE_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    rows = await _all(conn, sql + " ORDER BY m.id DESC LIMIT ?", (*params, limit))
    return [_message(r) for r in rows]


async def thread_task(conn: aiosqlite.Connection, source_id: int, thread: str) -> int | None:
    row = await _one(
        conn,
        "SELECT task_id FROM intake_thread WHERE source_id = ? AND thread = ?",
        (source_id, thread),
    )
    return int(row[0]) if row else None


async def set_thread(
    conn: aiosqlite.Connection, source_id: int, thread: str, task_id: int, now: str
) -> None:
    await conn.execute(
        "INSERT INTO intake_thread (source_id, thread, task_id, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (source_id, thread) DO UPDATE SET task_id = excluded.task_id,"
        " updated_at = excluded.updated_at",
        (source_id, thread, task_id, now),
    )


async def set_answer_mark(
    conn: aiosqlite.Connection, task_id: int, received_at: str | None, message_id: int | None
) -> None:
    """The "answer received" mark; no version bump: an open edit form must not conflict."""
    await conn.execute(
        "UPDATE task SET answer_received_at = ?, answer_message_id = ? WHERE id = ?",
        (received_at, message_id, task_id),
    )


_BACKLOG_SELECT = """
SELECT b.*, p.slug AS project_slug, ot.key AS origin_task_key, ot.stage AS origin_task_stage,
       os.name AS origin_session_name, t.key AS task_key
FROM backlog_item b
JOIN project p ON p.id = b.project_id
LEFT JOIN task ot ON ot.id = b.origin_task_id
LEFT JOIN session os ON os.id = b.origin_session_id
LEFT JOIN task t ON t.id = b.task_id
"""


def _backlog(row: aiosqlite.Row) -> BacklogItem:
    return BacklogItem(
        id=row["id"],
        project_id=row["project_id"],
        project_slug=row["project_slug"],
        title=row["title"],
        body_md=row["body_md"],
        area=row["area"],
        origin=row["origin"],
        origin_task_id=row["origin_task_id"],
        origin_task_key=row["origin_task_key"],
        origin_task_stage=row["origin_task_stage"],
        origin_session_id=row["origin_session_id"],
        origin_session_name=row["origin_session_name"],
        intake_message_id=row["intake_message_id"],
        status=row["status"],
        decline_reason=row["decline_reason"],
        link=row["link"],
        task_id=row["task_id"],
        task_key=row["task_key"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        priority=row["priority"],
        assignee=row["assignee"],
    )


async def insert_backlog_item(conn: aiosqlite.Connection, values: dict[str, Any]) -> int:
    """`values`: project_id, title, origin, created_at, created_by and any optional column."""
    cols = list(values)
    cur = await conn.execute(
        f"INSERT INTO backlog_item ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        tuple(values.values()),
    )
    return int(cur.lastrowid or 0)


async def get_backlog_item(conn: aiosqlite.Connection, item_id: int) -> BacklogItem | None:
    row = await _one(conn, _BACKLOG_SELECT + " WHERE b.id = ?", (item_id,))
    return _backlog(row) if row else None


async def update_backlog_item(
    conn: aiosqlite.Connection, item_id: int, changes: dict[str, Any]
) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(f"UPDATE backlog_item SET {sets} WHERE id = ?", (*changes.values(), item_id))


async def list_backlog(
    conn: aiosqlite.Connection,
    *,
    project_id: int | None = None,
    status: str | None = None,
    area: str | None = None,
    assignee: str | None = None,
    origin_task_id: int | None = None,
    task_id: int | None = None,
    limit: int = 200,
) -> list[BacklogItem]:
    """`new` first, then later, planned and done, declined; within a status the highest priority
    first, then the newest. `assignee` "-" means items that are for nobody."""
    where, params = [], []
    for column, value in (
        ("b.project_id", project_id),
        ("b.status", status),
        ("b.origin_task_id", origin_task_id),
        ("b.task_id", task_id),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    if area is not None:
        where.append("b.area = ? COLLATE NOCASE")
        params.append(area)
    if assignee == UNASSIGNED:
        where.append("b.assignee IS NULL")
    elif assignee is not None:
        where.append("b.assignee = ? COLLATE NOCASE")
        params.append(assignee)
    sql = _BACKLOG_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    order = (
        " ORDER BY CASE b.status WHEN 'new' THEN 0 WHEN 'later' THEN 1 WHEN 'planned' THEN 2"
        " WHEN 'done' THEN 2 ELSE 3 END, b.priority DESC, b.id DESC LIMIT ?"
    )
    return [_backlog(r) for r in await _all(conn, sql + order, (*params, limit))]


async def backlog_new_counts(conn: aiosqlite.Connection) -> dict[int, int]:
    """New backlog items per project id."""
    rows = await _all(
        conn,
        "SELECT project_id, COUNT(*) FROM backlog_item WHERE status = 'new' GROUP BY project_id",
    )
    return {r[0]: r[1] for r in rows}


async def backlog_areas(conn: aiosqlite.Connection, project_id: int | None) -> list[str]:
    where = " AND project_id = ?" if project_id is not None else ""
    params = (project_id,) if project_id is not None else ()
    rows = await _all(
        conn,
        f"SELECT DISTINCT area FROM backlog_item WHERE area IS NOT NULL{where} ORDER BY area",
        params,
    )
    return [r[0] for r in rows]


async def backlog_assignees(conn: aiosqlite.Connection, project_id: int | None) -> list[str]:
    where = " AND project_id = ?" if project_id is not None else ""
    params = (project_id,) if project_id is not None else ()
    rows = await _all(
        conn,
        f"SELECT assignee FROM backlog_item WHERE assignee IS NOT NULL{where}"
        " GROUP BY assignee COLLATE NOCASE ORDER BY assignee COLLATE NOCASE",
        params,
    )
    return [r[0] for r in rows]
