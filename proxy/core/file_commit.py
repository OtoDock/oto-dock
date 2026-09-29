"""The file-commit executor: the thread pool every disk commit the event
loop must never wait on runs on (an upload's writes, its fsync and its
rename; a satellite pull's commit).

It is its own pool on purpose. ``run_db``'s lanes carry the store calls,
and one multi-second fsync parked there would stall every query behind
it. The default executor carries every ``asyncio.to_thread`` site in the
proxy, and a disk that stalls for seconds would drain it for everyone.
Here a slow commit queues behind the other commits and nothing else.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import config
from core import loop_watchdog

_executor: ThreadPoolExecutor | None = None


def executor() -> ThreadPoolExecutor:
    """The shared pool, created on first use and reported to the watchdog."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=config.FILE_COMMIT_WORKERS, thread_name_prefix="file-commit",
        )
        loop_watchdog.watch_executor("file-commit", _executor)
    return _executor


async def run(fn, /, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` on the file-commit pool and await it."""
    loop = asyncio.get_running_loop()
    if kwargs:
        return await loop.run_in_executor(executor(), lambda: fn(*args, **kwargs))
    return await loop.run_in_executor(executor(), fn, *args)
