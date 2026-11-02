"""MCP server (official SDK 2.x `MCPServer`) served over Streamable HTTP at `/mcp`."""

from collections.abc import Awaitable
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.routing import BaseRoute

from relaybay import __version__
from relaybay.domain.errors import DomainError, NotFoundError
from relaybay.mcp_server import format as fmt
from relaybay.service import Service

INSTRUCTIONS = " ".join(
    [
        "relaybay is the shared task board for this user's coding sessions.",
        "Projects contain tasks; a task moves through the stages plan → dev → test → live → done.",
        "Keep the board true: read the project snapshot when you start, create or pick a task for",
        "your work, move its stage as you progress, and add a report at the end of every stage.",
        "Pass `session` on writes so the board shows who did what: your own desktop session id",
        "(local_…) if you know it, with `session_title`, otherwise a human-readable name.",
        "If you don't know your local_ id, don't rebind a task — the coordinator binds you.",
    ]
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

SESSION_HELP = (
    "Who is calling: pass your own desktop session id (local_…) if you know it, otherwise a"
    " human-readable session name."
)
Session = Annotated[str | None, Field(description=SESSION_HELP + " Optional.")]
SessionTitle = Annotated[
    str | None,
    Field(description="Display name of your session (its title), used with a desktop session id."),
]
TaskRef = Annotated[str, Field(description="Task key such as 'acme-shop-12', or its numeric id.")]
ProjectSlug = Annotated[str, Field(description="Project slug such as 'acme-shop'.")]
StageArg = Annotated[str, Field(description="One of: plan, dev, test, live, done.")]
DESCRIPTION_HELP = "What to do and why, 1-5 sentences of markdown."
CLEARS = " '-' or '' clears it."
EMPTY = " '-' or '' leaves it empty."
TRACKER_REF_HELP = "Issue key or number in the external tracker, e.g. 'ENG-12'."
TRACKER_NAME_HELP = "Name of the external tracker, e.g. 'Linear'."
TRACKER_URL_HELP = "Link to the issue (http:// or https:// only)."
TRACKER_LOGGED_HELP = (
    "True once the work log is entered in the external tracker. Done tasks without it stay on"
    " the board marked 'not logged'."
)
NEXT_STEP_HELP = (
    "What happens next, one line; saved in the same write. A next step set before the last stage"
    " change shows as stale. Omit to keep it;"
)
WAITING_ON_HELP = "Who acts next; saved in the same write. Omit to keep it;"
PROGRESS_HELP = (
    "Markdown summary of everything done so far on the task (not just the last step). Rewrite"
    " it as the work goes on and keep it current with every report."
)


def cleared(value: str | None) -> str | None:
    """'-' means "empty": models cannot reliably send '' as a tool argument."""
    return "" if value is not None and value.strip() == "-" else value


async def run[T](awaitable: Awaitable[T]) -> T:
    """Domain errors become tool errors whose message the model reads."""
    try:
        return await awaitable
    except DomainError as exc:
        raise ToolError(str(exc)) from exc


def build_mcp(service: Service, *, require_tracker_log: bool = False) -> MCPServer:
    mcp = MCPServer(name="relaybay", version=__version__, instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ, structured_output=False)
    async def project_list() -> str:
        """List all projects with task counts per stage. Call it to find a project's slug."""
        return fmt.project_list(await service.list_projects())

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def project_upsert(
        slug: ProjectSlug,
        name: Annotated[str | None, Field(description="Display name.")] = None,
        repo_path: Annotated[str | None, Field(description="Path to the repository.")] = None,
        description: Annotated[str | None, Field(description="One-line description.")] = None,
        session: Session = None,
        session_title: SessionTitle = None,
    ) -> str:
        """Create a project, or rename it / set its repository path. Call it once before creating
        the first task of a project that is not on the board yet (see project_list)."""
        project, created = await run(
            service.upsert_project(
                slug,
                name=name,
                repo_path=repo_path,
                description=description,
                session=session,
                session_title=session_title,
            )
        )
        return fmt.project(project, created)

    @mcp.tool(annotations=READ, structured_output=False)
    async def project_snapshot(
        project: ProjectSlug,
        include_done: Annotated[bool, Field(description="Also list done tasks.")] = False,
    ) -> str:
        """Compact markdown snapshot of a project: open tasks by stage with waiting-on, next step,
        branch, the start of the description and progress, last report, plus recent decisions.
        Call it at the start of a session to see the state of the project before you pick or
        create a task."""
        return fmt.snapshot(await run(service.snapshot(project, include_done)), require_tracker_log)

    @mcp.tool(annotations=READ, structured_output=False)
    async def task_list(
        project: Annotated[str | None, Field(description="Project slug filter.")] = None,
        stage: Annotated[str | None, Field(description="Stage filter.")] = None,
        waiting_on: Annotated[str | None, Field(description="Exact 'waiting on' value.")] = None,
        session: Annotated[
            str | None, Field(description="Bound session: desktop session id or name.")
        ] = None,
        include_done: Annotated[bool, Field(description="Include done tasks.")] = False,
    ) -> str:
        """List tasks, filtered by project, stage, who they wait on or the bound session. Done
        tasks are left out unless a stage or include_done is given."""
        tasks = await run(
            service.list_tasks(
                project=project,
                stage=stage,
                waiting_on=waiting_on,
                session=session,
                include_done=include_done,
            )
        )
        return fmt.task_list(tasks, require_tracker_log)

    @mcp.tool(annotations=READ, structured_output=False)
    async def task_get(
        task: TaskRef,
        reports: Annotated[int, Field(ge=1, le=50, description="How many reports.")] = 5,
    ) -> str:
        """Full task: fields, description, progress, current version, the latest reports and
        decisions. Call it before task_update to get the version for expected_version."""
        return fmt.task_detail(await run(service.task_detail(task, reports)), require_tracker_log)

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def task_create(
        project: ProjectSlug,
        title: Annotated[str, Field(description="Short task title.")],
        stage: Annotated[str | None, Field(description="Initial stage; default plan.")] = None,
        waiting_on: Annotated[
            str | None, Field(description="Who the task waits on." + EMPTY)
        ] = None,
        next_step: Annotated[
            str | None, Field(description="The next concrete step." + EMPTY)
        ] = None,
        branch: Annotated[str | None, Field(description="Git branch." + EMPTY)] = None,
        doc_path: Annotated[
            str | None, Field(description="Path to a task document." + EMPTY)
        ] = None,
        description: Annotated[str | None, Field(description=DESCRIPTION_HELP + EMPTY)] = None,
        progress: Annotated[str | None, Field(description=PROGRESS_HELP + EMPTY)] = None,
        tracker_ref: Annotated[str | None, Field(description=TRACKER_REF_HELP + EMPTY)] = None,
        tracker_name: Annotated[str | None, Field(description=TRACKER_NAME_HELP + EMPTY)] = None,
        tracker_url: Annotated[str | None, Field(description=TRACKER_URL_HELP + EMPTY)] = None,
        tracker_logged: Annotated[bool, Field(description=TRACKER_LOGGED_HELP)] = False,
        session: Session = None,
        session_title: SessionTitle = None,
    ) -> str:
        """Create a task when you start work that is not on the board yet. Give it a short
        `description` (what to do and why). Returns its key (e.g. 'acme-shop-12'); use the key in
        later calls. If `session` is given, the task is bound to that session."""
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
                session=session,
                session_title=session_title,
            )
        )
        return fmt.task_saved(created, "Created")

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def task_update(
        task: TaskRef,
        title: Annotated[str | None, Field(description="New title.")] = None,
        waiting_on: Annotated[
            str | None, Field(description="Who the task waits on." + CLEARS)
        ] = None,
        next_step: Annotated[
            str | None, Field(description="The next concrete step." + CLEARS)
        ] = None,
        branch: Annotated[str | None, Field(description="Git branch." + CLEARS)] = None,
        doc_path: Annotated[
            str | None, Field(description="Path to a task document." + CLEARS)
        ] = None,
        stage_note: Annotated[
            str | None, Field(description="Note next to the stage." + CLEARS)
        ] = None,
        description: Annotated[str | None, Field(description=DESCRIPTION_HELP + CLEARS)] = None,
        progress: Annotated[str | None, Field(description=PROGRESS_HELP + CLEARS)] = None,
        tracker_ref: Annotated[str | None, Field(description=TRACKER_REF_HELP + CLEARS)] = None,
        tracker_name: Annotated[str | None, Field(description=TRACKER_NAME_HELP + CLEARS)] = None,
        tracker_url: Annotated[str | None, Field(description=TRACKER_URL_HELP + CLEARS)] = None,
        tracker_logged: Annotated[bool | None, Field(description=TRACKER_LOGGED_HELP)] = None,
        expected_version: Annotated[
            int | None,
            Field(description="Fail instead of overwriting if the task changed since you read it."),
        ] = None,
        session: Session = None,
        session_title: SessionTitle = None,
    ) -> str:
        """Change task fields (waiting on, next step, branch, description, progress, the tracker
        issue and its work-log mark, ...). Omitted fields stay as they are; '-' or an empty
        string clears a field. Keep `progress` current with every report. To move a task between
        stages use task_set_stage. Also update next_step (what happens next) and waiting_on (who
        acts next) whenever the stage changes or you report."""
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
        }
        changes: dict[str, object] = {
            k: (cleared(v) or None) for k, v in given.items() if v is not None
        }
        if tracker_logged is not None:
            changes["tracker_logged"] = tracker_logged
        if not changes:
            raise ToolError("Nothing to update: pass at least one field.")
        updated = await run(
            service.update_task(
                task,
                changes,
                expected_version=expected_version,
                session=session,
                session_title=session_title,
            )
        )
        return fmt.task_saved(updated, "Updated")

    @mcp.tool(annotations=WRITE, structured_output=False)
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
    ) -> str:
        """Move a task to another stage (plan, dev, test, live, done) when its work moves on, e.g.
        to dev when you start coding, to test when it is ready for review, to done when finished.
        Any transition is allowed. Also update next_step (what happens next) and waiting_on (who
        acts next) whenever the stage changes or you report."""
        updated = await run(
            service.set_stage(
                task,
                stage,
                note=note,
                next_step=cleared(next_step),
                waiting_on=cleared(waiting_on),
                session=session,
                session_title=session_title,
            )
        )
        return fmt.task_saved(updated, "Moved")

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def task_bind_session(
        task: TaskRef,
        session: Annotated[str, Field(description=SESSION_HELP)],
        session_title: SessionTitle = None,
    ) -> str:
        """Mark that this session works on the task now; the board shows it with a link that
        opens the Claude desktop app session. Call it when you pick up an existing task.
        Pass your own desktop session id (local_…) if you know it. If you don't know your local_
        id, don't rebind — the coordinator binds you; pass your session name only as `session` on
        writes. A plain name never replaces a desktop session bound to the task: it is recorded
        as that session's alias. Binding a session ends the previous session's binding; the
        history is kept."""
        updated, kept, alias_recorded = await run(
            service.bind_session(task, session, session_title=session_title)
        )
        if kept is not None:
            who = fmt.who_label(kept.name, kept.external_id)
            if not alias_recorded:
                return (
                    f"**{updated.key}** is already bound to {who}; pass your local id to rebind."
                    f" '{session.strip()}' was not recorded as its alias: a session with that"
                    " name already exists. Nothing changed."
                )
            return (
                f"Kept the desktop session {who} on **{updated.key}**; recorded"
                f" '{session.strip()}' as its alias. Pass your local_ id to rebind."
            )
        return fmt.task_saved(updated, f"Bound {fmt.session_label(updated)} to")

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def task_unbind_session(
        task: TaskRef,
        session: Annotated[
            str | None,
            Field(
                description="The session you expect to be bound (desktop session id or name);"
                " if another session is bound, nothing changes and an error says who. Optional."
            ),
        ] = None,
    ) -> str:
        """Remove the bound session from a task, e.g. when a session stops working on it without
        a successor. The binding history is kept. To hand the task to another session, call
        task_bind_session instead."""
        updated, unbound = await run(service.unbind_session(task, session))
        if unbound is None:
            return f"No session is bound to **{updated.key}**; nothing changed."
        who = fmt.who_label(unbound.name, unbound.external_id)
        return fmt.task_saved(updated, f"Unbound {who} from")

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def report_add(
        task: TaskRef,
        body: Annotated[
            str,
            Field(
                description="Markdown report. Its first line is the summary; a first line"
                " 'state · size · gist' (e.g. 'verified · M · login fixed') also sets state"
                " (inspected, changed, verified, committed, pushed, blocked) and size (S, M, L)."
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
        session: Session = None,
        session_title: SessionTitle = None,
    ) -> str:
        """Add a report to a task. Call it at the end of every stage with your final report: what
        changed, how it was checked, what is left. Pass `progress` too, so the task's summary of
        what is done stays current with every report. Also update next_step (what happens next)
        and waiting_on (who acts next) whenever the stage changes or you report."""
        saved, report = await run(
            service.add_report(
                task,
                body,
                session=session,
                session_title=session_title,
                progress=cleared(progress),
                next_step=cleared(next_step),
                waiting_on=cleared(waiting_on),
            )
        )
        parsed = " · ".join(x for x in (report.state, report.size) if x)
        extra = f" ({parsed})" if parsed else ""
        return f"Report #{report.id} added to **{saved.key}**{extra}: {report.summary}"

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def decision_add(
        project: ProjectSlug,
        text: Annotated[str, Field(description="The decision, one or two sentences.")],
        task: Annotated[str | None, Field(description="Related task key, optional.")] = None,
        decided_by: Annotated[str | None, Field(description="Who decided.")] = None,
        session: Session = None,
        session_title: SessionTitle = None,
    ) -> str:
        """Record a decision the user or the team made (scope, approach, trade-off), so later
        sessions find it in the project snapshot."""
        decision = await run(
            service.add_decision(
                project,
                text,
                task=task or None,
                decided_by=decided_by,
                session=session,
                session_title=session_title,
            )
        )
        return f"Decision #{decision.id} recorded for {project}."

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

    @mcp.resource(
        "relaybay://project/{slug}",
        name="project-snapshot",
        description="Markdown snapshot of a project (same as project_snapshot).",
        mime_type="text/markdown",
    )
    async def project_resource(slug: str) -> str:
        try:
            return fmt.snapshot(await service.snapshot(slug), require_tracker_log)
        except NotFoundError as exc:
            raise ResourceNotFoundError(str(exc)) from exc

    return mcp


def mcp_routes(mcp: MCPServer, allowed_hosts: list[str]) -> list[BaseRoute]:
    """Routes that serve the MCP endpoint at exactly `/mcp`.

    The routes are added to the main app instead of `Mount("/mcp", ...)`, which would answer
    `/mcp` with a redirect to `/mcp/`. The caller must run `mcp.session_manager.run()` in its
    lifespan.
    """
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
    app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security)
    return list(app.routes)
