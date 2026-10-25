"""In-process scheduler for periodic jobs (Garmin sync, backups)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[object]]


async def run_periodically(name: str, job: Job, interval: float, initial_delay: float) -> None:
    """Run `job` every `interval` seconds, forever. Failures are logged, not fatal.

    The initial delay keeps restarts quick and avoids hammering Garmin on crash loops.
    """
    await asyncio.sleep(initial_delay)
    while True:
        try:
            await job()
        except Exception:
            log.exception("job %r failed", name)
        await asyncio.sleep(interval)
