"""`POST /hooks/claude-code`: Claude Code hook events (Bearer token, checked by the middleware)."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask

from relaybay.domain.hooks import (
    ATTENDED_FIELD,
    ATTENDED_HEADER,
    HOOK_PATH,
    HOST_SESSION_FIELD,
    HOST_SESSION_HEADER,
    SPOOLED_FIELD,
    WAKE_HOLD_SECONDS,
    WAKE_PATH,
)
from relaybay.service import Service

# Hook input can carry a tool's whole input (a file being written); relaybay needs a few fields.
MAX_BODY = 256 * 1024
_INVALID = object()
logger = logging.getLogger("relaybay")


async def _body(request: Request) -> bytes | None:
    """The request body, or None when it is larger than MAX_BODY."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _payload(request: Request) -> Any:
    """The hook input; None when too large to read, `_INVALID` when it is not JSON."""
    body = await _body(request)
    if body is None:
        return None
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _INVALID


def build_hooks_router(
    service: Service, *, wake_hold: float = WAKE_HOLD_SECONDS, wake_poll: float = 1.0
) -> APIRouter:
    router = APIRouter()

    async def record_after_stop(payload: Any) -> None:
        try:
            await service.record_hook(payload)
        except Exception:
            logger.exception("Recording a hook event after a brake stop failed")

    @router.post(HOOK_PATH)
    async def claude_code_hook(request: Request) -> Response:
        payload = await _payload(request)
        if payload is None:
            # Too large to read (e.g. a big file write): drop it quietly, never in the way.
            return Response(status_code=204)
        if payload is _INVALID:
            return Response("Body must be the hook's JSON input.", status_code=400)
        host = request.headers.get(HOST_SESSION_HEADER)
        if host and isinstance(payload, dict):
            # http hooks: the desktop session id comes as a header (command hooks: in the body).
            payload.setdefault(HOST_SESSION_FIELD, host)
            payload.setdefault(ATTENDED_FIELD, request.headers.get(ATTENDED_HEADER, ""))
        if not (isinstance(payload, dict) and SPOOLED_FIELD in payload):
            stopped = await service.brake_stop(payload)
            if stopped is not None:
                # The stop goes out first: a slow or failing write of the event must not lose it.
                return JSONResponse(stopped, background=BackgroundTask(record_after_stop, payload))
        await service.record_hook(payload)
        if isinstance(payload, dict) and SPOOLED_FIELD in payload:
            # A replay from the forwarder's spool: nobody reads its answer, so owner comments
            # are not marked delivered to it.
            return Response(status_code=204)
        # SessionStart: the agent's exact id to pass as `hook_session` on writes. Prompt, tool
        # and Stop events: owner comments for the exactly linked session, if there are any.
        answer = await service.hook_answer(payload)
        return JSONResponse(answer) if answer else Response(status_code=204)

    @router.post(WAKE_PATH)
    async def claude_code_wait(request: Request) -> Response:
        """Long poll of the opt-in wake-up hook: `{"wake": text}` when an owner comment arrived
        for the idle session, 204 when there is nothing to wake it for."""
        payload = await _payload(request)
        if payload is None or payload is _INVALID:
            return Response(status_code=204)
        text = await service.wait_for_comments(
            payload, hold=wake_hold, poll=wake_poll, gone=request.is_disconnected
        )
        return JSONResponse({"wake": text}) if text else Response(status_code=204)

    return router
