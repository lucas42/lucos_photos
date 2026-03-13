"""Redis connection, job enqueueing, and WebSocket broadcast infrastructure."""

import asyncio
import json
import os

from fastapi import WebSocket
from redis import Redis
from rq import Queue
from rq.job import Retry

_redis_conn: Redis | None = None

PHOTO_PROCESSED_CHANNEL = "photos:processed"

# Set of active WebSocket connections; modified only on the main event loop thread.
_ws_clients: set[WebSocket] = set()


def get_redis() -> Redis:
    global _redis_conn
    if _redis_conn is None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        _redis_conn = Redis.from_url(redis_url)
    return _redis_conn


async def _redis_subscriber_task():
    """Background task: subscribe to the Redis pub/sub channel and broadcast to WebSocket clients.

    Runs for the lifetime of the application. Reconnects automatically on error.
    Uses a dedicated synchronous Redis connection (pubsub blocks) run via asyncio.to_thread.
    """
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    while True:
        try:
            redis_sub = Redis.from_url(redis_url)
            pubsub = redis_sub.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(PHOTO_PROCESSED_CHANNEL)

            async def _poll():
                while True:
                    message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
                    if message and message.get("type") == "message":
                        data = message["data"]
                        if isinstance(data, bytes):
                            data = data.decode()
                        await _broadcast(data)

            await _poll()
        except Exception as exc:
            print(f"WebSocket broadcaster: Redis subscriber error: {exc}", flush=True)
            await asyncio.sleep(5)


async def _broadcast(message: str):
    """Send a text message to all connected WebSocket clients, removing any that have disconnected."""
    disconnected = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    _ws_clients.difference_update(disconnected)


def enqueue_process_media(photo_id: str, media_type: str = "photo") -> None:
    """Enqueue a processing job for the given media item UUID string.

    Routes to process_video for videos, process_photo for photos.
    """
    if media_type == "video":
        from lucos_photos_common.jobs import process_video as _job_fn
    else:
        from lucos_photos_common.jobs import process_photo as _job_fn
    try:
        redis_conn = get_redis()
        queue = Queue("photos", connection=redis_conn)
        queue.enqueue(
            _job_fn,
            photo_id,
            retry=Retry(max=3, interval=[10, 30, 60]),
        )
    except Exception as exc:
        # Log but don't fail the upload — the worker's pending sweep will catch it.
        print(f"Warning: failed to enqueue {_job_fn.__name__} for {photo_id}: {exc}", flush=True)


# Keep the old name as an alias for backwards compatibility
enqueue_process_photo = enqueue_process_media
