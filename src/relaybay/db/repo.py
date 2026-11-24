"""SQL for every table. Functions take an open connection; transactions belong to the service."""

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from relaybay.domain.annotations import Annotation, AnnotationReply, geometry_json
from relaybay.domain.approvals import Approval
from relaybay.domain.areas import ProjectArea
from relaybay.domain.backlog import NO_KIND, UNASSIGNED, BacklogItem
from relaybay.domain.blockers import BlockLink
from relaybay.domain.code import ProjectRepo, TaskPr, TaskRepo
from relaybay.domain.doc_index import IndexedSection
from relaybay.domain.environments import (
    AccessEntry,
    Environment,
    PolicyProposal,
    split_test_secrets,
)
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
from relaybay.domain.questions import Question
from relaybay.domain.reports import ReportCheck
from relaybay.domain.review import Attachment, ReviewStep
from relaybay.domain.rules import RuleProposal, RuleVersion
from relaybay.domain.stages import ACCEPTANCE_STAGES, Stage

logger = logging.getLogger("relaybay")

# Within a stage column; the tie-break is the order before ranks existed.
BOARD_ORDER = "t.rank, t.updated_at DESC, t.id DESC"
_TASK_SELECT = """
SELECT t.*, p.slug AS project_slug,
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
           AS plan_revised_at,
       (SELECT COUNT(*) FROM question q WHERE q.task_id = t.id AND q.status = 'open')
           AS open_questions,
       (SELECT COUNT(*) FROM approval a WHERE a.task_id = t.id AND a.status = 'open')
           AS open_approvals,
       (SELECT ls.status FROM task_session b JOIN session ls ON ls.id = b.session_id
           WHERE b.task_id = t.id ORDER BY b.id DESC LIMIT 1) AS last_session_status,
       pa.name AS area_name, pa.color AS area_color
FROM task t
JOIN project p ON p.id = t.project_id
LEFT JOIN project_area pa ON pa.id = t.area_id
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
        rank=row["rank"],
        archived_at=row["archived_at"],
        archived_by=row["archived_by"],
        unarchived_at=row["unarchived_at"],
        archived_from=row["archived_from"],
        deploy_notes=row["deploy_notes"],
        deploy_done=bool(row["deploy_done"]),
        inspect_only=bool(row["inspect_only"]),
        cancelled=row["cancelled"],
        cancelled_at=row["cancelled_at"],
        area_id=row["area_id"],
        area=row["area_name"],
        area_color=row["area_color"],
        open_questions=row["open_questions"],
        open_approvals=row["open_approvals"],
        last_session_status=row["last_session_status"],
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


async def list_archived_projects(conn: aiosqlite.Connection) -> list[Project]:
    rows = await _all(conn, "SELECT * FROM project WHERE archived_at IS NOT NULL ORDER BY slug")
    return [_project(r) for r in rows]


async def archived_project_ids(conn: aiosqlite.Connection) -> set[int]:
    rows = await _all(conn, "SELECT id FROM project WHERE archived_at IS NOT NULL")
    return {r[0] for r in rows}


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


# --- project repositories, task code links --------------------------------


def _project_repo(row: aiosqlite.Row) -> ProjectRepo:
    return ProjectRepo(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        path=row["path"],
        kind=row["kind"],
        position=row["position"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _task_pr(row: aiosqlite.Row) -> TaskPr:
    return TaskPr(
        id=row["id"],
        task_id=row["task_id"],
        repo_id=row["project_repo_id"],
        url=row["url"],
        number=row["number"],
        title=row["title"],
        added_by=row["added_by"],
        added_at=row["added_at"],
    )


async def list_project_repos(conn: aiosqlite.Connection, project_id: int) -> list[ProjectRepo]:
    rows = await _all(
        conn,
        "SELECT * FROM project_repo WHERE project_id = ? ORDER BY position, id",
        (project_id,),
    )
    return [_project_repo(r) for r in rows]


async def all_project_repos(conn: aiosqlite.Connection) -> list[ProjectRepo]:
    rows = await _all(conn, "SELECT * FROM project_repo ORDER BY project_id, position, id")
    return [_project_repo(r) for r in rows]


async def insert_project_repo(
    conn: aiosqlite.Connection,
    project_id: int,
    name: str,
    path: str,
    kind: str | None,
    position: int,
    now: str,
) -> int:
    cur = await conn.execute(
        "INSERT INTO project_repo (project_id, name, path, kind, position, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, name, path, kind, position, now, now),
    )
    return int(cur.lastrowid or 0)


async def update_project_repo(
    conn: aiosqlite.Connection, repo_id: int, changes: dict[str, Any], now: str
) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(
        f"UPDATE project_repo SET {sets}, updated_at = ? WHERE id = ?",
        (*changes.values(), now, repo_id),
    )


async def delete_project_repo(conn: aiosqlite.Connection, repo_id: int) -> None:
    await conn.execute("DELETE FROM project_repo WHERE id = ?", (repo_id,))


def _area(row: aiosqlite.Row) -> ProjectArea:
    return ProjectArea(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        color=row["color"],
        position=row["position"],
        created_at=row["created_at"],
        tasks=row["tasks"],
    )


_AREA_SELECT = (
    "SELECT a.*, (SELECT COUNT(*) FROM task t WHERE t.area_id = a.id) AS tasks FROM project_area a"
)


async def list_areas(conn: aiosqlite.Connection, project_id: int) -> list[ProjectArea]:
    rows = await _all(
        conn, _AREA_SELECT + " WHERE a.project_id = ? ORDER BY a.position, a.id", (project_id,)
    )
    return [_area(r) for r in rows]


async def all_areas(conn: aiosqlite.Connection) -> list[ProjectArea]:
    rows = await _all(conn, _AREA_SELECT + " ORDER BY a.project_id, a.position, a.id")
    return [_area(r) for r in rows]


async def insert_area(
    conn: aiosqlite.Connection, project_id: int, name: str, color: str, position: int, now: str
) -> int:
    cur = await conn.execute(
        "INSERT INTO project_area (project_id, name, color, position, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (project_id, name, color, position, now),
    )
    return int(cur.lastrowid or 0)


async def update_area(conn: aiosqlite.Connection, area_id: int, changes: dict[str, Any]) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(f"UPDATE project_area SET {sets} WHERE id = ?", (*changes.values(), area_id))


async def delete_area(conn: aiosqlite.Connection, area_id: int) -> None:
    await conn.execute("DELETE FROM project_area WHERE id = ?", (area_id,))


async def move_area_tasks(
    conn: aiosqlite.Connection, area_id: int, into_id: int | None, now: str
) -> None:
    """The area's tasks carry `into_id` (None: no area): a change of each task, so an open form
    sees it as one."""
    await conn.execute(
        "UPDATE task SET area_id = ?, version = version + 1, updated_at = ? WHERE area_id = ?",
        (into_id, now, area_id),
    )


async def sync_repo_path(conn: aiosqlite.Connection, project_id: int, now: str) -> None:
    """`project.repo_path` follows the first repository; with none left it keeps its value."""
    first = "(SELECT path FROM project_repo WHERE project_id = ? ORDER BY position, id LIMIT 1)"
    await conn.execute(
        f"UPDATE project SET repo_path = {first}, updated_at = ?"
        f" WHERE id = ? AND {first} IS NOT NULL AND repo_path IS NOT {first}",
        (project_id, now, project_id, project_id, project_id),
    )


async def task_repo_links(
    conn: aiosqlite.Connection, *, task_id: int | None = None, project_id: int | None = None
) -> list[TaskRepo]:
    """The repository links of one task, or of every task of a project."""
    where, params = (
        ("tr.task_id = ?", task_id)
        if task_id is not None
        else (
            "t.project_id = ?",
            project_id,
        )
    )
    rows = await _all(
        conn,
        "SELECT tr.* FROM task_repo tr JOIN task t ON t.id = tr.task_id"
        f" WHERE {where} ORDER BY tr.id",
        (params,),
    )
    return [
        TaskRepo(
            r["task_id"],
            r["project_repo_id"],
            r["added_by"],
            r["how"],
            r["added_at"],
            r["branch"],
            r["worktree"],
        )
        for r in rows
    ]


async def task_prs(
    conn: aiosqlite.Connection, *, task_id: int | None = None, project_id: int | None = None
) -> list[TaskPr]:
    """The pull requests of one task, or of every task of a project, oldest first."""
    where, params = (
        ("tp.task_id = ?", task_id)
        if task_id is not None
        else (
            "t.project_id = ?",
            project_id,
        )
    )
    rows = await _all(
        conn,
        "SELECT tp.* FROM task_pr tp JOIN task t ON t.id = tp.task_id"
        f" WHERE {where} ORDER BY tp.id",
        (params,),
    )
    return [_task_pr(r) for r in rows]


async def insert_task_repo(
    conn: aiosqlite.Connection, task_id: int, repo_id: int, added_by: str, how: str, now: str
) -> bool:
    """False when the task already had the link (the first one stays)."""
    cur = await conn.execute(
        "INSERT INTO task_repo (task_id, project_repo_id, added_by, how, added_at)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT (task_id, project_repo_id) DO NOTHING",
        (task_id, repo_id, added_by, how, now),
    )
    return cur.rowcount > 0


async def delete_task_repo(conn: aiosqlite.Connection, task_id: int, repo_id: int) -> bool:
    cur = await conn.execute(
        "DELETE FROM task_repo WHERE task_id = ? AND project_repo_id = ?", (task_id, repo_id)
    )
    return cur.rowcount > 0


async def insert_task_pr(
    conn: aiosqlite.Connection,
    task_id: int,
    repo_id: int | None,
    url: str,
    number: int | None,
    title: str | None,
    added_by: str,
    now: str,
) -> bool:
    """False when the task already had the link (the first one stays)."""
    cur = await conn.execute(
        "INSERT INTO task_pr (task_id, project_repo_id, url, number, title, added_by, added_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (task_id, url) DO NOTHING",
        (task_id, repo_id, url, number, title, added_by, now),
    )
    return cur.rowcount > 0


async def delete_task_pr(conn: aiosqlite.Connection, task_id: int, url: str) -> bool:
    cur = await conn.execute("DELETE FROM task_pr WHERE task_id = ? AND url = ?", (task_id, url))
    return cur.rowcount > 0


async def insert_task_block(
    conn: aiosqlite.Connection, task_id: int, blocker_id: int, created_by: str, now: str
) -> bool:
    """False when the task was blocked by that card already."""
    cur = await conn.execute(
        "INSERT INTO task_block (task_id, blocker_id, created_by, created_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (task_id, blocker_id) DO NOTHING",
        (task_id, blocker_id, created_by, now),
    )
    return cur.rowcount > 0


async def delete_task_block(conn: aiosqlite.Connection, task_id: int, blocker_id: int) -> bool:
    cur = await conn.execute(
        "DELETE FROM task_block WHERE task_id = ? AND blocker_id = ?", (task_id, blocker_id)
    )
    return cur.rowcount > 0


async def blocker_ids_of(conn: aiosqlite.Connection, task_ids: list[int]) -> dict[int, list[int]]:
    """task id -> the ids of the cards blocking it, for the cycle check's walk."""
    found: dict[int, list[int]] = {}
    for start in range(0, len(task_ids), _CHUNK):
        part = task_ids[start : start + _CHUNK]
        marks = ", ".join("?" for _ in part)
        rows = await _all(
            conn,
            f"SELECT task_id, blocker_id FROM task_block WHERE task_id IN ({marks})",
            tuple(part),
        )
        for r in rows:
            found.setdefault(r["task_id"], []).append(r["blocker_id"])
    return found


_TASK_BLOCK_SELECT = """
SELECT b.task_id, b.blocker_id, b.created_by, b.created_at,
       o.key, o.title, o.stage, o.archived_at, p.slug AS project_slug
FROM task_block b
JOIN task o ON o.id = b.{other}
JOIN project p ON p.id = o.project_id
"""


def _block_link(row: aiosqlite.Row) -> BlockLink:
    return BlockLink(
        task_id=row["task_id"],
        blocker_id=row["blocker_id"],
        key=row["key"],
        title=row["title"],
        stage=row["stage"],
        project_slug=row["project_slug"],
        archived=row["archived_at"] is not None,
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


async def task_blockers(
    conn: aiosqlite.Connection, *, task_id: int | None = None, project_id: int | None = None
) -> list[BlockLink]:
    """The cards blocking one task, or every task of a project; `key` names the blocker."""
    where, value = (
        ("b.task_id = ?", task_id)
        if task_id is not None
        else ("b.task_id IN (SELECT id FROM task WHERE project_id = ?)", project_id)
    )
    rows = await _all(
        conn,
        _TASK_BLOCK_SELECT.format(other="blocker_id") + f" WHERE {where} ORDER BY b.id",
        (value,),
    )
    return [_block_link(r) for r in rows]


async def task_blocked(conn: aiosqlite.Connection, task_id: int) -> list[BlockLink]:
    """The cards this task blocks; `key` names the blocked card."""
    rows = await _all(
        conn,
        _TASK_BLOCK_SELECT.format(other="task_id") + " WHERE b.blocker_id = ? ORDER BY b.id",
        (task_id,),
    )
    return [_block_link(r) for r in rows]


async def session_repo_candidates(
    conn: aiosqlite.Connection, session_id: int
) -> list[tuple[int, int, str, bool, str | None, str | None]]:
    """(task id, repository id, path, already linked, the branch and the worktree stored on the
    link) for the repositories of every task the session is bound to now: one read on the hook
    path."""
    rows = await _all(
        conn,
        "SELECT b.task_id, r.id AS repo_id, r.path, tr.task_id IS NOT NULL AS linked,"
        " tr.branch, tr.worktree FROM task_session b JOIN task t ON t.id = b.task_id"
        " JOIN project_repo r ON r.project_id = t.project_id"
        " LEFT JOIN task_repo tr ON tr.task_id = b.task_id AND tr.project_repo_id = r.id"
        " WHERE b.session_id = ? AND b.unbound_at IS NULL",
        (session_id,),
    )
    return [
        (r["task_id"], r["repo_id"], r["path"], bool(r["linked"]), r["branch"], r["worktree"])
        for r in rows
    ]


async def branch_holder(
    conn: aiosqlite.Connection, repo_id: int, branch: str, task_id: int
) -> str | None:
    """The key of another card not in done whose link to the repository already holds
    `branch`, or None."""
    row = await _one(
        conn,
        "SELECT t.key FROM task_repo tr JOIN task t ON t.id = tr.task_id"
        " WHERE tr.project_repo_id = ? AND tr.branch = ?"
        " AND tr.task_id != ? AND t.stage != 'done' AND t.cancelled_at IS NULL"
        " ORDER BY t.id LIMIT 1",
        (repo_id, branch, task_id),
    )
    return None if row is None else str(row["key"])


async def set_task_repo_branch(
    conn: aiosqlite.Connection, task_id: int, repo_id: int, branch: str
) -> bool:
    """Store the task's branch in a linked repository; False when it was that already."""
    cur = await conn.execute(
        "UPDATE task_repo SET branch = ? WHERE task_id = ? AND project_repo_id = ?"
        " AND branch IS NOT ?",
        (branch, task_id, repo_id, branch),
    )
    return cur.rowcount > 0


async def set_task_repo_worktree(
    conn: aiosqlite.Connection, task_id: int, repo_id: int, worktree: str
) -> bool:
    """Store the folder the task works in for a linked repository; False when it was that
    already."""
    cur = await conn.execute(
        "UPDATE task_repo SET worktree = ? WHERE task_id = ? AND project_repo_id = ?"
        " AND worktree IS NOT ?",
        (worktree, task_id, repo_id, worktree),
    )
    return cur.rowcount > 0


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
        no_live_at=row["no_live_at"],
        no_live_by=row["no_live_by"],
        surface=row["surface"],
        entrypoint=row["entrypoint"],
        start_cwd=row["start_cwd"],
        launched_by=row["launched_by"],
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


async def pulse_activity(
    conn: aiosqlite.Connection, since: str
) -> dict[int, tuple[int, int, tuple[str, str, str] | None]]:
    """Per project since `since`: cards moved to another stage, reports written, and the newest
    report ever (its card key, first line as `state · size · gist`, time)."""
    found: dict[int, list[Any]] = {}
    for row in await _all(
        conn,
        "SELECT project_id, COUNT(DISTINCT task_id) AS n FROM event"
        " WHERE kind = 'task.stage_changed' AND ts >= ? AND project_id IS NOT NULL"
        " GROUP BY project_id",
        (since,),
    ):
        found.setdefault(row["project_id"], [0, 0, None])[0] = row["n"]
    for row in await _all(
        conn,
        "SELECT t.project_id, COUNT(*) AS n FROM report r JOIN task t ON t.id = r.task_id"
        " WHERE r.created_at >= ? GROUP BY t.project_id",
        (since,),
    ):
        found.setdefault(row["project_id"], [0, 0, None])[1] = row["n"]
    for row in await _all(
        conn,
        "SELECT t.project_id, t.key, r.state, r.size, r.summary, r.created_at FROM report r"
        " JOIN task t ON t.id = r.task_id"
        " WHERE r.id IN (SELECT MAX(r2.id) FROM report r2 JOIN task t2 ON t2.id = r2.task_id"
        " GROUP BY t2.project_id)",
    ):
        first = " · ".join(x for x in (row["state"], row["size"], row["summary"]) if x)
        latest = (row["key"], first, row["created_at"])
        found.setdefault(row["project_id"], [0, 0, None])[2] = latest
    return {pid: (v[0], v[1], v[2]) for pid, v in found.items()}


async def projects_with_tracker(conn: aiosqlite.Connection) -> set[int]:
    """Projects any of whose cards has or ever had a tracker reference (its tracker history),
    or has a tracker link now (a link is not kept in the history)."""
    rows = await _all(
        conn,
        "SELECT DISTINCT project_id FROM task WHERE tracker_ref IS NOT NULL"
        " OR tracker_url IS NOT NULL"
        " UNION SELECT DISTINCT t.project_id FROM tracker_entry e JOIN task t ON t.id = e.task_id",
        (),
    )
    return {r["project_id"] for r in rows}


async def list_unlinked_hook_sessions(
    conn: aiosqlite.Connection, limit: int, *, ended: bool = True
) -> list[Session]:
    """Hook sessions not linked to one relaybay session; without `ended`, only live ones."""
    live = "" if ended else " AND (status IS NULL OR status != 'ended')"
    rows = await _all(
        conn,
        "SELECT * FROM session WHERE unlinked_reason IS NOT NULL"
        + live
        + " ORDER BY last_seen_at DESC, id DESC LIMIT ?",
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


async def open_binding_at(conn: aiosqlite.Connection, task_id: int) -> str | None:
    row = await _one(
        conn,
        "SELECT bound_at FROM task_session WHERE task_id = ? AND unbound_at IS NULL",
        (task_id,),
    )
    return row[0] if row else None


async def rebind(
    conn: aiosqlite.Connection,
    task_id: int,
    session_id: int,
    actor: str,
    now: str,
    reason: str | None = None,
) -> None:
    """Close the task's open binding (if any, with `reason`) and open one for `session_id`."""
    await conn.execute(
        "UPDATE task_session SET unbound_at = ?, unbound_reason = ?"
        " WHERE task_id = ? AND unbound_at IS NULL",
        (now, reason, task_id),
    )
    await conn.execute(
        "INSERT INTO task_session (task_id, session_id, bound_at, bound_by) VALUES (?, ?, ?, ?)",
        (task_id, session_id, now, actor),
    )


async def unbind(
    conn: aiosqlite.Connection, task_id: int, now: str, reason: str | None = None
) -> None:
    await conn.execute(
        "UPDATE task_session SET unbound_at = ?, unbound_reason = ?"
        " WHERE task_id = ? AND unbound_at IS NULL",
        (now, reason, task_id),
    )


async def tasks_ended_with_session(conn: aiosqlite.Connection, session_id: int) -> list[Task]:
    """Open tasks whose latest binding was to this session and ended with its own end (none
    bound since, not unbound by hand)."""
    rows = await _all(
        conn,
        _TASK_SELECT + " WHERE t.session_id IS NULL AND t.stage != 'done' AND EXISTS ("
        "SELECT 1 FROM task_session b WHERE b.task_id = t.id AND b.session_id = ?"
        " AND b.unbound_reason = 'session_end'"
        " AND b.id = (SELECT MAX(b2.id) FROM task_session b2 WHERE b2.task_id = t.id))"
        " ORDER BY t.id",
        (session_id,),
    )
    return [_task(r) for r in rows]


async def tasks_last_bound_to(conn: aiosqlite.Connection, session_id: int) -> list[Task]:
    """Tasks whose latest binding is (or was) to this session."""
    rows = await _all(
        conn,
        _TASK_SELECT + " WHERE EXISTS (SELECT 1 FROM task_session b WHERE b.task_id = t.id"
        " AND b.session_id = ?"
        " AND b.id = (SELECT MAX(b2.id) FROM task_session b2 WHERE b2.task_id = t.id))"
        " ORDER BY t.id",
        (session_id,),
    )
    return [_task(r) for r in rows]


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
    area_ids: list[int] | None = None,
    by_rank: bool = False,
) -> list[Task]:
    """Most recently updated first; `by_rank` gives the board's order within each stage."""
    where: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        where.append("t.project_id = ?")
        params.append(project_id)
    if area_ids is not None:
        where.append(f"t.area_id IN ({', '.join('?' for _ in area_ids) or 'NULL'})")
        params.extend(area_ids)
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
    order = BOARD_ORDER if by_rank else "t.priority DESC, t.updated_at DESC, t.id DESC"
    sql += f" ORDER BY {order}"
    return [_task(r) for r in await _all(conn, sql, tuple(params))]


async def top_rank(conn: aiosqlite.Connection, project_id: int, stage: str) -> int:
    """A rank above every card of the column: new and moved-in tasks go to the top."""
    row = await _one(
        conn,
        "SELECT MIN(rank) FROM task WHERE project_id = ? AND stage = ?",
        (project_id, stage),
    )
    return (row[0] if row and row[0] is not None else 1) - 1


async def set_rank(conn: aiosqlite.Connection, task_id: int, rank: int) -> None:
    """Not a change of the task: its version and updated_at stay, so an open form keeps working."""
    await conn.execute("UPDATE task SET rank = ? WHERE id = ?", (rank, task_id))


async def column_order(conn: aiosqlite.Connection, project_id: int, stage: str) -> list[int]:
    """Task ids of a column in the board's order, archived done tasks included."""
    rows = await _all(
        conn,
        f"SELECT t.id FROM task t WHERE t.project_id = ? AND t.stage = ? ORDER BY {BOARD_ORDER}",
        (project_id, stage),
    )
    return [r[0] for r in rows]


async def renumber_column(conn: aiosqlite.Connection, ids: list[int]) -> None:
    """Ranks 1..n in the order of `ids`: a drop never needs a rank between two others."""
    await conn.executemany(
        "UPDATE task SET rank = ? WHERE id = ? AND rank != ?",
        [(n, task_id, n) for n, task_id in enumerate(ids, 1)],
    )


async def insert_task(
    conn: aiosqlite.Connection,
    project_id: int,
    key: str,
    fields: dict[str, Any],
    session_id: int | None,
    now: str,
) -> int:
    fields = {**fields, "rank": await top_rank(conn, project_id, fields["stage"])}
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
        # The archive marks belong to this stay in done: any stage change ends it.
        sets += ["archived_at = NULL", "archived_by = NULL", "unarchived_at = NULL"]
        sets.append("archived_from = NULL")
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


async def all_report_bodies(conn: aiosqlite.Connection) -> list[tuple[int, str]]:
    rows = await _all(conn, "SELECT id, body_md FROM report ORDER BY id")
    return [(r["id"], r["body_md"]) for r in rows]


async def get_report(conn: aiosqlite.Connection, report_id: int) -> Report | None:
    row = await _one(conn, "SELECT * FROM report WHERE id = ?", (report_id,))
    return _report(row) if row else None


async def insert_report_checks(
    conn: aiosqlite.Connection, report_id: int, checks: list[ReportCheck]
) -> None:
    await conn.executemany(
        "INSERT INTO report_check (report_id, line, location, claim) VALUES (?, ?, ?, ?)",
        [(report_id, c.line, c.where, c.claim) for c in checks],
    )


async def report_checks(
    conn: aiosqlite.Connection, report_ids: list[int]
) -> dict[int, list[ReportCheck]]:
    """The `verified:` marks of each report, by report id, in line order."""
    if not report_ids:
        return {}
    marks = ", ".join("?" for _ in report_ids)
    rows = await _all(
        conn,
        f"SELECT * FROM report_check WHERE report_id IN ({marks}) ORDER BY report_id, line, id",
        tuple(report_ids),
    )
    out: dict[int, list[ReportCheck]] = {}
    for r in rows:
        out.setdefault(r["report_id"], []).append(ReportCheck(r["line"], r["location"], r["claim"]))
    return out


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
    area: str | None = None,
) -> int:
    source = source or Source()
    cur = await conn.execute(
        "INSERT INTO decision (project_id, task_id, text, decided_by, created_at, scope,"
        " source_kind, source_ref, source_text, valid_from, recorded_at, area)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            area,
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
        area=r["area"],
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
    scope: str | None = None,
) -> int:
    where, params = _decision_filter(project_id, None, None, False, scope, since)
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


async def untold_plan_acceptances(
    conn: aiosqlite.Connection,
    session_id: int,
    actors: tuple[str, ...],
    told_kind: str,
    since: str,
) -> list[Task]:
    """Tasks bound to this session whose plan one of `actors` accepted while it was bound and
    not before `since`, and that acceptance was not told to it yet (no `told_kind` event for it
    since)."""
    marks = ", ".join("?" for _ in actors)
    rows = await _all(
        conn,
        _TASK_SELECT
        + "JOIN task_session b ON b.task_id = t.id AND b.session_id = ? AND b.unbound_at IS NULL"
        f" WHERE t.plan_status = 'accepted' AND t.plan_accepted_by IN ({marks})"
        " AND b.bound_at <= t.plan_accepted_at AND t.plan_accepted_at >= ?"
        " AND NOT EXISTS (SELECT 1 FROM event d WHERE d.task_id = t.id AND d.kind = ?"
        " AND d.session_id = ? AND d.ts >= t.plan_accepted_at)"
        " ORDER BY t.id",
        (session_id, *actors, since, told_kind, session_id),
    )
    return [_task(r) for r in rows]


async def untold_board_acceptances(
    conn: aiosqlite.Connection,
    session_id: int,
    actors: tuple[str, ...],
    told_kind: str,
    since: str,
) -> list[Task]:
    """Tasks of any project whose plan one of `actors` accepted not before `since` and that
    acceptance was not told to this session yet: what a coordinator session is told."""
    marks = ", ".join("?" for _ in actors)
    rows = await _all(
        conn,
        _TASK_SELECT + f"WHERE t.plan_status = 'accepted' AND t.plan_accepted_by IN ({marks})"
        " AND t.plan_accepted_at >= ?"
        " AND NOT EXISTS (SELECT 1 FROM event d WHERE d.task_id = t.id AND d.kind = ?"
        " AND d.session_id = ? AND d.ts >= t.plan_accepted_at)"
        " ORDER BY t.id",
        (*actors, since, told_kind, session_id),
    )
    return [_task(r) for r in rows]


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


async def waiting_owner_comments(
    conn: aiosqlite.Connection, before: str, limit: int = 200
) -> list[Comment]:
    """Comments for the agent written before `before` and still not delivered, oldest first."""
    rows = await _all(
        conn,
        _COMMENT_SELECT + " WHERE c.author_kind != 'agent' AND c.delivered_at IS NULL"
        " AND c.read_at IS NULL AND c.created_at < ? ORDER BY c.id LIMIT ?",
        (before, limit),
    )
    return [_comment(r) for r in rows]


async def unread_agent_replies(conn: aiosqlite.Connection, limit: int = 200) -> list[Comment]:
    """Agent replies the owner has not marked read, oldest first. On a done card only those
    written after it was closed: the close marked the older ones read."""
    rows = await _all(
        conn,
        _COMMENT_SELECT + " WHERE c.author_kind = 'agent' AND c.read_at IS NULL"
        " AND (t.stage != 'done' OR t.closed_at IS NULL OR c.created_at > t.closed_at)"
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
    archived_projects: bool = True,
) -> list[tuple[int | None, str | None, str | None, str, int, int]]:
    """Events in (after_id, upto_id] counted per task or project and kind: (task id, task key or
    project slug, task title, kind, count, last id). `own_kinds` are left out for one session,
    and so is every automatic change (`how: auto` in the payload, e.g. a hook's repository link).
    `archived_projects=False` leaves out the events of projects in the archive."""
    where = [
        "e.id > ?",
        "e.id <= ?",
        f"e.kind NOT IN ({', '.join('?' for _ in exclude_kinds)})",
        "json_extract(e.payload, '$.how') IS NOT 'auto'",
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
    if not archived_projects:
        where.append("p.archived_at IS NULL")
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
        area=row["area"],
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
        "INSERT INTO saved_filter (project_id, name, stage, area, waiting_on, session, q,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            name,
            query.get("stage"),
            query.get("area"),
            query.get("waiting_on"),
            query.get("session"),
            query.get("q"),
            now,
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


async def set_saved_filter_area(
    conn: aiosqlite.Connection, filter_id: int, area: str | None
) -> None:
    await conn.execute("UPDATE saved_filter SET area = ? WHERE id = ?", (area, filter_id))


async def delete_saved_filter(conn: aiosqlite.Connection, filter_id: int) -> None:
    await conn.execute("DELETE FROM saved_filter WHERE id = ?", (filter_id,))


# --- quick switcher --------------------------------------------------------


async def switcher_tasks(
    conn: aiosqlite.Connection, limit: int
) -> list[tuple[str, str, str, str, str | None, str | None]]:
    """(key, title, stage, project slug, area, area colour) of the most recently changed tasks."""
    rows = await _all(
        conn,
        "SELECT t.key, t.title, t.stage, p.slug, pa.name AS area, pa.color AS area_color"
        " FROM task t JOIN project p ON p.id = t.project_id"
        " LEFT JOIN project_area pa ON pa.id = t.area_id"
        " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?",
        (limit,),
    )
    return [(r["key"], r["title"], r["stage"], r["slug"], r["area"], r["area_color"]) for r in rows]


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
        source_path=row["source_path"],
        file_hash=row["file_hash"],
        file_size=row["file_size"],
        file_mtime=row["file_mtime"],
        file_seen_at=row["file_seen_at"],
        file_missing_at=row["file_missing_at"],
        masked_lines=row["masked_lines"],
        file_changed_by=row["file_changed_by"],
        file_changed_at=row["file_changed_at"],
        file_changed_sections=row["file_changed_sections"],
        file_writable=bool(row["file_writable"]),
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


async def list_file_document_paths(
    conn: aiosqlite.Connection, project_id: int
) -> list[tuple[str, str]]:
    """(slug, source_path) of the project's file documents, by slug."""
    rows = await _all(
        conn,
        "SELECT slug, source_path FROM document WHERE project_id = ? AND source_path IS NOT NULL"
        " ORDER BY slug",
        (project_id,),
    )
    return [(r["slug"], r["source_path"]) for r in rows]


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
    source_path: str | None = None,
    file_writable: bool = False,
) -> int:
    """A document without versions yet (version 0); `add_document_version` gives it text. A
    file document (`source_path`) has no stored versions: its version counts its file's hashes
    and starts at 1."""
    cur = await conn.execute(
        "INSERT INTO document (project_id, slug, title, kind, version, created_at, updated_at,"
        " source_url, source_path, file_writable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            slug,
            title,
            kind,
            0 if source_path is None else 1,
            now,
            now,
            source_url,
            source_path,
            int(file_writable),
        ),
    )
    return int(cur.lastrowid or 0)


async def set_file_state(
    conn: aiosqlite.Connection,
    document_id: int,
    *,
    file_hash: str | None,
    size: int | None,
    mtime: float | None,
    seen_at: str,
    masked_lines: int | None = None,
    changed_by: str | None = None,
    changed_sections: str | None = None,
) -> None:
    """What a read or a write of a file document saw; the file is there again if it was
    missing. `changed_by` ('relaybay' or 'outside') records a new hash: the version moves on
    and the change is named."""
    changed = changed_by is not None
    await conn.execute(
        "UPDATE document SET file_hash = ?, file_size = ?, file_mtime = ?, file_seen_at = ?,"
        " file_missing_at = NULL, masked_lines = coalesce(?, masked_lines),"
        " updated_at = CASE WHEN ? THEN ? ELSE updated_at END,"
        " version = version + ?,"
        " file_changed_by = CASE WHEN ? THEN ? ELSE file_changed_by END,"
        " file_changed_at = CASE WHEN ? THEN ? ELSE file_changed_at END,"
        " file_changed_sections = CASE WHEN ? THEN ? ELSE file_changed_sections END"
        " WHERE id = ?",
        (
            file_hash,
            size,
            mtime,
            seen_at,
            masked_lines,
            int(changed),
            seen_at,
            int(changed),
            int(changed),
            changed_by,
            int(changed),
            seen_at,
            int(changed),
            changed_sections,
            document_id,
        ),
    )


async def set_file_writable(conn: aiosqlite.Connection, document_id: int, on: bool) -> None:
    await conn.execute("UPDATE document SET file_writable = ? WHERE id = ?", (int(on), document_id))


async def set_file_missing(conn: aiosqlite.Connection, document_id: int, now: str) -> None:
    await conn.execute(
        "UPDATE document SET file_missing_at = coalesce(file_missing_at, ?) WHERE id = ?",
        (now, document_id),
    )


async def set_document_meta(
    conn: aiosqlite.Connection, document_id: int, title: str, kind: str, now: str
) -> None:
    await conn.execute(
        "UPDATE document SET title = ?, kind = ?, updated_at = ? WHERE id = ?",
        (title, kind, now, document_id),
    )


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
    return [ImportItem(**{**dict(r), "bom": bool(r["bom"])}) for r in rows]


async def save_import_item(conn: aiosqlite.Connection, item: ImportItem) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO import_item (kind, item, source_path, target_id, content_hash,"
        " target_hash, imported_at, eol, bom, lead_blank, final_newlines)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item.kind,
            item.item,
            item.source_path,
            item.target_id,
            item.content_hash,
            item.target_hash,
            item.imported_at,
            item.eol,
            int(item.bom),
            item.lead_blank,
            item.final_newlines,
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
    # The words of a registry row: its text, where in the code, the source and the decision.
    "backlog": (
        "SELECT 'backlog' AS kind, b.id AS id, b.project_id AS project_id,"
        " p.slug AS project_slug, t.key AS task_key, NULL AS name, b.title AS title,"
        " trim(coalesce(b.body_md, '') || char(10) || coalesce(b.location, '') || char(10)"
        " || coalesce(b.origin_text, '') || char(10) || coalesce(b.triage_note, '') || char(10)"
        " || coalesce(b.decline_reason, ''), char(10)) AS body,"
        " NULL AS superseded_at, NULL AS note_scope, NULL AS note_kind"
        " FROM backlog_item b JOIN project p ON p.id = b.project_id"
        " LEFT JOIN task t ON t.id = coalesce(b.task_id, b.origin_task_id)"
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


# --- the section index of documents (design 0026) ----------------------------------------

_SECTION_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS doc_section_index USING fts5(document_id UNINDEXED,"
    " position UNINDEXED, project_id UNINDEXED, title, body, stems,"
    " tokenize = 'unicode61 remove_diacritics 2')"
)
# Without FTS5 the same rows go to a plain table that the LIKE fallback reads.
_SECTION_PLAIN = (
    "CREATE TABLE IF NOT EXISTS doc_section_text (id INTEGER PRIMARY KEY, document_id INTEGER,"
    " position INTEGER, project_id INTEGER, title TEXT, body TEXT, stems TEXT)"
)
# bm25 weights per column (document_id, position, project_id, title, body, stems).
_SECTION_RANK = "bm25(doc_section_index, 0, 0, 0, 10.0, 1.0, 0.5)"


def _section_table(fts: bool) -> str:
    return "doc_section_index" if fts else "doc_section_text"


async def ensure_section_index(conn: aiosqlite.Connection, fts: bool) -> None:
    """The searchable text of the sections: FTS5 when search has it, a plain table otherwise."""
    if fts:
        await conn.execute(_SECTION_FTS)
        await conn.execute("SELECT 1 FROM doc_section_index LIMIT 0")
        return
    await conn.execute(_SECTION_PLAIN)


def _indexed(row: aiosqlite.Row) -> IndexedSection:
    return IndexedSection(
        position=row["position"],
        anchor=row["anchor"],
        alias=row["alias"],
        level=row["level"],
        title=row["title"],
        heading_path=row["heading_path"],
        first_line=row["first_line"],
        last_line=row["last_line"],
        own_last=row["own_last"],
        chars=row["chars"],
        bytes=row["bytes"],
        hash=row["hash"],
    )


async def list_sections(conn: aiosqlite.Connection, document_id: int) -> list[IndexedSection]:
    rows = await _all(
        conn,
        "SELECT * FROM document_section WHERE document_id = ? ORDER BY position",
        (document_id,),
    )
    return [_indexed(r) for r in rows]


async def replace_sections(
    conn: aiosqlite.Connection,
    document_id: int,
    project_id: int,
    title: str,
    rows: list[IndexedSection],
    fts: bool,
) -> None:
    """The document's index rows and their searchable text, in the caller's transaction."""
    table = _section_table(fts)
    key = "rowid" if fts else "id"
    await conn.execute(
        f"DELETE FROM {table} WHERE {key} IN"
        " (SELECT id FROM document_section WHERE document_id = ?)",
        (document_id,),
    )
    await conn.execute("DELETE FROM document_section WHERE document_id = ?", (document_id,))
    for r in rows:
        cur = await conn.execute(
            "INSERT INTO document_section (document_id, position, anchor, alias, level, title,"
            " heading_path, first_line, last_line, own_last, chars, bytes, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                document_id,
                r.position,
                r.anchor,
                r.alias,
                r.level,
                r.title,
                r.heading_path,
                r.first_line,
                r.last_line,
                r.own_last,
                r.chars,
                r.bytes,
                r.hash,
            ),
        )
        heading = r.heading_path or title
        await conn.execute(
            f"INSERT INTO {table} ({key}, document_id, position, project_id, title, body, stems)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cur.lastrowid,
                document_id,
                r.position,
                project_id,
                heading,
                r.text,
                stem_text(f"{heading} {r.text}"),
            ),
        )


@dataclass(frozen=True, slots=True)
class TaskDocSection:
    """A section linked to a card: the document by slug and the anchor it was linked with."""

    id: int
    task_id: int
    document_id: int
    doc: str
    anchor: str
    created_by: str
    created_at: str


async def list_task_doc_sections(conn: aiosqlite.Connection, task_id: int) -> list[TaskDocSection]:
    rows = await _all(
        conn,
        "SELECT s.*, d.slug AS doc FROM task_doc_section s JOIN document d ON d.id = s.document_id"
        " WHERE s.task_id = ? ORDER BY s.id",
        (task_id,),
    )
    return [
        TaskDocSection(
            r["id"],
            r["task_id"],
            r["document_id"],
            r["doc"],
            r["anchor"],
            r["created_by"],
            r["created_at"],
        )
        for r in rows
    ]


async def list_project_doc_sections(
    conn: aiosqlite.Connection, project_id: int
) -> list[tuple[int, str, str]]:
    """(task id, document slug, anchor) of every section linked to a card of the project."""
    rows = await _all(
        conn,
        "SELECT s.task_id, d.slug, s.anchor FROM task_doc_section s"
        " JOIN document d ON d.id = s.document_id WHERE d.project_id = ? ORDER BY s.id",
        (project_id,),
    )
    return [(r[0], r[1], r[2]) for r in rows]


async def insert_task_doc_section(
    conn: aiosqlite.Connection, task_id: int, document_id: int, anchor: str, by: str, now: str
) -> bool:
    """False when the card already links that section."""
    cur = await conn.execute(
        "INSERT OR IGNORE INTO task_doc_section (task_id, document_id, anchor, created_by,"
        " created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, document_id, anchor, by, now),
    )
    return cur.rowcount > 0


async def delete_task_doc_section(
    conn: aiosqlite.Connection, task_id: int, document_id: int, anchor: str
) -> bool:
    cur = await conn.execute(
        "DELETE FROM task_doc_section WHERE task_id = ? AND document_id = ? AND anchor = ?",
        (task_id, document_id, anchor),
    )
    return cur.rowcount > 0


async def section_index_stale(conn: aiosqlite.Connection, fts: bool) -> bool:
    """The searchable rows do not match the index rows, or a database document with text has
    no index rows (an upgrade from before the index)."""
    rows = (await _one(conn, "SELECT count(*) FROM document_section", ()))[0]
    text = (await _one(conn, f"SELECT count(*) FROM {_section_table(fts)}", ()))[0]
    missing = await _one(
        conn,
        "SELECT count(*) FROM document d WHERE d.source_path IS NULL AND d.version > 0"
        " AND NOT EXISTS (SELECT 1 FROM document_section s WHERE s.document_id = d.id)",
        (),
    )
    return bool(rows != text or missing[0])


async def clear_section_index(conn: aiosqlite.Connection, fts: bool) -> None:
    """Empty the index; file documents are read and indexed again at their next read, which
    finds their hash unchanged and records no change."""
    await conn.execute(f"DELETE FROM {_section_table(fts)}")
    await conn.execute("DELETE FROM document_section")
    await conn.execute("UPDATE document SET file_size = NULL WHERE source_path IS NOT NULL")


async def count_sections(conn: aiosqlite.Connection, document_id: int) -> int:
    row = await _one(
        conn, "SELECT count(*) FROM document_section WHERE document_id = ?", (document_id,)
    )
    return int(row[0])


async def database_documents_with_text(
    conn: aiosqlite.Connection,
) -> list[tuple[int, int, str, str]]:
    """(id, project_id, title, current text) of every database document that has a version."""
    rows = await _all(
        conn,
        "SELECT d.id, d.project_id, d.title, v.body FROM document d"
        " JOIN document_version v ON v.document_id = d.id AND v.version = d.version"
        " WHERE d.source_path IS NULL ORDER BY d.id",
    )
    return [(r["id"], r["project_id"], r["title"], r["body"]) for r in rows]


@dataclass(frozen=True, slots=True)
class SectionHit:
    document_id: int
    position: int
    body: str  # the section's own text as indexed


async def search_sections(
    conn: aiosqlite.Connection,
    words: list[str],
    match: str | None,
    project_id: int,
    document_id: int | None,
    limit: int,
) -> list[SectionHit]:
    """The best sections for the words, best first: by FTS5 rank with `match`, else every word
    (or its stem) as a substring of the heading path or the text, in document order."""
    where = " AND project_id = ?"
    params: list[Any] = [project_id]
    if document_id is not None:
        where += " AND document_id = ?"
        params.append(document_id)
    if match is not None:
        rows = await _all(
            conn,
            "SELECT document_id, position, body FROM doc_section_index"
            f" WHERE doc_section_index MATCH ?{where} ORDER BY {_SECTION_RANK} LIMIT ?",
            (match, *params, limit),
        )
    else:
        one = "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
        cond = " AND ".join(f"({one} OR {one})" for _ in words)
        likes = [p for w in words for n in (w, stem(w)) for p in (_like(n), _like(n))]
        rows = await _all(
            conn,
            f"SELECT document_id, position, body FROM doc_section_text WHERE ({cond}){where}"
            " ORDER BY document_id, position LIMIT ?",
            (*likes, *params, limit),
        )
    return [SectionHit(r["document_id"], r["position"], r["body"]) for r in rows]


async def document_events(
    conn: aiosqlite.Connection, project_id: int, slug: str, limit: int = 200
) -> list[Event]:
    """`document.updated` events of one document, newest first."""
    rows = await _all(
        conn,
        "SELECT * FROM event WHERE kind = 'document.updated' AND project_id = ?"
        " AND json_extract(payload, '$.doc') = ? ORDER BY id DESC LIMIT ?",
        (project_id, slug, limit),
    )
    return [_event(r) for r in rows]


# --- sessions at once ---------------------------------------------------------


async def move_open_bindings(
    conn: aiosqlite.Connection, from_session: int, to_session: int
) -> None:
    await conn.execute(
        "UPDATE task_session SET session_id = ? WHERE session_id = ? AND unbound_at IS NULL",
        (to_session, from_session),
    )
    await conn.execute(
        "UPDATE task SET session_id = ? WHERE session_id = ?", (to_session, from_session)
    )


async def running_sessions(conn: aiosqlite.Connection, since: str) -> int:
    row = await _one(
        conn,
        "SELECT COUNT(*) FROM session WHERE status = 'working' AND last_seen_at >= ?",
        (since,),
    )
    return int(row[0]) if row else 0


async def purge_event(conn: aiosqlite.Connection, key: str) -> Event | None:
    """The `task.purged` event of the task with this key (keys are never given out again)."""
    row = await _one(
        conn,
        "SELECT * FROM event WHERE kind = 'task.purged'"
        " AND lower(json_extract(payload, '$.key')) = ? ORDER BY id DESC LIMIT 1",
        (key,),
    )
    return _event(row) if row else None


async def last_event_of_kind(conn: aiosqlite.Connection, kind: str) -> str | None:
    row = await _one(conn, "SELECT ts FROM event WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))
    return row[0] if row else None


# --- archive ---------------------------------------------------------------


async def set_archive_mark(
    conn: aiosqlite.Connection,
    task_id: int,
    archived_at: str | None,
    archived_by: str | None,
    unarchived_at: str | None,
    archived_from: str | None = None,
) -> None:
    """The hand archive mark of a done task; no version bump, like the answer mark."""
    await conn.execute(
        "UPDATE task SET archived_at = ?, archived_by = ?, unarchived_at = ?, archived_from = ?"
        " WHERE id = ?",
        (archived_at, archived_by, unarchived_at, archived_from, task_id),
    )


# --- deleting from the archive ------------------------------------------------

_CHUNK = 500  # ids per IN (...): well under SQLite's bound-variable limit
# Where each kind of the search index keeps its items.
_SEARCH_TABLES = {
    "doc": "document",
    "decision": "decision",
    "note": "memory",
    "report": "report",
    "comment": "comment",
    "backlog": "backlog_item",
}


async def purgeable_task_ids(
    conn: aiosqlite.Connection, cutoff: str, task_id: int | None = None
) -> list[int]:
    """Done tasks archived by hand (or from live) before `cutoff` and not brought back since
    (only `task_id`, when given). The done window's archive by age sets no mark, so it never
    makes a task purgeable."""
    only = " AND id = ?" if task_id is not None else ""
    rows = await _all(
        conn,
        "SELECT id FROM task WHERE stage = 'done' AND archived_at IS NOT NULL"
        " AND archived_at < ? AND (unarchived_at IS NULL OR unarchived_at < archived_at)"
        f"{only} ORDER BY id",
        (cutoff, *(() if task_id is None else (task_id,))),
    )
    return [r[0] for r in rows]


async def _cascade_children(conn: aiosqlite.Connection) -> dict[str, list[tuple[str, str]]]:
    """parent table -> (child table, key column) of every ON DELETE CASCADE foreign key."""
    tables = await _all(
        conn,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
        " AND name NOT LIKE 'sqlite_%'",
    )
    children: dict[str, list[tuple[str, str]]] = {}
    for (name,) in tables:
        for fk in await _all(conn, "SELECT * FROM pragma_foreign_key_list(?)", (name,)):
            if fk["on_delete"] == "CASCADE":
                children.setdefault(fk["table"], []).append((name, fk["from"]))
    return children


async def cascade_rows(
    conn: aiosqlite.Connection, table: str, ids: list[int]
) -> dict[str, list[dict[str, Any]]]:
    """The rows of `table` with these ids and every row a DELETE of them takes along through
    ON DELETE CASCADE, by table: what the export before a deletion holds. Read from the
    schema, so a table a later migration adds is included without a change here."""
    children = await _cascade_children(conn)
    found: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[int]] = {}
    queue: list[tuple[str, str, list[int]]] = [(table, "id", ids)]
    while queue:
        name, column, keys = queue.pop(0)
        new_ids: list[int] = []
        for start in range(0, len(keys), _CHUNK):
            part = keys[start : start + _CHUNK]
            marks = ", ".join("?" for _ in part)
            rows = await _all(
                conn, f"SELECT * FROM {name} WHERE {column} IN ({marks})", tuple(part)
            )
            for row in rows:
                item = dict(row)
                row_id = item.get("id")
                if row_id is not None:
                    if row_id in seen.setdefault(name, set()):
                        continue
                    seen[name].add(row_id)
                    new_ids.append(row_id)
                found.setdefault(name, []).append(item)
        if new_ids:
            queue += [(child, key, new_ids) for child, key in children.get(name, [])]
    return found


async def attachments_of(
    conn: aiosqlite.Connection, *, task_id: int | None = None, project_id: int | None = None
) -> list[dict[str, Any]]:
    """The attachment rows of one task or of every task of a project, with the task's key."""
    column, value = ("t.id", task_id) if task_id is not None else ("t.project_id", project_id)
    rows = await _all(
        conn,
        "SELECT a.*, t.key AS task_key FROM attachment a JOIN task t ON t.id = a.task_id"
        f" WHERE {column} = ? ORDER BY a.id",
        (value,),
    )
    return [dict(r) for r in rows]


async def events_of_tasks(conn: aiosqlite.Connection, task_ids: list[int]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for start in range(0, len(task_ids), _CHUNK):
        part = task_ids[start : start + _CHUNK]
        marks = ", ".join("?" for _ in part)
        rows = await _all(
            conn, f"SELECT * FROM event WHERE task_id IN ({marks}) ORDER BY id", tuple(part)
        )
        found += [dict(r) for r in rows]
    return found


# A task decision that replaced a decision outside the task: deleting it would leave that one
# superseded by nothing, so the topic had no current decision.
_SUPERSEDED_ELSEWHERE = (
    "id IN (SELECT superseded_by FROM decision WHERE superseded_by IS NOT NULL"
    " AND task_id IS NOT ?)"
)
# The table each kind of import_item points into (`target_id`, no foreign key).
_IMPORT_TABLES = {
    "document": "document",
    "task": "task",
    "task_note": "task",
    "decision": "decision",
    "note": "memory",
    "backlog": "backlog_item",
}


async def task_decisions(conn: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    """The decisions deleted with the task: those recorded for it (scope `task`), except one
    that superseded a decision outside it, which stays as a project decision."""
    rows = await _all(
        conn,
        "SELECT * FROM decision WHERE task_id = ? AND scope = 'task'"
        f" AND NOT {_SUPERSEDED_ELSEWHERE} ORDER BY id",
        (task_id, task_id),
    )
    return [dict(r) for r in rows]


async def delete_task(conn: aiosqlite.Connection, task_id: int) -> None:
    """Delete one task: its task decisions here, its events keep their text without the link,
    the rest goes with the foreign keys (cascade, or the link cleared as in a backlog item)."""
    await conn.execute(
        "UPDATE decision SET task_id = NULL, scope = 'project'"
        f" WHERE task_id = ? AND scope = 'task' AND {_SUPERSEDED_ELSEWHERE}",
        (task_id, task_id),
    )
    await conn.execute("DELETE FROM decision WHERE task_id = ? AND scope = 'task'", (task_id,))
    await conn.execute("UPDATE event SET task_id = NULL WHERE task_id = ?", (task_id,))
    await conn.execute("DELETE FROM reader_cursor WHERE level = 'task' AND ref_id = ?", (task_id,))
    await conn.execute("DELETE FROM task WHERE id = ?", (task_id,))


async def project_events(conn: aiosqlite.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = await _all(
        conn,
        "SELECT * FROM event WHERE project_id = ?"
        " OR task_id IN (SELECT id FROM task WHERE project_id = ?) ORDER BY id",
        (project_id, project_id),
    )
    return [dict(r) for r in rows]


async def rule_seen_rows(conn: aiosqlite.Connection, project_id: int) -> list[dict[str, Any]]:
    rows = await _all(conn, "SELECT * FROM rule_seen WHERE project_id = ?", (project_id,))
    return [dict(r) for r in rows]


async def delete_project(conn: aiosqlite.Connection, project_id: int) -> None:
    """Delete a project with everything of it: its events too (they are in the export), so
    no digest or feed shows a project that is gone, and the rows keyed by its id without a
    foreign key, which a new project could inherit (ids are reused)."""
    await conn.execute(
        "DELETE FROM event WHERE project_id = ?"
        " OR task_id IN (SELECT id FROM task WHERE project_id = ?)",
        (project_id, project_id),
    )
    await conn.execute(
        "DELETE FROM reader_cursor WHERE (level = 'project' AND ref_id = ?)"
        " OR (level = 'task' AND ref_id IN (SELECT id FROM task WHERE project_id = ?))",
        (project_id, project_id),
    )
    await conn.execute("DELETE FROM rule_seen WHERE project_id = ?", (project_id,))
    await conn.execute("DELETE FROM project WHERE id = ?", (project_id,))


async def delete_document(conn: aiosqlite.Connection, document_id: int) -> None:
    """Its versions, sections and card section links go with the foreign keys; the search rows
    have none (`unindex`)."""
    await conn.execute("DELETE FROM document WHERE id = ?", (document_id,))


def _ids(rows: dict[str, list[dict[str, Any]]], table: str) -> list[int]:
    return [r["id"] for r in rows.get(table, [])]


async def import_rows(
    conn: aiosqlite.Connection, rows: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """The import records whose target is among the deleted `rows`: left behind, a later item
    that gets the same id would count as imported."""
    found: list[dict[str, Any]] = []
    for kind, table in _IMPORT_TABLES.items():
        ids = _ids(rows, table)
        for start in range(0, len(ids), _CHUNK):
            part = ids[start : start + _CHUNK]
            marks = ", ".join("?" for _ in part)
            found += [
                dict(r)
                for r in await _all(
                    conn,
                    f"SELECT * FROM import_item WHERE kind = ? AND target_id IN ({marks})",
                    (kind, *part),
                )
            ]
    return found


async def forget_imports(conn: aiosqlite.Connection, items: list[dict[str, Any]]) -> None:
    await conn.executemany(
        "DELETE FROM import_item WHERE kind = ? AND item = ?",
        [(i["kind"], i["item"]) for i in items],
    )


async def unindex(
    conn: aiosqlite.Connection, rows: dict[str, list[dict[str, Any]]], fts: bool
) -> None:
    """Drop the search rows of the deleted `rows`: the indexes have no foreign keys, so a
    deletion leaves them behind. By rowid, which the index derives from kind and id."""
    if fts:
        rowids = [
            _search_rowid(kind, item_id)
            for kind, table in _SEARCH_TABLES.items()
            for item_id in _ids(rows, table)
        ]
        await conn.executemany("DELETE FROM search_index WHERE rowid = ?", [(r,) for r in rowids])
    key = "rowid" if fts else "id"
    await conn.executemany(
        f"DELETE FROM {_section_table(fts)} WHERE {key} = ?",
        [(i,) for i in _ids(rows, "document_section")],
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
        kind=row["kind"],
        location=row["location"],
        origin_text=row["origin_text"],
        triage_note=row["triage_note"],
        duplicate_of=row["duplicate_of"],
        found_on=row["found_on"],
        seen_at=row["seen_at"],
    )


async def mark_backlog_seen(conn: aiosqlite.Connection, ids: list[int], at: str) -> int:
    """Record that the owner saw these items; an item seen before keeps its first time."""
    cur = await conn.executemany(
        "UPDATE backlog_item SET seen_at = ? WHERE id = ? AND seen_at IS NULL",
        [(at, i) for i in ids],
    )
    return cur.rowcount


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


async def backlog_duplicates_of(conn: aiosqlite.Connection, item_id: int) -> list[BacklogItem]:
    rows = await _all(conn, _BACKLOG_SELECT + " WHERE b.duplicate_of = ? ORDER BY b.id", (item_id,))
    return [_backlog(r) for r in rows]


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
    kind: str | None = None,
    since: str | None = None,
    either_task_id: int | None = None,
    has_location: bool | None = None,
    ids: list[int] | None = None,
    limit: int = 200,
) -> list[BacklogItem]:
    """`new` first, then later, planned and done, declined; within a status the highest priority
    first, then the newest. `assignee` "-" means items that are for nobody, `kind` "-" items
    without a kind. `since` is a day: items found (or else created) on it or later.
    `either_task_id`: items found in that task or planned as it."""
    where: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("b.project_id", project_id),
        ("b.status", status),
        ("b.origin_task_id", origin_task_id),
        ("b.task_id", task_id),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    if kind == NO_KIND:
        where.append("b.kind IS NULL")
    elif kind is not None:
        where.append("b.kind = ?")
        params.append(kind)
    if since is not None:
        where.append("substr(coalesce(b.found_on, b.created_at), 1, 10) >= ?")
        params.append(since)
    if either_task_id is not None:
        where.append("(b.task_id = ? OR b.origin_task_id = ?)")
        params.extend((either_task_id, either_task_id))
    if has_location is not None:
        where.append("b.location IS NOT NULL" if has_location else "b.location IS NULL")
    if ids is not None:
        where.append(f"b.id IN ({', '.join('?' for _ in ids) or 'NULL'})")
        params.extend(ids)
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


async def list_open_backlog(
    conn: aiosqlite.Connection, project_id: int, limit: int = 1000
) -> list[BacklogItem]:
    """The project's items with status new, later or planned: highest priority, then newest."""
    rows = await _all(
        conn,
        _BACKLOG_SELECT + " WHERE b.project_id = ? AND b.status IN ('new', 'later', 'planned')"
        " ORDER BY b.priority DESC, b.id DESC LIMIT ?",
        (project_id, limit),
    )
    return [_backlog(r) for r in rows]


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


# --- app settings ------------------------------------------------------------


async def get_app_setting(conn: aiosqlite.Connection, key: str) -> str | None:
    row = await _one(conn, "SELECT value FROM app_setting WHERE key = ?", (key,))
    return row[0] if row else None


async def set_app_setting(
    conn: aiosqlite.Connection, key: str, value: str, now: str, actor: str
) -> None:
    await conn.execute(
        "INSERT INTO app_setting (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (key, value, now, actor),
    )


async def delete_app_setting(conn: aiosqlite.Connection, key: str) -> bool:
    cur = await conn.execute("DELETE FROM app_setting WHERE key = ?", (key,))
    return cur.rowcount > 0


# --- questions ---------------------------------------------------------------

# LEFT JOINs: the coordinator's questions have no card.
_QUESTION_SELECT = """
SELECT q.*, COALESCE(t.key, '') AS task_key, COALESCE(t.title, '') AS task_title,
       COALESCE(p.slug, '') AS project_slug
FROM question q
LEFT JOIN task t ON t.id = q.task_id
LEFT JOIN project p ON p.id = t.project_id
"""


def _question(row: aiosqlite.Row) -> Question:
    return Question(
        id=row["id"],
        task_id=row["task_id"],
        session_id=row["session_id"],
        asked_by=row["asked_by"],
        kind=row["kind"],
        text=row["text"],
        options=json.loads(row["options"] or "[]"),
        default=row["default_answer"],
        blocking=bool(row["blocking"]),
        look_url=row["look_url"],
        look_account=row["look_account"],
        default_after=row["default_after"],
        asked_at=row["asked_at"],
        status=row["status"],
        answer=row["answer"],
        answered_by=row["answered_by"],
        answered_at=row["answered_at"],
        defaulted_at=row["defaulted_at"],
        withdrawn_at=row["withdrawn_at"],
        withdrawn_by=row["withdrawn_by"],
        comment_id=row["comment_id"],
        delivered_at=row["delivered_at"],
        task_key=row["task_key"],
        task_title=row["task_title"],
        project_slug=row["project_slug"],
    )


async def insert_question(conn: aiosqlite.Connection, values: dict[str, Any], now: str) -> int:
    row = {**values, "options": json.dumps(values.get("options") or []), "asked_at": now}
    row["status"] = "open"
    columns = ", ".join(row)
    cur = await conn.execute(
        f"INSERT INTO question ({columns}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )
    return int(cur.lastrowid or 0)


async def get_question(conn: aiosqlite.Connection, question_id: int) -> Question | None:
    row = await _one(conn, _QUESTION_SELECT + " WHERE q.id = ?", (question_id,))
    return _question(row) if row else None


async def list_questions(
    conn: aiosqlite.Connection,
    *,
    task_id: int | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[Question]:
    """Newest first."""
    where, params = [], []
    if task_id is not None:
        where.append("q.task_id = ?")
        params.append(task_id)
    if statuses:
        where.append(f"q.status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    sql = _QUESTION_SELECT + (f" WHERE {' AND '.join(where)}" if where else "")
    rows = await _all(conn, sql + " ORDER BY q.id DESC LIMIT ?", (*params, limit))
    return [_question(r) for r in rows]


async def count_open_questions(conn: aiosqlite.Connection, task_id: int | None) -> int:
    """Open questions of a card; None: the open questions without a card (the coordinator's)."""
    if task_id is None:
        sql, params = "task_id IS NULL", ()
    else:
        sql, params = "task_id = ?", (task_id,)
    row = await _one(conn, f"SELECT COUNT(*) FROM question WHERE {sql} AND status = 'open'", params)
    return int(row[0])


async def update_question(
    conn: aiosqlite.Connection, question_id: int, changes: dict[str, Any]
) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(f"UPDATE question SET {sets} WHERE id = ?", (*changes.values(), question_id))


async def open_blocking_questions(conn: aiosqlite.Connection, *, task_id: int) -> list[Question]:
    rows = await _all(
        conn,
        _QUESTION_SELECT + " WHERE q.task_id = ? AND q.status = 'open' AND q.blocking = 1"
        " ORDER BY q.id",
        (task_id,),
    )
    return [_question(r) for r in rows]


async def overdue_questions(conn: aiosqlite.Connection, now: str) -> list[Question]:
    rows = await _all(
        conn,
        _QUESTION_SELECT
        + " WHERE q.status = 'open' AND q.blocking = 0 AND q.default_after <= ? ORDER BY q.id",
        (now,),
    )
    return [_question(r) for r in rows]


async def overdue_unmarked(
    conn: aiosqlite.Connection, now: str, asked_before: str
) -> list[Question]:
    """Questions past their deadline without a `question.overdue` yet: a non-blocking one at its
    `default_after`, a blocking one (it has none) once asked before `asked_before`."""
    rows = await _all(
        conn,
        _QUESTION_SELECT + " WHERE q.status = 'open' AND q.overdue_at IS NULL AND"
        " ((q.blocking = 0 AND q.default_after <= ?) OR (q.blocking = 1 AND q.asked_at <= ?))"
        " ORDER BY q.id",
        (now, asked_before),
    )
    return [_question(r) for r in rows]


async def undelivered_answers_of_comments(
    conn: aiosqlite.Connection, comment_ids: list[int]
) -> list[Question]:
    marks = ", ".join("?" for _ in comment_ids)
    rows = await _all(
        conn,
        _QUESTION_SELECT
        + f" WHERE q.comment_id IN ({marks}) AND q.delivered_at IS NULL ORDER BY q.id",
        tuple(comment_ids),
    )
    return [_question(r) for r in rows]


# --- approvals ---------------------------------------------------------------------------------

_APPROVAL_SELECT = """
SELECT a.*, t.key AS task_key, t.title AS task_title, p.slug AS project_slug,
       t.deploy_notes AS task_deploy_notes, t.deploy_done AS task_deploy_done
FROM approval a
JOIN task t ON t.id = a.task_id
JOIN project p ON p.id = t.project_id
"""


def _approval(row: aiosqlite.Row) -> Approval:
    return Approval(
        id=row["id"],
        task_id=row["task_id"],
        session_id=row["session_id"],
        requested_by=row["requested_by"],
        requested_via=row["requested_via"],
        action=row["action"],
        mode=row["mode"],
        repository=row["repository"],
        branch=row["branch"],
        pr=row["pr"],
        environment_id=row["environment_id"],
        environment=row["environment"],
        summary=row["summary"],
        rollback=row["rollback"],
        requested_at=row["requested_at"],
        status=row["status"],
        decision=row["decision"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        decision_text=row["decision_text"],
        valid_until=row["valid_until"],
        used_at=row["used_at"],
        used_by=row["used_by"],
        used_session=row["used_session"],
        expired_at=row["expired_at"],
        expired_reason=row["expired_reason"],
        comment_id=row["comment_id"],
        delivered_at=row["delivered_at"],
        reserved_at=row["reserved_at"],
        reserved_for=row["reserved_for"],
        reserved_call=row["reserved_call"],
        reserved_session_id=row["reserved_session_id"],
        released_count=row["released_count"],
        task_key=row["task_key"],
        task_title=row["task_title"],
        project_slug=row["project_slug"],
        task_deploy_notes=row["task_deploy_notes"],
        task_deploy_done=bool(row["task_deploy_done"]),
    )


async def insert_approval(conn: aiosqlite.Connection, values: dict[str, Any], now: str) -> int:
    row = {**values, "requested_at": now, "status": "open"}
    columns = ", ".join(row)
    cur = await conn.execute(
        f"INSERT INTO approval ({columns}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )
    return int(cur.lastrowid or 0)


async def get_approval(conn: aiosqlite.Connection, approval_id: int) -> Approval | None:
    row = await _one(conn, _APPROVAL_SELECT + " WHERE a.id = ?", (approval_id,))
    return _approval(row) if row else None


async def list_approvals(
    conn: aiosqlite.Connection,
    *,
    task_id: int | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[Approval]:
    """Newest first."""
    where, params = [], []
    if task_id is not None:
        where.append("a.task_id = ?")
        params.append(task_id)
    if statuses:
        where.append(f"a.status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    sql = _APPROVAL_SELECT + (f" WHERE {' AND '.join(where)}" if where else "")
    rows = await _all(conn, sql + " ORDER BY a.id DESC LIMIT ?", (*params, limit))
    return [_approval(r) for r in rows]


async def open_approvals(conn: aiosqlite.Connection, *, task_id: int) -> list[Approval]:
    """Requests that wait for the owner, oldest first."""
    rows = await _all(
        conn,
        _APPROVAL_SELECT + " WHERE a.task_id = ? AND a.status = 'open' ORDER BY a.id",
        (task_id,),
    )
    return [_approval(r) for r in rows]


async def usable_approvals(conn: aiosqlite.Connection, task_ids: list[int]) -> list[Approval]:
    """Approved and not used yet, on any of the cards, the reserved ones too; the oldest first."""
    if not task_ids:
        return []
    marks = ", ".join("?" for _ in task_ids)
    rows = await _all(
        conn,
        _APPROVAL_SELECT + f" WHERE a.task_id IN ({marks}) AND a.status = 'approved' ORDER BY a.id",
        tuple(task_ids),
    )
    return [_approval(r) for r in rows]


async def reserve_approval(
    conn: aiosqlite.Connection,
    approval_id: int,
    tool_use_id: str | None,
    call: str,
    session_id: int,
    now: str,
) -> bool:
    """Hold an approved, unreserved approval for one call; False when another call holds it."""
    cur = await conn.execute(
        "UPDATE approval SET reserved_at = ?, reserved_for = ?, reserved_call = ?,"
        " reserved_session_id = ? WHERE id = ? AND status = 'approved' AND reserved_at IS NULL",
        (now, tool_use_id, call, session_id, approval_id),
    )
    return cur.rowcount == 1


async def reserved_for_call(conn: aiosqlite.Connection, tool_use_id: str) -> list[Approval]:
    """The approvals held for the call with this tool_use_id."""
    rows = await _all(
        conn,
        _APPROVAL_SELECT + " WHERE a.reserved_for = ? AND a.status = 'approved'"
        " AND a.reserved_at IS NOT NULL ORDER BY a.id",
        (tool_use_id,),
    )
    return [_approval(r) for r in rows]


async def reserved_by_session(conn: aiosqlite.Connection, session_id: int) -> list[Approval]:
    """The approvals held for calls of this session."""
    rows = await _all(
        conn,
        _APPROVAL_SELECT + " WHERE a.reserved_session_id = ? AND a.status = 'approved'"
        " AND a.reserved_at IS NOT NULL ORDER BY a.id",
        (session_id,),
    )
    return [_approval(r) for r in rows]


async def use_reserved(conn: aiosqlite.Connection, approval_id: int, now: str) -> bool:
    """The held call ran: the approval is used, by that call (its reservation stays as the record
    of which call it was). False when it is not held any more."""
    cur = await conn.execute(
        "UPDATE approval SET status = 'used', used_at = ?, used_by = reserved_call,"
        " used_session = (SELECT name FROM session WHERE id = reserved_session_id)"
        " WHERE id = ? AND status = 'approved' AND reserved_at IS NOT NULL",
        (now, approval_id),
    )
    return cur.rowcount == 1


async def update_approval(
    conn: aiosqlite.Connection, approval_id: int, changes: dict[str, Any]
) -> None:
    sets = ", ".join(f"{k} = ?" for k in changes)
    await conn.execute(f"UPDATE approval SET {sets} WHERE id = ?", (*changes.values(), approval_id))


async def expire_approvals(
    conn: aiosqlite.Connection, task_id: int, reason: str, now: str
) -> list[Approval]:
    """Close the card's open requests and its approved, unused approvals; returns them."""
    rows = await _all(
        conn,
        _APPROVAL_SELECT
        + " WHERE a.task_id = ? AND a.status IN ('open', 'approved') ORDER BY a.id",
        (task_id,),
    )
    await conn.execute(
        "UPDATE approval SET status = 'expired', expired_at = ?, expired_reason = ?"
        " WHERE task_id = ? AND status IN ('open', 'approved')",
        (now, reason, task_id),
    )
    return [_approval(r) for r in rows]


async def stale_approvals(conn: aiosqlite.Connection, now: str) -> list[Approval]:
    """Approved, unused approvals past their `valid_until`."""
    rows = await _all(
        conn,
        _APPROVAL_SELECT
        + " WHERE a.status = 'approved' AND a.valid_until IS NOT NULL AND a.valid_until <= ?"
        " ORDER BY a.id",
        (now,),
    )
    return [_approval(r) for r in rows]


async def expire_approval(
    conn: aiosqlite.Connection, approval_id: int, reason: str, now: str
) -> None:
    await conn.execute(
        "UPDATE approval SET status = 'expired', expired_at = ?, expired_reason = ?"
        " WHERE id = ? AND status IN ('open', 'approved')",
        (now, reason, approval_id),
    )


async def undelivered_decisions_of_comments(
    conn: aiosqlite.Connection, comment_ids: list[int]
) -> list[Approval]:
    marks = ", ".join("?" for _ in comment_ids)
    rows = await _all(
        conn,
        _APPROVAL_SELECT
        + f" WHERE a.comment_id IN ({marks}) AND a.delivered_at IS NULL ORDER BY a.id",
        tuple(comment_ids),
    )
    return [_approval(r) for r in rows]


async def archived_in_batch(
    conn: aiosqlite.Connection, project_id: int, batch: str
) -> list[tuple[int, str]]:
    """(task id, time) of each `task.archived` of one "Archive all shown" batch in a project."""
    rows = await _all(
        conn,
        "SELECT task_id, ts FROM event WHERE kind = 'task.archived' AND project_id = ?"
        " AND task_id IS NOT NULL AND json_extract(payload, '$.batch') = ? ORDER BY id",
        (project_id, batch),
    )
    return [(r[0], r[1]) for r in rows]
