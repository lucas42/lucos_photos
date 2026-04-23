"""Webhook receivers from external services."""

import asyncio
import os
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.auth import verify_key

router = APIRouter()


@router.post("/webhooks/loganne", status_code=status.HTTP_204_NO_CONTENT)
async def loganne_webhook(body: dict, _auth: Annotated[None, Depends(verify_key)]):
    """Receive event notifications from lucos_loganne.

    Loganne POSTs the full event object as JSON with an Authorization: Bearer header.
    Currently handles:
      - ``contactUpdated``: re-fetches the contact from the contacts API and syncs
        display_name for any person linked to that contact.

    Other event types are silently ignored (return 204).
    """
    event_type = body.get("type")
    if event_type != "contactUpdated":
        return

    url = body.get("url", "").strip()
    if not url:
        return

    # Validate the URL is on the contacts domain before extracting any data from it.
    contacts_base = os.environ.get("LUCOS_CONTACTS_URL", "")
    if not contacts_base or not url.startswith(contacts_base):
        return

    # Extract the contact_id from the URL path. This value is used only as a DB
    # lookup key — the outbound HTTP call is made in refresh_contact_display_name
    # using person.contact_id from the DB (untainted) + LUCOS_CONTACTS_URL (env var),
    # so no user-supplied data ever reaches the httpx request URL.
    contact_id = url.rstrip("/").rsplit("/", 1)[-1]
    if not contact_id:
        return

    from lucos_photos_common.jobs import refresh_contact_display_name
    await asyncio.to_thread(refresh_contact_display_name, contact_id)
