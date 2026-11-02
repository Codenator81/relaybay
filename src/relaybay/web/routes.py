"""Board: server-rendered pages and HTMX fragments, login, SSE endpoint."""

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse

from relaybay import __version__
from relaybay.config import Settings
from relaybay.domain.errors import ConflictError, DomainError, NotFoundError
from relaybay.domain.models import Task
from relaybay.domain.stages import STAGES, Stage
from relaybay.domain.tasks import is_archived
from relaybay.mcp_server.format import age
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
from relaybay.service import HTTP_URL_RE, Service
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
    )
    env.filters["age"] = age
    env.filters["markdown"] = render_markdown
    env.filters["http_url"] = http_url

    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

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
        return {"status": "ok", "version": __version__, "token_id": token_id(token)}

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
        return render(
            request,
            "projects.html",
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
        return render(
            request,
            "board.html",
            **context,
            **dialog,
            active_stage=first_busy,
            last_event_id=last_event_id,
        )

    @router.get("/t/{key}", response_class=HTMLResponse)
    async def task_page(request: Request, key: str):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        return render(
            request,
            "task.html",
            detail=await service.task_detail(key, reports=TASK_PAGE_REPORTS),
            last_event_id=last_event_id,
        )

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
    ) -> HTMLResponse:
        # Errors come back as 200: htmx does not swap 4xx bodies by default.
        return render(
            request,
            "_task_form.html",
            t=task,
            error=error,
            conflict=conflict,
            saved=saved,
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
        if changes.get("next_step", "") == task.next_step:
            # The form always resends the next step; only an edited one confirms it as current.
            del changes["next_step"]
        try:
            await service.update_task(task.key, changes, expected_version=version, actor="board")
        except ConflictError:
            fresh = await find_task(slug, key)
            return await edit_form(
                request, fresh, error=SAVE_CONFLICT, values=values, conflict=True
            )
        except DomainError as exc:
            fresh = await find_task(slug, key)
            return await edit_form(request, fresh, error=str(exc), values=values)
        return await edit_form(request, await find_task(slug, key), saved=True)

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

    @router.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    return router
