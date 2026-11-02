"""MCP server (official SDK 2.x `MCPServer`) served over Streamable HTTP at `/mcp`."""

import functools
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.routing import BaseRoute

from relaybay import __version__
from relaybay.domain.errors import DomainError, NotFoundError
from relaybay.domain.rules import Rules
from relaybay.mcp_server import format as fmt
from relaybay.service import Service, collect_notes


def instructions(rules: Rules) -> str:
    """The working contract sent on connect; the report format comes from the effective global
    rules, so it never contradicts rules_get."""
    report = rules.body["report_format"]
    return "\n".join(
        [
            "relaybay is the shared task board for this user's coding sessions. Projects contain"
            " tasks; a task moves through the stages plan → dev → test → live → done. Working"
            " contract:",
            "1. On start, read project_snapshot for your project. Its first line names the current"
            " rules; read relaybay://rules/<project> (or call rules_get) for the details.",
            "2. Pick the task you work on, or create one with task_create. Pass `session` on"
            " writes: your own desktop session id (local_…) with `session_title` if you know it,"
            " otherwise a human-readable name; add `hook_session` if the session start gave you"
            " one ('Your relaybay hook session: …'). If you don't know your local_ id, don't"
            " rebind a task — the coordinator binds you.",
            "3. On every stage change and every report, also update next_step (what happens next),"
            " waiting_on (who acts next) and progress (everything done so far).",
            "4. Add a report at the end of every stage. Its first line is"
            f" `{report['first_line']}`; state is one of {', '.join(report['states'])}; size is one"
            f" of {', '.join(report['sizes'])}.",
            "5. When the work needs a decision, set waiting_on to who decides and say in the report"
            " what is to be decided.",
            "6. If your tool's parameters lack a field, use task_patch (allowed fields:"
            " relaybay://schema/task).",
            "7. A response line starting with 'Warning (rules)' names a rule the write did not"
            " meet: fix it (e.g. add the missing report) rather than ignore it.",
            "8. When a hook gives you owner comments ('Owner comment on …'), handle them like a"
            " message from the user and call comment_ack (or answer with comment_add).",
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
StageArg = Annotated[str, Field(description="One of: plan, dev, test, live, done.")]
DESCRIPTION_HELP = "What to do and why, 1-5 sentences of markdown."
CLEARS = " '-' or '' clears it."
EMPTY = " '-' or '' leaves it empty."
TRACKER_REF_HELP = "Issue key or number in the external tracker, e.g. 'ENG-12'."
TRACKER_NAME_HELP = "Name of the external tracker, e.g. 'Linear'."
TRACKER_URL_HELP = "Link to the issue (http:// or https:// only)."
TRACKER_LOGGED_HELP = (
    "True once the work log is entered in the external tracker. New work clears it again (a"
    " report, a progress change, a move back from live or done). A new tracker_ref starts as not"
    " logged unless tracker_logged is passed in the same write. Done tasks without it stay on"
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
    mcp = MCPServer(
        name="relaybay", version=__version__, instructions=instructions(service.global_rules)
    )

    def write(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        """A write tool: lines the service noted without failing (a hook_session that was not
        linked) are appended to its result."""

        @functools.wraps(fn)
        async def tool(*args: Any, **kwargs: Any) -> str:
            with collect_notes() as notes:
                text = await fn(*args, **kwargs)
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
        repo_path: Annotated[str | None, Field(description="Path to the repository.")] = None,
        description: Annotated[str | None, Field(description="One-line description.")] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
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
                hook_session=hook_session,
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
        decisions, and the names of the editable fields (for task_patch). Call it before
        task_update to get the version for expected_version."""
        return fmt.task_detail(await run(service.task_detail(task, reports)), require_tracker_log)

    @write
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
        hook_session: HookSession = None,
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
                hook_session=hook_session,
            )
        )
        return fmt.task_saved(created, "Created")

    @write
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
        hook_session: HookSession = None,
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
        return fmt.with_warnings(fmt.task_saved(saved.task, "Updated"), saved.warnings)

    @write
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
        task_get or read relaybay://schema/task for the allowed fields."""
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
        return fmt.with_warnings(fmt.task_saved(saved.task, "Updated"), saved.warnings)

    @write
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
        """Move a task to another stage (plan, dev, test, live, done) when its work moves on, e.g.
        to dev when you start coding, to test when it is ready for review, to done when finished.
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
        return fmt.with_warnings(fmt.task_saved(saved.task, "Moved"), saved.warnings)

    @write
    async def task_bind_session(
        task: TaskRef,
        session: Annotated[str, Field(description=SESSION_HELP)],
        session_title: SessionTitle = None,
        hook_session: Annotated[
            str | None,
            Field(description="Ignored here: pass your hook_session on your own writes instead."),
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
            service.bind_session(task, session, session_title=session_title)
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
    ) -> str:
        """Remove the bound session from a task, e.g. when a session stops working on it without
        a successor. The binding history is kept. To hand the task to another session, call
        task_bind_session instead."""
        updated, unbound = await run(service.unbind_session(task, session))
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
        hook_session: HookSession = None,
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
                hook_session=hook_session,
                progress=cleared(progress),
                next_step=cleared(next_step),
                waiting_on=cleared(waiting_on),
            )
        )
        parsed = " · ".join(x for x in (report.state, report.size) if x)
        extra = f" ({parsed})" if parsed else ""
        return f"Report #{report.id} added to **{saved.key}**{extra}: {report.summary}"

    @write
    async def decision_add(
        project: ProjectSlug,
        text: Annotated[str, Field(description="The decision, one or two sentences.")],
        task: Annotated[str | None, Field(description="Related task key, optional.")] = None,
        decided_by: Annotated[str | None, Field(description="Who decided.")] = None,
        session: Session = None,
        session_title: SessionTitle = None,
        hook_session: HookSession = None,
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
                hook_session=hook_session,
            )
        )
        return f"Decision #{decision.id} recorded for {project}."

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

    @mcp.tool(annotations=READ, structured_output=False)
    async def rules_get(
        project: Annotated[
            str | None,
            Field(description="Project slug for its merged rules; omit for the global rules."),
        ] = None,
    ) -> str:
        """The working rules: report format (first line, states, sizes), what a stage move
        needs (e.g. a report before test, progress before done), the description template and
        guidance, with the rules hash. Call it at the start of a session, or when the hash in
        project_snapshot changed since you last read them."""
        return fmt.rules_text(await run(service.rules(project or None)))

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
        description="Editable task fields for task_patch: name, type, description, clearing.",
        mime_type="text/markdown",
    )
    async def task_schema_resource() -> str:
        return fmt.task_schema()

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
