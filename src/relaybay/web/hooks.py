"""`POST /hooks/claude-code`: Claude Code hook events (Bearer token, checked by the middleware)."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from relaybay.domain.hooks import HOOK_PATH, parse_hook, session_start_context
from relaybay.service import Service

# Hook input can carry a tool's whole input (a file being written); relaybay needs a few fields.
MAX_BODY = 256 * 1024


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


def build_hooks_router(service: Service) -> APIRouter:
    router = APIRouter()

    @router.post(HOOK_PATH)
    async def claude_code_hook(request: Request) -> Response:
        body = await _body(request)
        if body is None:
            # Too large to read (e.g. a big file write): drop it quietly, never in the way.
            return Response(status_code=204)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Response("Body must be the hook's JSON input.", status_code=400)
        await service.record_hook(payload)
        hook = parse_hook(payload)
        if hook is not None and hook.event == "SessionStart":
            # Added to the agent's context: its exact id to pass as `hook_session` on writes.
            return JSONResponse(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": session_start_context(hook.cc_session_id),
                    }
                }
            )
        return Response(status_code=204)

    return router
