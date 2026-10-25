"""In-process scheduler for daily maintenance jobs (gap-check backfill, backups)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[object]]

DAY_SECONDS = 24 * 3600


async def run_periodically(
    jobs: dict[str, Job], interval: float = DAY_SECONDS, initial_delay: float = 300
) -> None:
    """Run each job every `interval` seconds, forever. A failing job doesn't stop others.

    The initial delay keeps restarts quick and avoids hammering Garmin on crash loops.
    """
    await asyncio.sleep(initial_delay)
    while True:
        for name, job in jobs.items():
            try:
                await job()
            except Exception:
                log.exception("job %r failed", name)
        await asyncio.sleep(interval)
