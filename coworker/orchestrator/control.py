"""RunController — external control channel for one live swarm run (G2: the
SwarmView command deck).

Supports:
- pause / resume: workers hold between scheduling rounds while paused;
- inject_message: operator directives fed to the next task execution round;
- requeue approval: when the reviewer rejects a task, the run waits for the
  deck to approve re-running it (or rejects / times out → needs_human).
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

    # -- pause / resume --------------------------------------------------------
    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._pause_event.clear()
        else:
            self._pause_event.set()

    @property
    def paused(self) -> bool:
        return self._paused

    async def wait_if_paused(self) -> None:
        """Held by the orchestrator between scheduling rounds while paused."""
        await self._pause_event.wait()

    # -- operator messages -----------------------------------------------------
    def inject_message(self, text: str) -> None:
        self._messages.put_nowait(text)

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
        fut = self._requeue.get(task_id)
        if fut is not None and not fut.done():
            fut.set_result(True)
            return True
        return False

    def reject_requeue(self, task_id: str) -> bool:
        fut = self._requeue.get(task_id)
        if fut is not None and not fut.done():
            fut.set_result(False)
            return True
        return False

    def pending_requeues(self) -> list[dict[str, Any]]:
        return [
            {"task_id": tid, **meta}
            for tid, meta in self._requeue_meta.items()
        ]
