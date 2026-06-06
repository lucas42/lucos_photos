"""External service integrations: Loganne and lucos_contacts."""

import asyncio
import os
from typing import Optional

import httpx


async def emit_loganne_event(event_type: str, human_readable: str, url: str | None = None, level: str = "routine"):
    from loganne import updateLoganne
    await asyncio.to_thread(updateLoganne, type=event_type, humanReadable=human_readable, level=level, url=url)


async def fetch_contact_name(contact_id: str) -> Optional[str]:
    """Fetch a contact's name from lucos_contacts. Returns None on any failure."""
    contacts_url = os.environ.get("LUCOS_CONTACTS_ORIGIN", "")
    contacts_key = os.environ.get("KEY_LUCOS_CONTACTS", "")
    if not contacts_url or not contacts_key:
        print(f"Warning: LUCOS_CONTACTS_ORIGIN or KEY_LUCOS_CONTACTS not set, cannot fetch contact name for {contact_id}")
        return None
    try:
        async with httpx.AsyncClient(headers={"User-Agent": os.environ.get("SYSTEM", "")}) as client:
            response = await client.get(
                f"{contacts_url}/people/{contact_id}",
                headers={"Accept": "application/json", "Authorization": f"bearer {contacts_key}"},
                timeout=5.0,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("name") or None
    except Exception as e:
        print(f"Warning: failed to fetch contact name for {contact_id}: {e}")
        return None
