"""Single-flight probes for /_info, run on a dedicated executor.

A blocked socket call can't be cancelled, so a timed-out probe keeps its thread. Allowing
only one in-flight call per probe key bounds the stranded threads, and the dedicated
executor keeps them off the default one, so a hung dependency can't starve other checks.
"""

import asyncio
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")

PROBE_KEYS = (
    "db-check",
    "db-metrics",
    "redis-ping",
    "redis-worker-rss",
    "redis-chronic-media",
    "redis-chronic-persons",
)

_executor = ThreadPoolExecutor(max_workers=len(PROBE_KEYS), thread_name_prefix="info-probe")
# Only read and written on the event loop.
_inflight: dict[str, tuple[Future, float]] = {}


def _consume_result(fut: "asyncio.Future") -> None:
    if not fut.cancelled():
        fut.exception()  # mark retrieved so a late failure isn't logged as unretrieved


async def run_probe(key: str, fn: Callable[[], T], timeout: float) -> T:
    """Run `fn` on the probe executor, waiting at most `timeout` seconds. On timeout the
    call keeps running and holds the key's slot until it returns; later callers join it."""
    existing = _inflight.get(key)
    if existing is not None and not existing[0].done():
        wrapped = asyncio.wrap_future(existing[0])
        wrapped.add_done_callback(_consume_result)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), timeout=timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"probe in flight since {time.monotonic() - existing[1]:.0f}s") from None
    future = _executor.submit(fn)
    _inflight[key] = (future, time.monotonic())
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(_consume_result)
    return await asyncio.wait_for(asyncio.shield(wrapped), timeout=timeout)
