"""SQL for every table. Functions take an open connection; transactions belong to the service."""

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from relaybay.domain.environments import (
    AccessEntry,
    Environment,
    PolicyProposal,
    Proposal,
    split_test_secrets,
)
from relaybay.domain.models import (
    Binding,
    Comment,
    Decision,
    Event,
    Project,
    Report,
    Session,
    Task,
    TrackerEntry,
)
from relaybay.domain.stages import Stage

_TASK_SELECT = """
SELECT t.*, p.slug AS project_slug, s.name AS session_name, s.external_id AS session_external_id,
       s.status AS session_status, s.desktop_link AS session_link,
       s.activity AS session_activity, s.activity_at AS session_activity_at
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
) -> int:
    cur = await conn.execute(
        "INSERT INTO decision (project_id, task_id, text, decided_by, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (project_id, task_id, text, decided_by, now),
    )
    return int(cur.lastrowid or 0)


async def list_decisions(
    conn: aiosqlite.Connection,
    *,
    project_id: int | None = None,
    task_id: int | None = None,
    limit: int = 10,
) -> list[Decision]:
    where, params = [], []
    if project_id is not None:
        where.append("d.project_id = ?")
        params.append(project_id)
    if task_id is not None:
        where.append("d.task_id = ?")
        params.append(task_id)
    sql = "SELECT d.*, t.key AS task_key FROM decision d LEFT JOIN task t ON t.id = d.task_id"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.id DESC LIMIT ?"
    rows = await _all(conn, sql, (*params, limit))
    return [
        Decision(
            id=r["id"],
            task_id=r["task_id"],
            project_id=r["project_id"],
            text=r["text"],
            decided_by=r["decided_by"],
            created_at=r["created_at"],
            task_key=r["task_key"],
        )
        for r in rows
    ]


# --- comment ---------------------------------------------------------------

_COMMENT_SELECT = """
SELECT c.*, t.key AS task_key, d.name AS delivered_to_name
FROM comment c
JOIN task t ON t.id = c.task_id
LEFT JOIN session d ON d.id = c.delivered_to_session_id
"""
# Owner comments on the tasks a session is bound to now.
_BOUND_OWNER = (
    " WHERE c.author_kind = 'owner' AND c.read_at IS NULL AND c.task_id IN"
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
) -> int:
    cur = await conn.execute(
        "INSERT INTO comment (task_id, author_kind, author, session_id, body_md, in_reply_to,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, author_kind, author, session_id, body_md, in_reply_to, now),
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
    conn: aiosqlite.Connection, project_ids: list[int] | None = None
) -> list[Environment]:
    """Environments with their access entries, by project and position."""
    sql, params = _ENV_SELECT, ()
    if project_ids is not None:
        if not project_ids:
            return []
        sql += f" WHERE e.project_id IN ({', '.join('?' for _ in project_ids)})"
        params = tuple(project_ids)
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
