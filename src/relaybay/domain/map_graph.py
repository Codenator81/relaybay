"""The Map's graph as plain nodes and edges: who works on what, who talks to whom, who can touch
which environment. Pure: the service reads the rows, the page draws the result.

Only people and things the data names become nodes. Roles the data does not hold yet (coordinator,
managers, workers, intake) are not guessed: a session is a session.
"""

from dataclasses import dataclass, field
from typing import Any

from relaybay.domain.environments import Environment
from relaybay.domain.models import Project, Session, Task

VIEWS = ("structure", "now")
DEFAULT_MINUTES = 60
MAX_MINUTES = 7 * 24 * 60
# More nodes than this start collapsed by project.
COLLAPSE_OVER = 150
# A session is shown while bound to a task or seen this recently; messages count this far back.
RECENT_DAYS = 7
SESSIONS_MAX = 300
MESSAGES_MAX = 500


@dataclass(frozen=True, slots=True)
class MessageRow:
    """Messages from one session to one recipient within `RECENT_DAYS`."""

    session_id: int
    recipient: str
    count: int
    last_at: str
    recent: bool


@dataclass(frozen=True, slots=True)
class CommentRow:
    """Comments on a task: the owner's, and those not read by the other side yet."""

    task_id: int
    owner_comments: int
    last_owner_at: str | None
    unread_by_agent: int
    unread_by_owner: int


@dataclass(slots=True)
class MapData:
    projects: list[Project]
    tasks: list[Task]
    sessions: list[Session]
    environments: list[Environment]
    levels: dict[int, str]  # environment id -> effective permission level
    comments: list[CommentRow]
    messages: list[MessageRow]
    recipients: dict[str, Session]  # recipient as written -> the session it names
    owner_names: set[str]  # casefolded
    # Filled for the Now view only.
    now_comment_tasks: set[int] = field(default_factory=set)
    now_bindings: set[tuple[int, int]] = field(default_factory=set)  # (session id, task id)
    now_access: set[tuple[int, int]] = field(default_factory=set)  # (session id, environment id)


def _session_project(session: Session, slugs: dict[int, str], tasks: list[Task]) -> str | None:
    if session.project_id in slugs:
        return slugs[session.project_id]
    return tasks[0].project_slug if tasks else None


def build_graph(data: MapData, view: str, minutes: int, project: str | None) -> dict[str, Any]:
    slugs = {p.id: p.slug for p in data.projects}
    comments = {c.task_id: c for c in data.comments}
    by_session: dict[int, list[Task]] = {}
    for task in data.tasks:
        if task.session_id is not None:
            by_session.setdefault(task.session_id, []).append(task)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def edge(
        kind: str, source: str, target: str, *, recent: bool = False, **extra: Any
    ) -> dict[str, Any]:
        item = {
            "id": f"{kind}:{source}>{target}",
            "kind": kind,
            "source": source,
            "target": target,
            "recent": recent,
            **extra,
        }
        edges.append(item)
        return item

    waiting_total = unread_total = 0
    for task in data.tasks:
        note = comments.get(task.id)
        waiting = bool(task.waiting_on and task.waiting_on.strip().casefold() in data.owner_names)
        unread = note.unread_by_owner if note else 0
        waiting_total += waiting
        unread_total += unread
        nodes.append(
            {
                "id": f"task:{task.key}",
                "kind": "task",
                "label": f"{task.key} {task.title}",
                "project": task.project_slug,
                "stage": task.stage.value,
                "waiting_owner": waiting,
                "unread": unread,
                "href": f"/p/{task.project_slug}?task={task.key}",
            }
        )
        edge("hierarchy", f"project:{task.project_slug}", f"task:{task.key}")
        if note and note.owner_comments:
            edge(
                "comment",
                "owner",
                f"task:{task.key}",
                recent=task.id in data.now_comment_tasks,
                count=note.owner_comments,
                last_at=note.last_owner_at,
            )

    open_counts: dict[str, int] = {}
    for task in data.tasks:
        open_counts[task.project_slug] = open_counts.get(task.project_slug, 0) + 1
    nodes.insert(
        0,
        {
            "id": "owner",
            "kind": "owner",
            "label": "Owner",
            "project": None,
            "waiting_owner": waiting_total > 0,
            "waiting_count": waiting_total,
            "unread": unread_total,
            "href": "/inbox",
        },
    )
    for item in data.projects:
        nodes.append(
            {
                "id": f"project:{item.slug}",
                "kind": "project",
                "label": item.name,
                "project": item.slug,
                "slug": item.slug,
                "open_tasks": open_counts.get(item.slug, 0),
                "href": f"/p/{item.slug}",
            }
        )
        edge("hierarchy", "owner", f"project:{item.slug}")

    environments_of: dict[int, list[Environment]] = {}
    for env in data.environments:
        environments_of.setdefault(env.project_id, []).append(env)
        nodes.append(
            {
                "id": f"environment:{env.id}",
                "kind": "environment",
                "label": env.name,
                "project": env.project_slug,
                "env_kind": env.kind,
                "level": data.levels.get(env.id),
                "href": f"/p/{env.project_slug}/environments",
            }
        )
        edge("hierarchy", f"project:{env.project_slug}", f"environment:{env.id}")

    session_ids = {s.id for s in data.sessions}
    for session in data.sessions:
        bound = by_session.get(session.id, [])
        home = _session_project(session, slugs, bound)
        unread = sum(comments[t.id].unread_by_agent for t in bound if t.id in comments)
        nodes.append(
            {
                "id": f"session:{session.id}",
                "kind": "session",
                "label": session.name,
                "project": home,
                "status": session.status,
                "activity": session.activity,
                "waiting_owner": session.status == "waiting",
                "unread": unread,
                "href": f"/p/{home}/sessions" if home else "/sessions",
                "usage_href": f"/usage?session={session.id}",
            }
        )
        for task in bound:
            edge(
                "binding",
                f"session:{session.id}",
                f"task:{task.key}",
                recent=(session.id, task.id) in data.now_bindings,
            )
        if session.status == "ended":
            continue  # an ended session can no longer touch anything
        reach = {session.project_id, *(t.project_id for t in bound)} - {None}
        for project_id in sorted(reach):
            for env in environments_of.get(project_id, []):
                edge(
                    "access",
                    f"session:{session.id}",
                    f"environment:{env.id}",
                    recent=(session.id, env.id) in data.now_access,
                    level=data.levels.get(env.id),
                )

    peers: set[str] = set()
    for message in data.messages:
        if message.session_id not in session_ids:
            continue
        named = data.recipients.get(message.recipient)
        if named is not None and named.id in session_ids:
            target = f"session:{named.id}"
        else:
            # Not a session relaybay knows (or outside the project filter): still a recipient.
            target = f"peer:{message.recipient}"
            if target not in peers:
                peers.add(target)
                nodes.append(
                    {
                        "id": target,
                        "kind": "session",
                        "label": named.name if named else message.recipient,
                        "project": None,
                        "status": named.status if named else None,
                        "external": named is None,
                        "href": "/sessions",
                    }
                )
        edge(
            "message",
            f"session:{message.session_id}",
            target,
            recent=message.recent,
            count=message.count,
            last_at=message.last_at,
        )

    if view != "now":
        for item in edges:
            item["recent"] = False
    return {
        "view": view,
        "minutes": minutes,
        "project": project,
        "collapsed": len(nodes) > COLLAPSE_OVER,
        "nodes": nodes,
        "edges": edges,
    }
