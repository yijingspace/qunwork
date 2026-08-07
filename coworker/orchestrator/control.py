"""RunController — external control channel for one live swarm run (G2: the
SwarmView command deck).

Supports:
- pause / resume: workers hold between scheduling rounds while paused;
- inject_message: operator directives fed to the next task execution round;
- requeue approval: when the reviewer rejects a task, the run waits for the
  deck to approve re-running it (or rejects / times out → needs_human).

Owner-audit 2026-08-07 (bug #6): the deck is driven from FastAPI request
handlers (a different thread than the orchestrator's event loop). asyncio
Event/Queue/Future are loop-bound — calling .set()/.set_result()/.put_nowait()
from another thread is undefined behavior. We now bind the loop on first
orchestrator-side await and route cross-thread calls through
loop.call_soon_threadsafe().
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


class RunController:
    def __init__(self) -> None:
        self._paused = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # running by default
        self._messages: "asyncio.Queue[str]" = asyncio.Queue()
        self._requeue: dict[str, "asyncio.Future[bool]"] = {}
        self._requeue_meta: dict[str, dict[str, Any]] = {}
        # Loop bound on the first orchestrator-side await (the loop that owns
        # the Event/Queue/Future). Cross-thread callers route through it.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _bind_loop(self) -> None:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass  # not inside a loop yet; fall back to direct calls

    def _call_threadsafe(self, fn: Any) -> None:
        """Invoke fn on the bound loop thread; fall back to direct call when
        no loop is bound or when we're already on that loop."""
        loop = self._loop
        if loop is not None and loop.is_running() and asyncio.get_event_loop() is not loop:
            loop.call_soon_threadsafe(fn)
        else:
            fn()

    # -- pause / resume --------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        def _do() -> None:
            self._paused = paused
            if paused:
                self._pause_event.clear()
            else:
                self._pause_event.set()

        self._call_threadsafe(_do)

    @property
    def paused(self) -> bool:
        return self._paused

    async def wait_if_paused(self) -> None:
        """Held by the orchestrator between scheduling rounds while paused."""
        self._bind_loop()
        await self._pause_event.wait()

    # -- operator messages -----------------------------------------------------
    def inject_message(self, text: str) -> None:
        def _do() -> None:
            self._messages.put_nowait(text)

        self._call_threadsafe(_do)

    def drain_messages(self) -> list[str]:
        out: list[str] = []
        while True:
            try:
                out.append(self._messages.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    # -- requeue approval ------------------------------------------------------
    async def await_requeue(
        self, task_id: str, meta: dict[str, Any], timeout: float = 120.0
    ) -> bool:
        """Block until the deck approves (True) or rejects/times out (False).

        The orchestrator emits a `task_requeue_waiting` event before calling so
        the deck can show the approval card.
        """
        self._bind_loop()
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        self._requeue[task_id] = fut
        self._requeue_meta[task_id] = dict(meta)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._requeue.pop(task_id, None)
            self._requeue_meta.pop(task_id, None)

    def approve_requeue(self, task_id: str) -> bool:
        def _do() -> bool:
            fut = self._requeue.get(task_id)
            if fut is not None and not fut.done():
                fut.set_result(True)
                return True
            return False

        if self._loop is not None and self._loop.is_running() and asyncio.get_event_loop() is not self._loop:
            self._loop.call_soon_threadsafe(_do)
            return True  # queued; the future will resolve
        return _do()

    def reject_requeue(self, task_id: str) -> bool:
        def _do() -> bool:
            fut = self._requeue.get(task_id)
            if fut is not None and not fut.done():
                fut.set_result(False)
                return True
            return False

        if self._loop is not None and self._loop.is_running() and asyncio.get_event_loop() is not self._loop:
            self._loop.call_soon_threadsafe(_do)
            return True
        return _do()

    def pending_requeues(self) -> list[dict[str, Any]]:
        return [
            {"task_id": tid, **meta}
            for tid, meta in self._requeue_meta.items()
        ]
