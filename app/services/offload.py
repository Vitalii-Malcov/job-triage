"""Review finding 2.4: keep blocking service work off the web event loop.

Several `async def` endpoints (collector runs, automation cycles, Gmail sync,
company research, Bewerbung drafts, job scoring) interleave awaits with
SYNCHRONOUS SQLAlchemy work. Run on the web loop, one long collector run stalls
the API, the Telegram poller (same loop) and `/health` together.

`run_off_loop(factory)` executes `await factory()` on its OWN event loop in a
worker thread of a dedicated, bounded executor (not the loop's default executor
and not AnyIO's pool for sync endpoints, so DNS lookups and `/health` keep their
capacity). The service code is unchanged: it simply stops sharing a loop with
the web server.

Session ownership. The request-scoped `Session` is handed to the worker and used
by exactly one thread at a time: the request task only awaits. Therefore the
caller's cancellation must NOT return control (and let `get_db` / a Telegram
handler close the session) while the worker still runs. On cancellation the
helper keeps waiting for the worker to finish -- repeated cancellations are
absorbed while it does -- and re-raises the cancellation only afterwards. A
worker failure after the caller was cancelled is retrieved (never "exception was
never retrieved"). Work that must outlive a cancelled caller is not started by
this helper: it runs to completion or not at all, under the caller's await.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Bounded on purpose: long collector runs queue instead of exhausting threads.
OFFLOAD_MAX_WORKERS = 8
_EXECUTOR = ThreadPoolExecutor(max_workers=OFFLOAD_MAX_WORKERS, thread_name_prefix="offload")


def _run_in_new_loop(factory: Callable[[], Awaitable[T]]) -> T:
    async def _main() -> T:
        return await factory()

    return asyncio.run(_main())


async def run_off_loop(factory: Callable[[], Awaitable[T]]) -> T:
    """Run `await factory()` on a private event loop in the offload executor."""
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_EXECUTOR, _run_in_new_loop, factory)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue  # cancelled again: keep draining
            except BaseException:  # noqa: BLE001 - the worker's own failure ends the wait
                break
        if future.done() and not future.cancelled():
            future.exception()  # retrieve; the caller is being cancelled anyway
        raise
