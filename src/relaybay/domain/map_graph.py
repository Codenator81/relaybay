"""The Map's graph as plain nodes and edges: who works on what, who talks to whom, who can touch
which environment, and in the Agents view who started whom. Pure: the service reads the rows, the
page draws the result.

Only people and things the data names become nodes. Roles the data does not hold yet (coordinator,
managers, workers, intake) are not guessed: a session is a session.
"""

from dataclasses import dataclass, field
from typing import Any

from relaybay.domain.environments import Environment
from relaybay.domain.models import Project, Session, Task

VIEWS = ("agents", "structure", "now")
DEFAULT_VIEW = "agents"
DEFAULT_MINUTES = 60
MAX_MINUTES = 7 * 24 * 60
# More nodes than this start collapsed by project.
COLLAPSE_OVER = 150
# A session is shown while bound to a task or seen this recently; messages count this far back.
RECENT_DAYS = 7
SESSIONS_MAX = 300
MESSAGES_MAX = 500
LAUNCHES_MAX = 500
MOMENTS_MAX = 5  # the latest times an Agents view edge lists
OTHER_BOX = "project:~other"


@dataclass(frozen=True, slots=True)
class MessageRow:
    """Messages from one session to one recipient within `RECENT_DAYS`."""

    session_id: int
    recipient: str
    count: int
    last_at: str
    recent: bool
    moments: tuple[str, ...] = ()  # the latest times, newest first (Agents view only)


@dataclass(frozen=True, slots=True)
class LaunchRow:
    """A session that started another one (`session.launched`), known by its desktop id."""

    session_id: int
    child: str
    last_at: str


@dataclass(frozen=True, slots=True)
class OwnerCommentRow:
    """The owner's comments on a task within `RECENT_DAYS`."""

    task_id: int
    count: int
    moments: tuple[str, ...]


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


# --- Agents view ------------------------------------------------------------


@dataclass(slots=True)
class AgentsData:
    projects: list[Project]
    tasks: list[Task]  # open tasks of every project
    sessions: list[Session]  # every project's: the filter keeps linked ones, faded
    messages: list[MessageRow]
    recipients: dict[str, Session]
    launches: list[LaunchRow]
    children: dict[str, Session]  # desktop id -> the session that has it
    comments: list[OwnerCommentRow]


def _agent_home(session: Session, slugs: dict[int, str], tasks: list[Task]) -> str | None:
    """The project a session is drawn in: its bound tasks' first, else its folder's."""
    if tasks:
        return tasks[0].project_slug
    return slugs.get(session.project_id) if session.project_id is not None else None


def _short_id(external_id: str) -> str:
    return external_id[: len("local_") + 8] + "…"


def _box(box: str, names: dict[str, str], faded: bool) -> dict[str, Any]:
    slug = None if box == OTHER_BOX else box.removeprefix("project:")
    return {
        "id": box,
        "kind": "project",
        "label": names.get(slug, slug) if slug else "Other",
        "project": slug,
        "slug": slug,
        "parent": None,
        "href": f"/p/{slug}" if slug else "/sessions",
        "faded": faded,
    }


def build_agents(data: AgentsData, project: str | None) -> dict[str, Any]:
    """Sessions in a box per project, the owner, and who started, messaged or commented whom.
    With `project`, that project's sessions and, faded, those of others an edge links to them."""
    slugs = {p.id: p.slug for p in data.projects}
    names = {p.slug: p.name for p in data.projects}
    by_session: dict[int, list[Task]] = {}
    for task in data.tasks:
        if task.session_id is not None:
            by_session.setdefault(task.session_id, []).append(task)
    sessions = {s.id: s for s in data.sessions}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def edge(kind: str, source: str, target: str, **extra: Any) -> None:
        edges.append(
            {
                "id": f"{kind}:{source}>{target}",
                "kind": kind,
                "source": source,
                "target": target,
                "recent": False,
                **extra,
            }
        )

    def session_node(session: Session) -> str:
        node_id = f"session:{session.id}"
        if node_id in nodes:
            return node_id
        bound = by_session.get(session.id, [])
        home = _agent_home(session, slugs, bound)
        line = f"{bound[0].key} · {bound[0].stage.value}" if bound else None
        if line and len(bound) > 1:
            line += f" +{len(bound) - 1}"
        nodes[node_id] = {
            "id": node_id,
            "kind": "session",
            "label": session.name,
            "project": home,
            "parent": f"project:{home}" if home else OTHER_BOX,
            "status": session.status,
            "activity": session.activity,
            "waiting_owner": session.status == "waiting",
            "task_line": line,
            "tasks": [
                {
                    "key": t.key,
                    "title": t.title,
                    "stage": t.stage.value,
                    "href": f"/p/{t.project_slug}?task={t.key}",
                }
                for t in bound
            ],
            "launched_by": None,
            "href": f"/p/{home}/sessions" if home else "/sessions",
            "usage_href": f"/usage?session={session.id}",
        }
        return node_id

    def name_node(node_id: str, label: str, parent: str | None, **extra: Any) -> str:
        if node_id not in nodes:
            nodes[node_id] = {
                "id": node_id,
                "kind": "session",
                "label": label,
                "project": None,
                "parent": parent,
                "status": None,
                "external": True,
                "tasks": [],
                "launched_by": None,
                "href": "/sessions",
                **extra,
            }
        return node_id

    for session in data.sessions:
        session_node(session)

    for launch in data.launches:
        parent = sessions.get(launch.session_id)
        child = data.children.get(launch.child)
        if parent is None or (child is not None and child.id == parent.id):
            continue
        if child is not None:
            target = session_node(child)
        else:
            # Started but not known to relaybay yet: drawn beside the session that started it.
            target = name_node(
                f"launch:{launch.child}",
                _short_id(launch.child),
                nodes[f"session:{parent.id}"]["parent"],
                pending=True,
            )
        nodes[target]["launched_by"] = parent.name
        edge("launched", f"session:{parent.id}", target, last_at=launch.last_at)

    for message in data.messages:
        sender = sessions.get(message.session_id)
        named = data.recipients.get(message.recipient)
        if sender is None or (named is not None and named.id == sender.id):
            continue
        if named is not None:
            target = session_node(named)
        else:
            target = name_node(f"peer:{message.recipient}", message.recipient, None)
        edge(
            "message",
            f"session:{sender.id}",
            target,
            count=message.count,
            last_at=message.last_at,
            moments=list(message.moments),
        )

    tasks = {t.id: t for t in data.tasks}
    per_session: dict[int, list[OwnerCommentRow]] = {}
    for row in data.comments:
        task = tasks.get(row.task_id)
        if task is not None and task.session_id in sessions:
            per_session.setdefault(task.session_id, []).append(row)
    for session_id, rows in per_session.items():
        moments = sorted((m for r in rows for m in r.moments), reverse=True)[:MOMENTS_MAX]
        edge(
            "comment",
            "owner",
            session_node(sessions[session_id]),
            count=sum(r.count for r in rows),
            last_at=moments[0] if moments else None,
            moments=moments,
            tasks=sorted(tasks[r.task_id].key for r in rows),
        )

    if project is not None:
        inside = {i for i, n in nodes.items() if n["project"] == project}
        linked = {
            end
            for e in edges
            if e["source"] in inside or e["target"] in inside
            for end in (e["source"], e["target"])
        }
        nodes = {i: n for i, n in nodes.items() if i in inside or i in linked}
        for node_id, item in nodes.items():
            item["faded"] = node_id not in inside
        edges = [e for e in edges if e["source"] in inside or e["target"] in inside]

    boxes: dict[str, dict[str, Any]] = {}
    if project is not None:
        boxes[f"project:{project}"] = _box(f"project:{project}", names, False)
    for item in nodes.values():
        box = item["parent"]
        if box is not None and box not in boxes:
            members = [n for n in nodes.values() if n["parent"] == box]
            boxes[box] = _box(box, names, all(n.get("faded", False) for n in members))
    owner = {
        "id": "owner",
        "kind": "owner",
        "label": "Owner",
        "project": None,
        "parent": None,
        "href": "/inbox",
    }
    every = [owner, *boxes.values(), *nodes.values()]
    return {
        "view": "agents",
        "minutes": None,
        "project": project,
        "collapsed": len(every) > COLLAPSE_OVER,
        "nodes": every,
        "edges": edges,
    }
