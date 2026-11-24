"""MCP server (official SDK 2.x `MCPServer`) served over Streamable HTTP at `/mcp`."""

import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, TextContent, ToolAnnotations
from mcp.types import Tool as MCPTool
from pydantic import Field
from starlette.routing import BaseRoute

from relaybay import __version__
from relaybay.domain import backlog, report_items
from relaybay.domain.errors import DomainError, NotFoundError, ValidationError
from relaybay.domain.rules import Rules
from relaybay.domain.task_fields import BY_NAME as TASK_FIELD_BY_NAME
from relaybay.mcp_server import contract, roles
from relaybay.mcp_server import format as fmt
from relaybay.service import Service, collect_notes

logger = logging.getLogger("relaybay")


def instructions(rules: Rules, coordinated: bool = False) -> str:
    """The working contract sent on connect; the report format comes from the effective global
    rules, so it never contradicts rules_get. `coordinated`: the server names `coordinators`,
    whose session creates the cards and triages findings (service `_agent_cards`)."""
    report = rules.body["report_format"]
    no_card = (
        "2. No card for your work: the card is the coordinator's; ask it for one in your report"
        " and do not create cards."
        if coordinated
        else "2. No card for your work: create it with task_create and give it an area, the part"
        " of the project it is about (a site, an app, a service): reuse a name from the"
        " project's areas, add a new one only when none fits."
    )
    finding = (
        " Findings outside your task and questions for the owner go into report_add(findings=...,"
        " questions=...): the coordinator triages them; do not create cards or backlog items."
        if coordinated
        else " A finding outside your task goes to backlog_add, not into a report."
    )
    tracker = " The tracker mark is the coordinator's." if coordinated else ""
    return "\n".join(
        [
            "relaybay is the shared task board for this user's coding sessions: projects, their"
            " cards (tasks) and what each card needs. What to do and when:",
            "At the start",
            "1. Read task_context of your card (no card yet: project_digest; the whole board:"
            " portfolio_digest; more on a project: project_snapshot). Its rules line names the"
            " project's rules; relaybay://rules/<project> (or rules_get) has the details.",
            no_card + " On every write pass `session`"
            " (your desktop session id local_… with `session_title`, or a readable name) and"
            " `hook_session` if the session start gave you one ('Your relaybay hook session:"
            " …'); if you don't know your local_ id, don't rebind a card.",
            "3. Plan every task, whatever its size: plan_set with 2-5 steps, and keep your Claude"
            " Code todo (TaskCreate / TaskUpdate) as you work; the owner sees both on the card.",
            "While you work",
            "4. On every stage change and report also set next_step (what happens next),"
            " waiting_on (only who acts next: a name or role; the what goes to next_step) and"
            " progress (everything done so far). Mark plan steps with plan_step_update (done:"
            " 'as planned' or 'deviation: why'); new work is a new step with plan_step_add.",
            "5. Owner comments ('Owner comment on …') are messages from the user: act and answer"
            " with comment_add, or comment_ack. A decision goes to decision_add; when the work"
            " needs one, set waiting_on to who decides." + finding,
            "6. Read a large document by its outline: doc_outline, then doc_get of a section or"
            " lines (doc_search finds the place).",
            "At the end",
            "7. Add a report with report_add; its first line is"
            f" `{report['first_line']}`; state is one of {', '.join(report['states'])}; size is one"
            f" of {', '.join(report['sizes'])}. Finished work goes to done. Move a card to review"
            " only when the owner wants to accept it step by step: then write the steps with"
            " review_steps_set and attach screenshots with attach_file." + tracker,
            "Always",
            "- A line 'Warning (rules)' names a rule the write did not meet: fix it. 'rules updated"
            " to vN' means re-read relaybay://rules; changes go through rules_propose (rules_update"
            " is for coordinator sessions named in the server's config).",
            "- A parameter your tool lacks: task_update(fields={...}) (relaybay://schema/task; an"
            " older client without it: task_patch). A line"
            f" 'Deprecated: …' names the replacement (contract v{contract.CONTRACT_VERSION}:"
            " relaybay://contract).",
            "- Hosts and access come from the project's own notes; never paste secrets into"
            " relaybay. When the guard stops a call, its reason says why (policy_get); ask the"
            " owner or use policy_propose.",
        ]
    )


READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

SESSION_HELP = (
    "Who is calling: pass your own desktop session id (local_…) if you know it, otherwise a"
    " human-readable session name."
)
Session = Annotated[str | None, Field(description=SESSION_HELP + " Optional.")]
SessionTitle = Annotated[
    str | None,
    Field(description="Display name of your session (its title), used with a desktop session id."),
]
HookSession = Annotated[
    str | None,
    Field(
        description="Your own Claude Code hook session id, if the session start told you one"
        " ('Your relaybay hook session: …'); never another session's. Links the board's live"
        " status and owner comments to your session. An id another live session linked first is"
        " not moved (the response says so). Optional."
    ),
]
TaskRef = Annotated[str, Field(description="Task key such as 'acme-shop-12', or its numeric id.")]
ProjectSlug = Annotated[str, Field(description="Project slug such as 'acme-shop'.")]
StageArg = Annotated[str, Field(description="One of: plan, dev, review, test, live, done.")]
DESCRIPTION_HELP = "What to do and why, 1-5 sentences of markdown."
CLEARS = " '-' or '' clears it."
EMPTY = " '-' or '' leaves it empty."
AREA_HELP = (
    "What part of the project the task belongs to, by name, e.g. 'web shop' (project_snapshot"
    " lists the project's areas; case does not matter). A new name is added to the project's"
    " areas and the answer says so."
)
TRACKER_REF_HELP = "Issue key or number in the external tracker, e.g. 'ENG-12'."
TRACKER_NAME_HELP = "Name of the external tracker, e.g. 'Linear'."
TRACKER_URL_HELP = "Link to the issue (http:// or https:// only)."
TRACKER_LOGGED_HELP = (
    "True once the work log is entered in the external tracker. New work clears it again (a"
    " report, a progress change, a move back from live or done). A new tracker_ref starts as not"
    " logged unless tracker_logged is passed in the same write. Done tasks without it stay on"
    " the board marked 'not logged'."
)
AT_HELP = (
    "When, if not now: HH:MM (today, server time) or an ISO time like 2026-11-24T09:00+02:00;"
    " at most a minute ahead."
)
INSPECT_ONLY_HELP = (
    "true for a card that only reads (an analysis, a repro, a question answered by reading): it"
    " needs no accepted plan before dev and may go straight to done. A report on it that says"
    " changed, committed or pushed warns."
)
DEPLOY_NOTES_HELP = TASK_FIELD_BY_NAME["deploy_notes"].description
DEPLOY_DONE_HELP = TASK_FIELD_BY_NAME["deploy_done"].description
CANCELLED_HELP = TASK_FIELD_BY_NAME["cancelled"].description + " '-' clears it and its time."
NEXT_STEP_HELP = (
    "What happens next, one line; saved in the same write. A next step set before the last stage"
    " change shows as stale. Omit to keep it;"
)
WAITING_ON_WHO = (
    "Only who acts next: a name or role, no text after it; what happens goes to next_step."
)
WAITING_ON_HELP = WAITING_ON_WHO + " Saved in the same write. Omit to keep it;"
SOURCE_HELP = (
    "Where it came from: 'report:12', 'comment:7', 'event:301' or 'message:<id>', optionally"
    " followed by display text ('comment:7 owner in the review thread'); or display text alone"
    " ('owner, chat')."
)
SIZE_HELP = (
    "Task size: S, M or L. S tasks may go without an accepted plan; a task of unknown size needs"
    " one."
)
PLAN_STEP_HELP = "The step's number in the plan (1, 2, …)."
REPOS_HELP = (
    "Repositories of the task's project the task touches, by name, added to its links; '-name'"
    " removes one. An unknown name fails with the project's names. A session bound to the task"
    " links the repository it works in by itself. 'name <absolute path>' also sets the folder"
    " (worktree) the task works in there."
)
BLOCKED_BY_HELP = (
    "Cards this task waits for, by key, of any project on the board: 'acme-shop-12'; '-key'"
    " removes one. A blocker blocks while it is in plan, dev, review or test, and no longer at"
    " live or done. An unknown key, the task itself and a cycle are refused."
)
PRS_HELP = (
    "Pull request links (http:// or https://) added to the task, optionally after a repository"
    " name: 'web https://host/acme/web/pull/12'; '-url' removes one. Without a name, a link whose"
    " path names a project repository is tied to it (and links it)."
)
Budget = Annotated[
    int | None,
    Field(description="Size limit in characters, held between 500 and 40000. Optional."),
]
SinceEvent = Annotated[
    int | None,
    Field(
        ge=0,
        description="Show changes after this event id and leave your read position alone."
        " Optional.",
    ),
]
DigestSession = Annotated[
    str | None,
    Field(
        description=SESSION_HELP + " Optional. Changes show from your last read of this digest,"
        " and the read position moves on; without it, the last 24 hours."
    ),
]
PROGRESS_HELP = (
    "Markdown summary of everything done so far on the task (not just the last step). Rewrite"
    " it as the work goes on and keep it current with every report."
)


BacklogPriority = Annotated[
    int | None,
    Field(
        ge=backlog.PRIORITY_MIN,
        le=backlog.PRIORITY_MAX,
        description="-9 to 9, higher first; 0 is the default and resets it.",
    ),
]
BacklogAssignee = Annotated[
    str | None,
    Field(description="Who it is for, a name or a role: To plan makes it waiting_on." + CLEARS),
]
BacklogKind = Annotated[
    str | None,
    Field(description=f"What it is: {', '.join(backlog.KINDS)}. Optional." + CLEARS),
]
BacklogLocation = Annotated[
    str | None,
    Field(
        description="Where in the code: 'repo:path:line', 'path:line' or a link, up to"
        f" {backlog.LOCATION_MAX} characters. Optional." + CLEARS
    ),
]


def cleared(value: str | None) -> str | None:
    """'-' means "empty": models cannot reliably send '' as a tool argument."""
    return "" if value is not None and value.strip() == "-" else value


async def run[T](awaitable: Awaitable[T]) -> T:
    """Domain errors become tool errors whose message the model reads."""
    try:
        return await awaitable
    except DomainError as exc:
        raise ToolError(str(exc)) from exc


DOC_PATH = (
    "The task's document: a relaybay://doc link, or a local .md file (an absolute path), which"
    " becomes the file document task-<key> when no document reads it yet."
)


class RoleServer(MCPServer):
    """An MCPServer whose tools/list shows only its role's tools (roles.py); a tool left out
    still runs when called by name. `role=None` is the full set."""

    def __init__(self, role: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.role = role

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        if self.role is None:
            return tools
        shown = roles.role_tools(self.role, current_tools({t.name for t in tools}))
        return [t for t in tools if t.name in shown]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        """The SDK drops an argument the tool does not take without a word. A write with one
        is refused before it runs (nothing changes); a read runs and its answer names them."""
        tool = self._tool_manager.get_tool(name)
        unknown = unknown_arguments(tool.parameters, arguments) if tool is not None else []
        writes = tool is not None and not (
            tool.annotations is not None and tool.annotations.read_only_hint
        )
        if unknown and writes:
            raise ToolError(unknown_refusal(name, unknown, tool.parameters))
        result = await super().call_tool(name, arguments, context)
        if unknown and isinstance(result, CallToolResult) and not result.is_error:
            note = f"Ignored unknown parameter(s): {', '.join(unknown)}."
            result.content.append(TextContent(type="text", text=note))
        return result


def unknown_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    """The argument names a tool's input schema does not declare, in the order sent."""
    known = schema.get("properties") or {}
    return [k for k in arguments if k not in known]


def unknown_refusal(name: str, unknown: list[str], schema: Mapping[str, Any]) -> str:
    takes = ", ".join(sorted(schema.get("properties") or {}))
    hatch = ""
    if name.startswith("task_"):
        hatch = ", or reach a task field through task_update(fields={...})"
    elif name.startswith("backlog_"):
        hatch = ", or reach an item field through backlog_patch(item, fields={...})"
    return (
        f"Unknown parameter(s) for {name}: {', '.join(unknown)}. Nothing changed. {name} takes:"
        f" {takes}. Your client's tool list may be older or newer than this server: reconnect"
        f" to refresh it{hatch}."
    )


def current_tools(names: set[str]) -> set[str]:
    """The tool names that are not deprecated (old names and stubs stay out of every role)."""
    gone = {d.tool for d in contract.DEPRECATIONS if d.parameter is None}
    return names - gone


def server_instructions(rules: Rules, role: str | None, coordinated: bool = False) -> str:
    if role is None:
        return instructions(rules, coordinated)
    return roles.role_instructions(rules, role, coordinated)


def build_mcp(
    service: Service, *, require_tracker_log: bool = False, role: str | None = None
) -> MCPServer:
    """The MCP server; `role` (roles.ROLES) serves that role's tool list and instructions, None
    the full set."""
    if role is not None and role not in roles.ROLES:
        raise ValueError(f"Unknown role {role!r}.")
    coordinated = bool(service.coordinators)
    mcp = RoleServer(
        role,
        name="relaybay",
        version=__version__,
        instructions=server_instructions(service.global_rules, role, coordinated),
    )

    async def doc_field_note(
        key: str,
        doc_path: object,
        session: str | None,
        session_title: str | None,
        hook_session: str | None,
    ) -> list[str]:
        """A write that set the document field to a local Markdown file links it as the
        card's file document (`Service.link_task_file`); the line says what happened. The task
        is saved by then: nothing here may turn the answer into an error (and a retry into a
        second card)."""
        if not isinstance(doc_path, str) or not doc_path:
            return []
        try:
            note = await service.link_task_file(
                key, session=session, session_title=session_title, hook_session=hook_session
            )
        except Exception:
            logger.exception("linking the document of %s failed", key)
            return ["Document field: the file is not a board document (see the server log)."]
        return [note] if note else []

    async def log_rule() -> fmt.LogRule:
        """`require_tracker_log` per task: only in projects that keep a tracker, read now."""
        if not require_tracker_log:
            return None
        await service.tracker_projects()
        return service.tracker_log_required

    def refresh_instructions() -> None:
        # The SDK has no setter: the text it sends on connect is this attribute. Without this a
        # new session would read the report format of config.toml, not of the current version.
        try:
            mcp._lowlevel_server.instructions = server_instructions(
                service.global_rules, role, coordinated
            )
        except AttributeError as exc:
            logger.warning("Could not refresh the MCP instructions after an SDK change: %s.", exc)

    # Several servers (one per role) share the service: each refreshes its own text.
    earlier = service.rules_changed

    def rules_changed() -> None:
        if earlier is not None:
            earlier()
        refresh_instructions()

    service.rules_changed = rules_changed

    def write(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        """A write tool: lines the service noted without failing (a hook_session that was not
        linked, a missed report format, newer rules) are appended to its result."""

        @functools.wraps(fn)
        async def tool(*args: Any, **kwargs: Any) -> str:
            with collect_notes() as notes:
                text = await fn(*args, **kwargs)
                # In these two `session` names another session, not the caller.
                if fn.__name__ not in ("task_bind_session", "task_unbind_session"):
                    place = kwargs.get("project") or kwargs.get("slug") or kwargs.get("scope")
                    # The write is committed: a hint that fails must not turn it into an error.
                    try:
                        await service.note_rules_update(
                            kwargs.get("session"), place, kwargs.get("task")
                        )
                    except Exception:
                        logger.exception("Could not work out the rules-updated hint.")
            return "\n".join([text, *notes])

        return mcp.tool(annotations=WRITE, structured_output=False)(tool)

    @mcp.tool(annotations=READ, structured_output=False)
    async def project_list() -> str:
        """List all projects with task counts per stage. Call it to find a project's slug."""
        return fmt.project_list(await service.list_projects())

    @write
    async def project_upsert(
        slug: ProjectSlug,
        name: Annotated[str | None, Field(description="Display name.")] = None,
        repo_path: Annotated[
            str | None,
            Field(description="Path to the repository: the path of the first one in `repos`."),
        ] = None,
        description: Annotated[str | None, Field(description="One-line description.")] = None,
        archived: Annotated[
            bool | None,
            Field(
                description="Coordinator sessions only. true sends the project to the archive:"
                " it leaves the project list, the board's sidebar, the Inbox and the digests, and"
                " nothing is deleted; false brings it back."
            ),
        ] = None,
        repos: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="The project's repositories, the whole list in order:"
                ' [{"name": "web", "path": "/code/acme/web", "kind": "web"}]. kind is one of'
                " app, web, api, lib, other (optional); without a name the path's last folder"
                " names it. Matched by name, so tasks keep their links to a repository that stays;"
                " [] removes them all. The path is stored as written."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Create a project, or rename it / set its repositories / archive it. Call it once
        before creating the first task of a project that is not on the board yet (see
        project_list)."""
        project, created = await run(
            service.upsert_project(
                slug,
                name=name,
                repo_path=repo_path,
                description=description,
                repos=repos,
                archived=archived,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        _, repos_now = await run(service.project_repos(project.slug))
        return fmt.project(project, created, repos_now)

    # Not under `write`: its rules hint would look up the project this call just deleted.
    @mcp.tool(annotations=DELETE, structured_output=False)
    async def project_delete(
        slug: ProjectSlug,
        confirm: Annotated[
            str, Field(description="The project's slug once more: the deletion cannot be undone.")
        ],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Delete an archived project for good, for coordinator sessions only: its tasks with
        their reports, comments, plans and attachments, its documents (the rows only; files on
        disk stay), backlog, decisions, notes and areas. An export of everything deleted is
        written first to `deleted/` in the server's data folder. A project not in the archive
        is refused; archive it first with project_upsert(archived=true)."""
        with collect_notes() as notes:
            project, path = await run(
                service.delete_project_mcp(
                    slug,
                    confirm,
                    session=session,
                    session_title=session_title,
                    hook_session=hook_session,
                )
            )
        return "\n".join([fmt.project_deleted(project, path.name), *notes])

    @mcp.tool(annotations=READ, structured_output=False)
    async def project_snapshot(
        project: ProjectSlug,
        include_done: Annotated[bool, Field(description="Also list done tasks.")] = False,
    ) -> str:
        """Compact markdown snapshot of a project: open tasks by stage with waiting-on, next step,
        branch, the start of the description and progress, last report, the merge order of the
        cards on test (two or more), plus recent decisions.
        Call it at the start of a session to see the state of the project before you pick or
        create a task."""
        snap = await run(service.snapshot(project, include_done))
        return fmt.snapshot(snap, await log_rule())

    @write
    async def project_pulse_set(
        project: ProjectSlug,
        text: Annotated[
            str,
            Field(description="Where the project stands: at most 5 lines and 600 characters."),
        ],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Write the project's pulse note: a short hand-written summary, shown with its date
        under the pulse line relaybay derives from the cards (in the digests and on the board).
        Optional; it is never marked stale."""
        return fmt.pulse_saved(
            await run(
                service.set_pulse(
                    project,
                    text,
                    session=session,
                    session_title=session_title,
                    hook_session=hook_session,
                )
            )
        )

    @write
    async def timelog_mark(
        kind: Annotated[
            Literal["start", "switch", "pause"],
            Field(
                description="start: work begins on a card or a label; switch: moves to another"
                " one; pause: work stops (a gap until the next start)."
            ),
        ],
        task: Annotated[
            str | None,
            Field(description="The card worked on (start, switch), e.g. 'acme-shop-12'."),
        ] = None,
        label: Annotated[
            str | None,
            Field(
                description="Work without a card (start, switch), e.g. 'call' or 'email'; on a"
                " pause, why. Not with task."
            ),
        ] = None,
        at: Annotated[str | None, Field(description=AT_HELP)] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Mark the day time log, for the coordinator: when work starts, switches to another
        card or label, or pauses. A mark opens a span that runs until the next mark; a pause
        is a gap. The answer names the span the mark closed and the day's total. The same mark
        again within a minute writes nothing. In coordinator mode an agent's mark is refused."""
        result = await run(
            service.mark_time(
                kind,
                task=task,
                label=label,
                at=at,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.time_marked(result, service.timelog_tz)

    @mcp.tool(annotations=READ, structured_output=False)
    async def timelog_list(
        day: Annotated[
            str | None,
            Field(description="A date like 2026-11-24, 'today' (the default) or 'yesterday'."),
        ] = None,
        task: Annotated[
            str | None,
            Field(description="One card instead: today, total, in the tracker, per day, rows."),
        ] = None,
        project: Annotated[
            str | None, Field(description="One project's cards on the day, and its totals.")
        ] = None,
    ) -> str:
        """The day time log: the spans and pauses of a day in time order with their durations,
        the totals per project, card and label, what runs now and the unclosed spans (a past
        day's last span without a pause counts 0). With task: that card's time (today, total,
        in the tracker, per day, the tracker rows). With project: its cards on the day."""
        if task and project:
            raise ToolError(
                "Pass task or project, not both: task shows one card's time, project one"
                " project's cards on a day."
            )
        if task:
            card = await run(service.get_task(task))
            time = await run(service.task_time(card))
            return fmt.card_time(card, time, await run(service.worklog_rows(card)))
        if project:
            found = await run(service.get_project(project))
            log = await run(service.day_log(day, found.slug))
            times = await run(service.project_times([found.id]))
            return fmt.project_time(found.slug, log, times[found.id])
        return fmt.day_log(await run(service.day_log(day)), service.timelog_tz)

    @write
    async def timelog_fix(
        mark: Annotated[int, Field(description="The mark's number (timelog_list shows #N).")],
        reason: Annotated[str, Field(description="Why the mark was wrong; kept in the audit.")],
        at: Annotated[
            str | None,
            Field(
                description="The right time: HH:MM on the mark's own day (server time), or an"
                " ISO time like 2026-11-24T09:00+02:00; at most a minute ahead."
            ),
        ] = None,
        kind: Annotated[
            Literal["start", "switch", "pause"] | None, Field(description="The right kind.")
        ] = None,
        task: Annotated[
            str | None, Field(description="The right card; clears the label." + CLEARS)
        ] = None,
        label: Annotated[
            str | None, Field(description="The right label; clears the card." + CLEARS)
        ] = None,
        delete: Annotated[bool, Field(description="true deletes the mark.")] = False,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Correct a mark of the day time log in place, or delete it, for the coordinator. The
        reason is required; the audit keeps the mark before and after. The answer shows the
        mark's day again."""
        fixed, before, log = await run(
            service.fix_time_mark(
                mark,
                reason,
                at=at,
                kind=kind,
                task=cleared(task),
                label=cleared(label),
                delete=delete,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.time_fixed(fixed, before, log, reason.strip(), service.timelog_tz)

    @write
    async def worklog_add(
        task: TaskRef,
        minutes: Annotated[
            int,
            Field(
                description="Minutes entered in the external tracker; a correction is negative"
                " (with reason) and may not take the card's day below 0."
            ),
        ],
        day: Annotated[
            str | None, Field(description="The day they were logged for; default today.")
        ] = None,
        ref: Annotated[
            str | None, Field(description="The tracker's work log entry or issue, e.g. 'ENG-12'.")
        ] = None,
        reason: Annotated[
            str | None, Field(description="Why; required for a negative correction.")
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Record minutes entered in the external tracker for a card, for the coordinator: an
        append-only ledger, shown as "in tracker" next to the card's time. tracker_logged stays
        the separate mark that the work log is done."""
        row_id, logged, time = await run(
            service.add_worklog(
                task,
                minutes,
                day=day,
                ref=ref,
                reason=reason,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        card = await run(service.get_task(task))
        return fmt.worklog_added(card, row_id, minutes, logged, time)

    # The digests are writes for the annotations: `session` moves the caller's read position and
    # can create or link its session row.
    @write
    async def portfolio_digest(
        budget: Budget = None,
        since_event: SinceEvent = None,
        session: DigestSession = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Everything at a glance, for a coordinator: one line per project with stage counts and
        pulse, what waits for the owner, the week's decisions, tasks without progress, global
        blocks and what changed since your last read. Index lines; each section says which tool
        shows more."""
        return await run(
            service.portfolio_digest(
                budget=budget,
                since_event=since_event,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )

    @write
    async def project_digest(
        project: ProjectSlug,
        budget: Budget = None,
        since_event: SinceEvent = None,
        session: DigestSession = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """One project, for a manager: pulse, tasks by stage with next step and waiting on, the
        merge order of the cards on test (two or more), open questions, recent decisions, blocks,
        notes flagged 'verify' or 'never read' and what changed since your last read. Index
        lines; each section says which tool shows more."""
        return await run(
            service.project_digest(
                project,
                budget=budget,
                since_event=since_event,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )

    @write
    async def task_context(
        task: TaskRef,
        budget: Budget = None,
        since_event: SinceEvent = None,
        session: DigestSession = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """One task, for the session working on it: fields and progress, plan with the current
        step, its decisions and the project decisions related to it, linked document sections,
        rules, unread owner comments (ids; text only of those already delivered to you), relevant
        notes, the open backlog items of its subject and what changed since your last read. It
        delivers no comment."""
        return await run(
            service.task_context(
                task,
                budget=budget,
                since_event=since_event,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )

    @mcp.tool(annotations=READ, structured_output=False)
    async def task_list(
        project: Annotated[str | None, Field(description="Project slug filter.")] = None,
        stage: Annotated[str | None, Field(description="Stage filter.")] = None,
        waiting_on: Annotated[str | None, Field(description="Exact 'waiting on' value.")] = None,
        session: Annotated[
            str | None, Field(description="Bound session: desktop session id or name.")
        ] = None,
        include_done: Annotated[bool, Field(description="Include done tasks.")] = False,
        area: Annotated[
            str | None, Field(description="Area name filter (case does not matter).")
        ] = None,
    ) -> str:
        """List tasks, filtered by project, stage, who they wait on, the bound session or the
        area. Done tasks are left out unless a stage or include_done is given."""
        tasks = await run(
            service.list_tasks(
                project=project,
                stage=stage,
                waiting_on=waiting_on,
                session=session,
                area=area,
                include_done=include_done,
            )
        )
        return fmt.task_list(tasks, await log_rule())

    @mcp.tool(annotations=READ, structured_output=False)
    async def task_get(
        task: TaskRef,
        reports: Annotated[int, Field(ge=1, le=50, description="How many reports.")] = 5,
    ) -> str:
        """Full task: fields, description, progress, current version, the linked document
        sections, the latest reports and decisions, and the names of the editable fields (for
        task_update(fields=...)). Call it before task_update to get the version for
        expected_version."""
        detail = await run(service.task_detail(task, reports))
        return fmt.task_detail(detail, await log_rule(), service.board_url)

    @write
    async def task_create(
        project: ProjectSlug,
        title: Annotated[str, Field(description="Short task title.")],
        stage: Annotated[str | None, Field(description="Initial stage; default plan.")] = None,
        waiting_on: Annotated[str | None, Field(description=WAITING_ON_WHO + EMPTY)] = None,
        next_step: Annotated[
            str | None, Field(description="The next concrete step." + EMPTY)
        ] = None,
        branch: Annotated[str | None, Field(description="Git branch." + EMPTY)] = None,
        doc_path: Annotated[str | None, Field(description=DOC_PATH + EMPTY)] = None,
        description: Annotated[str | None, Field(description=DESCRIPTION_HELP + EMPTY)] = None,
        progress: Annotated[str | None, Field(description=PROGRESS_HELP + EMPTY)] = None,
        tracker_ref: Annotated[str | None, Field(description=TRACKER_REF_HELP + EMPTY)] = None,
        tracker_name: Annotated[str | None, Field(description=TRACKER_NAME_HELP + EMPTY)] = None,
        tracker_url: Annotated[str | None, Field(description=TRACKER_URL_HELP + EMPTY)] = None,
        tracker_logged: Annotated[bool, Field(description=TRACKER_LOGGED_HELP)] = False,
        area: Annotated[str | None, Field(description=AREA_HELP + EMPTY)] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
        bind: Annotated[
            bool,
            Field(
                description="false: you create the card for someone else; it is not bound to"
                " your session (a session started for it binds itself, or the launch does)."
            ),
        ] = True,
        inspect_only: Annotated[bool, Field(description=INSPECT_ONLY_HELP)] = False,
        sections: Annotated[
            list[str] | None,
            Field(
                description="Sections of the project's documents to link to the card, each"
                " 'doc#anchor' (as task_doc_link links one). Optional."
            ),
        ] = None,
    ) -> str:
        """Create a task when you start work that is not on the board yet. Give it a short
        `description` (what to do and why). Returns its key (e.g. 'acme-shop-12'); use the key in
        later calls. If `session` is given, the task is bound to that session unless `bind` is
        false."""
        created = await run(
            service.create_task(
                project,
                title,
                stage=stage,
                waiting_on=cleared(waiting_on),
                next_step=cleared(next_step),
                branch=cleared(branch),
                doc_path=cleared(doc_path),
                description=cleared(description),
                progress=cleared(progress),
                tracker_ref=cleared(tracker_ref),
                tracker_name=cleared(tracker_name),
                tracker_url=cleared(tracker_url),
                tracker_logged=tracker_logged,
                area=cleared(area),
                session=session,
                session_title=session_title,
                hook_session=hook_session,
                bind=bind,
                sections=sections or None,
                inspect_only=inspect_only,
            )
        )
        filed = await doc_field_note(
            created.key, cleared(doc_path), session, session_title, hook_session
        )
        return fmt.with_warnings(
            fmt.task_saved(created, "Created"), [*filed, *fmt.waiting_on_hint(waiting_on)]
        )

    @write
    async def task_doc_link(
        task: TaskRef,
        doc: Annotated[str, Field(description="Document name of the task's project, e.g. 'spec'.")],
        section: Annotated[
            str,
            Field(
                description="The section: its heading anchor or explicit anchor, as doc_outline"
                " lists them, e.g. 'payouts'."
            ),
        ],
        remove: Annotated[bool, Field(description="true: unlink the section.")] = False,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Link a section of a project document to the card (remove=true unlinks it). The
        anchor is checked against the document's current outline. task_context lists the linked
        sections first, and takes the card's subject (for its backlog items and related
        decisions) from them; a section that disappears later shows as broken."""
        saved, line, changed = await run(
            service.link_task_section(
                task,
                doc,
                section,
                remove=remove,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.task_section_saved(saved, line, remove, changed)

    @write
    async def task_update(
        task: TaskRef,
        title: Annotated[str | None, Field(description="New title.")] = None,
        waiting_on: Annotated[str | None, Field(description=WAITING_ON_WHO + CLEARS)] = None,
        next_step: Annotated[
            str | None, Field(description="The next concrete step." + CLEARS)
        ] = None,
        branch: Annotated[str | None, Field(description="Git branch." + CLEARS)] = None,
        doc_path: Annotated[str | None, Field(description=DOC_PATH + CLEARS)] = None,
        stage_note: Annotated[
            str | None, Field(description="Note next to the stage." + CLEARS)
        ] = None,
        description: Annotated[str | None, Field(description=DESCRIPTION_HELP + CLEARS)] = None,
        progress: Annotated[str | None, Field(description=PROGRESS_HELP + CLEARS)] = None,
        tracker_ref: Annotated[str | None, Field(description=TRACKER_REF_HELP + CLEARS)] = None,
        tracker_name: Annotated[str | None, Field(description=TRACKER_NAME_HELP + CLEARS)] = None,
        tracker_url: Annotated[str | None, Field(description=TRACKER_URL_HELP + CLEARS)] = None,
        tracker_logged: Annotated[bool | None, Field(description=TRACKER_LOGGED_HELP)] = None,
        size: Annotated[str | None, Field(description=SIZE_HELP + CLEARS)] = None,
        area: Annotated[str | None, Field(description=AREA_HELP + CLEARS)] = None,
        repos: Annotated[list[str] | None, Field(description=REPOS_HELP)] = None,
        prs: Annotated[list[str] | None, Field(description=PRS_HELP)] = None,
        blocked_by: Annotated[list[str] | None, Field(description=BLOCKED_BY_HELP)] = None,
        inspect_only: Annotated[bool | None, Field(description=INSPECT_ONLY_HELP)] = None,
        stage: Annotated[
            str | None,
            Field(
                description="Move the task: plan, dev, review, test, live or done (a move that"
                " misses a working rule adds a 'Warning (rules)' line)."
            ),
        ] = None,
        deploy_notes: Annotated[str | None, Field(description=DEPLOY_NOTES_HELP + CLEARS)] = None,
        deploy_done: Annotated[bool | None, Field(description=DEPLOY_DONE_HELP)] = None,
        cancelled: Annotated[str | None, Field(description=CANCELLED_HELP)] = None,
        fields: Annotated[
            dict[str, Any] | None,
            Field(
                description="Any other task field as one JSON object, e.g. {'priority': 2}"
                " (relaybay://schema/task lists them); for a field this tool has no parameter"
                " for yet. A field given both here and as a parameter must agree."
            ),
        ] = None,
        expected_version: Annotated[
            int | None,
            Field(description="Fail instead of overwriting if the task changed since you read it."),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change task fields and move its stage in one write: the stage, waiting on, next
        step, branch, description, progress, the area, the tracker issue and its work-log mark,
        deploy notes, a cancel reason, the repositories it touches, its pull requests, the cards
        blocking it; `fields` for anything else. Omitted fields stay as they are; '-' or an
        empty string clears a field. Keep `progress` current with every report. Also update
        next_step (what happens next) and waiting_on (who acts next) whenever the stage changes
        or you report."""
        given = {
            "title": title,
            "waiting_on": waiting_on,
            "next_step": next_step,
            "branch": branch,
            "doc_path": doc_path,
            "stage_note": stage_note,
            "description": description,
            "progress": progress,
            "tracker_ref": tracker_ref,
            "tracker_name": tracker_name,
            "tracker_url": tracker_url,
            "size": size,
            "area": area,
            "stage": stage,
            "deploy_notes": deploy_notes,
            "cancelled": cancelled,
        }
        changes: dict[str, object] = {
            k: (cleared(v) or None) for k, v in given.items() if v is not None
        }
        if tracker_logged is not None:
            changes["tracker_logged"] = tracker_logged
        if inspect_only is not None:
            changes["inspect_only"] = inspect_only
        if deploy_done is not None:
            changes["deploy_done"] = deploy_done
        if repos:
            changes["repos"] = repos
        if prs:
            changes["prs"] = prs
        if blocked_by:
            changes["blocked_by"] = blocked_by
        for name, value in (fields or {}).items():
            if isinstance(value, str):
                value = cleared(value) or None
            if name in changes and changes[name] != value:
                raise ToolError(
                    f"{name} is given twice with different values (as a parameter and in"
                    " fields). Nothing changed."
                )
            changes.setdefault(name, value)
        if not changes:
            raise ToolError("Nothing to update: pass at least one field.")
        before = await run(service.get_task(task)) if "stage" in changes else None
        saved = await run(
            service.update_task_checked(
                task,
                changes,
                expected_version=expected_version,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        filed = await doc_field_note(
            saved.task.key, changes.get("doc_path"), session, session_title, hook_session
        )
        moved = before is not None and before.stage != saved.task.stage
        return fmt.with_warnings(
            fmt.task_saved(saved.task, "Moved" if moved else "Updated"),
            [*saved.warnings, *filed, *fmt.waiting_on_hint(changes.get("waiting_on"))],
        )

    @write
    @contract.deprecated()
    async def task_patch(
        task: TaskRef,
        fields: Annotated[
            dict[str, Any],
            Field(
                description='Task fields to change as one JSON object, e.g. {"branch": "x"}.'
                " Call task_get or read relaybay://schema/task for the allowed fields; '-', ''"
                " or null clears an optional text field."
            ),
        ],
        expected_version: Annotated[
            int | None,
            Field(description="Fail instead of overwriting if the task changed since you read it."),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change any task fields given as one JSON object, with the same rules as task_update
        and task_set_stage (a `stage` value moves the task). Use it for a field your client's
        task_update does not offer yet: its parameter list may be older than the server. Call
        task_get or read relaybay://schema/task for the allowed fields. `session_surface`
        ("desktop" or "terminal", with your hook_session) is not a task field: it tells relaybay
        where you run, when your session start asks for it."""
        changes = {k: cleared(v) if isinstance(v, str) else v for k, v in fields.items()}
        if not changes:
            raise ToolError("Nothing to update: pass at least one field in `fields`.")
        saved = await run(
            service.update_task_checked(
                task,
                changes,
                expected_version=expected_version,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        filed = await doc_field_note(
            saved.task.key, changes.get("doc_path"), session, session_title, hook_session
        )
        return fmt.with_warnings(
            fmt.task_saved(saved.task, "Updated"),
            [*saved.warnings, *filed, *fmt.waiting_on_hint(fields.get("waiting_on"))],
        )

    @write
    @contract.deprecated()
    async def task_set_stage(
        task: TaskRef,
        stage: StageArg,
        note: Annotated[
            str | None, Field(description="Short note next to the stage, e.g. 'PR open'.")
        ] = None,
        next_step: Annotated[str | None, Field(description=NEXT_STEP_HELP + CLEARS)] = None,
        waiting_on: Annotated[str | None, Field(description=WAITING_ON_HELP + CLEARS)] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Move a task to another stage (plan, dev, review, test, live, done) when its work
        moves on, e.g. to dev when you start coding, to review when the owner should accept it
        step by step (fill the checklist with review_steps_set first), to test when it is on the
        test environment, to done when finished.
        Any transition is allowed; a move that misses a working rule (see rules_get) adds a
        'Warning (rules)' line. Also update next_step (what happens next) and waiting_on (who
        acts next) whenever the stage changes or you report."""
        saved = await run(
            service.set_stage_checked(
                task,
                stage,
                note=note,
                next_step=cleared(next_step),
                waiting_on=cleared(waiting_on),
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.with_warnings(
            fmt.task_saved(saved.task, "Moved"),
            [*saved.warnings, *fmt.waiting_on_hint(waiting_on)],
        )

    @write
    async def task_archive(
        task: TaskRef,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Send a done or live task to the archive now, for coordinator sessions only: it leaves
        the board's columns (the board's "show archive" and search still find it). A live task is
        closed and archived in one step, and task_unarchive returns it to live. Any later stage
        change takes it out of the archive. Nothing is deleted now; after the server's
        `archive_purge_days` (30 by default) in the archive relaybay deletes it, with an export
        written first."""
        saved = await run(
            service.set_archived_mcp(
                task,
                True,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.task_archive_saved(saved)

    @write
    async def task_unarchive(
        task: TaskRef,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Bring a task back from the archive, for coordinator sessions only: a done one shows in
        the done column again (the automatic done window starts again from now), one archived from
        live goes back to live."""
        saved = await run(
            service.set_archived_mcp(
                task,
                False,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.task_archive_saved(saved)

    @write
    async def task_bind_session(
        task: TaskRef,
        session: Annotated[str, Field(description=SESSION_HELP)],
        session_title: SessionTitle = None,
        hook_session: Annotated[
            str | None,
            Field(
                description="Not linked here: pass your hook_session on your own writes instead."
            ),
        ] = None,
        caller: Annotated[
            str | None,
            Field(description="You, when `session` is someone else. Optional."),
        ] = None,
    ) -> str:
        """Mark that this session works on the task now; the board shows it with a link that
        opens the Claude desktop app session. Call it when you pick up an existing task.
        Pass your own desktop session id (local_…) if you know it. If you don't know your local_
        id, don't rebind — the coordinator binds you; pass your session name only as `session` on
        writes. A plain name never replaces a desktop session bound to the task: it is recorded
        as that session's alias. Binding a session ends the previous session's binding; the
        history is kept. It never links a hook session (the caller may bind someone else): your
        own `hook_session` goes on your other writes."""
        updated, kept, alias_recorded = await run(
            service.bind_session(
                task,
                session,
                session_title=session_title,
                caller=caller or session,
                hook_session=hook_session,
            )
        )
        ignored = (
            "\nNote: hook_session is not linked by task_bind_session; pass it on your own"
            " writes (report_add, task_update, …)."
            if hook_session
            else ""
        )
        if kept is not None:
            who = fmt.who_label(kept.name, kept.external_id)
            if not alias_recorded:
                return (
                    f"**{updated.key}** is already bound to {who}; pass your local id to rebind."
                    f" '{session.strip()}' was not recorded as its alias: a session with that"
                    " name already exists. Nothing changed." + ignored
                )
            return (
                f"Kept the desktop session {who} on **{updated.key}**; recorded"
                f" '{session.strip()}' as its alias. Pass your local_ id to rebind." + ignored
            )
        return fmt.task_saved(updated, f"Bound {fmt.session_label(updated)} to") + ignored

    @write
    async def task_unbind_session(
        task: TaskRef,
        session: Annotated[
            str | None,
            Field(
                description="The session you expect to be bound (desktop session id or name);"
                " if another session is bound, nothing changes and an error says who. Optional."
            ),
        ] = None,
        caller: Annotated[
            str | None,
            Field(description="You, when `session` is someone else. Optional."),
        ] = None,
        hook_session: HookSession = None,
    ) -> str:
        """Remove the bound session from a task, e.g. when a session stops working on it without
        a successor. The binding history is kept. To hand the task to another session, call
        task_bind_session instead."""
        updated, unbound = await run(
            service.unbind_session(
                task, session, caller=caller or session, hook_session=hook_session
            )
        )
        if unbound is None:
            return f"No session is bound to **{updated.key}**; nothing changed."
        who = fmt.who_label(unbound.name, unbound.external_id)
        return fmt.task_saved(updated, f"Unbound {who} from")

    @write
    async def report_add(
        task: TaskRef,
        body: Annotated[
            str,
            Field(
                description="Markdown report. Its first line is the summary; a first line"
                " 'state · size · gist' (e.g. 'verified · M · login fixed') also sets state"
                " (inspected, changed, verified, committed, pushed, blocked) and size (S, M, L)."
                " Mark each claim you checked with 'verified: <where>' (e.g. 'verified: pytest"
                " on Windows'); an M or L report without any mark gets a rules warning."
            ),
        ],
        progress: Annotated[
            str | None,
            Field(
                description=PROGRESS_HELP
                + " Saved together with the report; omit to keep it;"
                + CLEARS
            ),
        ] = None,
        next_step: Annotated[str | None, Field(description=NEXT_STEP_HELP + CLEARS)] = None,
        waiting_on: Annotated[str | None, Field(description=WAITING_ON_HELP + CLEARS)] = None,
        stage: Annotated[
            str | None,
            Field(
                description="Move the task to this stage in the same write, after the report,"
                " so a rule asking for a report since the last stage change is met; the move's"
                " rule warnings follow (strict rules refuse the report too). Omit: the answer"
                " names what the usual next move needs."
            ),
        ] = None,
        findings: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Findings outside this task, for the coordinator to triage (none"
                f" becomes a backlog item by itself), at most {report_items.ITEMS_MAX}:"
                ' [{"title": "Cart total rounds down", "body": "...", "location":'
                ' "web/cart.py:41", "kind": "bug", "area": "checkout"}]. title is required'
                f" (up to {backlog.TITLE_MAX} characters); kind is one of"
                f" {', '.join(backlog.KINDS)}."
            ),
        ] = None,
        questions: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Questions for the coordinator, at most"
                f' {report_items.ITEMS_MAX}: [{{"text": "Keep the old route?", "default":'
                ' "keep it until the next release"}]. Both are required: default is what'
                " happens if nobody answers."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Add a report to a task. Call it at the end of every stage with your final report: what
        changed, how it was checked, what is left. Pass `progress` too, so the task's summary of
        what is done stays current with every report. Also update next_step (what happens next)
        and waiting_on (who acts next) whenever the stage changes or you report. `stage` ends
        the stage in the same call; findings and questions outside the task go in `findings`
        and `questions`."""
        saved = await run(
            service.add_report_checked(
                task,
                body,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
                progress=cleared(progress),
                next_step=cleared(next_step),
                waiting_on=cleared(waiting_on),
                stage=stage or None,
                findings=findings,
                questions=questions,
            )
        )
        return fmt.with_warnings(
            "\n".join(fmt.report_saved(saved, stage or None)), fmt.waiting_on_hint(waiting_on)
        )

    @write
    async def decision_add(
        project: ProjectSlug,
        text: Annotated[str, Field(description="The decision, one or two sentences.")],
        task: Annotated[
            str | None, Field(description="Related task key; makes the decision task-scoped.")
        ] = None,
        decided_by: Annotated[str | None, Field(description="Who decided.")] = None,
        source: Annotated[str | None, Field(description=SOURCE_HELP)] = None,
        supersedes: Annotated[
            int | None,
            Field(
                description="Id of a current decision of the same project this one replaces;"
                " the old one stays in history marked superseded."
            ),
        ] = None,
        area: Annotated[
            str | None,
            Field(
                description="The area of the project it is about, by name (case does not"
                " matter): task_context shows a project decision on the cards of that area."
                " Optional."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Record a decision the user or the team made (scope, approach, trade-off), so later
        sessions find it in the project snapshot and decision_list. Decisions belong to a
        project (or one of its tasks); a fact for all projects goes to memory_add with scope
        'global'. To change a decision, add the new one with `supersedes`."""
        decision = await run(
            service.add_decision(
                project,
                text,
                task=task or None,
                decided_by=decided_by,
                source=source,
                supersedes=supersedes,
                area=area or None,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.decision_saved(decision)

    @mcp.tool(annotations=READ, structured_output=False)
    async def decision_list(
        project: ProjectSlug,
        task: Annotated[str | None, Field(description="Only this task's decisions.")] = None,
        query: Annotated[
            str | None, Field(description="Words to search for; best matches first.")
        ] = None,
        include_superseded: Annotated[
            bool, Field(description="Also list replaced decisions (marked superseded by #n).")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=200, description="How many.")] = 20,
    ) -> str:
        """Decisions of a project, newest first (or the best matches of `query`), with their
        source and supersede links. Current decisions only unless include_superseded."""
        proj, items = await run(
            service.list_decisions(
                project,
                task=task or None,
                query=query,
                include_superseded=include_superseded,
                limit=limit,
            )
        )
        return fmt.decisions(proj, items, query)

    @write
    async def memory_add(
        scope: Annotated[
            str,
            Field(
                description="global (every project), role (one role, e.g. coordinator) or"
                " project (one project)."
            ),
        ],
        kind: Annotated[
            str,
            Field(
                description="user (the owner's preferences), feedback (a lesson from a"
                " correction), project (a fact about a project) or reference (where to find"
                " something)."
            ),
        ],
        title: Annotated[str, Field(description="One line that names the fact.")],
        body: Annotated[
            str, Field(description="Markdown: the fact, why it holds, how to apply it.")
        ],
        project: Annotated[
            str | None, Field(description="Project slug, for scope project.")
        ] = None,
        role: Annotated[str | None, Field(description="Role name, for scope role.")] = None,
        tags: Annotated[str | None, Field(description="Comma separated tags.")] = None,
        source: Annotated[str | None, Field(description=SOURCE_HELP)] = None,
        review_at: Annotated[
            str | None, Field(description="Date to verify the note again, e.g. 2026-12-31.")
        ] = None,
        op: Annotated[
            str | None,
            Field(
                description="Leave empty on the first call. After candidates came back: add,"
                " update (rewrite target), supersede (replace target, which stays in history)"
                " or noop."
            ),
        ] = None,
        target: Annotated[
            int | None, Field(description="The candidate's id, for op update or supersede.")
        ] = None,
        base_version: Annotated[
            int | None, Field(description="With op=update: the candidate's version (v<n>).")
        ] = None,
        force_new: Annotated[
            bool, Field(description="With op=add: add even next to a strong match.")
        ] = False,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Save a memory note: an owner preference, feedback, a project fact or a reference.
        First call without `op`: if similar notes exist, nothing is written and they are
        listed; call again with op add / update / supersede / noop. Never put secrets here
        (a write that looks like one is refused). Visibility per role is not enforced yet."""
        result = await run(
            service.add_note(
                scope,
                kind,
                title,
                body,
                project=project or None,
                role=role or None,
                tags=tags,
                source=source,
                review_at=review_at,
                op=op or None,
                target=target,
                base_version=base_version,
                force_new=force_new,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.note_result(result)

    @write
    async def memory_update(
        id: Annotated[int, Field(description="The note's id.")],
        base_version: Annotated[
            int, Field(description="The note's version you read (memory_get shows it).")
        ],
        title: Annotated[str | None, Field(description="New title.")] = None,
        body: Annotated[str | None, Field(description="New body (the whole text).")] = None,
        tags: Annotated[str | None, Field(description="New tags." + CLEARS)] = None,
        review_at: Annotated[str | None, Field(description="New review date." + CLEARS)] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change a memory note. If someone changed it after you read it, nothing is saved and
        the current note comes back to merge; send it again with its version."""
        saved = await run(
            service.update_note(
                id,
                base_version,
                title=title,
                body=body,
                tags=cleared(tags),
                review_at=cleared(review_at),
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.note_updated(saved)

    @mcp.tool(annotations=READ, structured_output=False)
    async def audit_list(
        project: Annotated[str | None, Field(description="Only this project (slug).")] = None,
        environment: Annotated[str | None, Field(description="Only this stand (its name).")] = None,
        session: Annotated[
            str | None,
            Field(description="Only this session: its name, desktop id or hook session id."),
        ] = None,
        decision: Annotated[
            str | None, Field(description="Only this decision: allow, log, warn, ask or deny.")
        ] = None,
        days: Annotated[int, Field(ge=1, le=366, description="How many days back.")] = 7,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> str:
        """Agents' tool calls that touched a live stand, newest first: who, where, the redacted
        command, the guard's decision, level and rule, the task. Read-only: the "no live now"
        switch of a session is the owner's, on the board."""
        ids = await run(service.named_session_ids(session)) if session else None
        items = await run(
            service.audit(
                project=project or None,
                environment=environment or None,
                session_ids=ids,
                decision=decision or None,
                days=days,
                limit=limit,
            )
        )
        return fmt.audit(items, days)

    # --- questions to the owner ---------------------------------------------------------

    @write
    async def question_ask(
        text: Annotated[
            str,
            Field(
                description="The question, or for kind look what to check on the screen. Never a"
                " password or token."
            ),
        ],
        default: Annotated[
            str,
            Field(
                description="What you do when the owner does not answer: required, a question"
                " without a default is refused."
            ),
        ],
        task: Annotated[
            str | None,
            Field(
                description="Task key such as 'acme-shop-12', or its numeric id: the card the"
                " question is on. Required."
            ),
        ] = None,
        options: Annotated[
            list[str] | None,
            Field(description="Short answers the owner picks from (at most 8). Optional."),
        ] = None,
        blocking: Annotated[
            bool | None,
            Field(
                description="true: you end your turn and wait for the answer. Default false: you"
                " go on with the default. A look question is always blocking."
            ),
        ] = None,
        default_after: Annotated[
            str | None,
            Field(
                description="For a non-blocking question: when it closes with its default, a"
                " time with its zone (2026-12-01T15:00:00Z) or a duration (30m, 2h, 1d)."
                " Default 1h."
            ),
        ] = None,
        kind: Annotated[
            str | None,
            Field(
                description="question (default) or look: the owner looks at a screen you"
                " brought up and answers fine or remarks."
            ),
        ] = None,
        url: Annotated[
            str | None,
            Field(description="For kind look: the http(s) address the owner opens."),
        ] = None,
        account: Annotated[
            str | None,
            Field(
                description="For kind look: the name of the test account to sign in with, never"
                " its password."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Ask the owner a question on a card; it shows in the owner's Inbox and on the card, and
        only the owner answers it, on the board. Every question carries a default; at most 5 are
        open per card. A blocking one: end your turn, the answer comes as an owner comment
        through your hooks. kind look: bring up the screen first, pass its url and the test
        account's name."""
        asked, hint = await run(
            service.ask_question(
                task,
                text,
                default,
                options=options or None,
                blocking=blocking,
                default_after=default_after or None,
                kind=kind or None,
                url=url or None,
                account=account or None,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        found = await run(service.get_task(asked.task_id)) if asked.task_id else None
        return fmt.question_asked(found, asked, hint)

    @mcp.tool(annotations=READ, structured_output=False)
    async def question_list(
        task: Annotated[
            str | None, Field(description="Task key: only this card's questions. Optional.")
        ] = None,
        status: Annotated[
            str | None,
            Field(description="open (default), answered, defaulted, withdrawn or all."),
        ] = None,
    ) -> str:
        """Questions to the owner, newest first: open ones of all cards by default, with their
        state, options, default and the owner's answer verbatim. Changes nothing but closes
        questions past their deadline with their default."""
        found, items = await run(service.list_questions(task or None, status or None))
        return fmt.questions(found, items, status)

    @write
    async def question_withdraw(
        question: Annotated[int, Field(description="The question's number, e.g. 7 for '#7'.")],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Withdraw an open question you asked (a coordinator may withdraw any): it leaves the
        Inbox."""
        gone = await run(
            service.withdraw_question(
                question,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.question_withdrawn(gone)

    # --- approvals: the owner decides on the board ----------------------------------------

    @write
    async def approval_request(
        task: TaskRef,
        action: Annotated[
            str,
            Field(description="push, merge, commit, deploy or sql: what you want to do."),
        ],
        target: Annotated[
            str,
            Field(
                description="Where: for push and commit the branch (feat/x, or backend:feat/x"
                " with the repository; +feat/x for a force push, :feat/x to delete the"
                " branch); for merge the PR (12, #12 or its URL); for deploy and sql the"
                " stand, in words: test, live, prod (a plain name, no setup needed)."
            ),
        ],
        summary: Annotated[
            str,
            Field(
                description="What goes where: the commits, the files, the CI state. Never a"
                " password or token."
            ),
        ],
        rollback: Annotated[
            str | None,
            Field(description="The command that undoes it: required for a deploy."),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Ask the owner to approve one action on a card: it shows in the owner's Inbox and on
        the card, and only the owner decides, on the board. The guard denies a call that needs
        an approval until one is given; one approval allows exactly one matching call, run alone
        as its own command (a push approval names its branch; a force push or a delete needs one
        that says so). End your turn after asking: the decision comes as an owner
        comment through your hooks. An open request for the same
        action and target is updated instead of a second one."""
        asked, hint, updated = await run(
            service.request_approval(
                task,
                action,
                target,
                summary,
                rollback=rollback or None,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        found = await run(service.get_task(asked.task_id))
        return fmt.approval_requested(found, asked, hint, updated)

    @mcp.tool(annotations=READ, structured_output=False)
    async def approval_list(
        task: Annotated[
            str | None, Field(description="Task key: only this card's approvals. Optional.")
        ] = None,
        status: Annotated[
            str | None,
            Field(description="open (default), approved, refused, used, expired or all."),
        ] = None,
    ) -> str:
        """Approval requests and the owner's decisions, newest first: open ones of all cards by
        default, with the target, the summary, the decision with the owner's line verbatim, and
        which call used an approval. Read-only: no MCP tool decides."""
        found, items = await run(service.list_approval_requests(task or None, status or None))
        return fmt.approvals(found, items, status)

    @write
    async def approval_withdraw(
        approval: Annotated[int, Field(description="The approval's number, e.g. 7 for '#7'.")],
        reason: Annotated[
            str,
            Field(
                description="Why it is no longer needed, one line, e.g. 'the owner merged the"
                " PR by hand'. Shown on the card and in the owner's Inbox history."
            ),
        ],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Withdraw an approval request you asked (a coordinator may withdraw any) that is no
        longer needed: an open one, or an approved one not used yet. It expires with your
        reason, leaves the owner's Inbox, and no longer holds the card's archive."""
        gone = await run(
            service.withdraw_approval(
                approval,
                reason,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.approval_withdrawn(gone)

    @mcp.tool(annotations=READ, structured_output=False)
    async def memory_search(
        query: Annotated[str, Field(description="Words to look for (word forms match).")],
        scope: Annotated[
            str | None,
            Field(description="Only notes of this scope: global, role or project."),
        ] = None,
        project: Annotated[
            str | None,
            Field(description="Only this project's items plus global and role notes."),
        ] = None,
        kinds: Annotated[
            list[str] | None,
            Field(
                description="What to search: note, decision, doc, report, comment, backlog"
                " (default all; only notes when scope is given)."
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="How many hits.")] = 10,
    ) -> str:
        """Search memory notes, decisions, documents, reports, comments and backlog items. Each
        hit has its id and the matching line; open a note with memory_get, a document with
        doc_get and a backlog item with backlog_list."""
        hits = await run(
            service.search(query, project=project or None, kinds=kinds, scope=scope, limit=limit)
        )
        return fmt.search_hits(hits, query)

    @mcp.tool(annotations=READ, structured_output=False)
    async def memory_get(id: Annotated[int, Field(description="The note's id.")]) -> str:
        """A memory note in full: body, scope, source, dates, version (to change it later)."""
        return fmt.note(await run(service.get_note(id)))

    @mcp.tool(annotations=READ, structured_output=False)
    async def doc_list(project: ProjectSlug) -> str:
        """The documents of a project (spec, status, brief, runbooks, ADRs) with versions."""
        proj, items = await run(service.list_documents(project))
        return fmt.documents(proj, items, service.board_url)

    @mcp.tool(annotations=READ, structured_output=False)
    async def doc_get(
        project: ProjectSlug,
        doc: Annotated[str, Field(description="Document name, e.g. 'spec'.")],
        section: Annotated[
            str | None,
            Field(
                description="Section anchor from a heading ('## Gift cards' → gift-cards):"
                " returns that heading and its text only."
            ),
        ] = None,
        version: Annotated[int | None, Field(description="An older version.")] = None,
        lines: Annotated[
            str | None,
            Field(description="A line range 'a-b' (doc_outline and doc_search name the lines)."),
        ] = None,
        max_chars: Annotated[
            int | None,
            Field(
                description="Cap of the answer in characters. Default: no cap, except 40000 for"
                " a document over 200000 characters (a file document)."
            ),
        ] = None,
    ) -> str:
        """A project document as markdown (or one section, or a line range), with its current
        version to pass as base_version to doc_update; a file document names its file, hash and
        whether doc_update writes it, and a change made to it outside relaybay.
        A read over the cap returns the first lines, the outline of the rest and the call that
        reads on, never a silent cut. For a large document: doc_outline first."""
        found = await run(
            service.read_document(
                project,
                doc,
                section=section or None,
                lines=lines or None,
                max_chars=max_chars,
                version=version,
            )
        )
        marks = []
        links = None
        if not section and not lines and version is None and found.cut is None:
            whole = found.text if found.doc.is_file else found.doc.body
            if whole:
                links = fmt.broken_links(await run(service.broken_links(found.doc, whole)))
        if found.section is not None and found.section.anchor and version is None:
            by_anchor = await run(service.change_marks(found.doc, found.rows))
            marks = [
                service.change_mark_line(found.doc.project_slug, m)
                for m in by_anchor.get(found.section.anchor, [])
            ]
        text = fmt.document_read(found, service.board_url, marks)
        return "\n".join([text, *([links] if links else [])])

    @mcp.tool(annotations=READ, structured_output=False)
    async def doc_outline(
        project: ProjectSlug,
        doc: Annotated[str, Field(description="Document name, e.g. 'spec'.")],
        depth: Annotated[
            int | None, Field(ge=1, le=6, description="At most this many heading levels.")
        ] = None,
        under: Annotated[
            str | None, Field(description="Only the headings inside this section (its anchor).")
        ] = None,
    ) -> str:
        """The headings of a document, one line each: level, anchor (and explicit anchor),
        title, lines a-b, characters and the token estimate. Read a part with doc_get(section)
        or doc_get(lines)."""
        found, rows, parent = await run(
            service.document_outline(project, doc, depth=depth, under=under or None)
        )
        marks = await run(service.change_marks(found, rows))
        return fmt.document_outline(found, rows, parent, len(rows), service.board_url, marks)

    @mcp.tool(annotations=READ, structured_output=False)
    async def doc_search(
        project: ProjectSlug,
        query: Annotated[str, Field(description="Words to look for (word forms match).")],
        doc: Annotated[str | None, Field(description="Only this document.")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="How many sections.")] = 10,
    ) -> str:
        """Search a project's documents per section: each hit names the document, the section's
        anchor and heading path, the matching line with one line around it and the section's
        lines and size, so doc_get reads just that part."""
        proj, hits = await run(
            service.search_documents(project, query, doc=doc or None, limit=limit)
        )
        return fmt.document_hits(proj, hits, query, service.board_url)

    @write
    async def doc_update(
        project: ProjectSlug,
        doc: Annotated[str, Field(description="Document name: lowercase letters, digits, dashes.")],
        body: Annotated[
            str,
            Field(
                description="Markdown: the whole document, or with `section` that section"
                " (starting with its heading to rename it, or just the text under it)."
            ),
        ],
        reason: Annotated[str, Field(description="What changed and why, one line.")],
        base_version: Annotated[
            int,
            Field(description="The version you read (doc_get shows it); 0 creates the document."),
        ],
        section: Annotated[
            str | None, Field(description="Replace only this section (its anchor).")
        ] = None,
        title: Annotated[
            str | None, Field(description="Title; default: the first '# ' heading.")
        ] = None,
        kind: Annotated[
            str | None,
            Field(
                description="spec, status, task, brief, runbook, adr, retro or your own"
                " (default doc)."
            ),
        ] = None,
        source_url: Annotated[
            str | None,
            Field(
                description="http(s) link when the document lives in a repository: the board"
                " links there and later writes need force; '-' unlinks."
            ),
        ] = None,
        force: Annotated[
            bool, Field(description="Write a document that lives in a repository anyway.")
        ] = False,
        source_path: Annotated[
            str | None,
            Field(
                description="Absolute path of a file on this machine: with base_version 0 and"
                " body '-' it becomes a file document that relaybay indexes on demand."
            ),
        ] = None,
        lines: Annotated[
            str | None,
            Field(description="Replace only these lines 'a-b' (with expected); body '-' removes."),
        ] = None,
        expected: Annotated[
            str | None,
            Field(description="The current text of `lines` as you read it; else a conflict."),
        ] = None,
        file_writes: Annotated[
            Literal["on", "off"] | None,
            Field(
                description="Coordinators only: turn writes through relaybay into a file"
                " document's file on or off (default off); with body '-' that is all it does."
            ),
        ] = None,
        delete: Annotated[
            bool,
            Field(
                description="Delete the document: base_version must be the current version,"
                " body is ignored (pass '-'), reason says why. An export is written first; a"
                " file document's file stays on disk. spec and status documents: the owner or a"
                " coordinator only (spec-<task key> also the session bound to that task)."
            ),
        ] = False,
        from_path: Annotated[
            str | None,
            Field(
                description="Absolute path of a local text or markdown file to import once as"
                " the whole body (body '-'): a new document (base_version 0) or a whole write of"
                " a database document. The document keeps no link to the file."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Write a new version of a project document (base_version 0 creates it). If it changed
        since you read it, nothing is saved and the current text comes back to merge. With
        lines + expected only those lines change, as long as they still read as expected. A
        task's document is `task-<ref>`; point the task at it with task_update(doc_path=
        'relaybay://doc/<project>/task-<ref>'). A document linked with source_url may have body
        '-' (no text of its own). A file document (source_path) whose writes through relaybay
        are on is written in its file: only the replaced lines change, its line endings stay;
        a change made to the file outside relaybay is a new version first. With writes off it
        is refused: edit the file. With delete the document is deleted (see delete). A write
        ends with a warning that lists the links leading nowhere: relaybay://doc links to a
        missing document or section and in-document (#anchor) links."""
        if delete and from_path:
            raise ToolError("from_path imports a document; it does not go with delete.")
        if delete:
            gone, path = await run(
                service.delete_document(
                    project,
                    doc,
                    reason,
                    base_version,
                    session=session,
                    session_title=session_title,
                    hook_session=hook_session,
                )
            )
            return fmt.document_deleted(gone, path.name)
        saved = await run(
            service.update_document(
                project,
                doc,
                body,
                reason,
                base_version,
                section=section or None,
                title=title or None,
                kind=kind or None,
                source_url=source_url or None,
                source_path=source_path or None,
                lines=lines or None,
                expected=expected,
                force=force,
                file_writes=file_writes or None,
                from_path=from_path or None,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        warning = await run(service.change_doc_warning(saved))
        links = None
        if saved.is_file and source_path:
            _, rows, _ = await run(service.document_outline(project, saved.slug))
            text = fmt.file_document_saved(saved, len(rows))
        elif saved.is_file and body.strip() == "-":
            text = fmt.file_writes_set(saved)
        else:
            _, whole = await run(service.get_document(project, saved.slug))
            links = fmt.broken_links(await run(service.broken_links(saved, whole)))
            text = fmt.file_document_written(saved) if saved.is_file else fmt.document_saved(saved)
            if from_path:
                text += f" Imported from {from_path.strip()} ({len(whole)} characters)."
        return "\n".join([text, *([warning] if warning else []), *([links] if links else [])])

    @mcp.tool(annotations=READ, structured_output=False)
    async def doc_history(
        project: ProjectSlug,
        doc: Annotated[str, Field(description="Document name.")],
        from_version: Annotated[
            int | None, Field(description="With to_version: show the diff between them.")
        ] = None,
        to_version: Annotated[int | None, Field(description="See from_version.")] = None,
    ) -> str:
        """The versions of a document (author, reason, section, size), newest first; with
        from_version and to_version also the unified diff between those two. A file document
        lists the changes relaybay saw in its file (sections, hash before and after)."""
        found, versions = await run(service.document_history(project, doc))
        if found.is_file:
            found, events = await run(service.file_history(project, doc))
            return fmt.file_history(found, events)
        diff: list[str] = []
        if from_version is not None and to_version is not None:
            _, diff = await run(service.document_diff(project, doc, from_version, to_version))
        return fmt.document_history(found, versions, diff)

    @mcp.tool(annotations=READ, structured_output=False)
    async def block_get(
        label: Annotated[
            str | None, Field(description="Block label; omit to list the blocks.")
        ] = None,
        project: Annotated[
            str | None, Field(description="Project slug; omit for global blocks.")
        ] = None,
    ) -> str:
        """A memory block (a short labelled text shown at its level, e.g. 'owner-preferences')
        with its version and character limit, or the list of blocks."""
        if not label:
            return fmt.blocks(await run(service.list_blocks(project or None)), project)
        return fmt.block(await run(service.get_block(label, project or None)))

    @write
    async def block_set(
        label: Annotated[str, Field(description="Block label, e.g. 'owner-preferences'.")],
        body: Annotated[str, Field(description="The whole new text; must fit the limit.")],
        base_version: Annotated[
            int, Field(description="The version you read (block_get); 0 creates the block.")
        ],
        project: Annotated[
            str | None, Field(description="Project slug; omit for a global block.")
        ] = None,
        description: Annotated[
            str | None, Field(description="What the block is for, one line.")
        ] = None,
        char_limit: Annotated[
            int | None, Field(description="Character limit (default 2000 for a new block).")
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Write a memory block. Over its character limit the write is refused with the count.
        Owner-only blocks are read-only here (the owner edits them on the board)."""
        saved = await run(
            service.set_block(
                label,
                body,
                base_version,
                project=project or None,
                description=description,
                char_limit=char_limit,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.block_saved(saved)

    @mcp.tool(annotations=READ, structured_output=False)
    async def comments_list(
        task: TaskRef,
        unread_only: Annotated[
            bool,
            Field(
                description="Only unread ones: owner comments not acknowledged yet and"
                " replies the owner has not read."
            ),
        ] = False,
    ) -> str:
        """Comments on a task, oldest first: the owner's (with sent / delivered / read marks)
        and agents' replies. Call it when a hook or the session start says you have unread
        owner comments."""
        row, items = await run(service.list_comments(task, unread_only=unread_only))
        return fmt.comments(row, items, unread_only)

    @write
    async def comment_ack(
        comment_id: Annotated[
            int | None, Field(description="The owner comment to mark read, e.g. 7 for '#7'.")
        ] = None,
        task: Annotated[
            str | None,
            Field(description="Task key: mark every unread owner comment on it read."),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Acknowledge owner comments once you have handled them: one by `comment_id`, or all
        unread ones of `task`. The board then shows them as read, and the Stop hook stops
        reminding you of them."""
        row, read = await run(
            service.ack_comments(
                comment_id=comment_id,
                task=task or None,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        if not read:
            return f"Nothing to acknowledge on **{row.key}**: no unread owner comment."
        return f"Marked {', '.join(f'#{i}' for i in read)} read on **{row.key}**."

    @write
    async def comment_add(
        task: TaskRef,
        body: Annotated[str, Field(description="Your reply to the owner, markdown.")],
        in_reply_to: Annotated[
            int | None,
            Field(
                description="The owner comment you answer; it is marked read. Without it, every"
                " unread owner comment on the task is marked read."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Reply to the owner on a task's comment thread (shown on the board and in the
        owner's Inbox). Use it to answer an owner comment or to ask a short question; reports
        stay in report_add."""
        comment, read = await run(
            service.add_agent_comment(
                task,
                body,
                in_reply_to=in_reply_to,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        marked = f"; marked {', '.join(f'#{i}' for i in read)} read" if read else ""
        return f"Comment #{comment.id} added to **{comment.task_key}**{marked}."

    # --- backlog ------------------------------------------------------------------------

    @write
    async def backlog_add(
        project: ProjectSlug,
        title: Annotated[
            str | None,
            Field(description="Short title of the finding; required unless from_report gives it."),
        ] = None,
        body: Annotated[str | None, Field(description="What and why, markdown.")] = None,
        area: Annotated[
            str | None, Field(description="Free area, e.g. 'checkout' or 'board'.")
        ] = None,
        task: Annotated[
            str | None, Field(description="The task you found it in. Optional.")
        ] = None,
        priority: BacklogPriority = None,
        assignee: BacklogAssignee = None,
        kind: BacklogKind = None,
        location: BacklogLocation = None,
        from_report: Annotated[
            int | None,
            Field(
                description="Triage a finding a report carries (report_add findings): the"
                " report id; pass `finding` with it. The finding fills title, body, location,"
                " kind and area unless given, its task becomes the item's task, and the digests"
                " stop listing it."
            ),
        ] = None,
        finding: Annotated[
            int | None, Field(description="With from_report: the finding's number, from 1.")
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Put a finding outside your task (a bug, a missing feature, a question for later) on
        the project's backlog instead of a comment or a report. It starts as new; the owner
        decides on the board ("To plan" creates the task). It is always added; open items with
        a similar title are named so that a duplicate can be closed."""
        item = await run(
            service.add_backlog_item(
                project,
                title or None,
                body=cleared(body),
                area=cleared(area),
                task=task or None,
                priority=priority,
                assignee=cleared(assignee),
                kind=cleared(kind),
                location=cleared(location),
                from_report=from_report,
                finding=finding,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        similar = await run(service.similar_backlog(item))
        return fmt.backlog_saved(item, "Added") + fmt.backlog_similar(item, similar)

    @write
    async def report_item_dismiss(
        report: Annotated[int, Field(description="The report id (report #N).")],
        n: Annotated[int, Field(ge=1, description="The item's number in the report, from 1.")],
        kind: Annotated[
            str, Field(description="finding or question; default finding.")
        ] = report_items.FINDING,
        reason: Annotated[
            str | None,
            Field(
                description="Why it needs no backlog item; for a question, what was decided."
                " Optional."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Close a finding or question a report carries (report_add findings / questions)
        without a backlog item: the digests stop listing it. A finding worth keeping goes to
        backlog_add(from_report=, finding=) instead."""
        item = await run(
            service.dismiss_report_item(
                report,
                n,
                kind=kind,
                reason=cleared(reason) or None,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.report_item_dismissed(item)

    @mcp.tool(annotations=READ, structured_output=False)
    async def backlog_list(
        project: Annotated[str | None, Field(description="Project slug. Optional.")] = None,
        status: Annotated[
            str | None,
            Field(description="new, later, planned, done or declined. Optional."),
        ] = None,
        area: Annotated[str | None, Field(description="Area. Optional.")] = None,
        assignee: Annotated[
            str | None,
            Field(description="Who it is for; '-' lists the items for nobody. Optional."),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Words that must all be in the title, the text, the location, the"
                " source or the decision (word forms match). Optional."
            ),
        ] = None,
        kind: Annotated[
            str | None,
            Field(
                description=f"{', '.join(backlog.KINDS)}; '-' lists the items without a kind."
                " Optional."
            ),
        ] = None,
        since: Annotated[
            str | None,
            Field(description="A day like 2026-11-05: items found on it or later. Optional."),
        ] = None,
        task: Annotated[
            str | None,
            Field(description="A task key: items found in it or linked to it. Optional."),
        ] = None,
        has_location: Annotated[
            bool | None,
            Field(description="true: only items with a code location; false: only without."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="At most this many.")] = 50,
    ) -> str:
        """Backlog items: new first, the highest priority first within a status, then newest."""
        items = await run(
            service.list_backlog(
                project=project,
                status=status,
                area=area,
                assignee=assignee,
                query=query,
                kind=kind,
                since=since,
                task=task or None,
                has_location=has_location,
                limit=limit,
            )
        )
        return fmt.backlog_items(items)

    @write
    async def backlog_update(
        item: Annotated[int, Field(description="The item's number, e.g. 7.")],
        status: Annotated[
            str | None,
            Field(
                description="new, later, done (fixed without a task of its own, with an"
                " optional link) or declined (with reason); coordinator sessions only. planned"
                " is never set here: the owner's 'To plan' creates the task."
            ),
        ] = None,
        reason: Annotated[
            str | None, Field(description="Why, required to decline; a note for done.")
        ] = None,
        link: Annotated[
            str | None,
            Field(description="With status done: where it was fixed, an http(s) link (a PR)."),
        ] = None,
        title: Annotated[str | None, Field(description="New title.")] = None,
        body: Annotated[str | None, Field(description="New body, markdown." + CLEARS)] = None,
        area: Annotated[str | None, Field(description="New area." + CLEARS)] = None,
        priority: BacklogPriority = None,
        assignee: BacklogAssignee = None,
        kind: BacklogKind = None,
        location: BacklogLocation = None,
        triage_note: Annotated[
            str | None,
            Field(
                description=f"The decision in words, up to {backlog.TRIAGE_MAX} characters;"
                " coordinator sessions only." + CLEARS
            ),
        ] = None,
        duplicate_of: Annotated[
            int | None,
            Field(
                ge=1,
                description="Close it as a duplicate of this item of the same project (status"
                " declined, reason 'duplicate of #N'); coordinator sessions only.",
            ),
        ] = None,
        task: Annotated[
            str | None,
            Field(
                description="Link an existing card of the same project (status planned);"
                " coordinator sessions only. 'To plan' on the board creates a new one.",
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change a backlog item. Its title, body, area, priority, assignee, kind and location:
        the session that added it, while it is new. Its status, triage note, duplicate and
        linked card: coordinator sessions named in the server's config only; everyone else
        leaves that to the owner on the board."""
        saved = await run(
            service.update_backlog_item(
                item,
                status=status,
                reason=reason,
                link=link or None,
                title=title,
                body=cleared(body),
                area=cleared(area),
                priority=priority,
                assignee=cleared(assignee),
                kind=cleared(kind),
                location=cleared(location),
                triage_note=cleared(triage_note),
                duplicate_of=duplicate_of,
                task=task or None,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.backlog_saved(saved, "Updated")

    @write
    async def backlog_patch(
        item: Annotated[int, Field(description="The item's number, e.g. 7.")],
        fields: Annotated[
            dict[str, Any],
            Field(
                description='Item fields to change as one JSON object, e.g. {"priority": 2}.'
                f" Allowed: {', '.join(backlog.PATCH_FIELDS)}. '-', '' or null clears body, area,"
                " assignee, kind, location and triage_note, and resets priority to 0."
            ),
        ],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change any backlog item fields given as one JSON object, with the same rules as
        backlog_update. Use it for a field your client's backlog_update does not offer yet: its
        parameter list may be older than the server."""
        if not fields:
            raise ToolError("Nothing to update: pass at least one field in `fields`.")
        unknown = sorted(set(fields) - set(backlog.PATCH_FIELDS))
        if unknown:
            raise ToolError(
                f"Unknown backlog fields: {', '.join(unknown)}."
                f" Allowed: {', '.join(backlog.PATCH_FIELDS)}."
            )
        changes: dict[str, Any] = {}
        for name, value in fields.items():
            if not isinstance(value, str | None) and name not in ("priority", "duplicate_of"):
                raise ToolError(f"{name} must be text.")
            changes[name] = (
                "" if value is None else cleared(value) if isinstance(value, str) else value
            )
        for name in ("link", "duplicate_of", "task"):
            if name in changes:
                changes[name] = changes[name] or None
        saved = await run(
            service.update_backlog_item(
                item,
                **changes,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.backlog_saved(saved, "Updated")

    @mcp.tool(annotations=READ, structured_output=False)
    async def rules_get(
        project: Annotated[
            str | None,
            Field(description="Project slug for its merged rules; omit for the global rules."),
        ] = None,
    ) -> str:
        """The working rules: report format (first line, states, sizes), what a stage move
        needs (e.g. a report before test, progress before done), the description template and
        guidance, with the version and the rules hash. Call it at the start of a session, or when
        the version in project_snapshot changed or a response says the rules were updated."""
        return fmt.rules_text(await run(service.rules(project or None)))

    RulesScope = Annotated[str, Field(description="'global' or a project slug.")]
    RulesPatch = Annotated[
        dict[str, Any],
        Field(
            description="Merged on top of the scope's current version: objects key by key, other"
            " values (lists included) replace, null removes a key (a project then follows the"
            " global rules, the global rules the built-in default). Keys: report_format"
            " ({first_line, states, sizes}), stage_requirements ({stage: [requirement]}; [] drops"
            " a stage's requirements), description_template, guidance. The result must be valid"
            " rules (rules_get shows the current ones)."
        ),
    ]

    @write
    async def rules_update(
        scope: RulesScope,
        patch: RulesPatch,
        reason: Annotated[str, Field(description="Why the rules change; kept in the history.")],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change the working rules directly: a new version of the scope. Only for coordinator
        sessions named in the server's config; everyone else gets an error and uses
        rules_propose. An invalid patch is refused with the reason."""
        canonical, number = await run(
            service.update_rules(
                scope,
                patch,
                reason,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        rules = await run(service.rules_of_scope(canonical))
        return fmt.rules_saved(canonical, number, rules)

    @write
    async def rules_propose(
        scope: RulesScope,
        patch: RulesPatch,
        reason: Annotated[str, Field(description="Why the change is needed, for the owner.")],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Propose a change to the working rules. Nothing changes until the owner accepts it on
        the board (Inbox, Settings → Rules); it is checked again against the current version
        then. A rejected proposal stays in the history."""
        proposal = await run(
            service.propose_rules(
                scope,
                patch,
                reason,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.rule_proposal_saved(proposal)

    @mcp.tool(annotations=READ, structured_output=False)
    async def events_since(
        since_id: Annotated[int, Field(ge=0, description="Last event id you have seen.")] = 0,
        project: Annotated[str | None, Field(description="Project slug filter.")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> str:
        """Events after `since_id` (task created, stage changed, report added, ...), oldest
        first. Call it to catch up on what other sessions did since you last looked."""
        items = await run(service.events_since(since_id, project, limit))
        return fmt.events(items, since_id)

    @mcp.tool(annotations=READ, structured_output=False)
    async def review_get(task: TaskRef) -> list[str | Image]:
        """The task's review checklist: each step with its status (open, accepted, rejected), what
        to check, its screenshots with captions and ids, the owner's pins on them with coordinates
        and replies, plus the owner's comments on the steps. Flattened images with the owner's
        markup come as image content after the text. Call it after an owner decision arrives, or
        before you change the checklist."""
        row, review, comments = await run(service.review(task))
        annotations, images, notes = await run(service.review_markup(row, review))
        text = fmt.review_text(row, review, comments, annotations, images, notes)
        return [text, *(Image(data=data, format="png") for data in images.values())]

    @mcp.tool(annotations=READ, structured_output=False)
    async def attachment_get(
        id: Annotated[int, Field(description="The screenshot's id, as review_get lists it.")],
        annotated: Annotated[
            bool,
            Field(description="True: the flattened PNG with the owner's markup, not the original."),
        ] = False,
    ) -> list[str | Image]:
        """One screenshot as image content, with a text line and its pins. An image too large for
        a tool answer is refused with a message that says so."""
        item, data, mime = await run(service.attachment_image(id, annotated=annotated))
        _, marks = await run(service.annotations(item.id))
        what = "Annotated image" if annotated else "Original"
        lines = [
            f"{what} of screenshot #{item.id} on **{item.task_key}** ({mime}, {len(data)} bytes)."
        ]
        lines.extend(fmt.annotation_lines(marks))
        return ["\n".join(lines), Image(data=data, format=mime.split("/")[1])]

    @write
    async def annotation_reply(
        annotation: Annotated[
            int, Field(description="The pin's annotation id, as the owner's comment names it.")
        ],
        body: Annotated[str, Field(description="Your answer, up to 2000 characters.")],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Answer a pin the owner put on a screenshot. The owner reads it in the pin's thread and
        in the task's thread. Secret values are refused."""
        item, pin = await run(
            service.reply_as_agent(
                annotation,
                body,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return f"Replied to pin #{pin.pin_number} of screenshot #{item.id} on **{item.task_key}**."

    @write
    async def review_steps_set(
        task: TaskRef,
        steps: Annotated[
            list[dict[str, Any]],
            Field(
                description="The whole checklist in order, e.g. [{'title': 'Login with 2FA',"
                " 'details': 'What to check, how and why (markdown)'}]. It replaces the current"
                " one: step N keeps its decision and screenshots while its title and details stay"
                " the same; a changed step is open again; steps past the end are removed."
            ),
        ],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Write what the owner should check by eye (the Owner check) as an ordered checklist the
        owner accepts step by step on the board. Do it before moving the task to review or
        test."""
        row, review = await run(
            service.set_review_steps(
                task,
                steps,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.review_saved(row, review)

    @write
    async def review_step_update(
        task: TaskRef,
        step: Annotated[int, Field(ge=1, description="The step's number in the checklist.")],
        title: Annotated[str | None, Field(description="New title.")] = None,
        details: Annotated[
            str | None, Field(description="New 'what, how and why' text (markdown).")
        ] = None,
        status: Annotated[
            str | None,
            Field(
                description="Only 'open': ask the owner to look again after a fix. Accepting and"
                " rejecting is the owner's, on the board."
            ),
        ] = None,
        remark: Annotated[
            str | None,
            Field(
                description="A comment under the step on the board (markdown), e.g. what you"
                " fixed or where to look; it does not change the step's status."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change one review step: its text, or reopen it after you fixed what the owner
        rejected. A changed text opens the step again. `remark` adds your comment under the
        step without changing its status."""
        updated = await run(
            service.update_review_step(
                task,
                step,
                title=title,
                details=details,
                status=status,
                remark=remark,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        said = " Your remark is under it." if (remark or "").strip() else ""
        return f"Step {updated.position} '{updated.title}' is {updated.status}.{said}"

    @mcp.tool(annotations=READ, structured_output=False)
    async def plan_get(task: TaskRef) -> str:
        """The task's plan: status (draft or accepted), each step with its status and, when done,
        its checkpoint (time, conformance, outcome, link), plus your Claude Code todo as a
        separate list. Call it when you pick up a task and before you change the plan."""
        row, plan, todo = await run(service.plan(task))
        return fmt.plan_text(row, plan, todo)

    @write
    async def plan_set(
        task: TaskRef,
        steps: Annotated[
            list[dict[str, Any]],
            Field(
                description="The whole plan in order, 2-5 steps as a rule, e.g. [{'title':"
                " 'Migration and model', 'details': 'What and why, 1-2 sentences (markdown)'}]."
                " It replaces the current plan: step N keeps its status and checkpoint while"
                " its title stays the same; a renamed step starts again as pending; steps past"
                " the end are removed; an empty list removes the plan."
            ),
        ],
        reason: Annotated[
            str | None,
            Field(description="Why the plan is written or changed, one line; kept in its history."),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Write the task's plan (from PLAN.md or your own 2-5 steps) before you start coding.
        A new plan is a draft until the owner (board) or the coordinator (plan_accept) accepts
        it; changing the titles or the order of an accepted plan makes it a draft again."""
        row, plan, reverted = await run(
            service.set_plan(
                task,
                steps,
                reason=reason,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.plan_saved(row, plan, reverted)

    @write
    async def plan_step_update(
        task: TaskRef,
        step: Annotated[int, Field(ge=1, description=PLAN_STEP_HELP)],
        status: Annotated[str, Field(description="One of: pending, in_progress, done, blocked.")],
        outcome: Annotated[
            str | None,
            Field(description="One line: what came out of the step (or why it is blocked)."),
        ] = None,
        link: Annotated[
            str | None,
            Field(description="Done only: a report of this task ('12') or a PR link."),
        ] = None,
        conformance: Annotated[
            str | None,
            Field(
                description="Required with done: 'as planned' or 'deviation: <why it went"
                " otherwise>'."
            ),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Mark a plan step in_progress when you start it and done when it is finished (a
        checkpoint: one line 'as planned' or 'deviation: why', optionally an outcome and a link
        to the report or PR). Status changes never turn an accepted plan back into a draft."""
        _, updated = await run(
            service.update_plan_step(
                task,
                step,
                status,
                outcome=outcome,
                link=cleared(link) or None,
                conformance=conformance,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.plan_step_saved(updated)

    @write
    async def plan_step_add(
        task: TaskRef,
        title: Annotated[str, Field(description="The new step's title.")],
        reason: Annotated[
            str,
            Field(
                description="Required: why the step is added, one line (e.g. the owner's"
                " decision after review); kept on the step and in the plan's history."
            ),
        ],
        details: Annotated[
            str | None, Field(description="What and why, 1-2 sentences (markdown).")
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Append a pending step to the plan when the work changes or grows after a report.
        An accepted plan stays accepted (the step is marked 'added after acceptance'); a task in
        review or test goes back to dev. Also update progress and next_step."""
        row, step = await run(
            service.add_plan_step(
                task,
                title,
                details=details,
                reason=reason,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.plan_step_added(row, step)

    @write
    async def plan_accept(
        task: TaskRef,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Accept the task's plan as the coordinator (the owner accepts on the board). Agents do
        not accept their own plans."""
        row, plan, accepted = await run(
            service.accept_plan(
                task,
                actor="mcp",
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        if not accepted:
            return f"The plan of **{row.key}** is already accepted; nothing changed."
        text = f"Plan of **{row.key}** accepted: {len(plan.steps)} step(s)."
        if row.stage.value == "plan":
            text += (
                " The card is still in plan: its session moves it with"
                f' task_update(task="{row.key}", stage="dev") when the work starts.'
            )
        return text

    @write
    async def attach_file(
        task: TaskRef,
        path: Annotated[
            str | None,
            Field(
                description="Absolute path of a PNG, JPEG, WebP or GIF file inside the project's"
                " repository, the temp folder or a folder in `attachment_roots`. It is copied;"
                " the original stays."
            ),
        ] = None,
        content_base64: Annotated[
            str | None,
            Field(description="The image itself, base64, instead of a path (same rules)."),
        ] = None,
        filename: Annotated[
            str | None, Field(description="A display name for content_base64.")
        ] = None,
        step: Annotated[
            int | None, Field(ge=1, description="Review step number it belongs to.")
        ] = None,
        caption: Annotated[
            str | None, Field(description="What the screenshot shows (markdown).")
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Attach a screenshot to a task, optionally to one review step, with a caption. The type
        is checked by content (SVG and other files are refused) and sizes are limited. See the
        how-to 'Screenshots for review' for producing the file."""
        item, new = await run(
            service.attach_file(
                task,
                path=path,
                content_base64=content_base64,
                filename=filename,
                step=step,
                caption=caption,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.attachment_saved(item, new)

    @write
    async def attachment_caption(
        attachment_id: Annotated[int, Field(description="The attachment id from attach_file.")],
        caption: Annotated[str, Field(description="New caption (markdown);" + CLEARS)],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Change the caption of a screenshot."""
        item = await run(
            service.set_attachment_caption(
                attachment_id,
                cleared(caption),
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return f"Caption of attachment #{item.id} on **{item.task_key}** saved."

    @mcp.tool(annotations=READ, structured_output=False)
    @contract.deprecated()
    async def environments_get(project: ProjectSlug) -> str:
        """Removed in 0.15 with the environments: shows nothing."""
        return contract.STUB_RESULT

    @mcp.tool(annotations=WRITE, structured_output=False)
    @contract.deprecated()
    async def environment_propose(
        project: ProjectSlug,
        change: Annotated[dict[str, Any], Field(description="Ignored.")],
        reason: Annotated[str, Field(description="Ignored.")],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Removed in 0.15 with the environments: records nothing."""
        return contract.STUB_RESULT

    @mcp.tool(annotations=READ, structured_output=False)
    async def policy_get(
        project: Annotated[
            str | None,
            Field(description="Project slug; omit for the global policy."),
        ] = None,
        environment: Annotated[
            str | None,
            Field(description="Environment name in the project, for its own policy."),
        ] = None,
    ) -> str:
        """The effective policy (levels per environment kind and capability, guard mode and
        rules, secret rules, test secrets) with, for every value, the layer that set it:
        preset, config, global file, project, environment. Read it to know why the guard or a
        secret check answered as it did."""
        pol = await run(service.effective_policy(project or None, environment or None))
        scope = "/".join(x for x in (project, environment) if x) or "global"
        return fmt.policy_text(pol, scope)

    @write
    async def policy_propose(
        scope: Annotated[
            str,
            Field(description="'global', a project slug, or '<project>/<environment>'."),
        ],
        patch: Annotated[
            dict[str, Any],
            Field(
                description="Merged into that layer's JSON: objects key by key, other values"
                " (lists included) replace, null removes a key. Keys: levels"
                " ({kind: {default|capability: level}}), guard ({mode, disable, patterns}),"
                " secrets ({disable, allow, test_secrets, expose_test_secrets_to_agents}). The"
                " result must validate against the policy schema (policy_get shows the current"
                " values)."
            ),
        ],
        reason: Annotated[str, Field(description="Why the change is needed, for the owner.")],
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
    ) -> str:
        """Propose a change to the policy. Nothing changes until the owner accepts it on the
        board (Settings → Policy); it shows in the owner's Inbox."""
        proposal = await run(
            service.propose_policy_change(
                scope,
                patch,
                reason,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
            )
        )
        return fmt.policy_proposal_saved(proposal)

    @mcp.resource(
        "relaybay://attachments/{attachment_id}/annotated",
        name="annotated-screenshot",
        description="The flattened PNG of a screenshot with the owner's markup"
        " (same as attachment_get with annotated=true).",
        mime_type="image/png",
    )
    async def annotated_resource(attachment_id: str) -> bytes:
        if not attachment_id.isdigit():
            raise ResourceNotFoundError(f"No attachment {attachment_id!r}.")
        try:
            _, data, _ = await service.attachment_image(int(attachment_id), annotated=True)
        except NotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        except ValidationError as exc:
            raise ResourceError(str(exc)) from exc
        return data

    @mcp.resource(
        "relaybay://project/{slug}/environments",
        name="project-environments",
        description=f"Removed in 0.15 with the environments. {contract.ENVIRONMENTS_GONE}",
        mime_type="text/markdown",
    )
    async def environments_resource(slug: str) -> str:
        return contract.STUB_RESULT + "\n" + contract.ENVIRONMENTS_GONE

    @mcp.resource(
        "relaybay://project/{slug}",
        name="project-snapshot",
        description="Markdown snapshot of a project (same as project_snapshot).",
        mime_type="text/markdown",
    )
    async def project_resource(slug: str) -> str:
        try:
            return fmt.snapshot(await service.snapshot(slug), await log_rule())
        except NotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc

    @mcp.resource(
        "relaybay://doc/{project}/{doc}",
        name="project-document",
        description="A project document's current markdown (same as doc_get).",
        mime_type="text/markdown",
    )
    async def document_resource(project: str, doc: str) -> str:
        try:
            _, text = await service.get_document(project, doc)
        except NotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        return text

    @mcp.resource(
        "relaybay://rules",
        name="rules",
        description="The global working rules (same as rules_get without a project).",
        mime_type="text/markdown",
    )
    async def rules_resource() -> str:
        return fmt.rules_text(await service.rules())

    @mcp.resource(
        "relaybay://rules/{project}",
        name="project-rules",
        description="A project's working rules: global rules merged with the project override.",
        mime_type="text/markdown",
    )
    async def project_rules_resource(project: str) -> str:
        try:
            return fmt.rules_text(await service.rules(project))
        except NotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc

    @mcp.resource(
        "relaybay://schema/task",
        name="task-schema",
        description="Editable task fields for task_update(fields=...): name, type, description,"
        " clearing.",
        mime_type="text/markdown",
    )
    async def task_schema_resource() -> str:
        return fmt.task_schema()

    @mcp.resource(
        "relaybay://contract",
        name="contract",
        description="The MCP contract: its version, the compatibility rule and the deprecations.",
        mime_type="text/markdown",
    )
    async def contract_resource() -> str:
        lines = ["# relaybay MCP contract", "", f"Server {__version__}.", ""]
        lines += contract.summary()
        return "\n".join(lines)

    async def prompt_context(project: str, task: str) -> tuple[Any, Any]:
        # Only an MCPError keeps its message: the SDK turns anything else into "Error rendering".
        try:
            rules = await service.rules(project)
            found = await service.get_task(task.strip()) if task.strip() else None
        except DomainError as exc:
            raise MCPError(INVALID_PARAMS, str(exc)) from exc
        if found is not None and found.project_slug != rules.project:
            message = f"Task {found.key} is not in project {rules.project}."
            raise MCPError(INVALID_PARAMS, message)
        return rules, found

    @mcp.prompt(
        name="start_task",
        title="Start a task",
        description="The project's current working rules and a checklist for starting a task.",
    )
    async def start_task(
        project: Annotated[str, Field(description="Project slug such as 'acme-shop'.")],
        task: Annotated[str, Field(description="Task key; empty to pick or create one.")] = "",
    ) -> str:
        rules, found = await prompt_context(project, task)
        return fmt.start_task_prompt(rules, found)

    @mcp.prompt(
        name="finish_stage",
        title="Finish a stage",
        description="The project's current working rules and a checklist for ending a stage.",
    )
    async def finish_stage(
        project: Annotated[str, Field(description="Project slug such as 'acme-shop'.")],
        task: Annotated[str, Field(description="Task key of the task whose stage ends.")] = "",
    ) -> str:
        rules, found = await prompt_context(project, task)
        return fmt.finish_stage_prompt(rules, found)

    return mcp


def role_path(role: str | None) -> str:
    """`/mcp` for the full set, `/mcp/<role>` for a role's."""
    return "/mcp" if role is None else f"/mcp/{role}"


def mcp_routes(
    servers: MCPServer | Mapping[str, MCPServer], allowed_hosts: list[str]
) -> list[BaseRoute]:
    """Routes that serve each MCP endpoint at exactly its path (`/mcp`, `/mcp/agent`, ...); a
    single server is served at `/mcp`.

    The routes are added to the main app instead of `Mount("/mcp", ...)`, which would answer
    `/mcp` with a redirect to `/mcp/`. The caller must run every server's
    `session_manager.run()` in its lifespan.
    """
    if isinstance(servers, MCPServer):
        servers = {"/mcp": servers}
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*allowed_hosts, *(f"{h}:*" for h in allowed_hosts)],
        allowed_origins=[
            f"{scheme}://{h}{port}"
            for h in allowed_hosts
            for scheme in ("http", "https")
            for port in ("", ":*")
        ],
    )
    routes: list[BaseRoute] = []
    for path, mcp in servers.items():
        app = mcp.streamable_http_app(streamable_http_path=path, transport_security=security)
        routes.extend(app.routes)
    return routes
