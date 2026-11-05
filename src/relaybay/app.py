"""ASGI application: board, SSE, MCP endpoint and security middleware in one process."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from relaybay import __version__
from relaybay.config import Settings, load_settings
from relaybay.domain.errors import DomainError, NotFoundError
from relaybay.domain.review import MB
from relaybay.mcp_server.server import build_mcp, mcp_routes
from relaybay.security import SecurityMiddleware, load_or_create_token
from relaybay.service import Service
from relaybay.web.hooks import build_hooks_router
from relaybay.web.routes import STATIC_DIR, build_router


def create_app(settings: Settings) -> FastAPI:
    token = load_or_create_token(settings.token_path)
    service = Service(
        settings.db_path,
        desktop_link_template=settings.desktop_link_template,
        rules=settings.rules,
        rules_table=settings.rules_table,
        rules_mode=settings.rules_mode,
        permission_defaults=settings.policy_config.get("permission_defaults"),
        guard_mode=settings.policy_config.get("guard_mode"),
        policy_file=settings.policy_path,
        attachments_dir=settings.attachments_dir,
        attachment_roots=settings.attachment_roots,
        attachment_max_bytes=settings.attachments_max_mb * MB,
        attachment_task_max_bytes=settings.attachments_task_max_mb * MB,
        plan_reminder_every=settings.plan_reminder_every,
        owner_names=settings.owner_names,
        require_tracker_log=settings.require_tracker_log,
        done_window_days=settings.done_window_days,
        pulse_stale_days=settings.pulse_stale_days,
        task_stale_days=settings.task_stale_days,
        digest_budget_chars=settings.digest_budget_chars,
        session_context=settings.session_context,
        session_context_project_chars=settings.session_context_project_chars,
        usage_enabled=settings.usage_enabled,
        coordinators=settings.coordinators,
    )
    mcp = build_mcp(service, require_tracker_log=settings.require_tracker_log)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        reader = (
            asyncio.create_task(service.usage_loop(settings.usage_interval_seconds))
            if settings.usage_enabled
            else None
        )
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if reader is not None:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader
            await service.close()

    app = FastAPI(
        title="relaybay",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.mcp = mcp

    allowed = settings.host_allow_list()
    app.router.routes.extend(mcp_routes(mcp, allowed))
    app.include_router(
        build_hooks_router(
            service, wake_hold=settings.wake_hold_seconds, wake_poll=settings.wake_poll_seconds
        )
    )
    app.include_router(build_router(service, settings, token))
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(SecurityMiddleware, token=token, allowed_hosts=allowed)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> PlainTextResponse:
        status = 404 if isinstance(exc, NotFoundError) else 400
        return PlainTextResponse(str(exc), status_code=status)

    return app


def server_options(settings: Settings) -> dict[str, Any]:
    """uvicorn options shared by `relaybay serve` and the desktop program's embedded server."""
    return {
        "host": settings.host,
        "port": settings.port,
        "proxy_headers": False,
        "server_header": False,
        # SSE and MCP streams never finish on their own; do not wait for them on Ctrl+C.
        "timeout_graceful_shutdown": 2,
    }


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --reload`, which re-imports the app in a child process."""
    return create_app(load_settings(serving=True))
