"""Routes for recording and listing telemetry events from mobile clients."""

import re
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import verify_key
from app.database import get_db
from lucos_photos_common.models import TelemetryEvent

router = APIRouter()


@router.post("/api/telemetry", status_code=status.HTTP_201_CREATED)
def create_telemetry_event(
    body: dict,
    _: Annotated[None, Depends(verify_key)],
    db: Session = Depends(get_db),
):
    """Record a telemetry event from a client (e.g. the Android app).

    The ``timestamp`` field is the client-supplied event time (ISO-8601, UTC).
    The server also records ``received_at`` independently so that telemetry
    remains useful even when the client clock is wrong.
    """
    event_type = body.get("event_type")
    if not event_type:
        raise HTTPException(status_code=422, detail="event_type is required")

    # Parse optional ISO-8601 timestamp supplied by the client
    client_timestamp = None
    raw_ts = body.get("timestamp")
    if raw_ts is not None:
        try:
            # Replace trailing 'Z' with '+00:00' so fromisoformat works on all Python versions
            normalized = re.sub(r'Z$', '+00:00', str(raw_ts))
            client_timestamp = datetime.fromisoformat(normalized)
            if client_timestamp.tzinfo is None:
                client_timestamp = client_timestamp.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="timestamp must be a valid ISO-8601 datetime")

    event = TelemetryEvent(
        event_type=event_type,
        app_version=body.get("app_version"),
        timestamp=client_timestamp,
        data=body.get("data"),
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "app_version": event.app_version,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "received_at": event.received_at.isoformat(),
        "data": event.data,
    }


@router.get("/api/telemetry")
def list_telemetry_events(
    _: Annotated[None, Depends(verify_key)],
    event_type: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """List recorded telemetry events, optionally filtered by type and date.

    ``since`` is an ISO-8601 date or datetime; filtering is applied against
    ``received_at`` (the server-side receipt timestamp).
    """
    query = db.query(TelemetryEvent)

    if event_type:
        query = query.filter(TelemetryEvent.event_type == event_type)

    if since:
        try:
            normalized = re.sub(r'Z$', '+00:00', str(since))
            # Accept date-only strings by appending midnight UTC
            if 'T' not in normalized and '+' not in normalized:
                normalized = f"{normalized}T00:00:00+00:00"
            since_dt = datetime.fromisoformat(normalized)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="since must be a valid ISO-8601 date or datetime")
        query = query.filter(TelemetryEvent.received_at >= since_dt)

    events = query.order_by(TelemetryEvent.received_at.desc()).limit(limit).all()

    return {
        "events": [
            {
                "id": str(e.id),
                "event_type": e.event_type,
                "app_version": e.app_version,
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "received_at": e.received_at.isoformat(),
                "data": e.data,
            }
            for e in events
        ],
        "count": len(events),
    }
