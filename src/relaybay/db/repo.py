"""SQL for every table. Functions take an open connection; transactions belong to the service."""

import json
from typing import Any

import aiosqlite

from relaybay.domain.models import (
    Binding,
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
       s.status AS session_status, s.desktop_link AS session_link
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


async def last_event_id(conn: aiosqlite.Connection) -> int:
    row = await _one(conn, "SELECT COALESCE(MAX(id), 0) FROM event", ())
    return int(row[0])
