"""Board: server-rendered pages and HTMX fragments, login, SSE endpoint, Integrations."""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse

from relaybay import __version__, machine_setup
from relaybay.config import Settings
from relaybay.domain import environments as env_domain
from relaybay.domain.comments import COMMENT_MAX
from relaybay.domain.errors import ConfirmationNeeded, ConflictError, DomainError, NotFoundError
from relaybay.domain.models import Task
from relaybay.domain.stages import STAGES, Stage
from relaybay.domain.tasks import is_archived
from relaybay.mcp_server.format import age, event_detail, event_subject
from relaybay.security import (
    COOKIE_NAME,
    CSRF_FIELD,
    csrf_token,
    login_url,
    safe_next,
    same_origin,
    same_secret,
    session_cookie_value,
    token_id,
)
from relaybay.service import HTTP_URL_RE, Inbox, Service
from relaybay.web.markdown import render_markdown
from relaybay.web.sse import event_stream

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGIN_CSRF_COOKIE = "relaybay_login_csrf"
LOGIN_ERROR_COOKIE = "relaybay_login_error"
INVALID_TOKEN = (
    "That token is not valid for this server. Run 'relaybay token' with the same --home as"
    " 'relaybay serve'."
)
DOCS_URL = "https://github.com/Codenator81/relaybay#readme"
STATIC_DIR = Path(__file__).parent / "static"
DONE_COLUMN_LIMIT = 200
SEARCH_MAX = 200
EDITABLE_FIELDS = (
    "title",
    "stage",
    "waiting_on",
    "next_step",
    "branch",
    "description",
    "progress",
    "tracker_ref",
    "tracker_name",
    "tracker_url",
    "tracker_logged",
)
TASK_PAGE_REPORTS = 10
SAVE_CONFLICT = (
    "Someone else changed this task while you were editing. Your edits are kept below; Save"
    " again to overwrite their change, or reload to discard your edits."
)
COOKIE_MAX_AGE = 60 * 60 * 24 * 365
ACTIVITY_PAGE = 50
EVENT_KINDS = (
    "project.created",
    "project.updated",
    "task.created",
    "task.updated",
    "task.stage_changed",
    "task.session_bound",
    "task.session_unbound",
    "task.tracker_unlogged",
    "report.added",
    "decision.added",
    "session.seen",
    "session.status_changed",
    "session.alias_added",
    "comment.added",
    "comment.delivered",
    "comment.read",
    "environment.created",
    "environment.updated",
    "environment.deleted",
    "environment.access_added",
    "environment.access_updated",
    "environment.access_removed",
    "environment.proposed",
    "environment.proposal_accepted",
    "environment.proposal_rejected",
    "live.action",
)
BOARD_ONLY = (
    "Environments and permission levels change only from the board in a browser; agents propose"
    " changes with environment_propose."
)
ENV_TEXT_FIELDS = ("name", "kind", "base_url", "login_state_path", "notes", "level")
JOURNAL_LIMIT = 20
# Hook status changes arrive every few seconds per working session: the Activity feed shows
# them only when the kind filter asks for them.
QUIET_KINDS = ("session.status_changed",)


def utcnow() -> datetime:
    return datetime.now(UTC)


def http_url(value: str | None) -> str | None:
    """`value` when it is an http(s) link, else None. Writes validate it already; this keeps a
    hand-edited database from turning into a `javascript:` link."""
    return value if value and HTTP_URL_RE.fullmatch(value) else None


def build_router(service: Service, settings: Settings, token: str) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    env = templates.env
    env.globals.update(
        stages=STAGES,
        label=settings.label,
        report_label=settings.report_label,
        report_hint=settings.report_hint,
        csrf_field=CSRF_FIELD,
        csrf_token=csrf_token(token),
        require_tracker_log=settings.require_tracker_log,
        comment_max=COMMENT_MAX,
    )
    env.filters["age"] = age
    env.filters["markdown"] = render_markdown
    env.filters["http_url"] = http_url
    env.filters["event_detail"] = event_detail
    env.filters["event_subject"] = event_subject
    env.filters["change_summary"] = env_domain.change_summary
    env.globals["fields_text"] = env_domain.fields_text

    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    async def inbox() -> Inbox:
        return await service.inbox(
            settings.owner_names,
            require_tracker_log=settings.require_tracker_log,
            done_window_days=settings.done_window_days,
        )

    async def page(
        request: Request,
        name: str,
        active: str,
        nav_project: str | None = None,
        **context: Any,
    ) -> HTMLResponse:
        """A full page with the sidebar: `active` names the highlighted item (`inbox`,
        `boards`, `activity`, `settings`, `sessions`; `board`, `sessions` or `environments`
        with `project`)."""
        nav = {
            "active": active,
            "project": nav_project,
            "projects": [item.project for item in await service.list_projects()],
            "inbox_count": (await inbox()).count,
        }
        return render(request, name, nav=nav, **context)

    async def board_context(slug: str, archived: bool = False, q: str = "") -> dict[str, Any]:
        """Columns of the board. Archived done tasks are left out unless `archived` is set or a
        search `q` (matched against key and title) is given; a search covers the archive too."""
        project = await service.get_project(slug)
        tasks = await service.list_tasks(project=project.slug, include_done=True)
        q = q.strip()[:SEARCH_MAX]
        needle = q.casefold()
        now = utcnow()
        by_stage: dict[str, list[Task]] = {s.value: [] for s in STAGES}
        hidden_archived = 0
        for task in tasks:
            if needle and needle not in task.key.casefold() and needle not in task.title.casefold():
                continue
            if not (archived or needle) and is_archived(
                task, now, settings.done_window_days, settings.require_tracker_log
            ):
                hidden_archived += 1
                continue
            by_stage[task.stage.value].append(task)
        done_more = max(0, len(by_stage[Stage.DONE]) - DONE_COLUMN_LIMIT)
        by_stage[Stage.DONE] = by_stage[Stage.DONE][:DONE_COLUMN_LIMIT]
        view = {"archived": "1" if archived else "", "q": q}
        return {
            "project": project,
            "columns": by_stage,
            "archived": archived,
            "q": q,
            "view_qs": urlencode({k: v for k, v in view.items() if v}),
            "archived_qs": urlencode({k: v for k, v in {**view, "archived": "1"}.items() if v}),
            "hidden_archived": hidden_archived,
            "done_more": done_more,
            "done_window_days": settings.done_window_days,
        }

    async def find_task(slug: str, key: str) -> Task:
        task = await service.get_task(key)
        if task.project_slug != slug:
            raise NotFoundError(f"Task {key!r} not found in {slug!r}.")
        return task

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "token_id": token_id(token),
            "rules_hash": service.global_rules.hash,
        }

    def signed_in(request: Request, next_path: str) -> Response:
        response = RedirectResponse(safe_next(next_path), status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            session_cookie_value(token),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE)
        return response

    def login_form(request: Request, next_path: str, error: str = "", status: int = 200):
        # Double-submit CSRF: a random value in a SameSite=Strict cookie and in the form.
        nonce = secrets.token_urlsafe(16)
        response = render(
            request,
            "login.html",
            status=status,
            next_path=safe_next(next_path),
            login_csrf=nonce,
            error=error,
            docs_url=DOCS_URL,
        )
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            nonce,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @router.get("/login")
    async def login(
        request: Request,
        token_param: Annotated[str, Query(alias="token")] = "",
        next_path: Annotated[str, Query(alias="next")] = "/",
    ):
        if same_secret(token_param, token):
            return signed_in(request, next_path)
        if token_param:
            # Never leave a token in the address bar or history: redirect and flash the error.
            response = RedirectResponse(login_url(next_path), status_code=303)
            response.set_cookie(
                LOGIN_ERROR_COOKIE, "invalid", max_age=60, httponly=True, samesite="strict"
            )
            return response
        if request.cookies.get(LOGIN_ERROR_COOKIE):
            response = login_form(request, next_path, INVALID_TOKEN, status=401)
            response.delete_cookie(LOGIN_ERROR_COOKIE)
            return response
        return login_form(request, next_path)

    @router.post("/login")
    async def login_submit(
        request: Request,
        token_field: Annotated[str, Form(alias="token")] = "",
        next_path: Annotated[str, Form(alias="next")] = "/",
        nonce: Annotated[str, Form(alias="login_csrf")] = "",
    ):
        cookie = request.cookies.get(LOGIN_CSRF_COOKIE)
        if not same_origin(request.headers) or not nonce or not same_secret(cookie, nonce):
            return login_form(request, next_path, "The form expired. Please try again.", 403)
        if not same_secret(token_field.strip(), token):
            return login_form(request, next_path, INVALID_TOKEN, status=401)
        return signed_in(request, next_path)

    @router.get("/", response_class=HTMLResponse)
    async def projects_page(request: Request):
        # Read the event id before the data: an event committed in between is then replayed
        # by the SSE stream instead of being skipped.
        last_event_id = await service.last_event_id()
        return await page(
            request,
            "projects.html",
            "boards",
            projects=await service.list_projects(),
            last_event_id=last_event_id,
        )

    @router.get("/fragments/projects", response_class=HTMLResponse)
    async def projects_fragment(request: Request):
        return render(request, "_projects.html", projects=await service.list_projects())

    @router.get("/p/{slug}", response_class=HTMLResponse)
    async def board_page(
        request: Request, slug: str, task: str | None = None, archived: bool = False, q: str = ""
    ):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        context = await board_context(slug, archived, q)
        first_busy = next((s for s in STAGES if context["columns"][s]), Stage.PLAN)
        # `?task=<key>` is the URL of the open task dialog: render it so the page opens with it.
        dialog = await dialog_context(slug, task) if task else {}
        return await page(
            request,
            "board.html",
            "board",
            context["project"].slug,
            **context,
            **dialog,
            active_stage=first_busy,
            last_event_id=last_event_id,
        )

    @router.get("/t/{key}", response_class=HTMLResponse)
    async def task_page(request: Request, key: str):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        detail = await service.task_detail(key, reports=TASK_PAGE_REPORTS)
        return await page(
            request,
            "task.html",
            "board",
            detail.task.project_slug,
            detail=detail,
            last_event_id=last_event_id,
        )

    @router.get("/inbox", response_class=HTMLResponse)
    async def inbox_page(request: Request):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        return await page(
            request, "inbox.html", "inbox", inbox=await inbox(), last_event_id=last_event_id
        )

    @router.get("/fragments/inbox", response_class=HTMLResponse)
    async def inbox_fragment(request: Request):
        return render(request, "_inbox.html", inbox=await inbox())

    @router.get("/fragments/inbox-badge", response_class=HTMLResponse)
    async def inbox_badge(request: Request):
        return render(request, "_inbox_badge.html", count=(await inbox()).count)

    @router.get("/sessions", response_class=HTMLResponse)
    async def all_sessions_page(request: Request):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        return await page(
            request,
            "sessions.html",
            "sessions",
            sessions_project=None,
            rows=await service.list_sessions(),
            last_event_id=last_event_id,
        )

    @router.get("/p/{slug}/sessions", response_class=HTMLResponse)
    async def sessions_page(request: Request, slug: str):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        project = await service.get_project(slug)
        return await page(
            request,
            "sessions.html",
            "sessions",
            project.slug,
            sessions_project=project,
            rows=await service.list_sessions(project.slug),
            last_event_id=last_event_id,
        )

    @router.get("/fragments/sessions", response_class=HTMLResponse)
    async def sessions_fragment(request: Request, project: str = ""):
        sessions_project = await service.get_project(project) if project else None
        rows = await service.list_sessions(sessions_project.slug if sessions_project else None)
        return render(request, "_sessions.html", sessions_project=sessions_project, rows=rows)

    async def activity_context(
        project: str, kind: str, before: int | None, after: int | None
    ) -> dict[str, Any]:
        if kind and kind not in EVENT_KINDS:
            raise HTTPException(status_code=400, detail=f"Unknown event kind {kind!r}.")
        filters = {"project": project, "kind": kind}
        # A live refetch (`after`) takes everything new at once; a page stops at ACTIVITY_PAGE.
        limit = ACTIVITY_PAGE * 4 if after is not None else ACTIVITY_PAGE
        items = await service.activity(
            project=project or None,
            kind=kind or None,
            before_id=before,
            after_id=after,
            exclude_kinds=() if kind else QUIET_KINDS,
            limit=limit + 1,
        )
        more = after is None and len(items) > limit
        items = items[:limit]
        more_qs = ""
        if more:
            more_qs = urlencode({k: v for k, v in {**filters, "before": items[-1].id}.items() if v})
        return {
            "events": items,
            "more_qs": more_qs,
            "filters_qs": urlencode({k: v for k, v in filters.items() if v}),
        }

    @router.get("/activity", response_class=HTMLResponse)
    async def activity_page(
        request: Request, project: str = "", kind: str = "", before: int | None = None
    ):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        project = project.strip().lower()
        if project:
            await service.get_project(project)  # an unknown project is a 404
        context = await activity_context(project, kind, before, None)
        return await page(
            request,
            "activity.html",
            "activity",
            **context,
            project_filter=project,
            kind_filter=kind,
            kinds=EVENT_KINDS,
            paged=before is not None,
            journal=await service.live_journal(project or None, JOURNAL_LIMIT),
            last_event_id=last_event_id,
        )

    @router.get("/fragments/journal", response_class=HTMLResponse)
    async def journal_fragment(request: Request, project: str = ""):
        project = project.strip().lower()
        journal = await service.live_journal(project or None, JOURNAL_LIMIT)
        return render(request, "_journal.html", journal=journal, project_filter=project)

    @router.get("/fragments/activity", response_class=HTMLResponse)
    async def activity_fragment(
        request: Request,
        project: str = "",
        kind: str = "",
        before: int | None = None,
        after: int | None = None,
    ):
        context = await activity_context(project.strip().lower(), kind, before, after)
        return render(request, "_activity_rows.html", **context)

    @router.get("/p/{slug}/col/{stage}", response_class=HTMLResponse)
    async def column_fragment(
        request: Request, slug: str, stage: Stage, archived: bool = False, q: str = ""
    ):
        context = await board_context(slug, archived, q)
        return render(
            request, "_column.html", **context, stage=stage, tasks=context["columns"][stage]
        )

    @router.get("/p/{slug}/t/{key}/card", response_class=HTMLResponse)
    async def card_fragment(request: Request, slug: str, key: str):
        task = await find_task(slug, key)
        return render(request, "_card.html", t=task, slug=slug)

    async def dialog_context(slug: str, key: str) -> dict[str, Any]:
        task = await find_task(slug, key)
        return {
            "detail": await service.task_detail(task.key, reports=TASK_PAGE_REPORTS),
            "values": {},
            "waiting_values": await service.waiting_on_values(),
        }

    @router.get("/p/{slug}/t/{key}/dialog", response_class=HTMLResponse)
    async def dialog_fragment(request: Request, slug: str, key: str):
        return render(request, "_task_dialog.html", **await dialog_context(slug, key))

    @router.get("/p/{slug}/t/{key}/view", response_class=HTMLResponse)
    async def view_fragment(request: Request, slug: str, key: str):
        task = await find_task(slug, key)
        detail = await service.task_detail(task.key, reports=TASK_PAGE_REPORTS)
        return render(request, "_task_view.html", detail=detail)

    @router.get("/p/{slug}/t/{key}/form", response_class=HTMLResponse)
    async def form_fragment(request: Request, slug: str, key: str):
        return await edit_form(request, await find_task(slug, key))

    async def edit_form(
        request: Request,
        task: Task,
        error: str | None = None,
        values: dict[str, str] | None = None,
        saved: bool = False,
        conflict: bool = False,
        warnings: list[str] | None = None,
    ) -> HTMLResponse:
        # Errors come back as 200: htmx does not swap 4xx bodies by default.
        return render(
            request,
            "_task_form.html",
            t=task,
            error=error,
            conflict=conflict,
            saved=saved,
            warnings=warnings or [],
            values=values or {},
            waiting_values=await service.waiting_on_values(),
        )

    @router.post("/p/{slug}/t/{key}", response_class=HTMLResponse)
    async def save_task(
        request: Request,
        slug: str,
        key: str,
        version: Annotated[int | None, Form()] = None,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if not same_secret(csrf, csrf_token(token)):
            raise HTTPException(status_code=403, detail="Missing CSRF token")
        task = await find_task(slug, key)
        # A field missing from the form stays as it is; an empty one clears it. Read the raw
        # form: FastAPI turns an empty `Form()` value into None, the same as a missing field.
        form = await request.form()
        values: dict[str, str] = {}
        for name in EDITABLE_FIELDS:
            submitted = form.getlist(name)
            if submitted and isinstance(submitted[-1], str):
                values[name] = submitted[-1]
        changes: dict[str, Any] = {k: (v.strip() or None) for k, v in values.items()}
        if "tracker_logged" in values:
            # The form sends a hidden "0" before the checkbox, so the last value is the state.
            changes["tracker_logged"] = values["tracker_logged"] == "1"
            if changes["tracker_logged"] == task.tracker_logged:
                # Resent unchanged, it is not an explicit mark: new work may still clear it.
                del changes["tracker_logged"]
        if changes.get("next_step", "") == task.next_step:
            # The form always resends the next step; only an edited one confirms it as current.
            del changes["next_step"]
        try:
            result = await service.update_task_checked(
                task.key, changes, expected_version=version, actor="board"
            )
        except ConflictError:
            fresh = await find_task(slug, key)
            return await edit_form(
                request, fresh, error=SAVE_CONFLICT, values=values, conflict=True
            )
        except DomainError as exc:
            fresh = await find_task(slug, key)
            return await edit_form(request, fresh, error=str(exc), values=values)
        return await edit_form(request, result.task, saved=True, warnings=result.warnings)

    @router.post("/p/{slug}/t/{key}/unbind")
    async def unbind_task_session(
        request: Request,
        slug: str,
        key: str,
        session: Annotated[str | None, Form()] = None,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if not same_secret(csrf, csrf_token(token)):
            raise HTTPException(status_code=403, detail="Missing CSRF token")
        task = await find_task(slug, key)
        htmx = "hx-request" in request.headers
        error = None
        try:
            # `session` is the binding the user saw; a session that took over meanwhile stays.
            await service.unbind_session(task.key, session or None, actor="board")
        except ConflictError:
            fresh = await find_task(slug, key)
            error = (
                f"This task is now bound to {fresh.session_name}; nothing changed."
                if fresh.session_name
                else "No session is bound to this task any more; nothing changed."
            )
        except DomainError as exc:
            if not htmx:
                raise  # plain form post: the app's DomainError handler answers
            error = str(exc)
        if not htmx:
            if error:
                return PlainTextResponse(error, status_code=409)
            return RedirectResponse(f"/t/{task.key}", status_code=303)
        detail = await service.task_detail(task.key, reports=TASK_PAGE_REPORTS)
        return render(request, "_task_view.html", detail=detail, error=error)

    async def comment_thread(
        request: Request, task: Task, error: str | None = None, saved: bool = False
    ) -> HTMLResponse:
        _, comments = await service.list_comments(task.key)
        response = render(request, "_comment_thread.html", t=task, comments=comments, error=error)
        if saved:
            response.headers["X-Relaybay-Comment"] = "saved"  # app.js clears the form
        return response

    @router.get("/p/{slug}/t/{key}/comments", response_class=HTMLResponse)
    async def comments_fragment(request: Request, slug: str, key: str):
        return await comment_thread(request, await find_task(slug, key))

    @router.post("/p/{slug}/t/{key}/comments", response_class=HTMLResponse)
    async def add_comment(
        request: Request,
        slug: str,
        key: str,
        body: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        check_csrf(csrf)
        task = await find_task(slug, key)
        htmx = "hx-request" in request.headers
        try:
            await service.add_owner_comment(task.key, body, actor="owner")
        except DomainError as exc:
            if not htmx:
                raise
            # 200: htmx does not swap 4xx bodies by default.
            return await comment_thread(request, task, error=str(exc))
        if not htmx:
            return RedirectResponse(f"/t/{task.key}", status_code=303)
        return await comment_thread(request, task, saved=True)

    @router.post("/p/{slug}/t/{key}/comments/read", response_class=HTMLResponse)
    async def read_replies(
        request: Request,
        slug: str,
        key: str,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        check_csrf(csrf)
        task = await find_task(slug, key)
        await service.mark_replies_read(task.key, actor="owner")
        if "hx-request" not in request.headers:
            return RedirectResponse(f"/t/{task.key}", status_code=303)
        return await comment_thread(request, task)

    # --- Stands & access: environments, access map, permission levels -----------------

    def board_only(request: Request, csrf: str) -> None:
        """Environment writes come from a signed-in browser (cookie + CSRF), not from a
        client that sends the token as a Bearer header."""
        check_csrf(csrf)
        cookie = request.cookies.get(COOKIE_NAME)
        if "authorization" in request.headers or not same_secret(
            cookie, session_cookie_value(token)
        ):
            raise HTTPException(status_code=403, detail=BOARD_ONLY)

    async def environments_page(
        request: Request,
        slug: str,
        *,
        error: str | None = None,
        error_target: str = "",
        confirm: dict[str, Any] | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        project, items = await service.environments(slug)
        return await page(
            request,
            "environments.html",
            "environments",
            project.slug,
            status=status,
            project=project,
            environments=items,
            proposals=await service.proposals(project.slug),
            defaults=service.permission_defaults,
            guard_mode=service.guard_mode,
            error=error,
            error_target=error_target,
            confirm=confirm,
            last_event_id=last_event_id,
        )

    def environment_values(form: Any) -> dict[str, Any]:
        """The environment fields a board form sent; a field it did not send stays."""
        values: dict[str, Any] = {}
        for name in ENV_TEXT_FIELDS:
            submitted = form.getlist(name)
            if submitted and isinstance(submitted[-1], str):
                values[name] = submitted[-1]
        sign_in = form.getlist("sign_in_allowed")
        if sign_in:
            # A hidden "0" comes before the checkbox, so the last value is the state.
            values["sign_in_allowed"] = sign_in[-1] == "1"
        overrides = {
            cap: form.get(f"override_{cap}") or ""
            for cap in env_domain.CAPABILITIES
            if f"override_{cap}" in form
        }
        if overrides:
            values["overrides"] = overrides
        return values

    def confirm_fields(form: Any) -> list[tuple[str, str]]:
        """The submitted fields, to resend with the confirmation."""
        return [
            (k, v)
            for k, v in form.multi_items()
            if isinstance(v, str) and k not in (CSRF_FIELD, "confirm_live")
        ]

    async def environment_write(
        request: Request,
        slug: str,
        csrf: str,
        target: str,
        write: Callable[[Any], Awaitable[Any]],
        anchor: str = "",
    ) -> Response:
        """Run a board write; on success back to the page, on failure the page with the
        reason next to the form, or a confirmation for raising live above read."""
        board_only(request, csrf)
        form = await request.form()
        try:
            await write(form)
        except ConfirmationNeeded as exc:
            confirm = {
                "action": str(request.url.path),
                "message": str(exc),
                "fields": confirm_fields(form),
            }
            return await environments_page(request, slug, confirm=confirm)
        except NotFoundError:
            raise
        except DomainError as exc:
            return await environments_page(
                request, slug, error=str(exc), error_target=target, status=400
            )
        return RedirectResponse(f"/p/{slug}/environments{anchor}", status_code=303)

    def confirmed(form: Any) -> bool:
        return form.get("confirm_live") == "1"

    @router.get("/p/{slug}/environments", response_class=HTMLResponse)
    async def environments(request: Request, slug: str):
        return await environments_page(request, slug)

    @router.get("/p/{slug}/environments/proposals", response_class=HTMLResponse)
    async def proposals_fragment(request: Request, slug: str):
        project = await service.get_project(slug)
        return render(
            request,
            "_proposals.html",
            project=project,
            proposals=await service.proposals(project.slug),
        )

    @router.post("/p/{slug}/environments")
    async def create_environment(
        request: Request, slug: str, csrf: Annotated[str, Form(alias=CSRF_FIELD)] = ""
    ):
        async def write(form: Any) -> None:
            await service.save_environment(
                slug, environment_values(form), confirm_live=confirmed(form), actor="owner"
            )

        return await environment_write(request, slug, csrf, "new", write)

    @router.post("/p/{slug}/environments/{env_id}")
    async def update_environment(
        request: Request,
        slug: str,
        env_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        async def write(form: Any) -> None:
            await service.save_environment(
                slug,
                environment_values(form),
                env_id=env_id,
                confirm_live=confirmed(form),
                actor="owner",
            )

        return await environment_write(
            request, slug, csrf, f"env-{env_id}", write, f"#env-{env_id}"
        )

    @router.post("/p/{slug}/environments/{env_id}/delete")
    async def delete_environment(
        request: Request,
        slug: str,
        env_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        async def write(form: Any) -> None:
            await service.delete_environment(slug, env_id, actor="owner")

        return await environment_write(request, slug, csrf, f"env-{env_id}", write)

    @router.post("/p/{slug}/environments/{env_id}/access")
    async def add_access(
        request: Request,
        slug: str,
        env_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        async def write(form: Any) -> None:
            fields = env_domain.parse_fields_text(str(form.get("fields") or ""))
            await service.save_access(
                slug, env_id, str(form.get("kind") or ""), fields, actor="owner"
            )

        return await environment_write(
            request, slug, csrf, f"access-new-{env_id}", write, f"#env-{env_id}"
        )

    @router.post("/p/{slug}/environments/{env_id}/access/{access_id}")
    async def update_access(
        request: Request,
        slug: str,
        env_id: int,
        access_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        async def write(form: Any) -> None:
            fields = env_domain.parse_fields_text(str(form.get("fields") or ""))
            await service.save_access(
                slug,
                env_id,
                str(form.get("kind") or ""),
                fields,
                access_id=access_id,
                actor="owner",
            )

        return await environment_write(
            request, slug, csrf, f"access-{access_id}", write, f"#env-{env_id}"
        )

    @router.post("/p/{slug}/environments/{env_id}/access/{access_id}/delete")
    async def delete_access(
        request: Request,
        slug: str,
        env_id: int,
        access_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        async def write(form: Any) -> None:
            await service.delete_access(slug, env_id, access_id, actor="owner")

        return await environment_write(
            request, slug, csrf, f"access-{access_id}", write, f"#env-{env_id}"
        )

    @router.post("/p/{slug}/proposals/{proposal_id}/{decision}")
    async def decide_proposal(
        request: Request,
        slug: str,
        proposal_id: int,
        decision: str,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if decision not in ("accept", "reject"):
            raise HTTPException(status_code=404)

        async def write(form: Any) -> None:
            await service.decide_proposal(
                proposal_id,
                decision == "accept",
                project=slug,
                confirm_live=confirmed(form),
                actor="owner",
            )

        return await environment_write(request, slug, csrf, "", write)

    @router.get("/events")
    async def events(
        project: str | None = None,
        last_event_id_query: Annotated[int | None, Query(alias="last_event_id")] = None,
        last_event_id_header: Annotated[str | None, Header(alias="last-event-id")] = None,
    ):
        project_id = (await service.get_project(project)).id if project else None
        last_id = last_event_id_query
        # The header comes from the browser's own reconnect and is newer than the URL.
        if last_event_id_header and last_event_id_header.strip().isdigit():
            last_id = int(last_event_id_header)
        stream = event_stream(
            service,
            project_id=project_id,
            last_event_id=last_id,
            replay_limit=settings.replay_limit,
            heartbeat=settings.heartbeat_seconds,
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def integrations_plan(mode: str, detection: machine_setup.Detection, remove: bool = False):
        return machine_setup.make_plan(
            base_url=settings.base_url,
            token=token,
            token_path=settings.token_path,
            paths=machine_setup.claude_paths(settings.claude_home),
            detection=detection,
            mode=mode,
            remove=remove,
        )

    async def integrations_page(
        request: Request,
        mode: str,
        *,
        message: str | None = None,
        error: str | None = None,
        checks: list[machine_setup.Check] | None = None,
    ) -> HTMLResponse:
        if mode not in machine_setup.MODES:
            mode = machine_setup.DEFAULT_MODE
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        # `claude --version` may take a moment: keep the event loop free meanwhile.
        detection = await asyncio.to_thread(machine_setup.detect)
        context = {
            "plan": integrations_plan(mode, detection),
            "remove_plan": integrations_plan(mode, detection, remove=True),
            "mode": mode,
            "modes": machine_setup.MODES,
            "message": message,
            "error": error,
            "checks": checks,
            "unlinked": await service.unlinked_hook_sessions(),
        }
        if "hx-request" in request.headers:
            return render(request, "_integrations.html", **context)
        return await page(
            request,
            "integrations.html",
            "settings",
            **context,
            last_event_id=last_event_id,
        )

    def check_csrf(value: str) -> None:
        if not same_secret(value, csrf_token(token)):
            raise HTTPException(status_code=403, detail="Missing CSRF token")

    @router.get("/settings")
    async def settings_index() -> Response:
        return RedirectResponse("/settings/integrations", status_code=303)

    @router.get("/settings/integrations", response_class=HTMLResponse)
    async def integrations(request: Request, mode: str = machine_setup.DEFAULT_MODE):
        return await integrations_page(request, mode)

    @router.post("/settings/integrations/{action}", response_class=HTMLResponse)
    async def integrations_action(
        request: Request,
        action: str,
        mode: Annotated[str, Form()] = machine_setup.DEFAULT_MODE,
        expect: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        check_csrf(csrf)
        if mode not in machine_setup.MODES:
            mode = machine_setup.DEFAULT_MODE
        if action == "check":
            checks = await asyncio.to_thread(
                machine_setup.run_check,
                base_url=settings.base_url,
                token=token,
                paths=machine_setup.claude_paths(settings.claude_home),
            )
            return await integrations_page(request, mode, checks=checks)
        if action not in ("apply", "remove"):
            raise HTTPException(status_code=404)
        detection = await asyncio.to_thread(machine_setup.detect)
        plan = integrations_plan(mode, detection, remove=action == "remove")
        if plan.error or plan.hooks is None:
            return await integrations_page(request, mode, error=plan.error)
        try:
            # `expect` is the hash of the plan shown on the page: write only what was approved.
            backup = machine_setup.write_change(plan.hooks, expect=expect)
        except (machine_setup.SetupError, OSError) as exc:
            return await integrations_page(request, mode, error=str(exc))
        if not plan.hooks.changed:
            message = "Nothing to change."
        else:
            done = "Removed relaybay's hooks from" if action == "remove" else "Wrote"
            message = f"{done} {plan.hooks.path}" + (f" (backup: {backup.name})" if backup else "")
        return await integrations_page(request, mode, message=message)

    @router.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    return router
