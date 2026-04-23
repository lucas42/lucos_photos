"""Webhook receivers from external services."""

import asyncio
import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, status

from app.auth import verify_key

router = APIRouter()


async def _fetch_contact(url: str) -> tuple[str, str] | None:
    """Fetch contact JSON from url and return (contact_id, name), or None on failure.

    contact_id is extracted from the last path segment of the URL.
    name is read from the JSON response body.

    The URL must start with LUCOS_CONTACTS_URL to prevent SSRF and credential exfiltration.
    """
    contacts_base = os.environ.get("LUCOS_CONTACTS_URL", "")
    if not contacts_base or not url.startswith(contacts_base):
        return None
    contacts_key = os.environ.get("KEY_LUCOS_CONTACTS", "")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {contacts_key}"},
                timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
        contact_id = url.rstrip("/").rsplit("/", 1)[-1]
        name = str(data.get("name", "")).strip()
        if not contact_id or not name:
            return None
        return contact_id, name
    except Exception:
        return None


@router.post("/webhooks/loganne", status_code=status.HTTP_204_NO_CONTENT)
async def loganne_webhook(body: dict, _auth: Annotated[None, Depends(verify_key)]):
    """Receive event notifications from lucos_loganne.

    Loganne POSTs the full event object as JSON with an Authorization: Bearer header.
    Currently handles:
      - ``contactUpdated``: re-fetches the contact from the source URL and syncs
        display_name for any person linked to that contact.

    Other event types are silently ignored (return 204).
    """
    event_type = body.get("type")
    if event_type != "contactUpdated":
        return

    url = body.get("url", "").strip()
    if not url:
        return

    result = await _fetch_contact(url)
    if not result:
        return

    contact_id, name = result
    from lucos_photos_common.jobs import sync_single_contact_name
    await asyncio.to_thread(sync_single_contact_name, contact_id, name)
