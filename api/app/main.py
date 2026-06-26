import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from fastapi.responses import JSONResponse

from app.auth import (  # noqa: F401 - safe_path and verify_session_or_key re-exported for tests
    AITHNE_ORIGIN,
    has_photos_access,
    is_allowed_origin,
    safe_path,
    verify_aithne_token,
    verify_session_or_key,
)
from app.database import get_db, SessionLocal  # noqa: F401 - re-exported for tests; SessionLocal used by check_db/get_metrics
from app.redis_client import _broadcast, _redis_subscriber_task, _ws_clients, get_redis  # noqa: F401 - _broadcast and _ws_clients re-exported for tests
from app.routers import app_release, faces, people, photos, telemetry, webhooks
from app.serializers import person_to_dict, photo_to_dict
from lucos_photos_common.models import MediaItem, Person, PhotoPerson, ProcessingState, ProcessingStatus

log = logging.getLogger(__name__)


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


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: Exception):
    """Render a styled HTML 403 for browser clients; JSON for API clients.

    The scope-missing branch of verify_session_or_key raises HTTPException(403,
    detail="access_denied").  Browser users (Accept: text/html) see the error
    template; API clients get a JSON body.
    """
    from fastapi import HTTPException as _HTTPException
    detail = exc.detail if isinstance(exc, _HTTPException) else "Forbidden"
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Access Denied",
                "message": "You're signed in but don't have access to Photos. Contact the administrator to request access.",
            },
            status_code=403,
        )
    return JSONResponse({"detail": detail}, status_code=403)


STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Inject AITHNE_ORIGIN as a Jinja2 global so every template can render the
# aithne-origin attribute on the navbar without per-route boilerplate.
templates.env.globals["aithne_origin"] = AITHNE_ORIGIN

app.include_router(photos.router)
app.include_router(people.router)
app.include_router(faces.router)
app.include_router(telemetry.router)
app.include_router(webhooks.router)
app.include_router(app_release.router)


@app.websocket("/stream")
async def websocket_stream(websocket: WebSocket):
    """WebSocket endpoint: push photo-processed events to browser clients.

    Auth: requires a valid ``aithne_session`` cookie with the ``photos:use`` scope.
    A WebSocket cannot redirect a browser, so the unauthenticated and forbidden
    branches close the socket with custom close codes:
    - 4401 — no token, or token invalid/expired
    - 4403 — valid session but missing ``photos:use`` scope

    On successful connection the server sends a ``{"type": "connected"}`` message.
    When a photo finishes processing the server sends:
        {"type": "photoProcessed", "photoId": "<uuid>"}
    The client is responsible for fetching photo details and inserting the card into the grid.
    """
    # Guard against Cross-Site WebSocket Hijacking.
    # aithne_session is SameSite=None, so browsers attach it to cross-origin
    # WebSocket upgrades.  Browsers always send the Origin header on WS upgrades
    # (RFC 6455); reject any request whose Origin is not l42.eu or *.l42.eu.
    # Absent Origin (non-browser programmatic clients) is allowed — it cannot be
    # CSRF-triggered from a browser page.
    origin = websocket.headers.get("origin")
    if origin is not None and not is_allowed_origin(origin):
        await websocket.close(code=4403, reason="Cross-origin connection rejected")
        return

    aithne_session = websocket.cookies.get("aithne_session")
    if not aithne_session:
        await websocket.close(code=4401, reason="Authentication required")
        return

    payload = verify_aithne_token(aithne_session)
    if payload is None:
        await websocket.close(code=4401, reason="Authentication required")
        return

    if not has_photos_access(payload.get("scopes", [])):
        await websocket.close(code=4403, reason="Insufficient scope")
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
def root(request: Request, _: Annotated[None, Depends(verify_session_or_key)], db: Session = Depends(get_db)):
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
    ok = False
    debug_info = None
    error_occurred = False
    try:
        await asyncio.wait_for(asyncio.to_thread(db.execute, text("SELECT 1")), timeout=CHECK_TIMEOUT)
        ok = True
    except asyncio.TimeoutError:
        error_occurred = True
        log.warning("db-reachable check timed out after %ss", CHECK_TIMEOUT)
        debug_info = f"timeout after {CHECK_TIMEOUT}s"
    except Exception as exc:
        error_occurred = True
        log.warning("db-reachable check failed: %r", exc)
        debug_info = repr(exc)
    finally:
        # Only invalidate on error: invalidate() tells the pool to discard the connection
        # rather than recycle it (preventing a broken/in-flight connection from contaminating
        # future sessions).  It must NOT be called on the happy path because it can race with
        # other concurrent sessions sharing the same underlying connection (e.g. SQLite in tests).
        # Both calls are wrapped broadly because db.invalidate() can itself raise
        # IllegalStateChangeError when the underlying thread is still running _connection_for_bind()
        # — a concurrent state-machine violation that must never escape as a 500.
        if error_occurred:
            try:
                db.invalidate()
            except Exception:
                pass
        try:
            db.close()
        except Exception:
            log.debug("session.close() skipped in check_db: state error during cleanup")
    result: dict = {"ok": ok, "techDetail": tech_detail}
    if debug_info is not None:
        result["debug"] = debug_info
    return result


async def check_redis() -> dict:
    """Check whether Redis is reachable."""
    tech_detail = "Checks whether a connection to Redis can be established"
    try:
        redis_conn = get_redis()
        await asyncio.wait_for(asyncio.to_thread(redis_conn.ping), timeout=CHECK_TIMEOUT)
        return {"ok": True, "techDetail": tech_detail}
    except asyncio.TimeoutError:
        log.warning("redis-reachable check timed out after %ss", CHECK_TIMEOUT)
        return {"ok": False, "techDetail": tech_detail, "debug": f"timeout after {CHECK_TIMEOUT}s"}
    except Exception as exc:
        log.warning("redis-reachable check failed: %r", exc)
        return {"ok": False, "techDetail": tech_detail, "debug": repr(exc)}


async def get_worker_memory_rss_bytes() -> int | None:
    """Read the worker's last-reported RSS from the Redis heartbeat key, or None if absent."""
    try:
        redis_conn = get_redis()
        raw = await asyncio.wait_for(asyncio.to_thread(redis_conn.get, "worker:heartbeat"), timeout=CHECK_TIMEOUT)
        if raw:
            return json.loads(raw).get("rss_bytes")
    except Exception:
        pass
    return None


async def get_metrics() -> dict:
    """Return live metrics: photo count, video count, pending queue depth, worker RSS."""
    db = SessionLocal()
    photo_count = video_count = pending_count = 0
    metrics_error = False
    try:
        row = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: db.execute(text("""
                    SELECT
                        (SELECT COUNT(*) FROM media_item WHERE media_type = 'photo') AS photo_count,
                        (SELECT COUNT(*) FROM media_item WHERE media_type = 'video') AS video_count,
                        (SELECT COUNT(*) FROM processing_status WHERE state = 'pending') AS pending_count
                """)).fetchone()
            ),
            timeout=CHECK_TIMEOUT,
        )
        photo_count = row.photo_count if row else 0
        video_count = row.video_count if row else 0
        pending_count = row.pending_count if row else 0
    except Exception:
        metrics_error = True  # defaults already set
    finally:
        # Same error-guarded invalidate pattern as check_db: only invalidate on error to avoid
        # racing with other concurrent sessions on the shared SQLite connection in tests.
        if metrics_error:
            try:
                db.invalidate()
            except Exception:
                pass
        try:
            db.close()
        except Exception:
            log.debug("session.close() skipped in get_metrics: state error during cleanup")

    worker_rss = await get_worker_memory_rss_bytes()

    metrics = {
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
    if worker_rss is not None:
        metrics["worker-memory-rss-bytes"] = {
            "value": worker_rss,
            "techDetail": "Worker process resident memory (RSS) in bytes",
        }
    return metrics


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
