"""Webhook receivers from external services."""

import asyncio

from fastapi import APIRouter, status

router = APIRouter()


@router.post("/webhooks/loganne", status_code=status.HTTP_204_NO_CONTENT)
async def loganne_webhook(body: dict):
    """Receive event notifications from lucos_loganne.

    Loganne POSTs the full event object as JSON with no authentication header.
    Currently handles:
      - ``contactUpdated``: syncs the display_name of any person linked to the
        updated contact, using the name included in the event payload.

    Other event types are silently ignored (return 204).
    """
    event_type = body.get("type")
    if event_type != "contactUpdated":
        return

    agent = body.get("agent") or {}
    contact_id = str(agent.get("id", "")).strip()
    name = str(agent.get("name", "")).strip()

    if not contact_id or not name:
        return

    from lucos_photos_common.jobs import sync_single_contact_name
    await asyncio.to_thread(sync_single_contact_name, contact_id, name)
