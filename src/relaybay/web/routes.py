"""Board: server-rendered pages and HTMX fragments, login, SSE endpoint, Integrations."""

import asyncio
import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Header, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse

from relaybay import __version__, machine_setup
from relaybay.config import Settings
from relaybay.domain import environments as env_domain
from relaybay.domain import guard
from relaybay.domain import memory as mem
from relaybay.domain.annotations import BODY_MAX as ANNOTATION_BODY_MAX
from relaybay.domain.annotations import COLOURS
from relaybay.domain.backlog import ASSIGNEE_MAX
from relaybay.domain.comments import COMMENT_MAX
from relaybay.domain.errors import (
    ConfirmationNeeded,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from relaybay.domain.intake import autonomy_reason
from relaybay.domain.models import Task
from relaybay.domain.review import MB
from relaybay.domain.rules import rules_markdown
from relaybay.domain.stages import BOARD_PAGES, STAGES, Stage, parse_stage
from relaybay.domain.tasks import is_archived
from relaybay.domain.usage import UsageReport
from relaybay.mcp_server import format as fmt
from relaybay.mcp_server.format import age, event_detail, event_subject
from relaybay.policy import PRESETS
from relaybay.security import (
    COOKIE_NAME,
    CSRF_FIELD,
    csrf_token,
    login_url,
    port_cookie,
    safe_next,
    same_origin,
    same_secret,
    session_cookie_value,
    token_id,
)
from relaybay.service import HTTP_URL_RE, Inbox, Service, has_hidden_chars
from relaybay.web.markdown import render_document, render_markdown
from relaybay.web.sse import event_stream

TEMPLATES_DIR = Path(__file__).parent / "templates"
LOGIN_CSRF_COOKIE = "relaybay_login_csrf"
LOGIN_ERROR_COOKIE = "relaybay_login_error"
# The outcome of a full-page form POST, shown once by the GET it redirects to.
FLASH_COOKIE = "relaybay_flash"
FLASH_TEXT_MAX = 600
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
    "size",
)
TASK_PAGE_REPORTS = 10
SAVE_CONFLICT = (
    "Someone else changed this task while you were editing. Your edits are kept below; Save"
    " again to overwrite their change, or reload to discard your edits."
)
COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def cookie_name(request: Request, name: str) -> str:
    return port_cookie(name, request.headers.get("host"))


ACTIVITY_PAGE = 50
AUDIT_PAGE = 50
MESSAGES_PAGE = 50
AUDIT_DAYS = (1, 7, 30, 90, 0)
EVENT_ID_MAX = 2**63 - 1  # the largest integer SQLite binds
EVENT_KINDS = (
    "project.created",
    "project.updated",
    "project.pulse_set",
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
    "message.sent",
    "message.delivered",
    "message.read",
    "message.loop_stopped",
    "message.loop_dismissed",
    "environment.created",
    "environment.updated",
    "environment.deleted",
    "environment.access_added",
    "environment.access_updated",
    "environment.access_removed",
    "environment.proposed",
    "environment.proposal_accepted",
    "environment.proposal_rejected",
    "policy.updated",
    "policy.proposed",
    "policy.proposal_accepted",
    "policy.proposal_rejected",
    "policy.proposal_reopened",
    "live.action",
    "review.step_added",
    "review.step_updated",
    "review.step_removed",
    "review.step_accepted",
    "review.step_rejected",
    "plan.set",
    "plan.step_added",
    "plan.step_updated",
    "plan.accepted",
    "session.todo_updated",
    "session.message",
    "session.launched",
    "session.no_live_on",
    "session.no_live_off",
    "attachment.added",
    "attachment.updated",
    "attachment.removed",
    "annotation.added",
    "annotation.updated",
    "annotation.removed",
    "annotation.replied",
    "document.updated",
    "memory.added",
    "memory.updated",
    "block.updated",
    "rules.updated",
    "rules.proposed",
    "rules.proposal_accepted",
    "rules.proposal_rejected",
    "rules.notice_read",
    "filter.saved",
    "filter.deleted",
    "intake.received",
    "intake.tied",
    "intake.answer_received",
    "intake.answer_cleared",
    "intake.wake",
    "intake.forwarded",
    "intake.proposed",
    "intake.dismissed",
    "intake.source_trusted",
    "intake.source_untrusted",
    "backlog.added",
    "backlog.updated",
    "backlog.planned",
    "brake.set",
    "brake.released",
    "budget.set",
    "budget.removed",
    "budget.warned",
    "budget.exceeded",
    "concurrency.over",
)
CSRF_HEADER = "x-relaybay-csrf"
ANNOTATION_JSON_MAX = 256 * 1024  # a pen line of the most points fits
BOARD_ONLY = (
    "Environments, permission levels and the policy change only from the board in a browser;"
    " agents propose changes with environment_propose or policy_propose."
)
ENV_TEXT_FIELDS = ("name", "kind", "base_url", "login_state_path", "notes", "level")
JOURNAL_LIMIT = 20
DOCS_DECISIONS = 200
DOC_NEW_FIELDS = ("doc", "title", "kind", "source_url", "body", "reason")
# Hook status changes arrive every few seconds per working session: the Activity feed shows
# them only when the kind filter asks for them.
QUIET_KINDS = ("session.status_changed",)


BACKLOG_ROWS = 500
BACKLOG_STATUSES = ("new", "later", "planned", "done", "declined")
USAGE_ROWS = 500
USAGE_DAYS = (7, 30, 90, 365, 0)
CHART_WIDTH = 720
CHART_HEIGHT = 120
CHART_GAP = 2


def usage_chart(report: UsageReport) -> dict[str, Any] | None:
    """Bars for the Usage page as numbers for an inline SVG; drawn from presentation attributes,
    which the CSP allows, so the page needs no chart library."""
    if not report.periods:
        return None
    top = max(total.tokens for _, total in report.periods) or 1
    width = CHART_WIDTH / len(report.periods)
    bars = []
    for index, (period, total) in enumerate(report.periods):
        height = round(total.tokens / top * (CHART_HEIGHT - 2), 1)
        if total.tokens:
            height = max(height, 2)
        bars.append(
            {
                "x": round(index * width + CHART_GAP / 2, 2),
                "y": round(CHART_HEIGHT - height, 1),
                "width": round(max(width - CHART_GAP, 1), 2),
                "height": height,
                "hit_x": round(index * width, 2),
                "hit_width": round(width, 2),
                "title": f"{period}: {total.tokens:,} tokens",
            }
        )
    return {
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "bars": bars,
        "top": top,
        "first": report.periods[0][0],
        "last": report.periods[-1][0],
    }


def static_versions(folder: Path = STATIC_DIR) -> dict[str, str]:
    """A short content hash per static file: `?v=<hash>` makes a browser fetch a changed file
    instead of reusing its cached copy after an upgrade."""
    return {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest()[:12]
        for f in folder.iterdir()
        if f.is_file()
    }


def utcnow() -> datetime:
    return datetime.now(UTC)


def http_url(value: str | None) -> str | None:
    """`value` when it is an http(s) link, else None. Writes validate it already; this keeps a
    hand-edited database from turning into a `javascript:` link."""
    if not value or has_hidden_chars(value):
        return None
    return value if HTTP_URL_RE.fullmatch(value) else None


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
        annotation_colours=COLOURS,
        annotation_body_max=ANNOTATION_BODY_MAX,
        pulse_stale=service.pulse_stale,
        pulse_stale_days=settings.pulse_stale_days,
        plan_draft_hours=service.plan_draft_hours,
    )
    versions = static_versions()

    def static_url(name: str) -> str:
        return f"/static/{name}?v={versions[name]}"

    env.globals["static_url"] = static_url
    env.filters["age"] = age
    env.filters["markdown"] = render_markdown
    env.filters["document"] = render_document
    env.filters["http_url"] = http_url
    env.filters["event_detail"] = event_detail
    env.filters["event_subject"] = event_subject
    env.filters["change_summary"] = env_domain.change_summary
    env.globals["fields_text"] = env_domain.fields_text
    env.globals["autonomy_reason"] = autonomy_reason
    env.filters["tojson_compact"] = lambda value: json.dumps(value, ensure_ascii=False)

    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    def flash_redirect(request: Request, url: str, **flash: Any) -> RedirectResponse:
        """POST → 303 → GET: a refresh after saving does not resend the form. `flash`
        (message, error, checks) rides along in a short-lived cookie the GET shows once."""
        response = RedirectResponse(url, status_code=303)
        data = {k: v for k, v in flash.items() if v}
        for key in ("message", "error"):
            if key in data:
                data[key] = str(data[key])[:FLASH_TEXT_MAX]
        if data:
            value = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
            response.set_cookie(
                cookie_name(request, FLASH_COOKIE),
                value,
                max_age=60,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        return response

    def take_flash(request: Request) -> dict[str, Any]:
        raw = request.cookies.get(cookie_name(request, FLASH_COOKIE))
        if not raw:
            return {}
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode()))
        except (binascii.Error, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def shown_once(request: Request, response: Response) -> Response:
        name = cookie_name(request, FLASH_COOKIE)
        if name in request.cookies:
            response.delete_cookie(name)
        return response

    async def inbox() -> Inbox:
        return await service.inbox(
            settings.owner_names,
            require_tracker_log=settings.require_tracker_log,
            done_window_days=settings.done_window_days,
        )

    async def saved_filters_by_project() -> dict[str, list[Any]]:
        found: dict[str, list[Any]] = {}
        for item in await service.saved_filters():
            found.setdefault(item.project_slug, []).append(item)
        return found

    async def page(
        request: Request,
        name: str,
        active: str,
        nav_project: str | None = None,
        **context: Any,
    ) -> HTMLResponse:
        """A full page with the sidebar: `active` names the highlighted item (`inbox`,
        `boards`, `usage`, `activity`, `audit`, `messages`, `settings`, `sessions`;
        `board`,
        `sessions`,
        `environments` or
        `docs` with `project`)."""
        nav = {
            "active": active,
            "project": nav_project,
            "projects": [item.project for item in await service.list_projects()],
            "inbox_count": (await inbox()).count,
            "saved_filters": await saved_filters_by_project(),
            "backlog_counts": await service.backlog_counts(),
            "filter_active": bool(context.get("filter_query")),
            "brakes": await service.open_brakes(),
            "path": request.url.path,
        }
        return render(request, name, nav=nav, **context)

    async def board_context(
        slug: str,
        archived: bool = False,
        q: str = "",
        stage: str = "",
        waiting_on: str = "",
        session: str = "",
    ) -> dict[str, Any]:
        """Columns of the board. Archived done tasks are left out unless `archived` is set or a
        search `q` (matched against key and title) is given; a search covers the archive too.
        `stage`, `waiting_on` and `session` narrow the tasks like `task_list` does."""
        project = await service.get_project(slug)
        waiting_on = waiting_on.strip()[:SEARCH_MAX]
        session = session.strip()[:SEARCH_MAX]
        filter_error = None
        try:
            stage = parse_stage(stage).value if stage.strip() else ""
            tasks = await service.list_tasks(
                project=project.slug,
                stage=stage or None,
                waiting_on=waiting_on or None,
                session=session or None,
                include_done=True,
            )
        except ValidationError as exc:
            # A hand-typed URL still shows the board, with the reason, not a bare 400.
            filter_error = f"Filter ignored: {exc}"
            stage = waiting_on = session = ""
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
        query = {"stage": stage, "waiting_on": waiting_on, "session": session, "q": q}
        filter_query = {name: value for name, value in query.items() if value}
        # A saved filter's URL is exactly filter_query: the same order, so the sidebar can match.
        view = {**filter_query, "archived": "1" if archived else ""}
        page_qs = {
            n: urlencode({k: v for k, v in {**view, "page": str(n) if n > 1 else ""}.items() if v})
            for n in range(1, len(BOARD_PAGES) + 1)
        }
        return {
            "board_pages": BOARD_PAGES,
            "page_qs": page_qs,
            "project": project,
            "columns": by_stage,
            "archived": archived,
            "q": q,
            "stage_filter": stage,
            "waiting_filter": waiting_on,
            "session_filter": session,
            "filter_query": filter_query,
            "filter_error": filter_error,
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
            # What config.toml says, for `relaybay doctor`; accepted versions may differ.
            "rules_hash": service.config_rules.hash,
        }

    def signed_in(request: Request, next_path: str) -> Response:
        response = RedirectResponse(safe_next(next_path), status_code=303)
        response.set_cookie(
            cookie_name(request, COOKIE_NAME),
            session_cookie_value(token),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        response.delete_cookie(cookie_name(request, LOGIN_CSRF_COOKIE))
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
            cookie_name(request, LOGIN_CSRF_COOKIE),
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
                cookie_name(request, LOGIN_ERROR_COOKIE),
                "invalid",
                max_age=60,
                httponly=True,
                samesite="strict",
            )
            return response
        error_cookie = cookie_name(request, LOGIN_ERROR_COOKIE)
        if request.cookies.get(error_cookie):
            response = login_form(request, next_path, INVALID_TOKEN, status=401)
            response.delete_cookie(error_cookie)
            return response
        return login_form(request, next_path)

    @router.post("/login")
    async def login_submit(
        request: Request,
        token_field: Annotated[str, Form(alias="token")] = "",
        next_path: Annotated[str, Form(alias="next")] = "/",
        nonce: Annotated[str, Form(alias="login_csrf")] = "",
    ):
        cookie = request.cookies.get(cookie_name(request, LOGIN_CSRF_COOKIE))
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
        request: Request,
        slug: str,
        task: str | None = None,
        archived: bool = False,
        q: str = "",
        stage: str = "",
        waiting_on: str = "",
        session: str = "",
        page_number: Annotated[str, Query(alias="page")] = "",
        tab: str = "",
    ):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        flash = take_flash(request)
        context = await board_context(slug, archived, q, stage, waiting_on, session)
        chosen = context["stage_filter"]
        first_busy = chosen or next((s for s in STAGES if context["columns"][s]), Stage.PLAN)
        # Without an explicit page, a stage filter opens the page its column is on.
        default_page = next((n for n, page in enumerate(BOARD_PAGES, 1) if chosen in page), 1)
        board_page = int(page_number) if page_number.isdigit() else default_page
        board_page = min(max(board_page, 1), len(BOARD_PAGES))
        # `?task=<key>` is the URL of the open task dialog: render it so the page opens with it.
        dialog = (
            await dialog_context(slug, task, tab)
            if task
            else {"waiting_values": await service.waiting_on_values()}
        )
        response = await page(
            request,
            "board.html",
            "board",
            context["project"].slug,
            **context,
            **dialog,
            active_stage=first_busy,
            board_page=board_page,
            saved_filters=await service.saved_filters(context["project"].slug),
            message=flash.get("message"),
            error=flash.get("error") or context["filter_error"],
            last_event_id=last_event_id,
        )
        return shown_once(request, response)

    @router.post("/p/{slug}/filters")
    async def save_filter(
        request: Request,
        slug: str,
        name: Annotated[str, Form()] = "",
        stage: Annotated[str, Form()] = "",
        waiting_on: Annotated[str, Form()] = "",
        session: Annotated[str, Form()] = "",
        q: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        board_only(request, csrf)
        given = {"stage": stage, "waiting_on": waiting_on, "session": session, "q": q}
        back = f"/p/{slug}?{urlencode({k: v for k, v in given.items() if v})}"
        try:
            saved = await service.save_filter(slug, name, **given, actor="owner")
        except NotFoundError:
            raise
        except DomainError as exc:
            return flash_redirect(request, back, error=str(exc))
        return flash_redirect(request, back, message=f"Saved the filter {saved.name}.")

    @router.post("/p/{slug}/filters/{filter_id}/delete")
    async def delete_filter(
        request: Request,
        slug: str,
        filter_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        board_only(request, csrf)
        await service.delete_filter(slug, filter_id, actor="owner")
        return flash_redirect(request, f"/p/{slug}", message="Saved filter deleted.")

    @router.get("/switcher")
    async def switcher(q: str = "") -> JSONResponse:
        items = [asdict(item) for item in await service.switcher(q)]
        return JSONResponse({"items": items}, headers={"Cache-Control": "no-store"})

    @router.get("/update")
    async def update_status() -> JSONResponse:
        """The last update check of this server (signed in: the board cookie or the token)."""
        notice = service.update_notice()
        data = service.update.as_dict() | {"command": notice.command if notice else None}
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

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

    async def project_rows() -> list[Any]:
        return [item.project for item in await service.list_projects()]

    @router.get("/inbox", response_class=HTMLResponse)
    async def inbox_page(request: Request):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        flash = take_flash(request)
        response = await page(
            request,
            "inbox.html",
            "inbox",
            inbox=await inbox(),
            inbox_projects=await project_rows(),
            message=flash.get("message"),
            error=flash.get("error"),
            last_event_id=last_event_id,
        )
        return shown_once(request, response)

    @router.get("/fragments/inbox", response_class=HTMLResponse)
    async def inbox_fragment(request: Request):
        return render(
            request, "_inbox.html", inbox=await inbox(), inbox_projects=await project_rows()
        )

    @router.get("/fragments/inbox-badge", response_class=HTMLResponse)
    async def inbox_badge(request: Request):
        return render(request, "_inbox_badge.html", count=(await inbox()).count)

    @router.get("/usage", response_class=HTMLResponse)
    async def usage_page(
        request: Request,
        project: str = "",
        session: str = "",
        model: str = "",
        days: int = 30,
        group: str = "day",
    ):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        flash = take_flash(request)
        project = project.strip().lower()
        if session and not (session.isascii() and session.isdigit() and len(session) <= 18):
            raise ValidationError("session must be a session's number.")
        session_id = int(session) if session else None
        report = await service.usage_report(
            project=project or None,
            session_ids=[session_id] if session_id is not None else None,
            model=model or None,
            days=days,
            group=group,
        )
        sessions, models = await service.usage_filters()
        response = await page(
            request,
            "usage.html",
            "usage",
            budgets=await service.budget_states(),
            message=flash.get("message"),
            error=flash.get("error"),
            report=report,
            shown_rows=report.rows[:USAGE_ROWS],
            chart=usage_chart(report),
            project_filter=project,
            session_filter=session_id,
            last_event_id=last_event_id,
            model_filter=model,
            sessions=sessions,
            models=models,
            days_choices=USAGE_DAYS,
            usage_enabled=settings.usage_enabled,
        )
        return shown_once(request, response)

    @router.get("/fragments/budgets", response_class=HTMLResponse)
    async def budgets_fragment(request: Request):
        return render(request, "_budgets.html", budgets=await service.budget_states())

    @router.post("/usage/budgets")
    async def set_budget(
        request: Request,
        scope: Annotated[str, Form()] = "",
        project: Annotated[str, Form()] = "",
        task: Annotated[str, Form()] = "",
        period: Annotated[str, Form()] = "day",
        max_tokens: Annotated[str, Form()] = "",
        action: Annotated[str, Form()] = "warn",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        board_only(request, csrf)
        try:
            budget = await service.set_budget(
                scope,
                project=project or None,
                task=task or None,
                period=period,
                max_tokens=max_tokens,
                action=action,
            )
        except DomainError as exc:
            return flash_redirect(request, "/usage#budgets", error=str(exc))
        return flash_redirect(
            request, "/usage#budgets", message=f"Saved the budget of {budget.target}."
        )

    @router.post("/usage/budgets/{budget_id}/delete")
    async def remove_budget(
        request: Request,
        budget_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        board_only(request, csrf)
        await service.remove_budget(budget_id)
        return flash_redirect(request, "/usage#budgets", message="Budget removed.")

    @router.get("/fragments/brake-banner", response_class=HTMLResponse)
    async def brake_banner(request: Request, next: str = "/sessions"):
        return render(
            request,
            "_brake_banner.html",
            brakes=await service.open_brakes(),
            back=back_to(next, "/sessions"),
        )

    @router.post("/brake")
    async def set_brake(
        request: Request,
        scope: Annotated[str, Form()] = "all",
        project: Annotated[str, Form()] = "",
        reason: Annotated[str, Form()] = "",
        next_path: Annotated[str, Form(alias="next")] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        board_only(request, csrf)
        back = back_to(next_path, "/sessions")
        try:
            await service.set_board_brake(scope, project=project or None, reason=reason)
        except DomainError as exc:
            return flash_redirect(request, back, error=str(exc))
        return RedirectResponse(back, status_code=303)

    @router.post("/brake/{brake_id}/release")
    async def release_brake(
        request: Request,
        brake_id: int,
        next_path: Annotated[str, Form(alias="next")] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        board_only(request, csrf)
        await service.release_brake(brake_id, actor="owner")
        return RedirectResponse(back_to(next_path, "/sessions"), status_code=303)

    @router.get("/sessions", response_class=HTMLResponse)
    async def all_sessions_page(request: Request):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        flash = take_flash(request)
        response = await page(
            request,
            "sessions.html",
            "sessions",
            sessions_project=None,
            rows=await service.list_sessions(),
            last_event_id=last_event_id,
            message=flash.get("message"),
            error=flash.get("error"),
        )
        return shown_once(request, response)

    @router.get("/p/{slug}/sessions", response_class=HTMLResponse)
    async def sessions_page(request: Request, slug: str):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        project = await service.get_project(slug)
        flash = take_flash(request)
        response = await page(
            request,
            "sessions.html",
            "sessions",
            project.slug,
            sessions_project=project,
            rows=await service.list_sessions(project.slug),
            last_event_id=last_event_id,
            message=flash.get("message"),
            error=flash.get("error"),
        )
        return shown_once(request, response)

    @router.get("/fragments/sessions", response_class=HTMLResponse)
    async def sessions_fragment(request: Request, project: str = ""):
        sessions_project = await service.get_project(project) if project else None
        rows = await service.list_sessions(sessions_project.slug if sessions_project else None)
        return render(request, "_sessions.html", sessions_project=sessions_project, rows=rows)

    @router.post("/sessions/{session_id}/no-live")
    async def session_no_live(
        request: Request,
        session_id: int,
        on: Annotated[bool, Form()] = True,
        back: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            saved = await service.set_session_no_live(session_id, on, actor="owner")
            if on and saved.cc_session_id is None:
                return (
                    f"Live is off for {saved.name}, but it is not linked to hooks yet: this"
                    " applies once it is linked."
                )
            if on:
                return f"Live is off for {saved.name}: any touch of a live stand is denied."
            return f"Live is back for {saved.name}: its level on each stand applies again."

        return await intake_write(request, csrf, write, back_to(back, "/sessions"))

    async def audit_context(
        project: str,
        environment: str,
        session: str,
        decision: str,
        days: int,
        changes: bool,
        before: int | None,
        after: int | None,
    ) -> dict[str, Any]:
        if session and not (session.isascii() and session.isdigit() and len(session) <= 18):
            raise ValidationError("session must be a session's number.")
        if before is not None:
            before = max(-EVENT_ID_MAX, min(before, EVENT_ID_MAX))
        if after is not None:
            after = max(-EVENT_ID_MAX, min(after, EVENT_ID_MAX))
        filters: dict[str, Any] = {
            "project": project,
            "environment": environment,
            "session": session,
            "decision": decision,
            "changes": 1 if changes else "",
        }
        # A live refetch (`after`) takes everything new at once; a page stops at AUDIT_PAGE.
        limit = AUDIT_PAGE * 4 if after is not None else AUDIT_PAGE
        items = await service.audit(
            project=project or None,
            environment=environment or None,
            session_ids=[int(session)] if session else None,
            decision=decision or None,
            days=days,
            changes=changes,
            before_id=before,
            after_id=after,
            limit=limit + 1,
        )
        more = after is None and len(items) > limit
        items = items[:limit]
        kept = {k: v for k, v in filters.items() if v}
        kept["days"] = days  # 0 (all of it) is a choice, and the default is not 0
        more_qs = urlencode({**kept, "before": items[-1].id}) if more else ""
        return {"events": items, "more_qs": more_qs, "filters_qs": urlencode(kept)}

    @router.get("/audit", response_class=HTMLResponse)
    async def audit_page(
        request: Request,
        project: str = "",
        environment: str = "",
        session: str = "",
        decision: str = "",
        days: int = 7,
        changes: bool = False,
        before: int | None = None,
    ):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        project = project.strip().lower()
        context = await audit_context(
            project, environment, session, decision, days, changes, before, None
        )
        return await page(
            request,
            "audit.html",
            "audit",
            **context,
            project_filter=project,
            environment_filter=environment,
            session_filter=session,
            decision_filter=decision,
            days_filter=days,
            changes_filter=changes,
            days_choices=AUDIT_DAYS,
            decisions=guard.DECISION_ORDER,
            environment_names=await service.environment_names(),
            session_rows=await service.list_sessions(),
            paged=before is not None,
            last_event_id=last_event_id,
        )

    @router.get("/fragments/audit", response_class=HTMLResponse)
    async def audit_fragment(
        request: Request,
        project: str = "",
        environment: str = "",
        session: str = "",
        decision: str = "",
        days: int = 7,
        changes: bool = False,
        before: int | None = None,
        after: int | None = None,
    ):
        context = await audit_context(
            project.strip().lower(), environment, session, decision, days, changes, before, after
        )
        return render(request, "_audit_rows.html", **context)

    async def messages_context(session: str, before: int | None) -> dict[str, Any]:
        if session and not (session.isascii() and session.isdigit() and len(session) <= 18):
            raise ValidationError("session must be a session's number.")
        if before is not None:
            before = max(-EVENT_ID_MAX, min(before, EVENT_ID_MAX))
        items = await service.session_messages_page(
            session_id=int(session) if session else None,
            before_id=before,
            limit=MESSAGES_PAGE + 1,
        )
        more = len(items) > MESSAGES_PAGE
        items = items[:MESSAGES_PAGE]
        kept = {"session": session} if session else {}
        return {
            "messages": items,
            "more_qs": urlencode({**kept, "before": items[-1].id}) if more else "",
            "filters_qs": urlencode(kept),
            "live_rows": before is None,
        }

    @router.get("/messages", response_class=HTMLResponse)
    async def messages_page(request: Request, session: str = "", before: int | None = None):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        return await page(
            request,
            "messages.html",
            "messages",
            **await messages_context(session, before),
            session_filter=session,
            session_rows=await service.list_sessions(),
            last_event_id=last_event_id,
        )

    @router.get("/fragments/messages", response_class=HTMLResponse)
    async def messages_fragment(request: Request, session: str = ""):
        return render(request, "_messages.html", **await messages_context(session, None))

    @router.post("/messages/{message_id}/loop-seen")
    async def message_loop_seen(
        request: Request,
        message_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            await service.dismiss_message_loop(message_id, actor="owner")
            return f"Message #{message_id}: the stopped loop is marked as seen."

        return await intake_write(request, csrf, write)

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
        request: Request,
        slug: str,
        stage: Stage,
        archived: bool = False,
        q: str = "",
        stage_filter: Annotated[str, Query(alias="stage")] = "",
        waiting_on: str = "",
        session: str = "",
    ):
        context = await board_context(slug, archived, q, stage_filter, waiting_on, session)
        return render(
            request, "_column.html", **context, stage=stage, tasks=context["columns"][stage]
        )

    @router.get("/p/{slug}/t/{key}/card", response_class=HTMLResponse)
    async def card_fragment(request: Request, slug: str, key: str):
        task = await find_task(slug, key)
        return render(request, "_card.html", t=task, slug=slug)

    async def dialog_context(slug: str, key: str, tab: str = "") -> dict[str, Any]:
        task = await find_task(slug, key)
        return {
            "detail": await service.task_detail(task.key, reports=TASK_PAGE_REPORTS),
            "values": {},
            "waiting_values": await service.waiting_on_values(),
            "tab": tab,
        }

    @router.get("/p/{slug}/t/{key}/dialog", response_class=HTMLResponse)
    async def dialog_fragment(request: Request, slug: str, key: str, tab: str = ""):
        return render(request, "_task_dialog.html", **await dialog_context(slug, key, tab))

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
        autonomy = form.get("autonomy")
        if isinstance(autonomy, str) and autonomy != task.autonomy:
            # Autonomy lets an agent go on by itself: a browser session only, never a Bearer client.
            board_only(request, csrf)
        try:
            result = await service.update_task_checked(
                task.key, changes, expected_version=version, actor="board"
            )
            if isinstance(autonomy, str) and autonomy != result.task.autonomy:
                saved = await service.set_task_autonomy(task.key, autonomy, actor="owner")
                result = result._replace(task=saved)
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

    @router.post("/p/{slug}/t/{key}/bind")
    async def bind_task_session(
        request: Request,
        slug: str,
        key: str,
        session_id: Annotated[int, Form()],
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if not same_secret(csrf, csrf_token(token)):
            raise HTTPException(status_code=403, detail="Missing CSRF token")
        task = await find_task(slug, key)
        htmx = "hx-request" in request.headers
        error = None
        try:
            await service.bind_session_row(task.key, session_id, actor="board")
        except DomainError as exc:
            if not htmx:
                raise  # plain form post: the app's DomainError handler answers
            error = str(exc)
        if not htmx:
            return RedirectResponse(f"/t/{task.key}", status_code=303)
        detail = await service.task_detail(task.key, reports=TASK_PAGE_REPORTS)
        return render(request, "_task_view.html", detail=detail, error=error)

    async def comment_thread(
        request: Request, task: Task, error: str | None = None, saved: bool = False
    ) -> HTMLResponse:
        _, comments = await service.list_comments(task.key)
        doc_refs = await service.resolve_doc_links(*(c.body_md for c in comments))
        response = render(
            request,
            "_comment_thread.html",
            t=task,
            comments=comments,
            error=error,
            doc_refs=doc_refs,
        )
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

    # --- Review: checklist decisions, pasted screenshots, files ----------------------

    async def review_panel(request: Request, task: Task, error: str | None = None) -> HTMLResponse:
        _, review, _ = await service.review(task.key)
        fresh = await service.get_task(task.key)
        return render(request, "_review.html", t=fresh, review=review, error=error)

    @router.get("/p/{slug}/t/{key}/review", response_class=HTMLResponse)
    async def review_fragment(request: Request, slug: str, key: str):
        return await review_panel(request, await find_task(slug, key))

    @router.post("/p/{slug}/t/{key}/review/{step_id}", response_class=HTMLResponse)
    async def decide_step(
        request: Request,
        slug: str,
        key: str,
        step_id: int,
        decision: Annotated[str, Form()] = "",
        comment: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        board_only(request, csrf)
        task = await find_task(slug, key)
        htmx = "hx-request" in request.headers
        try:
            await service.decide_review_step(task.key, step_id, decision, comment, actor="owner")
        except NotFoundError:
            raise
        except DomainError as exc:
            if not htmx:
                raise
            return await review_panel(request, task, error=str(exc))  # 200: htmx swaps it
        if not htmx:
            return RedirectResponse(f"/p/{slug}?task={task.key}&tab=review", status_code=303)
        return await review_panel(request, task)

    @router.post("/p/{slug}/t/{key}/review/{step_id}/paste")
    async def paste_screenshot(request: Request, slug: str, key: str, step_id: int):
        """A screenshot pasted into a step: the raw image is the body, the CSRF token a header
        (a board page's script sends it)."""
        board_only(request, request.headers.get(CSRF_HEADER, ""))
        task = await find_task(slug, key)
        limit = service.attachment_max_bytes
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return PlainTextResponse(
                    f"The image is larger than {limit // MB} MB.", status_code=413
                )
        try:
            item, new = await service.attach_pasted(task.key, step_id, bytes(body))
        except NotFoundError:
            raise
        except DomainError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return PlainTextResponse(
            f"Attached #{item.id}." if new else f"Already attached as #{item.id}.",
            status_code=201 if new else 200,
        )

    @router.post("/p/{slug}/t/{key}/attachments/{attachment_id}/delete")
    async def delete_attachment(
        request: Request,
        slug: str,
        key: str,
        attachment_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        board_only(request, csrf)
        task = await find_task(slug, key)
        await service.remove_attachment(task.key, attachment_id, actor="owner")
        if "hx-request" not in request.headers:
            return RedirectResponse(f"/p/{slug}?task={task.key}&tab=review", status_code=303)
        return await review_panel(request, task)

    # --- Plan: the owner accepts it; the agent writes it over MCP --------------------------

    async def plan_panel(request: Request, task: Task, error: str | None = None) -> HTMLResponse:
        fresh, plan, todo = await service.plan(task.key)
        _, history = await service.plan_history(task.key)
        return render(
            request, "_plan.html", t=fresh, plan=plan, todo=todo, history=history, error=error
        )

    @router.get("/p/{slug}/t/{key}/plan", response_class=HTMLResponse)
    async def plan_fragment(request: Request, slug: str, key: str):
        return await plan_panel(request, await find_task(slug, key))

    @router.post("/p/{slug}/t/{key}/plan/accept", response_class=HTMLResponse)
    async def accept_plan(
        request: Request,
        slug: str,
        key: str,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        board_only(request, csrf)
        task = await find_task(slug, key)
        htmx = "hx-request" in request.headers
        try:
            await service.accept_plan(task.key, actor="owner")
        except DomainError as exc:
            if not htmx:
                raise
            return await plan_panel(request, task, error=str(exc))  # 200: htmx swaps it
        if not htmx:
            return RedirectResponse(f"/p/{slug}?task={task.key}&tab=plan", status_code=303)
        return await plan_panel(request, task)

    @router.get("/files/{attachment_id}")
    async def attachment_file(attachment_id: int) -> Response:
        """A stored screenshot, behind the board cookie, with its checked type; nosniff and the
        CSP come from the security middleware."""
        item, path = await service.attachment_file(attachment_id)
        if not path.is_file():
            raise NotFoundError(f"The file of attachment #{attachment_id} is missing.")
        return FileResponse(
            path,
            media_type=item.mime,
            content_disposition_type="inline",
            filename=item.stored_name,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # --- Annotations: the viewer's calls. A page's script sends the CSRF token as a header. ---

    def viewer_write(request: Request) -> None:
        board_only(request, request.headers.get(CSRF_HEADER, ""))

    async def limited_body(request: Request, limit: int, refusal: str) -> bytes:
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise HTTPException(status_code=413, detail=refusal)
        return bytes(body)

    async def json_body(request: Request) -> Any:
        raw = await limited_body(request, ANNOTATION_JSON_MAX, "The annotation is too large.")
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):  # RecursionError: nested too deep
            raise ValidationError("The body is not valid JSON.") from None

    def body_field(values: Any) -> Any:
        # A missing key is refused: `{}` must not wipe a pin's comment; null clears it on purpose.
        if not isinstance(values, dict) or "body" not in values:
            raise ValidationError("The body must be an object with body.")
        return values["body"]

    @router.get("/files/{attachment_id}/annotations")
    async def list_annotations(attachment_id: int) -> JSONResponse:
        item, annotations = await service.annotations(attachment_id)
        return JSONResponse(
            {
                "attachment": item.id,
                "annotated": bool(item.annotated_name),
                "annotations": [a.to_dict() for a in annotations],
            }
        )

    @router.post("/files/{attachment_id}/annotations")
    async def add_annotation(request: Request, attachment_id: int) -> JSONResponse:
        viewer_write(request)
        added = await service.add_annotation(attachment_id, await json_body(request))
        return JSONResponse(added.to_dict(), status_code=201)

    @router.post("/files/{attachment_id}/annotations/{annotation_id}")
    async def update_annotation(
        request: Request, attachment_id: int, annotation_id: int
    ) -> JSONResponse:
        viewer_write(request)
        values = await json_body(request)
        updated = await service.update_annotation(attachment_id, annotation_id, body_field(values))
        return JSONResponse(updated.to_dict())

    @router.post("/files/{attachment_id}/annotations/{annotation_id}/delete")
    async def remove_annotation(
        request: Request, attachment_id: int, annotation_id: int
    ) -> JSONResponse:
        viewer_write(request)
        removed = await service.remove_annotation(attachment_id, annotation_id)
        return JSONResponse(removed.to_dict())

    @router.post("/files/{attachment_id}/annotations/{annotation_id}/restore")
    async def restore_annotation(
        request: Request, attachment_id: int, annotation_id: int
    ) -> JSONResponse:
        viewer_write(request)
        restored = await service.restore_annotation(attachment_id, annotation_id)
        return JSONResponse(restored.to_dict())

    @router.post("/files/{attachment_id}/annotations/{annotation_id}/replies")
    async def reply_annotation(
        request: Request, attachment_id: int, annotation_id: int
    ) -> JSONResponse:
        viewer_write(request)
        values = await json_body(request)
        updated = await service.reply_as_owner(attachment_id, annotation_id, body_field(values))
        return JSONResponse(updated.to_dict(), status_code=201)

    @router.post("/files/{attachment_id}/annotated")
    async def upload_annotated(request: Request, attachment_id: int) -> JSONResponse:
        """The flattened PNG: the raw image is the body."""
        viewer_write(request)
        limit = service.attachment_max_bytes
        body = await limited_body(request, limit, f"The image is larger than {limit // MB} MB.")
        item = await service.store_annotated(attachment_id, body)
        return JSONResponse({"attachment": item.id, "size": item.annotated_size}, status_code=201)

    @router.get("/files/{attachment_id}/annotated")
    async def annotated_image(attachment_id: int) -> Response:
        """The latest flattened PNG. Its URL stays when the file is replaced: never cached blind."""
        item, path = await service.annotated_file(attachment_id)
        if not path.is_file():
            raise NotFoundError(f"The annotated image of attachment #{attachment_id} is missing.")
        return FileResponse(
            path,
            media_type="image/png",
            content_disposition_type="inline",
            filename=item.annotated_name,
            headers={"Cache-Control": "private, no-cache"},
        )

    @router.post("/files/{attachment_id}/send-annotations")
    async def send_annotations(request: Request, attachment_id: int) -> JSONResponse:
        viewer_write(request)
        comment = await service.send_annotations(attachment_id)
        return JSONResponse({"comment": comment.id if comment else None})

    # --- Stands & access: environments, access map, permission levels -----------------

    def board_only(request: Request, csrf: str) -> None:
        """Environment writes come from a signed-in browser (cookie + CSRF), not from a
        client that sends the token as a Bearer header."""
        check_csrf(csrf)
        cookie = request.cookies.get(cookie_name(request, COOKIE_NAME))
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
        message: str | None = None,
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
            policies=await service.environment_policies(items),
            project_policy=await service.effective_policy(project.slug),
            error=error,
            error_target=error_target,
            confirm=confirm,
            message=message,
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
        message: str = "Saved.",
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
        return flash_redirect(request, f"/p/{slug}/environments{anchor}", message=message)

    def confirmed(form: Any) -> bool:
        return form.get("confirm_live") == "1"

    def test_secrets_of(form: Any) -> dict[str, str] | None:
        """The test secrets textarea (`name: value` lines, `***` keeps a stored value); None
        when the form has none, so stored ones stay."""
        if "test_secrets" not in form:
            return None
        return env_domain.parse_fields_text(str(form.get("test_secrets") or ""))

    @router.get("/p/{slug}/environments", response_class=HTMLResponse)
    async def environments(request: Request, slug: str):
        flash = take_flash(request)
        response = await environments_page(request, slug, message=flash.get("message"))
        return shown_once(request, response)

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

        return await environment_write(
            request, slug, csrf, "new", write, message="Environment added."
        )

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
            request, slug, csrf, f"env-{env_id}", write, f"#env-{env_id}", "Environment saved."
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

        return await environment_write(
            request, slug, csrf, f"env-{env_id}", write, message="Environment deleted."
        )

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
                slug,
                env_id,
                str(form.get("kind") or ""),
                fields,
                test_secrets=test_secrets_of(form),
                actor="owner",
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
                test_secrets=test_secrets_of(form),
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

        return await environment_write(
            request, slug, csrf, "", write, message=f"Proposal #{proposal_id} {decision}ed."
        )

    # --- Docs & rules: documents, decisions, memory notes, blocks, rules, search --------------

    async def docs_context(slug: str) -> dict[str, Any]:
        """The live part of the page: documents, decisions and notes."""
        project, documents = await service.list_documents(slug)
        _, decisions = await service.list_decisions(
            project.slug, include_superseded=True, limit=DOCS_DECISIONS
        )
        notes = await service.list_notes(project.slug, include_superseded=True)
        return {
            "project": project,
            "documents": documents,
            "document_groups": mem.group_documents(documents),
            "decisions": [d for d in decisions if d.current],
            "superseded": [d for d in decisions if not d.current],
            "notes": [n for n in notes if n.current],
            "old_notes": [n for n in notes if not n.current],
        }

    async def docs_page(
        request: Request,
        slug: str,
        *,
        q: str = "",
        error: str | None = None,
        error_target: str = "",
        draft: dict[str, str] | None = None,
        message: str | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        context = await docs_context(slug)
        project = context["project"]
        q = q.strip()[:SEARCH_MAX]
        hits, search_error = [], None
        if q:
            try:
                hits = await service.search(q, project=project.slug, limit=30)
            except ValidationError as exc:
                search_error = str(exc)
        return await page(
            request,
            "docs.html",
            "docs",
            project.slug,
            status=status,
            **context,
            doc_kinds=mem.DOC_KINDS,
            blocks=await service.list_blocks(project.slug),
            global_blocks=await service.list_blocks(None),
            rules=fmt.rules_text(await service.rules(project.slug)),
            q=q,
            hits=hits,
            search_error=search_error,
            error=error,
            error_target=error_target,
            draft=draft or {},
            message=message,
            last_event_id=last_event_id,
        )

    @router.get("/p/{slug}/docs", response_class=HTMLResponse)
    async def docs(request: Request, slug: str, q: str = ""):
        flash = take_flash(request)
        response = await docs_page(request, slug, q=q, message=flash.get("message"))
        return shown_once(request, response)

    @router.get("/p/{slug}/docs-live", response_class=HTMLResponse)
    async def docs_live(request: Request, slug: str):
        return render(request, "_docs_live.html", **await docs_context(slug))

    @router.post("/p/{slug}/docs/blocks")
    async def save_block(
        request: Request, slug: str, csrf: Annotated[str, Form(alias=CSRF_FIELD)] = ""
    ):
        board_only(request, csrf)
        form = await request.form()
        label = str(form.get("label") or "").strip().lower()
        scope = str(form.get("scope") or "project")
        body = str(form.get("body") or "")
        limit_text = str(form.get("char_limit") or "").strip()
        version_text = str(form.get("base_version") or "0").strip()
        target = "block-new" if version_text == "0" else f"block-{scope}-{label}"
        try:
            saved = await service.set_block(
                label,
                body,
                int(version_text) if version_text.isdigit() else -1,
                project=slug if scope == "project" else None,
                description=str(form.get("description") or ""),
                char_limit=int(limit_text) if limit_text.isdigit() else None,
                owner_only=form.get("owner_only") == "1",
                by_owner=True,
            )
        except NotFoundError:
            raise
        except DomainError as exc:
            return await docs_page(
                request,
                slug,
                error=str(exc),
                error_target=target,
                draft={target: body},
                status=400,
            )
        anchor = f"#block-{saved.scope}-{saved.label}"
        return flash_redirect(
            request, f"/p/{slug}/docs{anchor}", message=f"Block {saved.label} saved."
        )

    @router.post("/p/{slug}/docs/new")
    async def new_document(
        request: Request, slug: str, csrf: Annotated[str, Form(alias=CSRF_FIELD)] = ""
    ):
        board_only(request, csrf)
        form = await request.form()
        fields = {name: str(form.get(name) or "") for name in DOC_NEW_FIELDS}
        try:
            saved = await service.update_document(
                slug,
                fields["doc"].strip().lower(),
                fields["body"],
                fields["reason"],
                0,
                title=fields["title"] or None,
                kind=fields["kind"].strip().lower() or None,
                source_url=fields["source_url"] or None,
                author="owner",
            )
        except DomainError as exc:
            await service.get_project(slug)  # an unknown project stays a 404
            return await docs_page(
                request,
                slug,
                error=str(exc),
                error_target="doc-new",
                draft={f"doc-new-{name}": value for name, value in fields.items()},
                status=400,
            )
        return flash_redirect(
            request, f"/p/{slug}/docs/{saved.slug}", message=f"Document {saved.slug} created."
        )

    @router.get("/p/{slug}/docs/{doc}", response_class=HTMLResponse)
    async def document_page(request: Request, slug: str, doc: str, version: int | None = None):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        found, text = await service.get_document(slug, doc, version=version)
        _, versions = await service.document_history(slug, doc)
        editable = found.shown_version == found.version and not found.source_url
        edit_url = f"/p/{found.project_slug}/docs/{found.slug}/edit" if editable else None
        flash = take_flash(request)
        response = await page(
            request,
            "document.html",
            "docs",
            found.project_slug,
            project=await service.get_project(slug),
            doc=found,
            body_html=render_document(text, edit_url),
            sections=mem.sections(text),
            versions=versions,
            edit_url=edit_url,
            message=flash.get("message"),
            last_event_id=last_event_id,
        )
        return shown_once(request, response)

    async def document_edit_page(
        request: Request,
        found: mem.Document,
        section: str | None,
        text: str,
        base_version: int,
        *,
        reason: str = "",
        error: str | None = None,
        note: str | None = None,
        current: str | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        """The edit form. `found`, `base_version` and `current` come from one read, so the text
        shown to merge and the version the next save starts from always belong together."""
        return await page(
            request,
            "document_edit.html",
            "docs",
            found.project_slug,
            status=status,
            project=await service.get_project(found.project_slug),
            doc=found,
            section=section,
            text=text,
            base_version=base_version,
            reason=reason,
            error=error,
            note=note,
            current=current,
        )

    @router.get("/p/{slug}/docs/{doc}/edit", response_class=HTMLResponse)
    async def document_edit(request: Request, slug: str, doc: str, section: str = ""):
        wanted = section.strip().lstrip("#").casefold() or None
        found, text = await service.get_document(slug, doc, section=wanted)
        if found.source_url:
            return flash_redirect(
                request,
                f"/p/{found.project_slug}/docs/{found.slug}",
                message="This document lives in the repository; change it there.",
            )
        return await document_edit_page(request, found, wanted, text, found.version)

    @router.post("/p/{slug}/docs/{doc}/edit", response_class=HTMLResponse)
    async def save_document(
        request: Request, slug: str, doc: str, csrf: Annotated[str, Form(alias=CSRF_FIELD)] = ""
    ):
        board_only(request, csrf)
        form = await request.form()
        body = str(form.get("body") or "")
        reason = str(form.get("reason") or "")
        section = str(form.get("section") or "").strip().lstrip("#").casefold() or None
        base_text = str(form.get("base_version") or "").strip()
        base = int(base_text) if base_text.isdigit() else -1
        try:
            saved = await service.update_document(
                slug, doc, body, reason, base, section=section, author="owner"
            )
        except NotFoundError:
            raise
        except ConflictError:
            found, current = await service.get_document(slug, doc)
            note = None
            if section and any(s.anchor == section for s in mem.sections(current)):
                current = mem.section_text(current, section)
            elif section:
                note = (
                    f"Section #{section} is no longer in the document: this is now an edit of"
                    " the whole document. Your text is the section you typed; merge it into the"
                    " current text."
                )
                section = None
            part = "section" if section else "text"
            return await document_edit_page(
                request,
                found,
                section,
                body,
                found.version,
                reason=reason,
                error=f"{found.slug} is at version {found.version}, not {base}: someone changed"
                f" it while you were editing. Merge the current {part} below into yours and"
                " save again.",
                note=note,
                current=current,
                status=409,
            )
        except DomainError as exc:
            found, _ = await service.get_document(slug, doc)
            return await document_edit_page(
                request, found, section, body, base, reason=reason, error=str(exc), status=400
            )
        anchor = f"#{section}" if section else ""
        return flash_redirect(
            request,
            f"/p/{slug}/docs/{saved.slug}{anchor}",
            message=f"Saved as version {saved.version}.",
        )

    @router.get("/p/{slug}/docs/{doc}/diff", response_class=HTMLResponse)
    async def document_diff(
        request: Request,
        slug: str,
        doc: str,
        from_version: Annotated[int, Query(alias="from")],
        to_version: Annotated[int, Query(alias="to")],
    ):
        last_event_id = await service.last_event_id()
        found, lines = await service.document_diff(slug, doc, from_version, to_version)
        _, versions = await service.document_history(slug, doc)
        return await page(
            request,
            "document_diff.html",
            "docs",
            found.project_slug,
            project=await service.get_project(slug),
            doc=found,
            lines=lines,
            versions=versions,
            from_version=from_version,
            to_version=to_version,
            last_event_id=last_event_id,
        )

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

    @router.get("/settings/notifications", response_class=HTMLResponse)
    async def notifications(request: Request):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        return await page(request, "notifications.html", "settings", last_event_id=last_event_id)

    @router.get("/settings/integrations", response_class=HTMLResponse)
    async def integrations(request: Request, mode: str = machine_setup.DEFAULT_MODE):
        flash = take_flash(request)
        checks = [
            machine_setup.Check(bool(c[0]), str(c[1]), str(c[2]))
            for c in flash.get("checks") or []
            if isinstance(c, list) and len(c) == 3
        ]
        response = await integrations_page(
            request,
            mode,
            message=flash.get("message"),
            error=flash.get("error"),
            checks=checks or None,
        )
        return shown_once(request, response)

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

        async def answer(
            message: str | None = None,
            error: str | None = None,
            checks: list[machine_setup.Check] | None = None,
        ) -> Response:
            # htmx swaps the fragment in place; a plain form post goes POST → 303 → GET.
            if "hx-request" in request.headers:
                return await integrations_page(
                    request, mode, message=message, error=error, checks=checks
                )
            rows = [[c.ok, c.name, c.detail[:200]] for c in checks or []]
            url = f"/settings/integrations?{urlencode({'mode': mode})}"
            return flash_redirect(request, url, message=message, error=error, checks=rows)

        if action == "check":
            checks = await asyncio.to_thread(
                machine_setup.run_check,
                base_url=settings.base_url,
                token=token,
                paths=machine_setup.claude_paths(settings.claude_home),
            )
            return await answer(checks=checks)
        if action not in ("apply", "remove"):
            raise HTTPException(status_code=404)
        detection = await asyncio.to_thread(machine_setup.detect)
        plan = integrations_plan(mode, detection, remove=action == "remove")
        if plan.error or plan.hooks is None:
            return await answer(error=plan.error)
        try:
            # `expect` is the hash of the plan shown on the page: write only what was approved.
            backup = machine_setup.write_change(plan.hooks, expect=expect)
        except (machine_setup.SetupError, OSError) as exc:
            return await answer(error=str(exc))
        if not plan.hooks.changed:
            message = "Nothing to change."
        else:
            done = "Removed relaybay's hooks from" if action == "remove" else "Wrote"
            message = f"{done} {plan.hooks.path}" + (f" (backup: {backup.name})" if backup else "")
        return await answer(message=message)

    # --- Settings → Policy -------------------------------------------------------------

    async def policy_scopes() -> list[str]:
        scopes = ["global"]
        for item in await service.list_projects():
            scopes.append(item.project.slug)
            _, items = await service.environments(item.project.slug)
            scopes.extend(f"{item.project.slug}/{e.name}" for e in items)
        return scopes

    async def policy_page(
        request: Request,
        scope: str = "global",
        *,
        text: str | None = None,
        error: str | None = None,
        preview: Any = None,
        widened: list[str] | None = None,
        confirm: dict[str, Any] | None = None,
        message: str | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        scopes = await policy_scopes()
        if scope not in scopes:
            scope = "global"
        project, _, environment = scope.partition("/")
        effective = (
            service.global_policy()
            if scope == "global"
            else await service.effective_policy(project, environment or None)
        )
        return await page(
            request,
            "policy.html",
            "settings",
            status=status,
            scope=scope,
            scopes=scopes,
            effective=effective,
            global_policy=service.global_policy(),
            policy_file=service.policy_file,
            layers=await service.policy_layers(),
            layer_text=await service.policy_layer_text(scope) if text is None else text,
            presets=PRESETS,
            proposals=await service.policy_proposals(),
            error=error,
            preview=preview,
            widened=widened,
            confirm=confirm,
            message=message,
            last_event_id=last_event_id,
        )

    async def policy_write(
        request: Request, csrf: str, scope: str, write: Callable[[Any], Awaitable[str]]
    ) -> Response:
        """A Settings → Policy write: back to the page, or the page with the reason, or a
        confirmation when the change widens agents' access on live."""
        board_only(request, csrf)
        form = await request.form()
        try:
            message = await write(form)
        except ConfirmationNeeded as exc:
            confirm = {
                "action": str(request.url.path),
                "message": str(exc),
                "fields": confirm_fields(form),
            }
            return await policy_page(request, scope, text=form.get("body"), confirm=confirm)
        except NotFoundError:
            raise
        except DomainError as exc:
            return await policy_page(
                request, scope, text=form.get("body"), error=str(exc), status=400
            )
        url = "/settings/policy" + (f"?{urlencode({'scope': scope})}" if scope != "global" else "")
        return flash_redirect(request, url, message=message)

    @router.get("/settings/policy", response_class=HTMLResponse)
    async def policy_settings(request: Request, scope: str = "global"):
        flash = take_flash(request)
        response = await policy_page(request, scope, message=flash.get("message"))
        return shown_once(request, response)

    @router.get("/settings/policy/proposals", response_class=HTMLResponse)
    async def policy_proposals_fragment(request: Request):
        return render(request, "_policy_proposals.html", proposals=await service.policy_proposals())

    @router.post("/settings/policy/layer", response_class=HTMLResponse)
    async def policy_layer(
        request: Request,
        scope: Annotated[str, Form()] = "global",
        body: Annotated[str, Form()] = "",
        action: Annotated[str, Form()] = "save",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if action == "preview":
            board_only(request, csrf)
            try:
                after, widened = await service.preview_policy(scope, body)
            except NotFoundError:
                raise
            except DomainError as exc:
                return await policy_page(request, scope, text=body, error=str(exc), status=400)
            return await policy_page(request, scope, text=body, preview=after, widened=widened)

        async def write(form: Any) -> str:
            saved = await service.save_policy_layer(
                scope, body, confirm_live=confirmed(form), actor="owner"
            )
            return f"Saved the {saved} policy."

        return await policy_write(request, csrf, scope, write)

    @router.post("/settings/policy/preset", response_class=HTMLResponse)
    async def policy_preset(
        request: Request,
        preset: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        async def write(form: Any) -> str:
            await service.set_preset(preset, confirm_live=confirmed(form), actor="owner")
            return f"Preset set to {preset}."

        return await policy_write(request, csrf, "global", write)

    @router.post("/settings/policy/proposals/{proposal_id}/{decision}", response_class=HTMLResponse)
    async def decide_policy_proposal(
        request: Request,
        proposal_id: int,
        decision: str,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if decision not in ("accept", "reject"):
            raise HTTPException(status_code=404)

        async def write(form: Any) -> str:
            decided = await service.decide_policy_proposal(
                proposal_id, decision == "accept", confirm_live=confirmed(form), actor="owner"
            )
            return f"Policy proposal #{decided.id} {decided.status}."

        return await policy_write(request, csrf, "global", write)

    # --- Settings → Rules --------------------------------------------------------------

    async def rules_page(
        request: Request,
        scope: str = "global",
        *,
        error: str | None = None,
        confirm: dict[str, Any] | None = None,
        message: str | None = None,
        status: int = 200,
    ) -> HTMLResponse:
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        scopes = await service.rule_scopes()
        if scope not in scopes:
            scope = "global"
        rules = await service.rules_of_scope(scope)
        return await page(
            request,
            "rules.html",
            "settings",
            status=status,
            scope=scope,
            scopes=scopes,
            rules=rules,
            rules_md=rules_markdown(rules),
            versions=await service.rule_versions(scope),
            proposals=await service.rule_proposals(),
            diffs=await service.rule_proposal_diffs(),
            version_error=await service.rule_version_error(scope),
            decided=await service.decided_rule_proposals(scope),
            coordinators=service.coordinators,
            config_differs=scope == "global" and rules.body != service.config_rules.body,
            error=error,
            confirm=confirm,
            message=message,
            last_event_id=last_event_id,
        )

    async def rules_write(
        request: Request, csrf: str, scope: str, write: Callable[[Any], Awaitable[str]]
    ) -> Response:
        """A Settings → Rules write: back to the page, or the page with the reason, or the
        confirmation a rollback asks for."""
        board_only(request, csrf)
        form = await request.form()
        try:
            message = await write(form)
        except ConfirmationNeeded as exc:
            confirm = {
                "action": str(request.url.path),
                "message": str(exc),
                "fields": confirm_fields(form),
            }
            return await rules_page(request, scope, confirm=confirm)
        except NotFoundError:
            raise
        except DomainError as exc:
            return await rules_page(request, scope, error=str(exc), status=400)
        url = "/settings/rules" + (f"?{urlencode({'scope': scope})}" if scope != "global" else "")
        return flash_redirect(request, url, message=message)

    @router.get("/settings/rules", response_class=HTMLResponse)
    async def rules_settings(request: Request, scope: str = "global"):
        flash = take_flash(request)
        response = await rules_page(request, scope, message=flash.get("message"))
        return shown_once(request, response)

    @router.get("/settings/rules/proposals", response_class=HTMLResponse)
    async def rule_proposals_fragment(request: Request):
        return render(
            request,
            "_rule_proposals.html",
            proposals=await service.rule_proposals(),
            diffs=await service.rule_proposal_diffs(),
        )

    @router.post("/settings/rules/notices/{version_id}/read")
    async def close_rule_notice(
        request: Request,
        version_id: int,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        board_only(request, csrf)
        await service.close_rule_notice(version_id, actor="owner")
        return RedirectResponse("/inbox", status_code=303)

    @router.post("/settings/rules/rollback", response_class=HTMLResponse)
    async def rules_rollback(
        request: Request,
        scope: Annotated[str, Form()] = "global",
        number: Annotated[int, Form()] = 0,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        async def write(form: Any) -> str:
            canonical, made = await service.rollback_rules(
                scope, number, confirmed=confirmed(form), actor="owner"
            )
            return f"The {canonical} rules are now v{made}, equal to v{number}."

        return await rules_write(request, csrf, scope, write)

    @router.post("/settings/rules/proposals/{proposal_id}/{decision}", response_class=HTMLResponse)
    async def decide_rule_proposal(
        request: Request,
        proposal_id: int,
        decision: str,
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        if decision not in ("accept", "reject"):
            raise HTTPException(status_code=404)
        board_only(request, csrf)
        proposal = await service.rule_proposal(proposal_id)

        async def write(form: Any) -> str:
            decided = await service.decide_rule_proposal(
                proposal_id, decision == "accept", actor="owner"
            )
            made = f" as v{decided.version}" if decided.version else ""
            return f"Rules proposal #{decided.id} {decided.status}{made}."

        return await rules_write(request, csrf, proposal.scope, write)

    # --- Intake: Inbox actions, Settings → Intake, the task's answer mark --------------------

    def back_to(value: str, default: str) -> str:
        """Only a local path (`safe_next`): a form field must not send the browser elsewhere."""
        safe = safe_next(value)
        return safe if value and safe == value else default

    async def intake_write(
        request: Request, csrf: str, write: Callable[[], Awaitable[str]], back: str = "/inbox"
    ) -> Response:
        board_only(request, csrf)
        try:
            message = await write()
        except NotFoundError:
            raise
        except DomainError as exc:
            return flash_redirect(request, back, error=str(exc))
        return flash_redirect(request, back, message=message)

    @router.post("/intake/{message_id}/tie")
    async def intake_tie(
        request: Request,
        message_id: int,
        task: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            tied = await service.intake_tie(message_id, task, actor="owner")
            done = "the agent was woken" if tied.woke else "notification only"
            return f"Message #{message_id} tied to {tied.task.key}: {done}."

        return await intake_write(request, csrf, write)

    @router.post("/intake/{message_id}/dismiss")
    async def intake_dismiss(
        request: Request, message_id: int, csrf: Annotated[str, Form(alias=CSRF_FIELD)] = ""
    ) -> Response:
        async def write() -> str:
            await service.intake_dismiss(message_id, actor="owner")
            return f"Message #{message_id} dismissed."

        return await intake_write(request, csrf, write)

    @router.post("/intake/{message_id}/propose")
    async def intake_propose(
        request: Request,
        message_id: int,
        project: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            item = await service.intake_propose(
                message_id, project, title, description, actor="owner"
            )
            return (
                f"Backlog item #{item.id} added to {item.project_slug}: plan it from the backlog."
            )

        return await intake_write(request, csrf, write)

    @router.post("/intake/{message_id}/forward", response_class=HTMLResponse)
    async def intake_forward(
        request: Request, message_id: int, csrf: Annotated[str, Form(alias=CSRF_FIELD)] = ""
    ):
        board_only(request, csrf)
        message = await service.intake_message(message_id)
        if message.task_key is None:
            raise ValidationError(f"Message #{message_id} is not tied to a task.")
        error = None
        try:
            await service.intake_forward(message_id, actor="owner")
        except NotFoundError:
            raise
        except DomainError as exc:
            error = str(exc)
        if "hx-request" not in request.headers:
            return RedirectResponse(f"/t/{message.task_key}", status_code=303)
        detail = await service.task_detail(message.task_key, reports=TASK_PAGE_REPORTS)
        return render(request, "_task_view.html", detail=detail, error=error)

    @router.post("/p/{slug}/t/{key}/answer/clear", response_class=HTMLResponse)
    async def clear_answer(
        request: Request,
        slug: str,
        key: str,
        target: Annotated[str, Form()] = "view",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ):
        board_only(request, csrf)
        task = await service.clear_answer((await find_task(slug, key)).key, actor="owner")
        if "hx-request" not in request.headers:
            return RedirectResponse(f"/p/{slug}?task={task.key}", status_code=303)
        if target == "card":
            return render(request, "_card.html", t=task, slug=slug)
        detail = await service.task_detail(task.key, reports=TASK_PAGE_REPORTS)
        return render(request, "_task_view.html", detail=detail)

    @router.get("/settings/intake", response_class=HTMLResponse)
    async def intake_settings(request: Request):
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        flash = take_flash(request)
        response = await page(
            request,
            "intake.html",
            "settings",
            sources=await service.intake_sources(),
            projects=await project_rows(),
            message=flash.get("message"),
            error=flash.get("error"),
            last_event_id=last_event_id,
        )
        return shown_once(request, response)

    @router.post("/settings/intake/sources/{source}/trust")
    async def trust_source(
        request: Request,
        source: str,
        may_wake: Annotated[str, Form()] = "0",
        back: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            saved = await service.set_source_trust(source, may_wake == "1", actor="owner")
            state = "may wake agents" if saved.may_wake else "notification only"
            return f"{saved.slug}: {state}."

        return await intake_write(request, csrf, write, back_to(back, "/settings/intake"))

    @router.post("/settings/intake/projects/{slug}")
    async def project_autonomy(
        request: Request,
        slug: str,
        autonomy: Annotated[str, Form()] = "0",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            saved = await service.set_project_autonomy(slug, autonomy == "1", actor="owner")
            state = "on" if saved.autonomy else "off"
            return f"Autonomy for {saved.slug}: {state}."

        return await intake_write(request, csrf, write, "/settings/intake")

    # --- Backlog -------------------------------------------------------------------------

    async def backlog_context(
        project: str | None, status: str, area: str, assignee: str, show_history: bool
    ) -> dict[str, Any]:
        status = status if status in BACKLOG_STATUSES else ""
        area = area.strip()[:SEARCH_MAX]
        assignee = assignee.strip()[:ASSIGNEE_MAX]
        items = await service.list_backlog(
            project=project,
            status=status or None,
            area=area or None,
            assignee=assignee or None,
            limit=BACKLOG_ROWS,
        )
        open_items = [b for b in items if b.status in ("new", "later")]
        history = [b for b in items if b.status in ("planned", "done", "declined")]
        query = {"status": status, "area": area, "assignee": assignee}
        live_query = {**query, "project": project or "", "history": "1" if show_history else ""}
        return {
            "fragment_qs": urlencode({k: v for k, v in live_query.items() if v}),
            "backlog_project": await service.get_project(project) if project else None,
            "open_items": open_items,
            "history": history,
            "show_history": show_history or status in ("planned", "done", "declined"),
            "status_filter": status,
            "area_filter": area,
            "assignee_filter": assignee,
            "areas": await service.backlog_areas(project),
            "assignees": await service.backlog_assignees(project),
            "statuses": BACKLOG_STATUSES,
            "filter_qs": urlencode({k: v for k, v in query.items() if v}),
        }

    async def backlog_page(
        request: Request, project: str | None, status: str, area: str, assignee: str, history: bool
    ) -> Response:
        last_event_id = await service.last_event_id()  # before the data, see projects_page
        flash = take_flash(request)
        context = await backlog_context(project, status, area, assignee, history)
        response = await page(
            request,
            "backlog.html",
            "backlog",
            project,
            **context,
            projects=await project_rows(),
            message=flash.get("message"),
            error=flash.get("error"),
            last_event_id=last_event_id,
        )
        return shown_once(request, response)

    @router.get("/backlog", response_class=HTMLResponse)
    async def all_backlog(
        request: Request,
        status: str = "",
        area: str = "",
        assignee: str = "",
        history: bool = False,
    ):
        return await backlog_page(request, None, status, area, assignee, history)

    @router.get("/p/{slug}/backlog", response_class=HTMLResponse)
    async def project_backlog(
        request: Request,
        slug: str,
        status: str = "",
        area: str = "",
        assignee: str = "",
        history: bool = False,
    ):
        project = await service.get_project(slug)
        return await backlog_page(request, project.slug, status, area, assignee, history)

    @router.get("/fragments/backlog", response_class=HTMLResponse)
    async def backlog_fragment(
        request: Request,
        project: str = "",
        status: str = "",
        area: str = "",
        assignee: str = "",
        history: bool = False,
    ):
        context = await backlog_context(project or None, status, area, assignee, history)
        return render(request, "_backlog.html", **context)

    def backlog_url(project: str, back: str) -> str:
        default = f"/p/{project}/backlog" if project else "/backlog"
        return back_to(back, default)

    @router.post("/backlog/new")
    async def backlog_new(
        request: Request,
        project: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        body: Annotated[str, Form()] = "",
        area: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            item = await service.add_backlog_item(
                project, title, body=body, area=area, by_owner=True
            )
            return f"Backlog item #{item.id} added."

        return await intake_write(request, csrf, write, backlog_url(project, back))

    # Before the decision route, which would take `queue` as a decision.
    @router.post("/backlog/{item_id}/queue")
    async def backlog_queue(
        request: Request,
        item_id: int,
        priority: Annotated[str, Form()] = "",
        assignee: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        async def write() -> str:
            item = await service.set_backlog_queue(item_id, priority, assignee, actor="owner")
            return f"Backlog item #{item.id} saved."

        return await intake_write(request, csrf, write, back_to(back, "/backlog"))

    @router.post("/backlog/{item_id}/{decision}")
    async def backlog_decide(
        request: Request,
        item_id: int,
        decision: str,
        reason: Annotated[str, Form()] = "",
        link: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "",
        csrf: Annotated[str, Form(alias=CSRF_FIELD)] = "",
    ) -> Response:
        if decision not in ("plan", "later", "done", "decline", "new"):
            raise HTTPException(status_code=404)

        async def write() -> str:
            item = await service.decide_backlog(
                item_id, decision, reason, link=link or None, actor="owner"
            )
            if item.task_key:
                return f"Backlog item #{item.id} planned as {item.task_key}."
            return f"Backlog item #{item.id}: {item.status}."

        return await intake_write(request, csrf, write, back_to(back, "/backlog"))

    @router.get("/favicon.ico")
    async def favicon() -> Response:
        # Browsers take a PNG under this name; drawn by packaging/icons/make_icons.py.
        return FileResponse(STATIC_DIR / "favicon.png", media_type="image/png")

    return router
