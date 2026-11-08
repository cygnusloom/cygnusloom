"""Run a GM turn in its own task, so leaving the page does not cancel it.

A turn is a long model call with many database writes behind it. When it runs inside the HTTP
response, Starlette cancels the response generator as soon as the browser goes away, and the
reply stops half-written with nothing recorded. A `TurnJob` owns the turn in a separate asyncio
task instead: the response only follows the job's buffered NDJSON lines, and closing or
cancelling that follower never touches the turn. A reload can follow the same job from its first
line, or read the finished reply from the log once the job is done.

The registry keeps at most one job per campaign, the same one-turn-at-a-time rule as the
service's `_exclusive` guard. It is per app process, like `turn_busy` and the typing hints: a
deployment with several worker processes would need a shared broker to follow a turn across
them, and the saved reply plus the manual Continue remain the fallback. The registry mapping is
guarded by a `threading.Lock` because synchronous routes and status reads run in worker threads;
everything inside a job (`buffer`, follower queues, events) belongs to the single event loop that
started it, and every follower must be read from that loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable

from cygnusloom.services.campaigns import TURN_BUSY_MESSAGE, TurnEvent
from cygnusloom.services.common import Conflict

logger = logging.getLogger(__name__)

# The NDJSON line shape matches `web.campaigns._line`; the module stays independent of the router.
_UNEXPECTED_ERROR_LINE = (
    json.dumps({"type": "error", "status": 500, "message": "an unexpected error"}) + "\n"
)

# Builds the job's NDJSON lines, collecting the exceptions it reported so a non-streaming route
# can answer with the same status code the stream carried.
type LinesFactory = Callable[[AsyncIterator[TurnEvent], int, list[Exception]], AsyncIterator[str]]


class TurnJob:
    """One running GM turn and the NDJSON lines it has produced so far.

    The producer appends lines and wakes every follower without ever awaiting a follower, so a
    slow or vanished page cannot hold the turn back. Late followers replay the buffer from the
    first line and then receive the rest live.
    """

    def __init__(
        self,
        campaign_id: int,
        events: AsyncIterator[TurnEvent],
        lines: LinesFactory,
    ) -> None:
        self.campaign_id = campaign_id
        # The original exception when the turn was refused or failed. A streaming route shows
        # it as an error line; a plain POST turns it into a status code.
        self.errors: list[Exception] = []
        self._events = events
        self._lines_factory = lines
        self._lines: list[str] = []
        self._followers: set[asyncio.Queue[str | None]] = set()
        self._done = False
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None]:
        assert self._task is not None, "the job has not started"
        return self._task

    @property
    def done(self) -> bool:
        """Whether the turn has finished, failed, or been cancelled."""
        return self._task is None or self._task.done()

    @property
    def error(self) -> Exception | None:
        """The turn's refusal or failure, or None after a normal finish."""
        return self.errors[0] if self.errors else None

    def start(self) -> None:
        """Own the turn in a task of the current event loop."""
        self._task = asyncio.create_task(self._run(), name=f"turn-job-{self.campaign_id}")

    async def wait(self) -> None:
        """Wait until the turn is over, without letting the caller's cancellation end the turn.

        `asyncio.shield` is the point of the whole module: a request that goes away cancels its
        own task, and awaiting the job directly would pass that cancellation on to the turn.
        """
        if self._task is not None:
            await asyncio.shield(self._task)

    def follow(self) -> AsyncIterator[str]:
        """The turn's NDJSON lines, from the first one, then live as they arrive."""
        return self._follow()

    async def _follow(self) -> AsyncIterator[str]:
        # Replaying the buffer and joining the live feed happen without an await in between, so
        # a line cannot slip through the gap. All of this runs on the producer's event loop.
        replay = list(self._lines)
        # `done` also covers a task cancelled before its first step, which never reaches the
        # finish sentinel.
        finished = self._done or self.done
        queue: asyncio.Queue[str | None] | None = None
        if not finished:
            queue = asyncio.Queue()
            self._followers.add(queue)
        try:
            for line in replay:
                yield line
            if queue is None:
                return
            while True:
                line = await queue.get()
                if line is None:
                    return
                yield line
        finally:
            if queue is not None:
                self._followers.discard(queue)

    def _append(self, line: str) -> None:
        self._lines.append(line)
        for queue in self._followers:
            # An unbounded queue, so the producer never waits on a reader. A closed follower
            # removes its queue in its own `finally`.
            queue.put_nowait(line)

    def _finish(self) -> None:
        self._done = True
        for queue in self._followers:
            queue.put_nowait(None)
        self._followers.clear()

    async def _run(self) -> None:
        lines = self._lines_factory(self._events, self.campaign_id, self.errors)
        try:
            async for line in lines:
                self._append(line)
        except asyncio.CancelledError:
            # Shutdown is not a turn failure. Let it through so the task ends cancelled; the
            # service's `_exclusive` releases below and the open turn stays for Continue.
            raise
        except Exception:
            logger.exception("the GM turn for campaign %s stopped unexpectedly", self.campaign_id)
            self._append(_UNEXPECTED_ERROR_LINE)
        finally:
            try:
                try:
                    await lines.aclose()
                finally:
                    # Closing the inner generator is what releases `_exclusive` when the
                    # follower went away, so it must happen before the job reports itself done.
                    await _aclose(self._events)
            finally:
                self._finish()


async def _aclose(iterator: AsyncIterator[TurnEvent]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


class TurnJobRegistry:
    """The running turn of each campaign, at most one per campaign."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, TurnJob] = {}

    def start(
        self,
        campaign_id: int,
        events: AsyncIterator[TurnEvent],
        lines: LinesFactory,
    ) -> TurnJob:
        """Start the campaign's turn, or raise `Conflict` when one is already running."""
        with self._lock:
            current = self._jobs.get(campaign_id)
            if current is not None and not current.done:
                raise Conflict(TURN_BUSY_MESSAGE)
            job = TurnJob(campaign_id, events, lines)
            job.start()
            self._jobs[campaign_id] = job
            # The callback forgets only its own job, so a stale job that finished after the
            # next one started cannot remove the running entry.
            job.task.add_done_callback(lambda _: self._forget(campaign_id, job))
            return job

    def active(self, campaign_id: int) -> bool:
        """Whether this process is still running a turn for the campaign."""
        with self._lock:
            job = self._jobs.get(campaign_id)
            return job is not None and not job.done

    def _forget(self, campaign_id: int, job: TurnJob) -> None:
        with self._lock:
            if self._jobs.get(campaign_id) is job:
                self._jobs.pop(campaign_id, None)
