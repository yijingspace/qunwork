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
        # P0 建议3 (蜂群指挥台): structured DAG edits drained by the orchestrator
        # between scheduling rounds — task injection (fork) + agent retargeting.
        self._task_injections: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self._retargets: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue()
        self._requeue: dict[str, "asyncio.Future[bool]"] = {}
        self._requeue_meta: dict[str, dict[str, Any]] = {}
        # Loop bound on the first orchestrator-side await (the loop that owns
        # the Event/Queue/Future). Cross-thread callers route through it.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Optional Inbox bridge for requeue approvals (attended async GUI runs).
        # When attached, a reviewer rejection surfaces a durable Inbox approval so
        # an AWAY user is actually asked — instead of the command-deck timeout
        # silently degrading the task. Set via attach_inbox_bridge(); the deck
        # remains a second, equally-valid answer surface (first responder wins).
        self._inbox: Any = None
        self._inbox_session_id: Optional[str] = None
        self._inbox_run_id: Optional[str] = None
        self._inbox_hold_seconds: float = 3600.0
        self._requeue_inbox_ids: dict[str, str] = {}

    def attach_inbox_bridge(
        self,
        inbox: Any,
        *,
        session_id: str,
        run_id: str,
        hold_seconds: float = 3600.0,
    ) -> None:
        """Enable durable Inbox requeue approvals for this run (see __init__)."""
        self._inbox = inbox
        self._inbox_session_id = session_id
        self._inbox_run_id = run_id
        self._inbox_hold_seconds = max(1.0, float(hold_seconds))

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
        if loop is not None and loop.is_running():
            try:
                # `get_event_loop()` raises RuntimeError on worker/threadpool
                # threads (FastAPI sync endpoints run there) — use
                # get_running_loop() which only succeeds on a loop thread.
                asyncio.get_running_loop()
                fn()
            except RuntimeError:
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

    # -- P0 建议3: structured DAG edits ----------------------------------------
    def inject_task(
        self,
        *,
        id: str,
        description: str,
        deps: Optional[list[str]] = None,
        agent: str = "",
    ) -> None:
        """Queue a fork task for the running plan. The orchestrator appends it
        (validated: unique id, known deps) at the next scheduling round. Only
        pending tasks are affected — running ones can't be interrupted."""

        def _do() -> None:
            self._task_injections.put_nowait(
                {
                    "id": id,
                    "description": description,
                    "deps": list(deps or []),
                    "agent": agent,
                }
            )

        self._call_threadsafe(_do)

    def drain_task_injections(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._task_injections.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    def retarget_task(self, task_id: str, agent: str) -> None:
        """Reassign a pending task to a different executor role at runtime."""

        def _do() -> None:
            self._retargets.put_nowait({"id": task_id, "agent": agent})

        self._call_threadsafe(_do)

    def drain_retargets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while True:
            try:
                out.append(self._retargets.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    # -- requeue approval ------------------------------------------------------
    async def await_requeue(
        self, task_id: str, meta: dict[str, Any], timeout: float = 120.0
    ) -> bool:
        """Block until the operator approves (True) or rejects/times out (False).

        The orchestrator emits a `task_requeue_waiting` event before calling so the
        deck can show the approval card. When an Inbox bridge is attached (attended
        GUI runs), the decision is ALSO surfaced as a durable Inbox approval and we
        PARK up to `_inbox_hold_seconds` instead of the deck's short timeout — so an
        away user gets a real chance to answer rather than the task being silently
        degraded. The deck and the Inbox both resolve the same future; first writer
        wins (`approve_requeue`/`reject_requeue` and the inbox waiter are all
        future-done guarded), and the loser is cleaned up in `finally`.
        """
        self._bind_loop()
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        self._requeue[task_id] = fut
        self._requeue_meta[task_id] = dict(meta)
        inbox_task = None
        try:
            if self._inbox is not None:
                inbox_task = asyncio.create_task(
                    self._await_requeue_inbox(task_id, dict(meta), fut)
                )
                hold = self._inbox_hold_seconds
            else:
                hold = timeout
            # shield() so a timeout cancels the OUTER await, not our shared `fut`
            # (the deck / inbox may still resolve it; and on timeout we set False
            # ourselves rather than re-raising the future's CancelledError).
            return await asyncio.wait_for(asyncio.shield(fut), timeout=hold)
        except asyncio.TimeoutError:
            # Nobody answered in the window → decline (accept current result, degraded).
            if not fut.done():
                fut.set_result(False)
            return False
        finally:
            self._requeue.pop(task_id, None)
            self._requeue_meta.pop(task_id, None)
            if inbox_task is not None:
                inbox_task.cancel()
            self._close_requeue_inbox(task_id)

    async def _await_requeue_inbox(
        self, task_id: str, meta: dict[str, Any], fut: "asyncio.Future[bool]"
    ) -> None:
        """Open the durable Inbox approval and resolve `fut` when answered."""
        item = self._inbox.add_approval(
            self._inbox_session_id,
            title=f"蜂群任务 {task_id} 评审否决 — 重跑还是接受当前结果？",
            body=str(meta.get("reason") or ""),
            data={
                "run_id": self._inbox_run_id,
                "task_id": task_id,
                "attempt": meta.get("attempt"),
                "swarm_requeue": True,
            },
        )
        self._requeue_inbox_ids[task_id] = item.id
        resolution = await self._inbox.wait(item.id)
        # Deck may have answered first; only act if still unresolved.
        if not fut.done():
            fut.set_result(resolution in ("allow", "allow_deliver"))

    def _close_requeue_inbox(self, task_id: str) -> None:
        """Best-effort: clear a still-pending Inbox card once the decision is made
        by another surface (the deck) or the window closed. Idempotent (InboxStore
        ignores a second resolve)."""
        item_id = self._requeue_inbox_ids.pop(task_id, None)
        if item_id and self._inbox is not None:
            try:
                self._inbox.resolve(item_id, "deny")  # neutral close; fut already decided
            except Exception:
                pass

    def approve_requeue(self, task_id: str) -> bool:
        def _do() -> bool:
            fut = self._requeue.get(task_id)
            if fut is not None and not fut.done():
                fut.set_result(True)
                return True
            return False

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.get_running_loop()  # already on the bound loop thread
                return _do()
            except RuntimeError:
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

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.get_running_loop()  # already on the bound loop thread
                return _do()
            except RuntimeError:
                self._loop.call_soon_threadsafe(_do)
                return True
        return _do()

    def pending_requeues(self) -> list[dict[str, Any]]:
        return [
            {"task_id": tid, **meta}
            for tid, meta in self._requeue_meta.items()
        ]
