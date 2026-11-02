"""Board: server-rendered pages and HTMX fragments, login, SSE endpoint."""

import secrets
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse

from relaybay import __version__
from relaybay.config import Settings
from relaybay.domain.errors import DomainError, NotFoundError
from relaybay.domain.models import Task
from relaybay.domain.stages import STAGES, Stage
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
from relaybay.service import Service
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
DONE_COLUMN_LIMIT = 20
TASK_PAGE_REPORTS = 10
COOKIE_MAX_AGE = 60 * 60 * 24 * 365
EDITABLE = ("title", "stage", "waiting_on", "next_step", "branch")


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
    )
    env.filters["age"] = age
    env.filters["markdown"] = render_markdown

    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    async def board_context(slug: str) -> dict[str, Any]:
        project = await service.get_project(slug)
        tasks = await service.list_tasks(project=project.slug, include_done=True)
        by_stage: dict[str, list[Task]] = {s.value: [] for s in STAGES}
        for task in tasks:
            by_stage[task.stage.value].append(task)
        by_stage[Stage.DONE] = by_stage[Stage.DONE][:DONE_COLUMN_LIMIT]
        shown = [t for column in by_stage.values() for t in column]
        return {
            "project": project,
            "columns": by_stage,
            "last_reports": await service.last_reports(shown),
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
    async def board_page(request: Request, slug: str):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        context = await board_context(slug)
        first_busy = next((s for s in STAGES if context["columns"][s]), Stage.PLAN)
        return render(
            request,
            "board.html",
            **context,
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
    async def column_fragment(request: Request, slug: str, stage: Stage):
        context = await board_context(slug)
        return render(
            request,
            "_column.html",
            project=context["project"],
            stage=stage,
            tasks=context["columns"][stage],
            last_reports=context["last_reports"],
        )

    @router.get("/p/{slug}/t/{key}/card", response_class=HTMLResponse)
    async def card_fragment(request: Request, slug: str, key: str):
        task = await find_task(slug, key)
        reports = await service.last_reports([task])
        return render(request, "_card.html", t=task, slug=slug, last=reports.get(task.id))

    @router.get("/p/{slug}/t/{key}/edit", response_class=HTMLResponse)
    async def edit_fragment(request: Request, slug: str, key: str):
        task = await find_task(slug, key)
        return await edit_form(request, slug, task)

    async def edit_form(
        request: Request,
        slug: str,
        task: Task,
        error: str | None = None,
        values: dict[str, str] | None = None,
    ) -> HTMLResponse:
        reports = await service.last_reports([task])
        response = render(
            request,
            "_card_form.html",
            t=task,
            slug=slug,
            last=reports.get(task.id),
            error=error,
            values=values or {},
            waiting_values=await service.waiting_on_values(),
        )
        if error:
            # htmx does not swap 4xx bodies by default, so the form comes back as 200 and is
            # re-targeted at itself.
            response.headers["HX-Retarget"] = f"#card-{task.key}"
            response.headers["HX-Reswap"] = "outerHTML"
        return response

    @router.post("/p/{slug}/t/{key}", response_class=HTMLResponse)
    async def save_card(
        request: Request,
        slug: str,
        key: str,
        title: Annotated[str, Form()] = "",
        stage: Annotated[str, Form()] = "",
        waiting_on: Annotated[str, Form()] = "",
        next_step: Annotated[str, Form()] = "",
        branch: Annotated[str, Form()] = "",
        version: Annotated[int | None, Form()] = None,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if not same_secret(csrf, csrf_token(token)):
            raise HTTPException(status_code=403, detail="Missing CSRF token")
        task = await find_task(slug, key)
        values = {
            "title": title,
            "stage": stage,
            "waiting_on": waiting_on,
            "next_step": next_step,
            "branch": branch,
        }
        changes = {k: (v.strip() or None) for k, v in values.items()}
        try:
            await service.update_task(task.key, changes, expected_version=version, actor="board")
        except DomainError as exc:
            fresh = await find_task(slug, key)
            return await edit_form(request, slug, fresh, error=str(exc), values=values)
        return render(request, "_columns.html", **await board_context(slug))

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
