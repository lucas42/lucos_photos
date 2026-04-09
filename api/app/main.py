import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.auth import _validate_token_with_auth_service, safe_path, verify_session, verify_session_or_key  # noqa: F401 - re-exported for tests
from app.database import get_db, SessionLocal  # noqa: F401 - re-exported for tests; SessionLocal used by check_db/get_metrics
from app.redis_client import _broadcast, _redis_subscriber_task, _ws_clients, get_redis  # noqa: F401 - _broadcast and _ws_clients re-exported for tests
from app.routers import app_release, faces, people, photos, telemetry, webhooks
from app.serializers import person_to_dict, photo_to_dict
from lucos_photos_common.models import MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus


@asynccontextmanager
async def lifespan(fastapi_app):
    task = asyncio.create_task(_redis_subscriber_task())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="lucos_photos", lifespan=lifespan)

from app.auth import _RedirectWithCookie  # noqa: E402


@app.middleware("http")
async def catch_redirect_with_cookie(request: Request, call_next):
    """Catch _RedirectWithCookie raised inside verify_session and return the redirect response."""
    try:
        return await call_next(request)
    except _RedirectWithCookie as exc:
        return exc.response


STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.include_router(photos.router)
app.include_router(people.router)
app.include_router(faces.router)
app.include_router(telemetry.router)
app.include_router(webhooks.router)
app.include_router(app_release.router)


@app.websocket("/stream")
async def websocket_stream(websocket: WebSocket):
    """WebSocket endpoint: push photo-processed events to browser clients.

    Auth: requires a valid `auth_token` cookie (same session cookie used by the web UI).
    On successful connection the server sends a `{"type": "connected"}` message.
    When a photo finishes processing the server sends:
        {"type": "photoProcessed", "photoId": "<uuid>"}
    The client is responsible for fetching photo details and inserting the card into the grid.
    """
    auth_token = websocket.cookies.get("auth_token")
    if not auth_token:
        await websocket.close(code=4401, reason="Authentication required")
        return

    data = await _validate_token_with_auth_service(auth_token)
    if not data or not data.get("id"):
        await websocket.close(code=4401, reason="Authentication required")
        return

    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "connected"}))
        # Keep the connection alive, waiting for the client to disconnect
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a keepalive ping so the connection doesn't time out via proxies
                await websocket.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root(request: Request, _: Annotated[None, Depends(verify_session)], db: Session = Depends(get_db)):
    # Photos: count and 10 most recently added (by uploaded_at)
    photo_order_cols = [MediaItem.taken_at.desc().nullslast(), MediaItem.uploaded_at.desc()]
    processed_photos_query = (
        db.query(MediaItem)
        .join(ProcessingStatus, MediaItem.id == ProcessingStatus.photo_id)
        .filter(ProcessingStatus.state == ProcessingState.complete)
    )
    photo_total = processed_photos_query.count()
    recent_photos = processed_photos_query.order_by(*photo_order_cols).limit(10).all()

    # People: count and top 10 by number of photos tagged in
    base_person_filter = Person.is_background == False  # noqa: E712
    people_total = db.query(func.count(Person.id)).filter(base_person_filter).scalar()
    top_people_with_counts = (
        db.query(Person, func.count(PhotoPerson.photo_id).label("photo_count"))
        .filter(base_person_filter)
        .outerjoin(PhotoPerson)
        .group_by(Person.id)
        .order_by(func.count(PhotoPerson.photo_id).desc())
        .limit(10)
        .all()
    )

    return templates.TemplateResponse(request, "homepage.html", {
        "current_page": "home",
        "photo_total": photo_total,
        "recent_photos": [photo_to_dict(p) for p in recent_photos],
        "people_total": people_total,
        "top_people": [person_to_dict(p, count) for p, count in top_people_with_counts],
    })


CHECK_TIMEOUT = 0.5  # seconds — must be well under monitoring system's 1s hard limit


async def check_db() -> dict:
    """Check whether a connection to PostgreSQL can be established."""
    tech_detail = "Checks whether a connection to PostgreSQL can be established"
    db = SessionLocal()
    try:
        await asyncio.wait_for(asyncio.to_thread(db.execute, text("SELECT 1")), timeout=CHECK_TIMEOUT)
        return {"ok": True, "techDetail": tech_detail}
    except Exception:
        # Invalidate rather than return to the pool: asyncio.wait_for cancels the
        # coroutine wrapper but the underlying thread keeps running, so db.close()
        # would race with the in-flight execute and return a broken connection.
        db.invalidate()
        return {"ok": False, "techDetail": tech_detail}
    finally:
        db.close()


async def check_redis() -> dict:
    """Check whether Redis is reachable."""
    tech_detail = "Checks whether a connection to Redis can be established"
    try:
        redis_conn = get_redis()
        await asyncio.wait_for(asyncio.to_thread(redis_conn.ping), timeout=CHECK_TIMEOUT)
        return {"ok": True, "techDetail": tech_detail}
    except Exception:
        return {"ok": False, "techDetail": tech_detail}


async def get_metrics() -> dict:
    """Return live metrics: photo count, video count, and pending processing queue depth."""
    db = SessionLocal()
    try:
        photo_count = await asyncio.to_thread(
            lambda: db.query(MediaItem).filter(MediaItem.media_type == "photo").count()
        )
        video_count = await asyncio.to_thread(
            lambda: db.query(MediaItem).filter(MediaItem.media_type == "video").count()
        )
        pending_count = await asyncio.to_thread(
            lambda: db.query(ProcessingStatus).filter(
                ProcessingStatus.state == ProcessingState.pending
            ).count()
        )
        return {
            "photo-count": {
                "value": photo_count,
                "techDetail": "Total number of photos stored",
            },
            "video-count": {
                "value": video_count,
                "techDetail": "Total number of videos stored",
            },
            "processing-pending-count": {
                "value": pending_count,
                "techDetail": "Number of media items awaiting processing",
            },
        }
    except Exception:
        db.invalidate()
        return {
            "photo-count": {
                "value": 0,
                "techDetail": "Total number of photos stored",
            },
            "video-count": {
                "value": 0,
                "techDetail": "Total number of videos stored",
            },
            "processing-pending-count": {
                "value": 0,
                "techDetail": "Number of media items awaiting processing",
            },
        }
    finally:
        db.close()


@app.get("/_info")
async def info():
    db_check, redis_check, metrics = await asyncio.gather(
        check_db(),
        check_redis(),
        get_metrics(),
    )
    return {
        "system": os.environ.get("SYSTEM", "lucos_photos"),
        "checks": {
            "db-reachable": db_check,
            "redis-reachable": redis_check,
        },
        "metrics": metrics,
        "ci": {
            "circle": "gh/lucas42/lucos_photos",
        },
        "icon": "/icon.png",
        "network_only": True,
        "title": "Photos",
        "show_on_homepage": True,
        "start_url": "/",
    }


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
