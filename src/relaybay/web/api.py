"""`/api`: JSON for the desktop program (Bearer token, checked by the middleware)."""

from typing import Any

from fastapi import APIRouter

from relaybay.service import Service


def build_api_router(service: Service) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/notifications")
    async def notifications(after: int | None = None, limit: int = 100) -> dict[str, Any]:
        """Notices for events after the id `after`, oldest first. `last_id` is where to go on
        from; without `after` it is the newest event and there are no items."""
        last_id, items = await service.notifications(after, limit)
        return {
            "last_id": last_id,
            "items": [
                {
                    "id": event_id,
                    "kind": notice.kind,
                    "title": notice.title,
                    "text": notice.text,
                    "url": notice.url,
                    "key": notice.key,
                }
                for event_id, notice in items
            ],
        }

    return router
