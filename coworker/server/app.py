"""FastAPI app — OpenAI-compatible endpoint + WS session API + REST.

The control plane every surface (GUI/IDE/messaging) rides on. The WS carries the engine
event stream and the approval channel; `/v1/chat/completions` is the OpenAI-compatible
proxy so any OpenAI-format client can use the runtime as a backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Origins allowed to talk to the local sidecar. It binds to 127.0.0.1, but a page in the
# user's own browser can still reach loopback — so without an origin gate, any website they
# visit could read `GET /v1/sessions` (CORS was `*`) and drive a session over the WS (which
# CORS never covers) into shell/file tools. We pin to the desktop webview's own origins
# (`tauri://localhost`, Windows' `http(s)://tauri.localhost`) and localhost dev/browser
# builds. Requests with NO Origin header (curl, native clients, tests, server-to-server) are
# allowed — the gate targets browsers, which always attach an unforgeable Origin.
_ALLOWED_ORIGIN_RE = re.compile(
    r"^(tauri://localhost"
    r"|https?://localhost(:\d+)?"
    r"|https?://127\.0\.0\.1(:\d+)?"
    r"|https?://tauri\.localhost)$"
)


def _origin_allowed(origin: str | None) -> bool:
    """True if a browser Origin may use the API. Missing Origin (non-browser) passes."""
    return origin is None or bool(_ALLOWED_ORIGIN_RE.match(origin))


# Caps on inbound WebSocket traffic. The loopback socket is unauthenticated (any local
# process can reach it), so bound frames, messages, and per-connection request rate before
# building model content or starting a turn.
_WS_MAX_FRAME_BYTES = 16 * 1024 * 1024
_WS_RATE_LIMIT_COUNT = 30
_WS_RATE_LIMIT_WINDOW_SECONDS = 10.0
_MAX_MESSAGE_TEXT_CHARS = 200_000
_MAX_ATTACHMENTS_BYTES = 15_000_000  # leaves JSON overhead below the 16 MiB frame cap


def _json_value_size(value: Any) -> int:
    """Conservative UTF-8 size of parsed JSON without allocating another giant string."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return sum(_json_value_size(k) + _json_value_size(v) for k, v in value.items())
    if isinstance(value, list):
        return sum(_json_value_size(v) for v in value)
    return 8  # numbers, booleans, null, separators


# Brand colors for the connector badge riding the ✓ (UX-DECISIONS §30). The GUI owns the
# real logos; this page must render offline with zero assets, so a colored initial stands in.
_BRAND_COLORS = {
    "slack": "#4A154B",
    "github": "#24292f",
    "hubspot": "#ff7a59",
    "gmail": "#ea4335",
    "google_calendar": "#4285f4",
}


def _browser_page(
    title: str, detail: str, *, ok: bool = True, error: str = "", connector: str = ""
) -> str:
    """The page shown in the user's browser at the end of a loopback flow (sign-in or
    connector callback) — one branded card (UX-DECISIONS §30): OCW mark, ok/fail icon
    (the connector's initial rides the ✓), the friendly detail, and the raw error
    preserved on failures (it's the debugging breadcrumb). Inline CSS, light/dark via
    prefers-color-scheme, no external assets — it must render offline."""
    import html as _html

    badge = ""
    if ok and connector:
        color = _BRAND_COLORS.get(connector, "#3670b2")
        initial = _html.escape((connector[:1] or "?").upper())
        badge = f'<span class="mini" style="background:{color}">{initial}</span>'
    icon = (
        f'<div class="ico ok">✓{badge}</div>' if ok else '<div class="ico bad">✕</div>'
    )
    err = f'<div class="err">{_html.escape(error)}</div>' if error else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_html.escape(title)} — QunWork</title><style>"
        ":root{--paper:#f6f5f2;--panel:#fff;--line:#e4e2dc;--ink:#2c2c2a;--muted:#6f6e68;"
        "--faint:#a3a19a;--accent:#3670b2;--ok:#2e7d4f;--ok-soft:#e3f2e9;--bad:#b3423a;"
        "--bad-soft:#f8e7e5}"
        "@media(prefers-color-scheme:dark){:root{--paper:#191918;--panel:#232322;"
        "--line:#373633;--ink:#e8e6e1;--muted:#9d9b94;--faint:#6b6a64;--accent:#6ba3dd;"
        "--ok:#5cb884;--ok-soft:#20362a;--bad:#d97b74;--bad-soft:#3a2422}}"
        "body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;"
        "justify-content:center;gap:18px;background:var(--paper);color:var(--ink);"
        'font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:24px}'
        ".card{background:var(--panel);border:1px solid var(--line);border-radius:16px;"
        "padding:34px 32px 28px;max-width:320px;width:100%;text-align:center;"
        "box-shadow:0 10px 30px rgba(0,0,0,.06);box-sizing:border-box}"
        ".mark{display:flex;align-items:center;justify-content:center;gap:7px;margin-bottom:22px;"
        "font-size:13px;font-weight:650}"
        ".mark i{width:20px;height:20px;border-radius:6px;background:var(--accent);"
        "display:inline-block;position:relative}"
        ".mark i::after{content:'';position:absolute;inset:5px;border-radius:2px;"
        "background:conic-gradient(from 0deg,#fff 0 25%,transparent 0 50%,#fff 0 75%,transparent 0)}"
        ".ico{width:52px;height:52px;border-radius:50%;margin:0 auto 14px;display:flex;"
        "align-items:center;justify-content:center;font-size:24px;position:relative}"
        ".ico.ok{background:var(--ok-soft);color:var(--ok)}"
        ".ico.bad{background:var(--bad-soft);color:var(--bad)}"
        ".mini{position:absolute;right:-3px;bottom:-3px;width:22px;height:22px;border-radius:7px;"
        "display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;"
        "font-weight:700;border:2px solid var(--panel)}"
        "h1{font-size:17px;font-weight:650;margin:0 0 6px;letter-spacing:-.01em}"
        "p{font-size:12.5px;color:var(--muted);margin:0}"
        ".err{font-size:11.5px;color:var(--bad);background:var(--bad-soft);border-radius:8px;"
        "padding:7px 10px;margin-top:12px;text-align:left;word-break:break-word}"
        ".foot{font-size:10.5px;color:var(--faint)}"
        "</style></head><body>"
        '<div class="card"><div class="mark"><i></i>QunWork</div>'
        f"{icon}<h1>{_html.escape(title)}</h1><p>{_html.escape(detail)}</p>{err}</div>"
        '<div class="foot">Served locally by QunWork on your device</div>'
        "</body></html>"
    )


def _connector_title(name: str) -> str:
    """Display name for the loopback page — 'Slack connected', never 'slack connected'."""
    from ..connectors.descriptors import get_descriptor

    d = get_descriptor(name)
    return d.title if d else (name[:1].upper() + name[1:])


_CONNECT_FAILED_DETAIL = (
    "Something went wrong finishing this connection. "
    "Close this tab and try again from QunWork."
)

from ..attachments import (
    MAX_ATTACHMENTS as _MAX_ATTACHMENTS,
    MAX_IMAGE_CHARS,
    MAX_PDF_CHARS,
    MAX_TEXT_CHARS,
    build_user_content,
)
from ..engine import ApprovalOutcome
from ..inbox import VIS_INBOX, VIS_INLINE, args_preview
from ..permissions import Mode
from ..providers import AssistantTurn
from .manager import SessionManager


def _extract_parent_id(intent: str) -> Optional[int]:
    """Parse the research-relay marker `[知识来源ID:123]` out of a prompt/intent.
    The UI's "continue research" action embeds it so the resulting report is
    linked back to the knowledge entry it was derived from."""
    import re as _re

    m = _re.search(r"\[知识来源ID:(\d+)\]", intent or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def create_app(manager: SessionManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # HORNET emergence observer: an auto-evolve pass at boot, then a
        # slow periodic sweep (6h) so new knowledge keeps surfacing to humans.
        _hornet_loop_stop = asyncio.Event()

        async def _hornet_observer_loop() -> None:
            await asyncio.sleep(1.5)  # let manager finish wiring
            try:
                # to_thread: evolve+freshness_pass sweep 698+ cells (Pisano
                # periods etc.) — running it synchronously here froze the event
                # loop and every HTTP request (detail view!) hung until it
                # finished (the "无法加载详情" report).
                await asyncio.to_thread(manager.hornet_auto_evolve)
            except Exception:
                pass
            while not _hornet_loop_stop.is_set():
                try:
                    await asyncio.wait_for(_hornet_loop_stop.wait(), timeout=6 * 3600)
                except asyncio.TimeoutError:
                    try:
                        await asyncio.to_thread(manager.hornet_auto_evolve)
                    except Exception:
                        pass

        task = asyncio.create_task(_hornet_observer_loop())
        # P0 建议1: cache warm-up sweep — every interval_hours, if the feature is
        # enabled AND the org hit-rate is below the threshold, inject the coldest
        # knowledge prefixes (max_tokens=1) to push them into the prefix cache.
        _cache_warm_stop = asyncio.Event()

        async def _cache_warm_loop() -> None:
            interval = max(manager.cache_warmer.interval_hours, 0.5) * 3600
            while not _cache_warm_stop.is_set():
                try:
                    await asyncio.wait_for(_cache_warm_stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    try:
                        warmer = manager.cache_warmer
                        if warmer.enabled:
                            hit = (manager.usage_store.totals() or {}).get(
                                "cache_hit_rate", 1.0
                            )
                            if hit < warmer.min_hit_rate:
                                await manager.cache_warm_now()
                    except Exception:
                        import traceback

                        traceback.print_exc()

        warm_task = asyncio.create_task(_cache_warm_loop())
        # P1 记忆维护 (GuaAgent/OpenClaw 文档): 自动去重合并 + 衰减遗忘 —
        # 低频周期 (24h) 后台整理 memories + vector_memories, 保持记忆库
        # 不膨胀、冷记忆自动降级。仿 _hornet_observer_loop 的 to_thread 模式。
        _memory_loop_stop = asyncio.Event()

        async def _memory_maintenance_loop() -> None:
            await asyncio.sleep(30.0)  # let manager finish wiring + first boot settle
            while not _memory_loop_stop.is_set():
                try:
                    await asyncio.wait_for(
                        _memory_loop_stop.wait(), timeout=24 * 3600
                    )
                except asyncio.TimeoutError:
                    try:
                        summary = await asyncio.to_thread(
                            manager.memory_maintenance
                        )
                        removed = (
                            len(summary.get("memories_dedupe", {}).get("removed", []))
                            + len(summary.get("vector_dedupe", {}).get("removed", []))
                        )
                        stale = (
                            len(summary.get("memories_decay", {}).get("stale", []))
                            + len(summary.get("vector_decay", {}).get("stale", []))
                        )
                        expired = (
                            len(
                                summary.get("memories_consolidate", {}).get(
                                    "expired", []
                                )
                            )
                            + len(
                                summary.get("vector_consolidate", {}).get(
                                    "expired", []
                                )
                            )
                        )
                        if removed or stale or expired:
                            print(
                                f"[coworker] memory maintenance: "
                                f"merged/removed {removed}, decayed {stale}, "
                                f"ttl-expired {expired}"
                            )
                    except Exception:
                        pass

        memory_task = asyncio.create_task(_memory_maintenance_loop())
        try:
            live = (
                await manager.start_gateway()
            )  # start messaging listeners (if configured)
            if live:
                print(f"[coworker] messaging gateway live: {', '.join(live)}")
        except Exception:  # never let a bad connector stop the server
            import traceback

            traceback.print_exc()
        yield
        _hornet_loop_stop.set()
        task.cancel()
        _cache_warm_stop.set()
        warm_task.cancel()
        _memory_loop_stop.set()
        memory_task.cancel()
        await manager.aclose()  # stop gateway + close MCP connections on shutdown

    app = FastAPI(title="coworker", version="0.0.0", lifespan=lifespan)
    api_token = os.environ.get("COWORKER_API_TOKEN", "")
    tokenless_paths = {
        "/v1/health",
        "/auth/callback",
        "/mcp/oauth/callback",
        "/oauth/callback",
        # P2P 同步: peer 之间互调(outbox 拉取 / ingest 推送) —
        # 数据本身 AES-GCM 加密 + Ed25519 签名, 传输面无需 API token。
        "/v1/team/sync/outbox",
        "/v1/team/sync/ingest",
    }

    def _request_authenticated(request: Request) -> bool:
        provided = request.headers.get("x-qunwork-token", "")
        return bool(
            api_token
            and provided
            and secrets.compare_digest(provided, api_token)
        )

    def _websocket_authenticated(ws: WebSocket) -> bool:
        if not api_token:
            return True
        protocols = {
            part.strip()
            for part in ws.headers.get("sec-websocket-protocol", "").split(",")
            if part.strip()
        }
        return any(secrets.compare_digest(part, api_token) for part in protocols)

    @app.middleware("http")
    async def require_sidecar_token(request: Request, call_next):
        # Preflights carry the requested header name, not its value. CORS checks the
        # Origin; the actual state-changing request still must authenticate.
        if (
            not api_token
            or request.method == "OPTIONS"
            or request.url.path in tokenless_paths
            or _request_authenticated(request)
        ):
            return await call_next(request)
        return JSONResponse(
            {"error": "missing or invalid QunWork sidecar token"},
            status_code=401,
        )

    app.add_middleware(
        CORSMiddleware,
        # Pinned to the desktop webview + localhost (see _ALLOWED_ORIGIN_RE): stops a random
        # website the user visits from reading local API responses cross-origin.
        allow_origin_regex=_ALLOWED_ORIGIN_RE.pattern,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.manager = manager

    @app.get("/v1/health")
    def health(request: Request) -> dict[str, Any]:
        if api_token and not _request_authenticated(request):
            return {"status": "ok"}
        return {
            "status": "ok",
            "default_workspace": manager.default_workspace,
            "model": manager.model,
        }

    # -- local web search (DuckDuckGo + Bing, zero-cost) ----------------------
    from ..tools.local_search import SearchCache, local_search as _local_search
    from ..secrets import state_dir as _state_dir

    _search_cache = SearchCache(_state_dir() / "search_cache.db")

    @app.post("/v1/local-search")
    async def local_search_endpoint(body: dict) -> dict[str, Any]:
        """Local web search: DuckDuckGo + Bing parallel, SQLite cache, 24h TTL.
        Zero API key cost. Replaces LLM-based web search for simple queries."""
        query = str(body.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query is required"}
        max_results = min(int(body.get("max_results") or 10), 20)
        return _local_search(query, max_results=max_results, cache=_search_cache)

    @app.get("/v1/local-search/stats")
    def local_search_stats() -> dict[str, Any]:
        return {"ok": True, "stats": _search_cache.stats()}

    @app.get("/v1/agents")
    def agents() -> dict[str, Any]:
        return {"agents": manager.list_agents()}

    @app.post("/v1/orchestrate")
    async def orchestrate(body: dict, request: Request) -> dict[str, Any]:
        """Delegate a whole multi-step goal to the multi-agent swarm (planner ->
        executors -> reviewer, governed). Executor writes are approval-gated to
        the Inbox. Default is ASYNC: returns a run_id immediately and streams
        progress events to the run store (poll GET /v1/orchestrate/{run_id}).
        Pass {"sync": true} for the blocking variant that returns the full result.
        """
        from ..orchestrator import Orchestrator

        intent = str(body.get("intent") or "").strip()
        if not intent:
            return {"ok": False, "error": "intent is required"}
        workspace = body.get("workspace") or manager.default_workspace
        if not workspace:
            return {
                "ok": False,
                "error": "no workspace configured — open a project folder first, or pass workspace",
            }
        session_id = f"__orchestrate__{secrets.token_hex(4)}"
        store = manager.orchestration_store
        # P0 建议3 (保留分支 A/B): fork_of = the parent run whose plan_ready we copy;
        # fork_inject = one extra task appended to the fork (the deck's "fork a
        # sub-task" flows through this path when the parent has finished).
        fork_of = str(body.get("fork_of") or "")
        fork_inject = body.get("fork_inject")
        run_id = store.create_run(intent, parent_run_id=fork_of or None)
        sync = bool(body.get("sync"))

        def _auto_write_coordination_report(rid: str) -> str | None:
            """Strategy report 5.2.4: every completed swarm run gets its
            coordination report written automatically, not only on button press."""
            from ..orchestrator.coordination_report import render_coordination_report

            run = store.get_run(rid)
            if not run:
                return None
            try:
                md = render_coordination_report(run)
                base = Path(manager.default_workspace or ".")
                base.mkdir(parents=True, exist_ok=True)
                out = base / f"coordination-report-{rid}.md"
                out.write_text(md, encoding="utf-8")
                return str(out)
            except Exception:
                return None

        def _ingest_swarm_assets(rid: str, final_report: str, ws: str) -> None:
            """Asset loop (Phase 1): a completed swarm run sinks its deliverable
            into the unified knowledge library (kind=swarm_report) and tallies
            the originating template's track record automatically.

            产物无损 (S3/S4 修复): 完整入库 (不截断 report) + 提取真实标题
            (首个 '# ' 标题行), 解决"蜂群报告 orch_xxx 标题泛化、内容被截断
            导致知识库搜不到/搜到也残缺"。
            """
            try:
                report = (final_report or "").strip()
                if report and len(report) > 40:
                    # 提取真实标题: 首个 '# ' 或 '## ' 标题行 (去 Markdown 标记)
                    title = f"蜂群报告 {rid[:8]}"
                    for line in report.splitlines():
                        s = line.strip()
                        if s.startswith("# ") and len(s) > 2:
                            title = s[2:].strip()[:120]
                            break
                    manager.knowledge.add_text(
                        title=title,
                        # 完整入库 — 报告是正式资产, 绝不截断 (产物无损原则)。
                        content=report,
                        kind="swarm_report",
                        workspace=ws,
                        source_run_id=rid,
                        # Research-relay: if the run was launched from a knowledge
                        # entry ("continue research"), link the report back to it.
                        parent_id=_extract_parent_id((body or {}).get("intent") or ""),
                    )
            except Exception:
                pass  # ingestion must never fail the run
            try:
                template_id = body.get("template_id")
                if template_id:
                    manager.record_swarm_template_run(
                        int(template_id), result.status == "completed"
                    )
            except (TypeError, ValueError):
                pass
        from ..orchestrator.control import RunController

        controller = RunController()
        manager.active_orchestration_controls[run_id] = controller

        # Phase 3 governance feedback: reusing a template with a poor track
        # record warns before the run starts (the asset's own history pre-judges
        # its risk), visible in the deck's governance report.
        try:
            template_id = body.get("template_id")
            if template_id:
                for t in manager.list_swarm_templates():
                    if t.get("id") == int(template_id) and (t.get("runs_count") or 0) >= 2:
                        rate = (t.get("success_count") or 0) / t.get("runs_count")
                        if rate < 0.5:
                            store.append_event(
                                run_id,
                                "governance",
                                {
                                    "step": 0,
                                    "action": "WARN",
                                    "reason": (
                                        f"template '{t.get('title')}' track record is weak "
                                        f"({t.get('success_count')}/{t.get('runs_count')} ok) — expect rework"
                                    ),
                                },
                            )
                        break
        except (TypeError, ValueError):
            pass

        # P0 建议3: fork runs start from the parent's plan_ready snapshot instead
        # of re-planning (a true A/B needs the SAME task graph, different run).
        initial_plan = None
        if fork_of:
            try:
                from ..orchestrator import Plan, Task

                parent = store.get_run(fork_of)
                snapshot = None
                if parent:
                    for ev in parent.get("events", []):
                        if ev.get("kind") == "plan_ready":
                            snapshot = ev.get("payload", {}).get("tasks") or []
                            break
                if snapshot:
                    initial_plan = Plan(
                        goal=intent,
                        tasks=[
                            Task(
                                id=str(t.get("id")),
                                description=str(t.get("description") or ""),
                                deps=list(t.get("deps") or []),
                                agent=str(t.get("agent") or ""),
                            )
                            for t in snapshot
                        ],
                    )
                    if isinstance(fork_inject, dict) and fork_inject.get("id"):
                        initial_plan.tasks.append(
                            Task(
                                id=str(fork_inject["id"]),
                                description=str(fork_inject.get("description") or ""),
                                deps=list(fork_inject.get("deps") or []),
                                agent=str(fork_inject.get("agent") or ""),
                            )
                        )
            except Exception:
                initial_plan = None  # a broken snapshot falls back to fresh planning

        def _build() -> "Orchestrator":
            # Refine 机制 (自进化闭环): GUI 蜂群 run 自动挂载 workspace 级经验库
            # (.qunwork/harness.db) — 规划注入历史经验, run 后蒸馏新经验。
            from ..orchestrator.harness import HarnessStore

            _harness = HarnessStore(Path(workspace) / ".qunwork")
            orch = Orchestrator(
                provider=manager.provider,
                model=body.get("model") or manager.model,
                workspace=workspace,
                initial_plan=initial_plan,
                # Auto-approve worker writes: running the swarm is the authorization.
                # (Inbox gating would deadlock headless workers waiting for clicks.)
                max_parallel=int(body.get("max_parallel") or 4),
                timeout_seconds=(
                    int(body["timeout_seconds"])
                    if body.get("timeout_seconds")
                    else 300
                ),
                memory_scope=body.get("memory_scope") or str(workspace),
                # Interconnect: thread the team memory store + unified knowledge
                # DB into every worker engine (asset loop — Phase 1).
                memory_store=manager.memory_store,
                knowledge_db_path=(
                    str(manager._knowledge_db_path)
                    if manager._data_base is not None
                    else None
                ),
                hornet_resonator=manager._hornet_resonator,
                harness=_harness,
                # S10: 治理命令写入持久化审计 (manager.audit_store), 可追溯。
                audit_sink=manager.audit_store.append,
                event_sink=lambda kind, payload: store.append_event(run_id, kind, payload),
                # G2: command deck wiring (pause/resume/message/requeue approval).
                controller=controller,
                requeue_approval_timeout=float(
                    body.get("requeue_approval_timeout") or 30.0
                ),
                # Engineering-style executor (code persona) for programming tasks;
                # 'cowork' (default) is the generalist. Validated in _build().
                executor_agent=(
                    str(body["executor_agent"])
                    if body.get("executor_agent") in ("cowork", "code")
                    else "cowork"
                ),
                # P0 增量1: shared stigmergic load field — batch sizes adapt to it.
                pheromone=manager.pheromone,
                # QunMesh M2: 邻域感知批选择开关 (body 优先, prefs 兜底)。
                mesh_scheduling=bool(
                    body.get("mesh_scheduling")
                    if body.get("mesh_scheduling") is not None
                    else (getattr(manager, "_prefs", {}) or {}).get("mesh_scheduling", False)
                ),
                # QunMesh M3: 就近评审 + swarm_bft (mesh_review) / 动态领取 (mesh_claim)。
                mesh_review=bool(
                    body.get("mesh_review")
                    if body.get("mesh_review") is not None
                    else (getattr(manager, "_prefs", {}) or {}).get("mesh_review", False)
                ),
                mesh_claim=bool(
                    body.get("mesh_claim")
                    if body.get("mesh_claim") is not None
                    else (getattr(manager, "_prefs", {}) or {}).get("mesh_claim", False)
                ),
                # QunMesh M4: mesh_mode 四档总开关 (off/serial/hybrid/full),
                # 与显式 bool 开关取或; full 启用每轮 λ₂ 拓扑遥测。
                mesh_mode=str(
                    body.get("mesh_mode")
                    or (getattr(manager, "_prefs", {}) or {}).get("mesh_mode", "off")
                    or "off"
                ),
                # P1 增量: optional agent pool + task group id.
                agent_pool=getattr(manager, "agent_pool", None),
                task_group_id=body.get("task_group_id") or None,
            )
            orch._harness_ref = _harness  # type: ignore[attr-defined]  # close after run
            return orch

        async def _finalize(orch: "Orchestrator") -> dict[str, Any]:
            try:
                result = await orch.run(intent)
                # final = the consolidation task's full output (the finished report)
                # when available, else the assembled task summary.
                last_done = [t for t in result.plan.tasks if t.done]
                final_report = (
                    last_done[-1].result
                    if last_done and last_done[-1].result
                    else result.summary
                )
                store.update_status(run_id, result.status, final=final_report)
                if result.status == "completed":
                    _auto_write_coordination_report(run_id)
                    _ingest_swarm_assets(run_id, final_report, workspace)
                return {
                    "ok": True,
                    "run_id": run_id,
                    "status": result.status,
                    "runs": result.runs,
                    "report_path": result.report_path,
                    "tasks": [
                        {
                            "id": t.id,
                            "description": t.description,
                            "deps": t.deps,
                            "agent": t.agent,
                            "status": t.status,
                            "confidence": t.confidence,
                            "result": (t.result or "")[:2000],
                        }
                        for t in result.plan.tasks
                    ],
                    "governance_report": result.governance_report,
                    "session_id": session_id,
                }
            except Exception as exc:  # surface failures via the run store
                # Do NOT clobber a genuinely completed run: ingestion/return
                # errors after success must not flip it to failed (observed:
                # report written + sunk, but status flipped to failed).
                try:
                    cur = store.get_run(run_id)
                    already = (cur or {}).get("status")
                except Exception:
                    already = None
                if already != "completed":
                    store.update_status(run_id, "failed", str(exc))
                store.append_event(run_id, "orchestration_error", {"error": str(exc)})
                return {"ok": False, "run_id": run_id, "error": str(exc)}
            finally:
                manager.active_orchestration_controls.pop(run_id, None)
                # Refine 机制: run 结束后关闭 harness 连接 (蒸馏已完成)。
                _h = getattr(orch, "_harness_ref", None)
                if _h is not None:
                    try:
                        _h.close()
                    except Exception:
                        pass
        if sync:
            return await _finalize(_build())
        import asyncio

        asyncio.create_task(_finalize(_build()))
        return {"ok": True, "run_id": run_id, "async": True}

    @app.get("/v1/orchestrate/history")
    def orchestrate_history() -> dict[str, Any]:
        return {"runs": manager.orchestration_store.list_runs(limit=50)}

    @app.post("/v1/orchestrate/{run_id}/dissolve")
    def orchestrate_dissolve(run_id: str) -> dict[str, Any]:
        """P0 增量2 (任务组生命周期): dissolve a FINISHED run — the explicit
        '解散蜂群,回收资源' step of the swarm group lifecycle. Marks the run
        dissolved (terminal), records a run_dissolved event, and refuses while
        the run is still active (pause it first, then dissolve)."""
        store = manager.orchestration_store
        run = store.get_run(run_id)
        if not run:
            return {"ok": False, "error": "run not found"}
        if run_id in manager.active_orchestration_controls:
            return {
                "ok": False,
                "error": "run is still active — pause it before dissolving",
            }
        if run.get("status") == "dissolved":
            return {"ok": True, "already": True, "run_id": run_id}
        store.append_event(run_id, "run_dissolved", {"by": "operator"})
        store.update_status(run_id, "dissolved")
        return {"ok": True, "run_id": run_id}

    @app.get("/v1/orchestrate/{run_id}/report")
    def orchestrate_report(run_id: str) -> dict[str, Any]:
        """Coordination report (benchmark showcase): render the run's event stream
        into a 'swarm narrative' Markdown deliverable."""
        from ..orchestrator.coordination_report import (
            coordination_report_summary,
            render_coordination_report,
        )

        run = manager.orchestration_store.get_run(run_id)
        if not run:
            return {"ok": False, "error": "run not found"}
        md = render_coordination_report(run)
        # persist alongside the run's own deliverable so it survives restarts and
        # is openable from the Artifacts panel (session workspace / parent).
        try:
            from pathlib import Path

            base = Path(manager.default_workspace or ".")
            base.mkdir(parents=True, exist_ok=True)
            out = base / f"coordination-report-{run_id}.md"
            out.write_text(md, encoding="utf-8")
            return {
                **coordination_report_summary(run),
                "markdown": md,
                "report_path": str(out),
            }
        except OSError as exc:
            return {**coordination_report_summary(run), "markdown": md, "error": str(exc)}

    @app.get("/v1/orchestrate/{run_id}")
    def orchestrate_run(run_id: str) -> dict[str, Any]:
        run = manager.orchestration_store.get_run(run_id)
        if not run:
            return {"ok": False, "error": "run not found"}
        return {"ok": True, **run}

    @app.get("/v1/orchestrate/{run_id}/control")
    def orchestrate_control_status(run_id: str) -> dict[str, Any]:
        """G2 command-deck state: paused flag + pending requeue approvals."""
        ctrl = manager.active_orchestration_controls.get(run_id)
        if ctrl is None:
            return {"ok": False, "error": "run not active"}
        return {
            "ok": True,
            "run_id": run_id,
            "paused": ctrl.paused,
            "requeues": ctrl.pending_requeues(),
        }

    @app.post("/v1/orchestrate/{run_id}/control")
    def orchestrate_control(run_id: str, body: dict) -> dict[str, Any]:
        """G2 command deck actions: pause | resume | message | requeue_approve |
        requeue_reject."""
        action = str(body.get("action") or "")
        ctrl = manager.active_orchestration_controls.get(run_id)
        if ctrl is None:
            return {"ok": False, "error": "run not active or already finished"}
        store = manager.orchestration_store
        if action == "pause":
            ctrl.set_paused(True)
            store.append_event(run_id, "operator_paused", {})
            return {"ok": True, "paused": True}
        if action == "resume":
            ctrl.set_paused(False)
            store.append_event(run_id, "operator_resumed", {})
            return {"ok": True, "paused": False}
        if action == "message":
            text = str(body.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "text is required"}
            ctrl.inject_message(text)
            store.append_event(run_id, "operator_message", {"text": text})
            return {"ok": True, "delivered": True}
        if action == "requeue_approve":
            task_id = str(body.get("task_id") or "")
            ok = ctrl.approve_requeue(task_id)
            store.append_event(run_id, "task_requeue_approved", {"id": task_id})
            return {"ok": ok, "task_id": task_id}
        if action == "requeue_reject":
            task_id = str(body.get("task_id") or "")
            ok = ctrl.reject_requeue(task_id)
            store.append_event(run_id, "task_requeue_declined", {"id": task_id})
            return {"ok": ok, "task_id": task_id}
        # P0 建议3 (蜂群指挥台): DAG edits at runtime.
        if action == "task_inject":
            tid = str(body.get("task_id") or "").strip()
            desc = str(body.get("description") or "").strip()
            if not tid or not desc:
                return {"ok": False, "error": "task_id + description are required"}
            deps = [str(d) for d in (body.get("deps") or [])]
            agent = str(body.get("agent") or "")
            ctrl.inject_task(id=tid, description=desc, deps=deps, agent=agent)
            store.append_event(
                run_id, "task_injected", {"id": tid, "description": desc, "deps": deps, "agent": agent}
            )
            return {"ok": True, "task_id": tid}
        if action == "retarget":
            tid = str(body.get("task_id") or "")
            agent = str(body.get("agent") or "")
            if not tid or agent not in ("cowork", "code"):
                return {"ok": False, "error": "task_id + agent (cowork|code) are required"}
            ctrl.retarget_task(tid, agent)
            store.append_event(run_id, "task_retargeted", {"id": tid, "agent": agent})
            return {"ok": True, "task_id": tid, "agent": agent}
        return {"ok": False, "error": f"unknown action: {action}"}

    @app.get("/v1/personas")
    def personas() -> dict[str, Any]:
        return {"personas": manager.personas.list_all()}

    @app.get("/v1/inbox")
    def inbox(session_id: str = "", state: str = "") -> dict[str, Any]:
        from dataclasses import asdict

        # The cross-session Inbox list shows only Unattended (inbox-visibility) items; a per-session
        # query returns inline ones too, so the answer-in-context card sees parked attended prompts.
        items = manager.inbox.list(
            session_id=session_id or None,
            state=state or None,
            visibility=None if session_id else VIS_INBOX,
        )
        # Enrich with the originating session's context so the Inbox is self-contained — the
        # "go to session" chip needs title/agent/workspace without depending on a (possibly stale)
        # client-side session list, and can link straight to it.
        out: list[dict[str, Any]] = []
        for i in items:
            d = asdict(i)
            rec = manager.session_store.load(i.session_id)
            if (
                rec is None
                and not session_id
                and i.state == "pending"
                and i.session_id not in manager._engines
            ):
                # Lazy cleanup for legacy orphans (sessions deleted before delete_session
                # started closing their items): an orphaned prompt can never be answered.
                # A LIVE engine without a record yet (brand-new session, first turn still
                # running) is NOT an orphan — hence the engine guard.
                manager.inbox.resolve_session(i.session_id)
                continue
            d["session_title"] = (rec.title if rec else None) or i.session_id
            d["session_agent"] = rec.agent if rec else None
            d["session_workspace"] = rec.workspace if rec else None
            d["session_exists"] = rec is not None
            out.append(d)
        return {"items": out}

    @app.post("/v1/inbox/{item_id}/resolve")
    async def resolve_inbox_item(item_id: str, body: dict) -> dict[str, Any]:
        # Idempotent + first-responder-wins: ok=False means it was already resolved elsewhere.
        # Routes through resolve_inbox so a restart-orphaned prompt durably resumes its turn.
        ok = await manager.resolve_inbox(item_id, str(body.get("resolution", "deny")))
        return {"ok": ok}

    # -- P0 建议2: Telegram Mini App -------------------------------------------
    @app.get("/v1/telegram/webapp_url")
    def telegram_webapp_url(init_data: str = "") -> dict[str, Any]:
        """Validate a Telegram WebApp initData and issue a one-time page ticket."""
        return manager.telegram_webapp_ticket(init_data)

    @app.get("/v1/telegram/webapp/verify")
    def telegram_webapp_verify(ticket: str = "") -> dict[str, Any]:
        """Consume a page ticket (the mini app calls this once on load)."""
        return manager.telegram_webapp_verify(ticket)

    @app.get("/v1/telegram/webapp")
    def telegram_webapp(ticket: str = ""):
        """The Mini App approval page — a self-contained page: verifies its ticket,
        lists pending Inbox items, and resolves them via the normal REST API. Data
        never lands on Telegram servers; it is only ever this local page."""
        from fastapi.responses import HTMLResponse

        return HTMLResponse(
            f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QunWork · Inbox</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 560px; padding: 16px; background:#0f1115; color:#e6e6e6; }}
 h1 {{ font-size: 18px; }}
 .item {{ border:1px solid #2a2f3a; border-radius:10px; padding:12px 14px; margin:10px 0; background:#161a22; }}
 .title {{ font-weight:600; }}
 .meta {{ color:#8b93a7; font-size:12.5px; margin:4px 0 8px; }}
 .body {{ white-space:pre-wrap; color:#c9cede; font-size:13.5px; }}
 .btns {{ display:flex; gap:8px; margin-top:10px; }}
 button {{ flex:1; padding:8px; border-radius:8px; border:1px solid #2a2f3a; background:#20242e; color:#e6e6e6; font-size:13.5px; cursor:pointer; }}
 button:active {{ transform:translateY(1px); }}
 button.approve {{ border-color:#2e7d4f; color:#7ee2a8; }}
 button.deny {{ border-color:#a04040; color:#f0a0a0; }}
 .done {{ color:#7ee2a8; font-size:12.5px; margin-top:8px; }}
 .empty {{ color:#8b93a7; }}
 .err {{ color:#f0a0a0; }}
</style></head><body>
<h1>🐝 QunWork · Inbox</h1>
<div id="root"><div class="empty">Loading…</div></div>
<script>
 const ticket = {json.dumps(ticket).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")};
 async function resolve(id, r) {{
   await fetch('/v1/inbox/' + encodeURIComponent(id) + '/resolve', {{
     method: 'POST', headers: {{'Content-Type': 'application/json'}},
     body: JSON.stringify({{resolution: r}})
   }});
   load();
 }}
 async function load() {{
   const root = document.getElementById('root');
   try {{
     const v = await (await fetch('/v1/telegram/webapp/verify?ticket=' + encodeURIComponent(ticket))).json();
     if (!v.ok) {{ root.innerHTML = '<div class="err">Invalid or expired link — open it from the bot again.</div>'; return; }}
     const d = await (await fetch('/v1/inbox')).json();
     const pending = (d.items || []).filter(i => i.state === 'pending');
     if (pending.length === 0) {{ root.innerHTML = '<div class="empty">No pending prompts. 🎉</div>'; return; }}
     root.innerHTML = '';
     for (const i of pending) {{
       const el = document.createElement('div');
       el.className = 'item';
       el.innerHTML =
         '<div class="title"></div><div class="meta"></div><div class="body"></div>' +
         '<div class="btns"><button class="approve">Approve</button><button class="deny">Deny</button></div>' +
         '<div class="done"></div>';
       el.querySelector('.title').textContent = i.title || 'Prompt';
       el.querySelector('.meta').textContent = (i.kind || '') + ' · ' + (i.session_title || '');
       el.querySelector('.body').textContent = i.body || '';
       el.querySelector('.approve').onclick = () => resolve(i.id, 'allow');
       el.querySelector('.deny').onclick = () => resolve(i.id, 'deny');
       root.appendChild(el);
     }}
   }} catch (e) {{ root.innerHTML = '<div class="err">Failed to load.</div>'; }}
 }}
 load();
</script></body></html>""",
            status_code=200,
        )

    @app.get("/v1/subscriptions")
    def subscriptions() -> dict[str, Any]:
        # Global view-only list: each (session → channel) subscription, enriched with the session's
        # title/agent and the channel its Inbox routes OUT to (so an inbound/outbound collision on
        # the same channel is visible).
        out: list[dict[str, Any]] = []
        for sub in manager.subscriptions.all():
            rec = manager.session_store.load(sub.session_id)
            agent = rec.agent if rec else ""
            routing = manager._routing_targets(sub.session_id, agent or "cowork")
            out.append(
                {
                    "session_id": sub.session_id,
                    "session_title": (rec.title if rec else None) or sub.session_id,
                    "agent": agent,
                    "channel": sub.channel,
                    # Display name from the channel buffer ("#ocw-test"), when any inbound
                    # message has carried one — the address stays the identifier.
                    "channel_name": manager.channel_buffer.name_for(sub.channel),
                    "routing_target": routing[0] if routing else None,
                    "collision": bool(routing and sub.channel in routing),
                }
            )
        return {"subscriptions": out}

    @app.get("/v1/channels/recent")
    def recent_channels() -> dict[str, Any]:
        # The picker's "recently-seen" source: channels the bot has received messages from.
        return {"channels": manager.channel_buffer.channels()}

    @app.get("/v1/unrouted")
    def unrouted() -> dict[str, Any]:
        # Dead-letter view: inbound messages with no destination + background-turn failures.
        return {"items": manager.unrouted.list()}

    @app.post("/v1/subscriptions")
    def subscribe(body: dict) -> dict[str, Any]:
        from ..subscriptions import resolve_channel

        session_id = str(body.get("session_id", "")).strip()
        raw = str(body.get("channel", ""))
        addr = resolve_channel(raw)
        if not session_id or not addr or ":" not in addr:
            if raw.strip().startswith("#"):
                # A bare #name can't be looked up locally — storing it literally would create a
                # subscription that never matches real traffic (resolve_channel returns "").
                return {
                    "ok": False,
                    "error": "Channel names can't be looked up — paste the channel ID "
                    "(channel name ▸ About) or the channel's Copy-link URL.",
                }
            return {"ok": False, "error": "need a session_id and a channel"}
        manager.subscriptions.subscribe(session_id, addr)
        return {"ok": True, "channel": addr}

    @app.post("/v1/subscriptions/remove")
    def unsubscribe(body: dict) -> dict[str, Any]:
        from ..subscriptions import resolve_channel

        session_id = str(body.get("session_id", "")).strip()
        addr = resolve_channel(str(body.get("channel", "")))
        removed = manager.subscriptions.unsubscribe(session_id, addr)
        return {"ok": True, "removed": removed}

    @app.get("/v1/inbox/reconcile")
    def reconcile_inbox(session_id: str) -> dict[str, Any]:
        # Called when a session resumes attended control (surface pending + recap inline).
        return manager.inbox.reconcile_on_resume(session_id)

    @app.get("/v1/inbox/routing")
    def inbox_routing() -> dict[str, Any]:
        return {"bindings": manager.inbox_routing.bindings()}

    @app.post("/v1/inbox/routing/binding")
    def set_inbox_binding(body: dict) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        if not name:
            return {"ok": False, "error": "binding needs a `name`"}
        manager.inbox_routing.set_binding(
            name,
            channel=body.get("channel") or None,
            target=str(body.get("target", "")),
        )
        return {"ok": True, "bindings": manager.inbox_routing.bindings()}

    @app.get("/v1/sessions/{session_id}/unattended")
    def get_unattended(session_id: str) -> dict[str, Any]:
        return {"unattended": manager.unattended.is_unattended(session_id)}

    @app.post("/v1/sessions/{session_id}/unattended")
    def set_unattended(session_id: str, body: dict) -> dict[str, Any]:
        # The GUI gates the on-transition behind a one-tap confirm.
        on = bool(body.get("unattended"))
        manager.unattended.set(session_id, on)
        return {"ok": True, "session_id": session_id, "unattended": on}

    @app.get("/v1/sessions/{session_id}/connections")
    def session_connections(session_id: str, persona: str = "") -> dict[str, Any]:
        # `persona` is the GUI's hint for brand-new sessions (no record yet) — without it the
        # view resolves to the default persona and shows the wrong defaults/recommends.
        # §6: the Sources drawer payload — connected connectors w/ state + recommended + ⚠ count.
        return manager.session_connections_view(session_id, persona or None)

    @app.post("/v1/sessions/{session_id}/connections")
    def set_session_connection(session_id: str, body: dict) -> dict[str, Any]:
        # §6: a session override. `clear` drops the override (inherit the persona default again);
        # otherwise set an explicit on/off. Return the refreshed view so the drawer can re-render.
        body = body or {}
        connector = str(body.get("connector", "")).strip()
        if not connector:
            return {"ok": False, "error": "connector required"}
        if body.get("clear"):
            manager.session_connections.clear(session_id, connector)
        else:
            manager.session_connections.set(
                session_id, connector, bool(body.get("enabled", False))
            )
        persona = str(body.get("persona", "")) or None
        return {
            "ok": True,
            "connections": manager.session_connections_view(session_id, persona),
        }

    @app.post("/v1/personas/install")
    def install_persona(body: dict) -> dict[str, Any]:
        # Returns a consent summary per persona; they land disabled pending the user's approval
        # (then POST /v1/personas/{id} {enabled:true, surfaced:true}).
        reg = manager.personas
        try:
            if body.get("git_url"):
                summaries = reg.install_from_git(str(body["git_url"]))
            elif body.get("dir"):
                summaries = reg.install_from_dir(str(body["dir"]))
            elif body.get("gallery_slug"):
                # Gallery install = fetch the manifest markdown from the cloud
                # (sign-in required), verify its hash, then reuse the exact
                # same parser + consent path as a local/Git install. The
                # gallery never changes the trust model: no executable code,
                # lands disabled pending consent.
                import hashlib
                import tempfile

                from .. import cloud
                from ..config import load_config

                slug = str(body["gallery_slug"]).strip()
                manifest = cloud.gallery_manifest(manager.secrets, load_config(), slug)
                if manifest is None:
                    return {
                        "ok": False,
                        "error": "gallery requires cloud sign-in (or the cloud is unreachable)",
                    }
                markdown = manifest.get("manifest_markdown", "")
                digest = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
                if (
                    manifest.get("manifest_hash")
                    and manifest["manifest_hash"] != digest
                ):
                    return {"ok": False, "error": "manifest hash mismatch"}
                with tempfile.TemporaryDirectory() as td:
                    (Path(td) / f"{slug}.md").write_text(markdown)
                    summaries = reg.install_from_dir(td)
                cloud.gallery_install_event(manager.secrets, load_config(), slug)
            else:
                return {
                    "ok": False,
                    "error": "provide a `dir`, `git_url`, or `gallery_slug`",
                }
        except Exception as e:  # surface manifest/clone errors to the caller
            return {"ok": False, "error": str(e)}
        return {"ok": True, "consent": summaries, "personas": reg.list_all()}

    @app.get("/v1/cloud/gallery/{slug}")
    def cloud_gallery_detail(slug: str) -> dict[str, Any]:
        """Solo page for one gallery coworker: publisher pitch + capabilities
        derived locally from the manifest (same parser as install)."""
        from .. import cloud
        from ..config import load_config

        body = cloud.gallery_detail(manager.secrets, load_config(), slug)
        if body is None:
            return {"ok": False, "error": "gallery requires cloud sign-in"}
        return body

    @app.get("/v1/cloud/gallery")
    def cloud_gallery() -> dict[str, Any]:
        """Gallery cards for the GUI. Signed out ⇒ ok:false (the gallery is a
        signed-in feature by design; local personas are unaffected)."""
        from .. import cloud
        from ..config import load_config

        body = cloud.gallery_list(manager.secrets, load_config())
        if body is None:
            return {
                "ok": False,
                "error": "gallery requires cloud sign-in",
                "personas": [],
            }
        return {"ok": True, "personas": body.get("personas", [])}

    @app.post("/v1/personas/{persona_id}")
    def update_persona(persona_id: str, body: dict) -> dict[str, Any]:
        reg = manager.personas
        archived = 0
        try:
            if "enabled" in body:
                # Disable archives the persona's sessions atomically (server-side, one
                # request) so any client gets the same semantic. See set_persona_enabled.
                archived = manager.set_persona_enabled(
                    persona_id, bool(body["enabled"])
                )["archived_sessions"]
            if "surfaced" in body:
                reg.set_surfaced(persona_id, bool(body["surfaced"]))
            if body.get("default"):
                reg.set_default(persona_id)
        except KeyError:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        return {"ok": True, "personas": reg.list_all(), "archived_sessions": archived}

    @app.delete("/v1/personas/{persona_id}")
    def persona_delete(persona_id: str) -> dict[str, Any]:
        # Uninstall a non-builtin persona (snapshot dir + lifecycle state). Local
        # operation — works signed out, regardless of where the persona came from.
        try:
            manager.personas.uninstall(persona_id)
        except KeyError:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "personas": manager.personas.list_all()}

    @app.get("/v1/personas/{persona_id}")
    def persona_detail(persona_id: str) -> dict[str, Any]:
        # §5 detail page: identity + capabilities + recommends(+connected) + default connections.
        detail = manager.persona_detail(persona_id)
        if detail is None:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        return detail

    @app.post("/v1/personas/{persona_id}/enable")
    def persona_enable(persona_id: str, body: dict) -> dict[str, Any]:
        # Dedicated §5/§8 route; delegates to the same manager toggle as POST /v1/personas/{id}
        # (so disable archives the persona's sessions here too).
        try:
            manager.set_persona_enabled(
                persona_id, bool((body or {}).get("enabled", True))
            )
        except KeyError:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        return {"ok": True, "personas": manager.personas.list_all()}

    @app.post("/v1/personas/{persona_id}/connections")
    def persona_set_connection(persona_id: str, body: dict) -> dict[str, Any]:
        # §5: flip a persona-default connector on/off; re-reads so the client can refresh.
        body = body or {}
        connector = str(body.get("connector", "")).strip()
        if not connector:
            return {"ok": False, "error": "connector required"}
        return manager.set_persona_connection(
            persona_id, connector, bool(body.get("enabled", False))
        )

    @app.get("/v1/skills/path")
    def skills_get_path() -> dict[str, Any]:
        """Get the current user skills directory path."""
        return manager.get_skills_path()

    @app.post("/v1/skills/path")
    def skills_set_path(body: dict) -> dict[str, Any]:
        """Set a new user skills directory. Optionally migrate existing skills.

        body:
            path: str  — new directory
            migrate: bool = False  — move existing skills to new location
        """
        new_path = str(body.get("path") or "").strip()
        if not new_path:
            return {"ok": False, "error": "path is required"}
        migrate = bool(body.get("migrate", False))
        return manager.set_skills_path(new_path, migrate=migrate)

    @app.get("/v1/skills")
    def skills() -> dict[str, Any]:
        return {"skills": manager.list_skills()}

    @app.get("/v1/skills/{name}")
    def skill_detail(name: str) -> dict[str, Any]:
        row = manager.skill_detail(name)
        if row is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        return {"ok": True, **row}

    @app.post("/v1/skills/import")
    def skill_import(body: dict) -> dict[str, Any]:
        from ..skills.base import SkillLoader as _SL

        zip_b64 = body.get("zip_base64") or ""
        if not zip_b64:
            return {"ok": False, "error": "zip_base64 is required"}
        import base64
        import tempfile
        import zipfile
        from pathlib import Path

        # M5: cap the wire size before decoding — base64 inflates by 4/3, and
        # the zip-bomb guard downstream only sees the DECODED archive, so the
        # raw payload must be bounded here too.
        if len(zip_b64) > 70 * 1024 * 1024:  # ~52 MiB decoded
            return {"ok": False, "error": "skill zip too large"}
        raw = base64.b64decode(zip_b64)
        tmp = Path(tempfile.gettempdir()) / f"qunwork-import-{secrets.token_hex(6)}.zip"
        tmp.write_bytes(raw)
        try:
            try:
                skill = manager.skill_import_zip(tmp)
            except (zipfile.BadZipFile, OSError, ValueError):
                skill = None
        finally:
            tmp.unlink(missing_ok=True)
        if skill is None:
            return {"ok": False, "error": "invalid skill zip (no SKILL.md found)"}
        return {"ok": True, **skill}

    @app.post("/v1/skills/export")
    def skill_export(body: dict) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        zip_path = manager.skill_export_zip(name)
        if zip_path is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        import base64

        return {
            "ok": True,
            "name": name,
            "zip_base64": base64.b64encode(zip_path.read_bytes()).decode(),
            "filename": zip_path.name,
        }

    @app.post("/v1/skills/{name}/rate")
    def skill_rate(name: str, body: dict) -> dict[str, Any]:
        score = float(body.get("score") or 0)
        stats = manager.skill_rate(name, score)
        if stats is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        return {"ok": True, **stats}

    @app.delete("/v1/skills/{name}")
    def skill_delete(name: str) -> dict[str, Any]:
        removed = manager.skill_delete(name)
        return {"ok": removed, "name": name} if removed else {"ok": False, "error": f"unknown skill: {name}"}

    @app.post("/v1/skills/{name}/promote")
    def skill_promote(name: str) -> dict[str, Any]:
        """涌现草稿转正为正式技能 (P1-8 补全: draft → published)。"""
        return manager.skill_promote(name)

    # -- Skill 信任基础 (版本市场 + lock + 安全 + 兼容 + 自动修复) --------
    @app.get("/v1/skills/{name}/versions")
    def skill_versions(name: str) -> dict[str, Any]:
        """某 skill 所有版本的安装量/评分 (含 per-name 聚合 + per-version 拆分)。"""
        return {"ok": True, **manager.skill_versions(name)}

    @app.post("/v1/skills/{name}/lock")
    def skill_generate_lock(name: str) -> dict[str, Any]:
        """(重新) 生成 skill.lock 并刷新所有 live 引擎。返回 lock data。"""
        data = manager.skill_generate_lock(name)
        if data is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        return {"ok": True, "lock": data}

    @app.get("/v1/skills/{name}/lock")
    def skill_lock_info(name: str) -> dict[str, Any]:
        """获取 skill.lock + 脚本完整性检查结果。"""
        info = manager.skill_lock_info(name)
        if info is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        return {"ok": True, **info}

    @app.get("/v1/skills/{name}/security")
    def skill_security(name: str, request: Request) -> dict[str, Any]:
        """完整安全报告: score + level + findings + recommendation。rescan=1 强制重扫。"""
        rescan = (request.query_params.get("rescan") or "").lower() in ("1", "true", "yes")
        rep = manager.skill_security_report(name, rescan=rescan)
        if rep is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        return {"ok": True, **rep}

    @app.post("/v1/skills/{name}/security/rescan")
    def skill_security_rescan(name: str) -> dict[str, Any]:
        """强制重新扫描 (POST 快捷)。"""
        rep = manager.skill_security_report(name, rescan=True)
        if rep is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        return {"ok": True, **rep}

    @app.get("/v1/skills/{name}/compatibility")
    def skill_compatibility(name: str) -> dict[str, Any]:
        """检查某 skill 兼容性报告 (含 autofix_plan, severity)。"""
        rep = manager.skill_check_compatibility(name)
        if rep is None:
            return {"ok": False, "error": f"unknown skill: {name}"}
        return {"ok": True, **rep}

    @app.get("/v1/skills/compatibility/all")
    def skill_compatibility_all() -> dict[str, Any]:
        """批量兼容性扫描 (所有 skill, severity 排序)。"""
        return {"ok": True, "reports": manager.skill_check_all_compatibility()}

    @app.post("/v1/skills/autofix")
    def skill_build_autofix(body: dict) -> dict[str, Any]:
        """构建 Swarm reviewer 批量自动修复计划。
        body.skill_names: [string] (可选, 否则所有不兼容的)。
        """
        names = body.get("skill_names") if isinstance(body, dict) else None
        result = manager.skill_build_autofix(names)
        return {"ok": True, **result}

    # -- knowledge file library --------------------------------------------
    @app.get("/v1/knowledge/path")
    def knowledge_get_path() -> dict[str, Any]:
        """Get the current knowledge library database path."""
        return manager.get_knowledge_path()

    @app.post("/v1/knowledge/path")
    def knowledge_set_path(body: dict) -> dict[str, Any]:
        """Set a new knowledge library path. Optionally migrate existing data.

        body:
            path: str  — new directory or full DB path
            migrate: bool = False  — move existing DB to new location
        """
        new_path = str(body.get("path") or "").strip()
        if not new_path:
            return {"ok": False, "error": "path is required"}
        migrate = bool(body.get("migrate", False))
        return manager.set_knowledge_path(new_path, migrate=migrate)

    @app.get("/v1/knowledge")
    def knowledge_list(request: Request) -> dict[str, Any]:
        ws = request.query_params.get("workspace") or None
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 100), 1000))
            offset = max(0, int(request.query_params.get("offset") or 0))
        except ValueError:
            limit, offset = 100, 0
        return manager.knowledge_list(workspace=ws, limit=limit, offset=offset)

    @app.post("/v1/knowledge/scan")
    def knowledge_scan() -> dict[str, Any]:
        return {"ok": True, **manager.knowledge_scan()}

    @app.post("/v1/knowledge/import-folder")
    def knowledge_import_folder(body: dict) -> dict[str, Any]:
        return manager.knowledge_import_folder(str(body.get("path") or ""))

    @app.post("/v1/knowledge")
    def knowledge_add(body: dict) -> dict[str, Any]:
        title = str(body.get("title") or "").strip()
        content = str(body.get("content") or "")
        if not title or not content:
            return {"ok": False, "error": "title and content are required"}
        return {"ok": True, **manager.knowledge_add(title, content)}

    @app.delete("/v1/knowledge/{item_id}")
    def knowledge_delete(item_id: int) -> dict[str, Any]:
        return {"ok": manager.knowledge_delete(item_id), "id": item_id}

    @app.get("/v1/knowledge/{item_id}/detail")
    def knowledge_detail(item_id: int) -> dict[str, Any]:
        """Full knowledge detail (content + source) for the view/resume actions."""
        item = manager.knowledge_get(item_id)
        if item is None:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "item": item}

    @app.get("/v1/knowledge/{item_id}/resume-context")
    def knowledge_resume_context(item_id: int) -> dict[str, Any]:
        """Resonance context pack: related hive cells for a wider research start."""
        return {"ok": True, "related": manager.knowledge_resume_context(item_id)}

    @app.get("/v1/knowledge/{item_id}/resume-pack")
    def knowledge_resume_pack(item_id: int) -> dict[str, Any]:
        """One-click research pack: full body + source + resonance related cells."""
        pack = manager.knowledge_resume_pack(item_id)
        if pack is None:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "pack": pack}

    @app.post("/v1/knowledge/resume-by-title")
    def knowledge_resume_by_title(body: dict) -> dict[str, Any]:
        """Resume from a title alone (emergent findings without a persisted entry)."""
        pack = manager.knowledge_resume_by_title((body or {}).get("title", ""))
        if pack is None:
            return {"ok": False, "error": "no matching knowledge entry"}
        return {"ok": True, "pack": pack}

    @app.post("/v1/knowledge/reveal-source")
    def knowledge_reveal_source(body: dict) -> dict[str, Any]:
        """Open a knowledge source path in the OS (folder via explorer)."""
        return manager.reveal_knowledge_source((body or {}).get("path", ""))

    # -- HORNET (蜂巢共振神经拓扑) 2D layer ---------------------------------
    @app.post("/v1/hornet/build")
    def hornet_build(body: dict) -> dict[str, Any]:
        return manager.hornet_build(
            rebuild=bool((body or {}).get("rebuild", True)),
            topo=bool((body or {}).get("topo", False)),
        )

    @app.post("/v1/hornet/resonate")
    def hornet_resonate(body: dict) -> dict[str, Any]:
        b = body or {}
        return manager.hornet_resonate(
            b.get("query", ""),
            k=int(b.get("k") or 10),
            hops=int(b["hops"]) if b.get("hops") else None,
        )

    @app.post("/v1/hornet/evolve")
    def hornet_evolve(body: dict) -> dict[str, Any]:
        return manager.hornet_evolve(limit=int((body or {}).get("limit") or 20))

    @app.get("/v1/hornet/graph")
    def hornet_graph() -> dict[str, Any]:
        return manager.hornet_graph()

    @app.get("/v1/usage")
    def usage_summary(days: int = 14) -> dict[str, Any]:
        return manager.usage_summary(days=days)

    # -- P1-5 零信任能力袋: 权限 scope API ------------------------------------
    @app.get("/v1/permissions/scopes")
    def permissions_scopes() -> dict[str, Any]:
        """全量连接器工具 scope 声明表 (供前端权限矩阵渲染)。"""
        return manager.connector_scope_matrix()

    @app.get("/v1/permissions/persona-scopes")
    def permissions_persona_scopes(persona_id: str = "default") -> dict[str, Any]:
        """读取某角色的 scope 配置。"""
        return manager.persona_scopes_get(persona_id)

    @app.put("/v1/permissions/persona-scopes")
    def permissions_persona_scopes_set(body: dict) -> dict[str, Any]:
        """设置某角色在某连接器上的 scope。"""
        return manager.persona_scopes_set(
            persona_id=body.get("persona_id", "default"),
            connector=body.get("connector", ""),
            scopes=body.get("scopes", []),
        )

    @app.get("/v1/permissions/heatmap")
    def permissions_heatmap() -> dict[str, Any]:
        """权限审计热力图 (persona × connector × tool 聚合)。"""
        return manager.permissions_heatmap()

    # -- P1-8 HORNET 涌现 → 自动生成 Draft Skill ------------------------------
    @app.post("/v1/hornet/emergence-to-skill")
    def hornet_emergence_to_skill(body: dict = None) -> dict[str, Any]:
        """手动触发: 把最近的 HORNET 涌现转化为 Draft Skill。

        body 可选 emergence_index (int, 默认 -1 = 最近一条)。
        """
        body = body or {}
        idx = int(body.get("emergence_index", -1))
        return manager.hornet_emergence_to_skill(emergence_index=idx)

    # -- 13 Agent 影子模式: 决策回放 ------------------------------------------
    @app.get("/v1/sessions/{session_id}/decision-trace")
    def session_decision_trace(session_id: str) -> dict[str, Any]:
        """返回该会话 engine 累积的决策轨迹 (工具选择/权限/scope/审批)。

        前端 SwarmView 的「决策回放时间轴」用这个渲染每一步 AI 看到了什么、
        考虑了哪些选项、为什么选了这个。engine 销毁后回退到 audit_events 表。
        """
        return manager.session_decision_trace(session_id)

    # -- cache warm-up (P0 建议1) ---------------------------------------------
    @app.get("/v1/cache/warm")
    def cache_warm_status() -> dict[str, Any]:
        """Warm-up toggle + this week's injected tokens (surface='cachewarm')."""
        return manager.cache_warm_status()

    @app.post("/v1/cache/warm")
    async def cache_warm_now(body: dict) -> dict[str, Any]:
        """Trigger one warm pass now (optional max_items)."""
        return await manager.cache_warm_now(
            max_items=body.get("max_items") if isinstance(body, dict) else None
        )

    @app.post("/v1/cache/warm/toggle")
    def cache_warm_toggle(body: dict) -> dict[str, Any]:
        return manager.cache_warm_toggle(bool((body or {}).get("enabled")))

    # -- P0 增量1: 信息素负载信号 ---------------------------------------------
    @app.get("/v1/pheromone")
    def pheromone_status() -> dict[str, Any]:
        """Stigmergic load field: busy signals per executor role + total load."""
        return manager.pheromone_status()

    # -- QunMesh M4 后续项: mesh_mode 运行时热切换 -----------------------------
    @app.get("/v1/mesh/mode")
    def get_mesh_mode() -> dict[str, Any]:
        """当前 mesh_mode 默认档位 (off/serial/hybrid/full)。"""
        return {"ok": True, "mesh_mode": str(
            (getattr(manager, "_prefs", {}) or {}).get("mesh_mode", "off") or "off"
        )}

    @app.put("/v1/mesh/mode")
    def put_mesh_mode(body: dict = None) -> dict[str, Any]:
        """设置 mesh_mode 默认档位: 写 prefs (后续新 run 生效) + 审计。
        正在运行中的 run 需 Orchestrator.set_mesh_mode 热切换 (G2 通道)。"""
        try:
            return manager.set_mesh_mode(str((body or {}).get("mesh_mode", "off")))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    # -- 7x24 长程任务管理 (健康控制台 / 遥测 / 检查点 / 存储) -------------------
    @app.get("/v1/7x24/health")
    def longrun_health() -> dict[str, Any]:
        """A: 长程任务健康控制台 — 心跳/自动化/唤醒状态总览。"""
        return manager.longrun_health()

    @app.get("/v1/7x24/telemetry")
    def longrun_telemetry(limit: int = 20) -> dict[str, Any]:
        """B: 运行遥测聚合 — 全局降级事件流 + 收敛报告历史对比。"""
        return manager.longrun_telemetry(limit=limit)

    @app.get("/v1/7x24/alerts")
    def longrun_alerts(
        limit: int = 50,
        task_id: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> dict[str, Any]:
        """① 告警历史: 落库可回溯, 可按任务/时间范围 (since/until, epoch 秒) 过滤。"""
        return manager.longrun_alerts(limit=limit, task_id=task_id, since=since, until=until)

    @app.get("/v1/7x24/alert-channels")
    def longrun_alert_channels() -> dict[str, Any]:
        """① 告警多渠道通知配置 (脱敏)。"""
        return manager.longrun_alert_channels()

    @app.put("/v1/7x24/alert-channels")
    def set_longrun_alert_channels(body: dict) -> dict[str, Any]:
        """① 保存告警渠道配置 (邮件/Telegram/飞书/钉钉/企业微信)。"""
        return manager.set_longrun_alert_channels((body or {}).get("channels"))

    @app.post("/v1/7x24/alert-channels/test")
    def test_longrun_alert_channels() -> dict[str, Any]:
        """① 测试告警渠道: 向每个启用渠道发测试消息。"""
        return manager.test_longrun_alert_channels()

    @app.post("/v1/7x24/alert-channels/probe")
    def longrun_channel_probe() -> dict[str, Any]:
        """① 告警渠道健康探针: 各启用渠道连通性 + 延迟 (落库历史)。"""
        return manager.longrun_channel_probe()

    @app.get("/v1/7x24/oir-longrun")
    def oir_longrun_config() -> dict[str, Any]:
        """OIR longrun 集成配置回读 (GUI 7×24 面板开关/查看)。"""
        return manager.oir_longrun_config()

    @app.put("/v1/7x24/oir-longrun")
    def set_oir_longrun_config(body: dict) -> dict[str, Any]:
        """更新 OIR longrun 集成配置 (enabled/doc_dir/glob/batch/goal_id)。"""
        return manager.set_oir_longrun_config(body or {})

    @app.get("/v1/7x24/oir-longrun/telemetry")
    def oir_longrun_telemetry() -> dict[str, Any]:
        """回读 OIR task/trend/growth 遥测（网关 reflect_telemetry 落盘文件）。"""
        return manager.oir_longrun_telemetry()

    @app.get("/v1/7x24/oir-longrun/tasks")
    async def oir_longrun_tasks() -> dict[str, Any]:
        """活跃长程任务列表（网关 + 本地驱动状态合并；GUI 任务控制台）。"""
        return await manager.oir_longrun_list_tasks()

    @app.post("/v1/7x24/oir-longrun/tasks")
    async def oir_longrun_submit_task(body: dict) -> dict[str, Any]:
        """用户发起一个真实长程任务（goal 文本 → 网关 submit，tick 自动驱动）。

        body: {goal: str, doc_paths?: [绝对路径...]} — doc_paths 提供时只索引清单内文档。
        """
        b = body or {}
        return await manager.oir_longrun_submit_task(
            b.get("goal") or "", doc_paths=b.get("doc_paths") or None
        )

    @app.post("/v1/7x24/oir-longrun/tasks/{goal_id}/{action}")
    async def oir_longrun_control_task(goal_id: str, action: str) -> dict[str, Any]:
        """对单个 goal 执行 pause / resume / complete。"""
        return await manager.oir_longrun_control_task(goal_id, action)

    @app.get("/v1/7x24/channel-health")
    def longrun_channel_health() -> dict[str, Any]:
        """① 渠道健康分/历史趋势 (probe_history, 阈值可配)。"""
        return manager.longrun_channel_health()

    @app.get("/v1/7x24/channel-health/export")
    def longrun_channel_health_export(format: str = "json", limit: int = 500):
        """② 导出探针历史 (CSV/JSON)。"""
        from fastapi.responses import PlainTextResponse

        content = manager.longrun_channel_health_export(fmt=format)
        if format == "csv":
            return PlainTextResponse(
                content,
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="qunwork-channel-health.csv"'},
            )
        return json.loads(content)

    @app.post("/v1/7x24/alerts/archive")
    def longrun_alert_archive(body: dict) -> dict[str, Any]:
        """② 告警/审计自动归档: 超过 keep_days 天的记录归档 (默认 30)。"""
        keep_days = int((body or {}).get("keep_days", 30))
        return manager.longrun_alert_archive(keep_days=keep_days)

    @app.get("/v1/7x24/alerts/archived")
    def longrun_archived_alerts(
        limit: int = 50, task_id: Optional[str] = None
    ) -> dict[str, Any]:
        """③ 归档数据查询: 从 alerts_archive 查历史 (可回溯)。"""
        return manager.longrun_archived_alerts(limit=limit, task_id=task_id)

    @app.post("/v1/7x24/alerts/archived/{archive_id}/restore")
    def longrun_restore_archived_alert(archive_id: int) -> dict[str, Any]:
        """③ 归档数据恢复: 把一条归档记录恢复到活跃表 (撤销归档)。"""
        return manager.longrun_restore_archived_alert(archive_id)

    @app.get("/v1/7x24/probe-schedule")
    def longrun_probe_schedule() -> dict[str, Any]:
        """② 探针定时化: 周期自动探针调度设置。"""
        return manager.longrun_probe_schedule()

    @app.put("/v1/7x24/probe-schedule")
    def set_longrun_probe_schedule(body: dict) -> dict[str, Any]:
        """② 保存探针调度 (启用 + 间隔分钟)。"""
        return manager.set_longrun_probe_schedule((body or {}).get("schedule"))

    @app.post("/v1/7x24/probe-schedule/run")
    def run_longrun_scheduled_probe() -> dict[str, Any]:
        """② 手动触发一次周期探针 (失败渠道落库告警)。"""
        return manager.run_scheduled_probe()

    @app.post("/v1/7x24/probe-history/prune")
    def longrun_probe_history_prune() -> dict[str, Any]:
        """③ 手动触发探针历史清理 (按配置保留窗口: 天数/条数)。"""
        return manager.longrun_probe_history_prune()

    @app.get("/v1/7x24/checkpoints")
    def longrun_checkpoints() -> dict[str, Any]:
        """C: 检查点浏览器 — 会话列表 (消息数/是否归档)。"""
        return manager.longrun_checkpoints()

    @app.get("/v1/7x24/checkpoints/{session_id}")
    def longrun_checkpoint_detail(session_id: str) -> dict[str, Any]:
        """C: 单会话检查点链 (七层粒度时间线)。"""
        return manager.longrun_checkpoint_detail(session_id)

    @app.post("/v1/7x24/checkpoints/{session_id}/restore")
    def longrun_checkpoint_restore(session_id: str, apply: bool = False) -> dict[str, Any]:
        """C: 检查点恢复 — 演练 (默认只读) 或真实恢复 (apply=true 写回会话)。"""
        return manager.longrun_checkpoint_restore(session_id, apply=apply)

    @app.post("/v1/7x24/checkpoints/{session_id}/rollback")
    def longrun_checkpoint_rollback(session_id: str) -> dict[str, Any]:
        """② 备份一键回滚: 从 backup:{sid} 恢复 (撤销上次真实恢复)。"""
        return manager.longrun_checkpoint_rollback(session_id)

    @app.get("/v1/7x24/alerts/aggregations")
    def longrun_alert_aggregations(
        resolved: Optional[bool] = None, limit: int = 50
    ) -> dict[str, Any]:
        """③ 告警聚合: 同任务连续卡死合并为持续告警 (次数/时长/解决状态)。"""
        return manager.longrun_alert_aggregations(resolved=resolved, limit=limit)

    @app.get("/v1/7x24/alerts/aggregations/stats")
    def longrun_aggregation_stats(days: int = 14) -> dict[str, Any]:
        """② 聚合历史统计: 每日告警趋势 (快照 + 最近 days 天)。"""
        return manager.longrun_aggregation_stats(days=days)

    @app.get("/v1/7x24/audit")
    def longrun_audit(limit: int = 50) -> dict[str, Any]:
        """③ 操作审计: 回滚/恢复/渠道变更等管理操作记录。"""
        return manager.longrun_audit(limit=limit)

    @app.get("/v1/7x24/audit/export")
    def longrun_audit_export(format: str = "json", limit: int = 500):
        """① 审计导出 (CSV/JSON)。返回文本/JSON 内容。"""
        store = getattr(manager, "alert_store", None)
        if store is None:
            raise HTTPException(status_code=404, detail="no alert store")
        content = store.export_audit(limit=limit, fmt=format)
        if format == "csv":
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(
                content,
                media_type="text/csv",
                headers={"Content-Disposition": 'attachment; filename="qunwork-audit.csv"'},
            )
        return json.loads(content)  # JSON 数组

    @app.get("/v1/7x24/alert-settings")
    def longrun_alert_settings() -> dict[str, Any]:
        """② 告警静默设置: 聚合静默阈值/时长 (可配置)。"""
        return manager.longrun_alert_settings()

    @app.put("/v1/7x24/alert-settings")
    def set_longrun_alert_settings(body: dict) -> dict[str, Any]:
        """② 保存告警静默设置 (silence_after / silence_seconds)。"""
        return manager.set_longrun_alert_settings((body or {}).get("settings"))

    @app.get("/v1/7x24/alerts/aggregations/week-compare")
    def longrun_aggregation_week_compare() -> dict[str, Any]:
        """③ 跨周对比: 本周 vs 上周每日告警数。"""
        store = getattr(manager, "alert_store", None)
        if store is None:
            return {"ok": False, "error": "no alert store"}
        try:
            store.snapshot_aggregation_history()
            return {"ok": True, **store.aggregation_week_compare()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.get("/v1/7x24/storage")
    def longrun_storage() -> dict[str, Any]:
        """D: 存储健康 — 会话存储占用/归档效果/记忆库规模。"""
        return manager.longrun_storage()

    @app.post("/v1/7x24/maintenance")
    def longrun_maintenance(body: dict) -> dict[str, Any]:
        """A: 一键维护 — 记忆去重/衰减/整理 + 会话定期归档。"""
        dry_run = bool((body or {}).get("dry_run", False))
        return manager.longrun_maintenance(dry_run=dry_run)

    # -- P1 团队 / Agent 状态池 / 任务组生命周期 API ----------------------------
    @app.get("/v1/team")
    def team_info(name: str = "My Team") -> dict[str, Any]:
        return manager.team_info(name_hint=name)

    @app.get("/v1/team/members")
    def list_team_members() -> list[dict]:
        return manager.list_members()

    @app.post("/v1/team/members")
    def add_team_member(body: dict) -> dict:
        name = str((body or {}).get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        return manager.add_member(
            name=name,
            role=str((body or {}).get("role") or "worker"),
            persona_id=(body or {}).get("persona_id"),
        )

    @app.patch("/v1/team/members/{member_id}")
    def update_team_member(member_id: str, body: dict) -> dict:
        return manager.update_member(member_id, **(body or {}))

    @app.delete("/v1/team/members/{member_id}")
    def remove_team_member(member_id: str) -> dict:
        return manager.remove_member(member_id)

    @app.get("/v1/team/agents")
    def list_team_agents() -> list[dict]:
        return manager.list_team_agents()

    @app.post("/v1/team/agents")
    def add_team_agent(body: dict) -> dict:
        body = body or {}
        role = str(body.get("role") or "worker").strip() or "worker"
        return manager.add_agent(role, persona_id=str(body.get("persona_id") or role))

    @app.delete("/v1/team/agents/{agent_id}")
    def remove_team_agent(agent_id: str) -> dict:
        return manager.remove_agent(agent_id)

    @app.get("/v1/team/agents/load")
    def team_agent_load() -> dict[str, Any]:
        return manager.agent_load()

    @app.get("/v1/team/task-groups")
    def list_team_task_groups(include_dissolved: bool = False) -> list[dict]:
        return manager.list_task_groups(include_dissolved=include_dissolved)

    @app.post("/v1/team/task-groups")
    def create_team_task_group(body: dict) -> dict:
        body = body or {}
        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="goal required")
        return manager.create_task_group(
            goal,
            owner_member=body.get("owner_member"),
            member_ids=body.get("member_ids") or None,
            agent_ids=body.get("agent_ids") or None,
            group_id=body.get("group_id") or None,
        )

    @app.post("/v1/team/task-groups/{group_id}/transition")
    def transition_task_group(group_id: str, body: dict) -> dict:
        new_state = str((body or {}).get("state") or "").strip()
        if not new_state:
            raise HTTPException(status_code=400, detail="state required")
        try:
            return manager._team_lifecycle.transition(group_id, new_state)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/team/task-groups/{group_id}/dissolve")
    def dissolve_task_group_post(group_id: str) -> dict:
        try:
            return manager.dissolve_task_group(group_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    # -- P2 权限矩阵 × inbox 审批合规标注 --------------------------------------
    @app.get("/v1/inbox/compliance")
    def inbox_compliance() -> dict[str, Any]:
        """Batch compliance annotation for all pending approval items.
        Returns each item's fund tier, role capability check, and escalation path."""
        return manager.inbox_compliance_view()

    @app.post("/v1/inbox/{item_id}/annotate")
    def annotate_inbox_item(item_id: str, body: dict) -> dict[str, Any]:
        """Manually annotate an inbox item with compliance info (e.g. when the
        member role was unknown at creation time and has since been set)."""
        from ..permission_matrix import annotate_compliance

        item = manager.inbox.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="item not found")
        tool_name = (body or {}).get("tool_name") or item.title.replace("Run `", "").replace("`?", "")
        arguments = (body or {}).get("arguments")
        member_role = (body or {}).get("member_role") or manager._get_my_member_role()
        compliance = annotate_compliance(tool_name, arguments, member_role)
        # Write back to the item's data
        if item.data is None:
            item.data = {}
        item.data["compliance"] = compliance
        manager.inbox._save()
        return {"item_id": item_id, "compliance": compliance}

    # -- P0 增量3: 组织级权限矩阵 ---------------------------------------------
    @app.get("/v1/permission-matrix")
    def permission_matrix() -> dict[str, Any]:
        """Org permission matrix (角色×能力) + fund tiers + human escalation."""
        return manager.permission_matrix_view()

    @app.get("/v1/team/permissions")
    def team_permissions() -> dict[str, Any]:
        """PermissionMatrix shape for the PermissionsView page (roles + thresholds)."""
        return manager.team_permissions_view()

    # -- P2P 团队同步 (设计方案第六章) ----------------------------------------
    @app.post("/v1/team/sync/config")
    def team_sync_config(body: dict) -> dict[str, Any]:
        """Configure the peer endpoint; collects a local snapshot on first set.
        S1: also attempts TOFU — fetches the peer's public key and records it
        into the trust allow-list (falls back to manual register_peer_key)."""
        return manager.team_sync_config(str((body or {}).get("peer_url") or ""))

    @app.post("/v1/team/sync/register-peer-key")
    def team_sync_register_peer_key(body: dict) -> dict[str, Any]:
        """S1: manually register a peer's Ed25519 public key into the trust
        allow-list (used when automatic TOFU failed)."""
        return manager.team_sync_register_peer_key(
            str((body or {}).get("peer_public_key") or "")
        )

    @app.get("/v1/team/sync/status")
    def team_sync_status() -> dict[str, Any]:
        return manager.team_sync_status()

    @app.post("/v1/team/sync/run")
    async def team_sync_run(body: dict) -> dict[str, Any]:
        """One sync round: push local pending → pull peer outbox → merge."""
        return await manager.team_sync_run()

    @app.get("/v1/team/sync/outbox")
    def team_sync_outbox() -> dict[str, Any]:
        """Peer-facing: this node's pending changes, encrypted (AES-GCM + sig)."""
        changes = manager.team_sync.pending()
        envelopes = manager.team_sync.pack_for_transport(changes)
        return {"envelopes": envelopes}

    @app.post("/v1/team/sync/ingest")
    def team_sync_ingest(body: dict) -> dict[str, Any]:
        """Peer-facing: receive encrypted envelopes, verify + decrypt + merge."""
        envelopes = (body or {}).get("envelopes") or []
        changes = manager.team_sync.unpack_from_transport(envelopes)
        if not changes:
            return {"ok": True, "received": len(envelopes), "applied": 0}
        result = manager.team_sync.merge_changes(changes)
        return {"ok": True, "received": len(changes), **result}

    # -- ROI 价值归因 (建议10: AI 团队账本) ------------------------------------
    @app.get("/v1/roi/config")
    def roi_config() -> dict[str, Any]:
        return manager.roi_config_get()

    @app.post("/v1/roi/config")
    def roi_config_set(body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.roi_config_set(body.get("rates"), body.get("hourly_rate"))

    @app.get("/v1/roi/report")
    def roi_report(month: str = "") -> dict[str, Any]:
        """月度 ROI 账本(成本/缓存节省/人工节省/技能复用, 按价值标签分组)。
        month 格式 YYYY-MM, 缺省用当前月。"""
        import re as _re

        if not month:
            from datetime import date as _date

            month = _date.today().strftime("%Y-%m")
        if not _re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            return {"ok": False, "error": "month must be YYYY-MM"}
        return manager.roi_report(month)

    @app.post("/v1/roi/report/html")
    def roi_report_html(body: dict) -> dict[str, Any]:
        """生成 HTML 价值报告并落盘(浏览器可打印为 PDF)。"""
        from datetime import date as _date

        from ..roi import render_html

        body = body or {}
        month = str(body.get("month") or _date.today().strftime("%Y-%m"))
        report = manager.roi_report(month)
        skill = report.pop("skill_reuse", {})
        html_text = render_html(report, skill)
        out = Path(manager.default_workspace or ".") / f"roi-report-{month}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_text, encoding="utf-8")
        return {"ok": True, "path": str(out), "month": month}

    @app.post("/v1/orchestrate/{run_id}/tag")
    def orchestrate_tag(run_id: str, body: dict) -> dict[str, Any]:
        """ROI 价值标签: 给蜂群运行打业务标签(如「Q3 客户报告」)。"""
        return manager.orchestrate_tag(
            run_id, str((body or {}).get("value_tag") or "").strip()
        )

    @app.get("/v1/hornet/stats")
    def hornet_stats() -> dict[str, Any]:
        return manager.hornet_stats()

    @app.get("/v1/hornet/health")
    def hornet_health() -> dict[str, Any]:
        """E: knowledge-field health assessment (structure/dynamics/evolution)."""
        return manager.hornet_health()

    @app.post("/v1/hornet/health-report")
    def hornet_health_report() -> dict[str, Any]:
        """E: render + persist the weekly hive health report."""
        return manager.hornet_health_report()

    @app.get("/v1/hornet/emergence")
    def hornet_emergence(limit: int = 20) -> dict[str, Any]:
        """Unread emergence feed — new knowledge surfaced by the hive."""
        return manager.hornet_emergence(limit=limit)

    @app.post("/v1/hornet/emergence/{eid}/mark")
    def hornet_emergence_mark(eid: int, body: dict) -> dict[str, Any]:
        ok = manager.hornet_emergence_mark(eid, (body or {}).get("status", "accepted"))
        return {"ok": ok, "unread": manager.hornet.count_unread_emergent()}

    @app.get("/v1/hornet/export")
    def hornet_export() -> dict[str, Any]:
        """C: 跨组织蜂巢共振对齐 — export hive topology."""
        return manager.hornet_export_hive()

    @app.post("/v1/hornet/import")
    def hornet_import(body: dict) -> dict[str, Any]:
        """C: 跨组织蜂巢共振对齐 — import & align remote hive."""
        return manager.hornet_import_hive(body or {})

    @app.post("/v1/knowledge/{item_id}/retire")
    def knowledge_retire(item_id: int, body: dict) -> dict[str, Any]:
        """Asset lifecycle (Phase 3): retire (hide from search) or restore."""
        retired = bool(body.get("retired", True))
        return {"ok": manager.knowledge_set_retired(item_id, retired), "id": item_id, "retired": retired}

    @app.get("/v1/rhythm/forecast")
    def rhythm_forecast() -> dict[str, Any]:
        """Phase 3 organizational rhythm: dominant cadence + what's due next week."""
        return manager.rhythm_forecast()

    @app.get("/v1/rhythm/recommendations")
    def rhythm_recommendations() -> dict[str, Any]:
        """P0 建议4: per-automation best trigger time (run-history valleys × org cadence)."""
        return manager.rhythm_recommendations()

    @app.get("/v1/knowledge/search")
    def knowledge_search(request: Request) -> dict[str, Any]:
        query = (request.query_params.get("q") or "").strip()
        if not query:
            return {"ok": False, "error": "q is required", "results": []}
        results = manager.knowledge_search(query, k=5)
        return {"ok": True, "query": query, "results": results}

    # -- user task templates (home quick-start cards) -------------------------
    @app.get("/v1/swarm-templates")
    def swarm_templates_list() -> dict[str, Any]:
        return {"templates": manager.list_swarm_templates()}

    @app.post("/v1/swarm-templates")
    def swarm_templates_add(body: dict) -> dict[str, Any]:
        try:
            template = manager.add_swarm_template(
                str(body.get("title") or ""),
                str(body.get("intent") or ""),
                body.get("plan"),
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "template": template}

    @app.delete("/v1/swarm-templates/{template_id}")
    def swarm_templates_delete(template_id: int) -> dict[str, Any]:
        return {"ok": manager.delete_swarm_template(template_id), "id": template_id}

    @app.post("/v1/swarm-templates/{template_id}/record")
    def swarm_templates_record(template_id: int, body: dict) -> dict[str, Any]:
        """Tally one reuse of a template (5.2.1 track record: runs + successes)."""
        success = bool(body.get("success"))
        ok = manager.record_swarm_template_run(template_id, success)
        return {"ok": ok, "id": template_id, "success": success}

    # -- swarm lessons (Refine 机制: 蜂群经验进化闭环) ------------------------
    @app.get("/v1/swarm-lessons")
    def swarm_lessons_list(
        kind: Optional[str] = None,
        limit: int = 50,
        workspace: Optional[str] = None,
    ) -> dict[str, Any]:
        """蜂群经验库 (自进化闭环的"学习成果"): lesson/skill_hint/task_template。
        支持 kind 过滤与 workspace 指定 (默认 default_workspace);
        供 GUI 蜂群面板的「蜂群经验」tab 展示。"""
        return {
            "lessons": manager.list_swarm_lessons(
                workspace=workspace, kind=kind, limit=limit
            )
        }

    @app.delete("/v1/swarm-lessons/{lesson_id}")
    def swarm_lessons_delete(lesson_id: int, workspace: Optional[str] = None) -> dict[str, Any]:
        """删除一条蜂群经验 (学习成果纠正)。"""
        return {
            "ok": manager.delete_swarm_lesson(lesson_id, workspace=workspace),
            "id": lesson_id,
        }

    # -- team workspace (dev-plan P2) ----------------------------------------
    @app.get("/v1/team/export")
    def team_export() -> dict[str, Any]:
        return manager.export_team_package()

    @app.post("/v1/team/import")
    def team_import(body: dict) -> dict[str, Any]:
        return manager.import_team_package(str(body.get("path") or ""))

    @app.get("/v1/task-templates")
    def task_templates_list() -> dict[str, Any]:
        return {"templates": manager.list_task_templates()}

    @app.post("/v1/task-templates")
    def task_templates_add(body: dict) -> dict[str, Any]:
        try:
            template = manager.add_task_template(
                str(body.get("title") or ""), str(body.get("prompt") or "")
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "template": template}

    @app.delete("/v1/task-templates/{template_id}")
    def task_templates_delete(template_id: int) -> dict[str, Any]:
        return {"ok": manager.delete_task_template(template_id), "id": template_id}

    @app.get("/v1/workspaces/recent")
    def recent_workspaces() -> dict[str, Any]:
        return {"workspaces": manager.recent_workspaces()}

    @app.post("/v1/workspaces/open")
    def open_workspace(body: dict) -> dict[str, Any]:
        return manager.open_workspace(
            body.get("path", ""), create=bool(body.get("create"))
        )

    @app.get("/v1/workspaces/trusted")
    def trusted_workspaces() -> dict[str, Any]:
        return {"workspaces": manager.trusted_workspaces()}

    @app.post("/v1/workspaces/trust")
    def set_workspace_trust(body: dict) -> dict[str, Any]:
        return manager.set_workspace_trust(
            str((body or {}).get("path", "")),
            trusted=bool((body or {}).get("trusted", False)),
        )

    @app.post("/v1/workspaces/pick")
    async def pick_workspace() -> dict[str, Any]:
        # Native folder picker opened by the LOCAL sidecar (browser GUIs can't get absolute
        # paths from web file dialogs). Off the event loop: blocks until pick/cancel.
        return await asyncio.to_thread(manager.pick_native_folder)

    @app.get("/v1/sessions")
    def sessions(workspace: str | None = None) -> dict[str, Any]:
        return {"sessions": manager.list_sessions(workspace)}

    @app.get("/v1/sessions/{session_id}/messages")
    def session_messages(session_id: str) -> dict[str, Any]:
        return {"messages": manager.session_messages(session_id)}

    @app.patch("/v1/sessions/{session_id}")
    def session_patch(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        if "pinned" in body or "archived" in body:
            return manager.set_session_flags(
                session_id,
                pinned=bool(body["pinned"]) if "pinned" in body else None,
                archived=bool(body["archived"]) if "archived" in body else None,
            )
        return manager.rename_session(session_id, str(body.get("title", "")))

    @app.delete("/v1/sessions/{session_id}")
    def session_delete(session_id: str) -> dict[str, Any]:
        return manager.delete_session(session_id)

    @app.get("/v1/sessions/{session_id}/roots")
    def session_roots(session_id: str) -> dict[str, Any]:
        return {"roots": manager.get_roots(session_id)}

    @app.post("/v1/sessions/{session_id}/roots")
    def session_add_root(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.add_root(
            session_id, str(body.get("path", "")), bool(body.get("writable", False))
        )

    @app.delete("/v1/sessions/{session_id}/roots")
    def session_remove_root(session_id: str, path: str) -> dict[str, Any]:
        return manager.remove_root(session_id, path)

    @app.get("/v1/sessions/{session_id}/artifacts")
    def session_artifacts(session_id: str) -> dict[str, Any]:
        return {"artifacts": manager.list_artifacts(session_id)}

    @app.get("/v1/sessions/{session_id}/artifacts/read")
    def session_artifact_read(session_id: str, path: str) -> dict[str, Any]:
        return manager.read_artifact(session_id, path)

    @app.post("/v1/sessions/{session_id}/artifacts/reveal")
    def session_artifact_reveal(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.reveal_artifact(
            session_id, str(body.get("path", "")), str(body.get("mode", "reveal"))
        )

    @app.get("/v1/memory")
    def memory() -> dict[str, Any]:
        return {"memory": manager.list_memory()}

    @app.post("/v1/memory")
    def add_memory(body: dict) -> dict[str, Any]:
        return manager.add_memory(
            body.get("content", ""), body.get("scope", "workspace")
        )

    @app.post("/v1/memory/search")
    def search_memory(body: dict) -> dict[str, Any]:
        """Team memory search (5.2.2): keyword relevance over the durable pool."""
        query = str(body.get("query") or "")
        k = max(1, min(int(body.get("k") or 10), 100))
        return {"query": query, "results": manager.search_memory(query, k)}

    @app.put("/v1/memory/{memory_id}")
    def update_memory(memory_id: int, body: dict) -> dict[str, Any]:
        item = manager.update_memory(memory_id, str(body.get("content") or ""))
        if item is None:
            return {"ok": False, "error": "memory not found or empty content"}
        return {"ok": True, "item": item}

    @app.delete("/v1/memory/{memory_id}")
    def delete_memory(memory_id: int) -> dict[str, Any]:
        return {"ok": manager.delete_memory(memory_id), "id": memory_id}

    @app.post("/v1/assets/search")
    def assets_search(body: dict) -> dict[str, Any]:
        """Unified organizational asset search (asset loop Phase 2): one query
        across knowledge / skills / templates / memories / swarm runs."""
        query = str(body.get("query") or "")
        k = max(1, min(int(body.get("k") or 5), 20))
        return {"query": query, **manager.search_assets(query, k)}

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict) -> dict[str, Any]:
        model = body.get("model", manager.model)
        turn = manager.provider_complete(
            model, body.get("messages", []), body.get("tools")
        )
        return _openai_response(model, turn)

    # -- local voice chat (VAD + streaming ASR + LLM + TTS) ---------------------
    # The layered voice pipeline runs entirely on-device. Models are downloaded on
    # demand (see coworker/voice/models.py); the pipeline is a lazily-created
    # singleton whose events the GUI polls via /v1/voice/events.
    _voice_lock = asyncio.Lock()
    _voice_pipeline = None
    _voice_install = {"active": False, "key": "", "done": 0, "total": 0, "error": ""}

    def _get_voice_pipeline():
        nonlocal _voice_pipeline
        if _voice_pipeline is None:
            from coworker.voice.pipeline import VoiceChatPipeline

            def _voice_complete(messages):
                turn = manager.provider_complete(manager.model, messages)
                return (turn.text or "").strip()

            _voice_pipeline = VoiceChatPipeline(complete=_voice_complete)
        return _voice_pipeline

    @app.get("/v1/voice/status")
    def voice_status() -> dict[str, Any]:
        from coworker.voice import voice_models_status

        pipe = _get_voice_pipeline()
        return {
            "models": voice_models_status(),
            "running": pipe.running,
            "install": dict(_voice_install),
            "model": manager.model,
        }

    @app.post("/v1/voice/install")
    async def voice_install() -> dict[str, Any]:
        """Start downloading the voice models in the background (if not already)."""
        async with _voice_lock:
            if _voice_install["active"]:
                return {"ok": True, "started": False, "reason": "already installing"}
            from coworker.voice import voice_models_installed

            if voice_models_installed():
                return {"ok": True, "started": False, "reason": "already installed"}

            def _run_install():
                from coworker.voice import install_voice_models

                def _progress(key, done, total):
                    _voice_install.update(key=key, done=done, total=total)

                try:
                    _voice_install.update(active=True, key="", done=0, total=0, error="")
                    install_voice_models(progress=_progress)
                except Exception as exc:  # noqa: BLE001
                    _voice_install["error"] = str(exc)
                finally:
                    _voice_install["active"] = False

            asyncio.create_task(asyncio.to_thread(_run_install))
            return {"ok": True, "started": True}

    @app.post("/v1/voice/start")
    def voice_start() -> dict[str, Any]:
        pipe = _get_voice_pipeline()
        try:
            pipe.start()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/v1/voice/stop")
    def voice_stop() -> dict[str, Any]:
        _get_voice_pipeline().stop()
        return {"ok": True}

    @app.get("/v1/voice/events")
    def voice_events(after: int = 0) -> dict[str, Any]:
        events = _get_voice_pipeline().drain_events(after=after)
        return {"events": events, "next": (events[-1]["index"] + 1) if events else after}

    # -- MCP servers ------------------------------------------------------------
    @app.get("/v1/mcp")
    def mcp_list() -> dict[str, Any]:
        return {"servers": manager.list_mcp()}

    @app.post("/v1/mcp")
    def mcp_add(body: dict) -> dict[str, Any]:
        name = body.get("name")
        config = body.get("config")
        if not name or not isinstance(config, dict):
            return {"ok": False, "error": "name and config required"}
        return manager.add_mcp(name, config)

    @app.patch("/v1/mcp/{name}")
    def mcp_patch(name: str, body: dict) -> dict[str, Any]:
        return manager.patch_mcp(name, body or {})

    @app.delete("/v1/mcp/{name}")
    def mcp_delete(name: str) -> dict[str, Any]:
        return manager.delete_mcp(name)

    @app.get("/v1/mcp/{name}/tools")
    async def mcp_tools(name: str) -> dict[str, Any]:
        return await manager.mcp_tools(name)

    @app.post("/v1/mcp/{name}/connect")
    async def mcp_connect(name: str) -> dict[str, Any]:
        # Connect now. For `auth: oauth` servers the first connect opens the system
        # browser and waits on the loopback callback — that can take minutes, so it
        # runs as a background task; the GUI polls /v1/mcp for the status flip
        # (authorizing → connected | needs_auth + last_error).
        asyncio.create_task(manager.connect_mcp(name))
        return {"ok": True, "started": True}

    @app.post("/v1/mcp/{name}/signout")
    async def mcp_signout(name: str) -> dict[str, Any]:
        return await manager.signout_mcp(name)

    @app.get("/mcp/oauth/callback")
    async def mcp_oauth_callback(
        code: str = "", state: str = "", error: str = ""
    ) -> Any:
        # Loopback landing for the MCP OAuth browser flow (mcp/oauth.py). Browser-facing:
        # returns the same styled page as the managed-connector callbacks.
        from fastapi.responses import HTMLResponse

        from ..mcp import oauth as mcp_oauth

        if error:
            return HTMLResponse(
                _browser_page(
                    "Sign-in failed",
                    "The service reported an error. Return to QunWork and try again.",
                    ok=False,
                    error=error,
                ),
                status_code=400,
            )
        if not code or not mcp_oauth.deliver_callback(code, state or None):
            return HTMLResponse(
                _browser_page(
                    "Nothing waiting for this sign-in",
                    "The sign-in may have timed out. Return to QunWork and start it again.",
                    ok=False,
                ),
                status_code=400,
            )
        return HTMLResponse(
            _browser_page(
                "Connected",
                "Sign-in complete. You can close this tab and return to QunWork.",
                ok=True,
            )
        )

    @app.post("/v1/mcp/reload")
    async def mcp_reload() -> dict[str, Any]:
        return await manager.reload_mcp()

    # -- connectors (Slack / Telegram / …) --------------------------------------
    @app.get("/v1/connectors")
    def connectors_list() -> dict[str, Any]:
        return {"connectors": manager.list_connectors()}

    async def _refresh_listeners_if_two_way(name: str) -> None:
        # New/removed creds only take effect when the platform socket reconnects (Socket Mode
        # authenticates at connect time) — hot-reload the listeners in-process so pasting
        # tokens works immediately, no sidecar restart (§19).
        from ..connectors.config import PLATFORMS

        if name in PLATFORMS:
            try:
                await manager.refresh_gateway()
            except Exception:
                pass  # a listener that fails to come up must not fail the save

    @app.post("/v1/connectors/{name}/connect")
    async def connector_connect(name: str, body: dict) -> dict[str, Any]:
        fields = body.get("fields") if isinstance(body, dict) else None
        # experimental connectors require the caller to explicitly acknowledge the risk notice
        acknowledged = bool(isinstance(body, dict) and body.get("acknowledge_risk"))
        # token validation does a blocking HTTP call → keep it off the event loop
        result = await asyncio.to_thread(
            lambda: manager.connect_connector(
                name, fields or {}, acknowledged=acknowledged
            )
        )
        if result.get("ok"):
            await _refresh_listeners_if_two_way(name)
        return result

    @app.post("/v1/connectors/{name}/mcp-connect")
    async def connector_mcp_connect(name: str) -> dict[str, Any]:
        # One-click connect for an MCP-backed connector: the browser OAuth flow can
        # take minutes, so it runs in the background; the GUI polls /v1/connectors
        # until the card flips to connected (mode "mcp").
        from ..connectors.descriptors import get_descriptor

        d = get_descriptor(name)
        if d is None or not d.mcp_url:
            return {"ok": False, "error": f"{name} has no MCP connect path"}
        asyncio.create_task(manager.mcp_connect_connector(name))
        return {"ok": True, "started": True}

    @app.post("/v1/connectors/{name}/disconnect")
    async def connector_disconnect(name: str) -> dict[str, Any]:
        # Managed profiles: best-effort flip of the cloud metadata record first
        # (network call → off the loop). Local deletion always proceeds.
        from .. import cloud
        from ..config import load_config

        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(manager.secrets, load_config(), name)
        )
        result = manager.disconnect_connector(name)
        await _refresh_listeners_if_two_way(name)
        return result

    @app.post("/v1/connectors/slack/workspaces/{team_id}/disconnect")
    async def slack_workspace_disconnect(team_id: str) -> dict[str, Any]:
        """Stop relaying one workspace (managed relay). Cloud routing row deleted
        best-effort, local per-team token removed, gateway hot-reloaded."""
        return await manager.disconnect_slack_workspace(team_id)

    @app.get("/v1/connectors/slack/status")
    async def slack_status() -> dict[str, Any]:
        """Slack health, three layers: relay socket / cloud sign-in / per-team tokens."""
        return manager.slack_status()

    @app.post("/v1/connectors/github/installations/{installation_id}/disconnect")
    async def github_installation_disconnect(installation_id: str) -> dict[str, Any]:
        """Stop relaying one GitHub App installation (managed relay). Cloud
        routing rows deleted best-effort, local profile removed, gateway
        hot-reloaded."""
        return await manager.disconnect_github_installation(installation_id)

    @app.get("/v1/connectors/github/status")
    async def github_status() -> dict[str, Any]:
        """GitHub health: relay socket / cloud sign-in / per-installation tokens."""
        return manager.github_status()

    @app.post("/v1/connectors/gmail/accounts/{email}/disconnect")
    async def gmail_account_disconnect(email: str) -> dict[str, Any]:
        """Drop ONE mailbox (cloud metadata best-effort first, like a full
        disconnect); the default pointer moves to the next account."""
        from .. import cloud
        from ..config import load_config
        from ..connectors import gmail_accounts

        profile_key = gmail_accounts.PREFIX + email.strip().lower()
        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(
                manager.secrets, load_config(), "gmail", profile_key=profile_key
            )
        )
        return gmail_accounts.disconnect_account(manager.secrets, email)

    @app.post("/v1/connectors/gmail/accounts/{email}/default")
    def gmail_account_default(email: str) -> dict[str, Any]:
        from ..connectors import gmail_accounts

        return gmail_accounts.set_default(manager.secrets, email)

    @app.patch("/v1/connectors/gmail/filters")
    def gmail_filters(body: dict) -> dict[str, Any]:
        """Replace the "Never show agents" lists. Enforced in the local tool
        layer; agents see silent omissions, the user sees counts + audit."""
        from ..connectors import gmail_accounts

        senders = body.get("senders") if isinstance(body, dict) else None
        labels = body.get("labels") if isinstance(body, dict) else None
        if senders is not None and not isinstance(senders, list):
            return {"ok": False, "error": "senders must be a list"}
        if labels is not None and not isinstance(labels, list):
            return {"ok": False, "error": "labels must be a list"}
        return gmail_accounts.set_filters(manager.secrets, senders, labels)

    @app.post("/v1/connectors/google_calendar/accounts/{email}/disconnect")
    async def gcal_account_disconnect(email: str) -> dict[str, Any]:
        """Drop ONE Google Calendar account (cloud metadata best-effort first);
        the default pointer moves to the next account."""
        from .. import cloud
        from ..config import load_config
        from ..connectors import gcal_accounts

        profile_key = gcal_accounts.PREFIX + email.strip().lower()
        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(
                manager.secrets,
                load_config(),
                "google_calendar",
                profile_key=profile_key,
            )
        )
        return gcal_accounts.disconnect_account(manager.secrets, email)

    @app.post("/v1/connectors/google_calendar/accounts/{email}/default")
    def gcal_account_default(email: str) -> dict[str, Any]:
        from ..connectors import gcal_accounts

        return gcal_accounts.set_default(manager.secrets, email)

    @app.post("/v1/connectors/hubspot/portals/{hub_id}/disconnect")
    async def hubspot_portal_disconnect(hub_id: str) -> dict[str, Any]:
        from .. import cloud
        from ..config import load_config
        from ..connectors import hubspot_portals

        profile_key = hubspot_portals.PREFIX + hub_id.strip()
        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(
                manager.secrets, load_config(), "hubspot", profile_key=profile_key
            )
        )
        return hubspot_portals.disconnect_portal(manager.secrets, hub_id)

    @app.post("/v1/connectors/hubspot/portals/{hub_id}/default")
    def hubspot_portal_default(hub_id: str) -> dict[str, Any]:
        from ..connectors import hubspot_portals

        return hubspot_portals.set_default(manager.secrets, hub_id)

    @app.post("/v1/connectors/{name}/accounts/{account_id}/disconnect")
    async def account_disconnect(name: str, account_id: str) -> dict[str, Any]:
        """Generic per-account disconnect for account-patterned connectors
        (batch 2+). Gmail/Calendar keep their specific email routes."""
        from .. import cloud
        from ..config import load_config
        from ..connectors import accounts

        if not accounts.is_account_connector(name):
            return {"ok": False, "error": "not a multi-account connector"}
        _id, profile_key, profile = accounts.resolve(manager.secrets, name, account_id)
        if profile and profile.get("managed"):
            await asyncio.to_thread(
                lambda: cloud.cloud_disconnect(
                    manager.secrets, load_config(), name, profile_key=profile_key
                )
            )
        return accounts.disconnect_account(manager.secrets, name, account_id)

    @app.post("/v1/connectors/{name}/accounts/{account_id}/default")
    def account_default(name: str, account_id: str) -> dict[str, Any]:
        from ..connectors import accounts

        if not accounts.is_account_connector(name):
            return {"ok": False, "error": "not a multi-account connector"}
        return accounts.set_default(manager.secrets, name, account_id)

    @app.patch("/v1/connectors/hubspot/hidden-fields")
    def hubspot_hidden_fields(body: dict) -> dict[str, Any]:
        """Replace the hidden-fields denylist (property names stripped from every
        record agents read — model-facing policy, not a human ACL)."""
        from ..connectors import hubspot_portals

        fields = body.get("hidden_fields") if isinstance(body, dict) else None
        if not isinstance(fields, list):
            return {"ok": False, "error": "hidden_fields must be a list"}
        return hubspot_portals.set_hidden_fields(manager.secrets, fields)

    @app.post("/v1/connectors/{name}/unauthorized/{item_id}")
    async def connector_unauthorized_resolve(
        name: str, item_id: str, body: dict
    ) -> dict[str, Any]:
        # Resolve a parked unauthorized message: dismiss / allow / allow_deliver (§19).
        action = str((body or {}).get("action", "")).strip()
        return await manager.resolve_unauthorized(name, item_id, action)

    # -- QunWork Cloud: sign-in + managed one-click connect ---------------
    # All optional: the app is fully functional signed out (manual token paste
    # stays available for every connector, before and after sign-in).

    @app.get("/v1/cloud/status")
    def cloud_status() -> dict[str, Any]:
        from .. import cloud

        return {
            **cloud.status(manager.secrets),
            "telemetry_enabled": cloud.telemetry_enabled(manager.secrets),
        }

    @app.post("/v1/cloud/telemetry")
    def cloud_telemetry(body: dict) -> dict[str, Any]:
        """The Phase 5 opt-out toggle. Local preference only — signed-out users
        send nothing regardless of this value."""
        from .. import cloud

        return cloud.set_telemetry_enabled(
            manager.secrets, bool((body or {}).get("enabled", True))
        )

    @app.post("/v1/cloud/login")
    def cloud_login() -> dict[str, Any]:
        """Start browser sign-in. The sidecar opens the system browser itself
        (works identically under Tauri and plain-browser dev)."""
        import webbrowser

        from .. import cloud
        from ..config import load_config

        out = cloud.begin_login(load_config())
        webbrowser.open(out["authorize_url"])
        return {"ok": True, "authorize_url": out["authorize_url"]}

    @app.post("/v1/cloud/logout")
    def cloud_logout() -> dict[str, Any]:
        from .. import cloud

        return cloud.logout(manager.secrets)

    @app.get("/auth/callback")
    async def cloud_auth_callback(code: str = "", state: str = "", error: str = ""):
        from fastapi.responses import HTMLResponse

        from .. import cloud
        from ..config import load_config

        signin_failed_detail = (
            "Close this tab and try signing in again from QunWork."
        )
        if error:
            return HTMLResponse(
                _browser_page(
                    "Sign-in failed", signin_failed_detail, ok=False, error=error
                ),
                status_code=400,
            )
        result = await asyncio.to_thread(
            lambda: cloud.complete_login(manager.secrets, load_config(), code, state)
        )
        if not result.get("ok"):
            return HTMLResponse(
                _browser_page(
                    "Sign-in failed",
                    signin_failed_detail,
                    ok=False,
                    error=result.get("error", ""),
                ),
                status_code=400,
            )

        # Restore managed connections in the background: best-effort metadata work
        # that must not hold the "Signed in" page (or the GUI's signed-in flip)
        # hostage to another broker round trip. Restored GitHub installs hot-add
        # the gateway so the relay connects without a restart.
        async def _restore_connections() -> None:
            try:
                out = await asyncio.to_thread(
                    lambda: cloud.sync_connections(manager.secrets, load_config())
                )
                if out.get("restored"):
                    await manager.refresh_gateway()
            except Exception:
                pass  # sign-in stands; the user can still connect by hand

        asyncio.get_running_loop().create_task(_restore_connections())
        return HTMLResponse(
            _browser_page(
                "Signed in",
                "You're signed in to QunWork Cloud. "
                "You can close this tab and return to QunWork.",
            )
        )

    @app.post("/v1/connectors/{name}/connect-managed")
    async def connector_connect_managed(
        name: str, body: Optional[dict] = None
    ) -> dict[str, Any]:
        """One-click managed OAuth (requires cloud sign-in). Opens the provider
        consent page in the system browser; the broker's callback page will
        form-POST the tokens to /oauth/callback below. `access` picks a consent
        tier by NAME (e.g. hubspot read | write) — the broker owns the scopes."""
        import webbrowser

        from .. import cloud
        from ..config import load_config
        from ..connectors.descriptors import get_descriptor

        d = get_descriptor(name)
        if d is not None and d.managed_paused:
            # GUI shows the Coming-soon state; this guard covers stale GUIs/API callers.
            return {
                "ok": False,
                "error": f"one-click connect for {d.title} is coming soon — connect manually for now",
            }
        access = str((body or {}).get("access") or "")
        flow = str((body or {}).get("flow") or "")  # github: "" install | "authorize"
        out = await asyncio.to_thread(
            lambda: cloud.begin_managed_connect(
                manager.secrets, load_config(), name, access=access, flow=flow
            )
        )
        if out.get("ok"):
            webbrowser.open(out["authorize_url"])
        return out

    @app.post("/oauth/callback")
    async def managed_oauth_callback(request: Request) -> Any:
        from fastapi.responses import HTMLResponse

        from .. import cloud
        from ..connectors.setup import (
            managed_connect_connector,
            managed_connect_slack_install,
        )

        form = await request.form()
        data = {k: str(v) for k, v in form.items()}
        connector = data.get("connector", "")
        if not cloud.consume_managed_state(data.get("app_state", "")):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error="unknown or expired connection attempt",
                ),
                status_code=400,
            )
        if data.get("error"):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error=data["error"],
                ),
                status_code=400,
            )
        # Managed GitHub deliberately carries NO token fields — the loopback POST
        # is routing metadata only (installation tokens are minted on demand,
        # github-relay-spec §4) — so its branch precedes the access_token check.
        if connector == "github" and data.get("installation_id"):
            from ..connectors.github_installs import managed_connect_install

            result = managed_connect_install(manager.secrets, data)
            if result.get("ok"):
                await manager.refresh_gateway()  # hot-add, like a workspace
            if not result.get("ok"):
                return HTMLResponse(
                    _browser_page(
                        "Connection failed",
                        _CONNECT_FAILED_DETAIL,
                        ok=False,
                        error=result.get("error", ""),
                    ),
                    status_code=400,
                )
            return HTMLResponse(
                _browser_page(
                    "GitHub connected",
                    "You can close this tab and return to QunWork.",
                    connector="github",
                )
            )
        if not connector or not data.get("access_token"):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error="missing fields",
                ),
                status_code=400,
            )
        # Managed Slack is multi-workspace + relay: store the per-team bot token
        # and flip to relay mode, rather than the single-token connector path.
        if connector == "slack" and data.get("team_id"):
            result = managed_connect_slack_install(manager.secrets, data)
            if result.get("ok"):
                # Hot-add: rebuild the gateway so the new workspace's token loads
                # (and the relay socket opens on a first-ever install) right away.
                await manager.refresh_gateway()
        elif connector == "gmail":
            # Multi-account: each sign-in lands in its own gmail:account:<email>
            # profile; the first becomes the default mailbox.
            from ..connectors import gmail_accounts

            result = gmail_accounts.managed_connect_account(
                manager.secrets, cloud.managed_profile_from_callback(data)
            )
        elif connector == "google_calendar":
            # Multi-account, same shape as gmail: google_calendar:account:<email>.
            from ..connectors import gcal_accounts

            result = gcal_accounts.managed_connect_account(
                manager.secrets, cloud.managed_profile_from_callback(data)
            )
        elif connector == "hubspot" and data.get("hub_id"):
            # Multi-portal: keyed by hub_id (broker sends it like Slack's team_id).
            from ..connectors import hubspot_portals

            profile = cloud.managed_profile_from_callback(data)
            profile["hub_id"] = data.get("hub_id", "")
            if data.get("sandbox"):
                profile["sandbox"] = True
            result = hubspot_portals.managed_connect_portal(manager.secrets, profile)
        else:
            result = managed_connect_connector(
                manager.secrets, connector, cloud.managed_profile_from_callback(data)
            )
        if not result.get("ok"):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error=result.get("error", ""),
                ),
                status_code=400,
            )
        return HTMLResponse(
            _browser_page(
                f"{_connector_title(connector)} connected",
                "You can close this tab and return to QunWork.",
                connector=connector,
            )
        )

    @app.patch("/v1/connectors/{name}/tools")
    def connector_tools_patch(name: str, body: dict) -> dict[str, Any]:
        enabled = (body or {}).get("enabled")
        if not isinstance(enabled, dict):
            return {"ok": False, "error": "enabled map required"}
        return manager.update_connector_tools(name, enabled)

    @app.post("/v1/connectors/{name}/allow")
    def connector_allow(name: str, body: dict) -> dict[str, Any]:
        # `team_id` scopes the edit to one workspace (managed relay); absent → flat list.
        # `name` (optional) seeds the people directory so a directory-picked user's
        # chip shows their display name before they've ever sent a message.
        return manager.allow_user(
            name,
            str(body.get("user_id", "")),
            str(body.get("team_id", "")) or None,
            display_name=str(body.get("name", "")),
        )

    @app.get("/v1/connectors/slack/workspaces/{team_id}/directory")
    async def slack_directory(
        team_id: str, q: str = "", limit: int = 25
    ) -> dict[str, Any]:
        """Workspace member roster for the people picker (team_id "default" =
        the manual Socket-Mode workspace). Cached locally; never leaves this machine."""
        from ..connectors import slack_directory as roster

        return await asyncio.to_thread(
            lambda: roster.list_members(manager.secrets, team_id, q, limit)
        )

    @app.get("/v1/connectors/slack/workspaces/{team_id}/channels")
    async def slack_channels(
        team_id: str, q: str = "", limit: int = 25
    ) -> dict[str, Any]:
        """Channel roster for the channel typeahead: all public channels, private
        ones only where the bot is a member (Slack API constraint)."""
        from ..connectors import slack_directory as roster

        return await asyncio.to_thread(
            lambda: roster.list_channels(manager.secrets, team_id, q, limit)
        )

    @app.post("/v1/connectors/{name}/disallow")
    def connector_disallow(name: str, body: dict) -> dict[str, Any]:
        return manager.disallow_user(
            name, str(body.get("user_id", "")), str(body.get("team_id", "")) or None
        )

    # -- audit / browser observability ------------------------------------------
    @app.get("/v1/audit")
    def audit_list(
        limit: int = 100,
        session_id: str | None = None,
        connector: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return {
            "events": manager.list_audit(
                limit=limit, session_id=session_id, connector=connector, tool=tool
            )
        }

    @app.get("/v1/browser/state")
    def browser_state_get() -> dict[str, Any]:
        return manager.browser_state()

    @app.post("/v1/browser/screenshot")
    def browser_screenshot_post() -> dict[str, Any]:
        return manager.browser_screenshot()

    @app.post("/v1/browser/close")
    def browser_close_post() -> dict[str, Any]:
        return manager.browser_close()

    # -- web search -------------------------------------------------------------
    @app.get("/v1/web-search")
    def web_search_get() -> dict[str, Any]:
        return manager.get_web_search()

    @app.post("/v1/web-search")
    def web_search_set(body: dict) -> dict[str, Any]:
        provider = (body or {}).get("provider", "")
        if not provider:
            return {"ok": False, "error": "provider required"}
        return manager.set_web_search(provider, (body or {}).get("api_key"))

    # -- model providers (OpenAI, Ollama, …) ------------------------------------
    @app.get("/v1/providers")
    def providers_get() -> list[dict[str, Any]]:
        return manager.get_providers()

    @app.post("/v1/providers")
    def providers_set(body: dict) -> dict[str, Any]:
        name = (body or {}).get("name", "")
        if not name:
            return {"ok": False, "error": "name required"}
        return manager.set_provider(name, (body or {}).get("fields"))

    @app.delete("/v1/providers/{name}")
    def providers_remove(name: str) -> dict[str, Any]:
        return manager.remove_provider(name)

    @app.post("/v1/providers/verify")
    async def providers_verify(body: dict) -> dict[str, Any]:
        # Live read-only credential check (sync httpx) — run off the event loop.
        name = (body or {}).get("name", "") or "openai"
        return await asyncio.to_thread(
            manager.verify_provider, name, (body or {}).get("fields")
        )

    # -- settings (model API key) -----------------------------------------------
    @app.get("/v1/settings")
    def settings_get() -> dict[str, Any]:
        return manager.get_settings()

    @app.post("/v1/settings/model-key")
    def settings_set_model_key(body: dict) -> dict[str, Any]:
        return manager.set_model_key((body or {}).get("api_key", ""))

    @app.post("/v1/settings/default-model")
    def settings_set_default_model(body: dict) -> dict[str, Any]:
        return manager.set_default_model((body or {}).get("model", ""))

    @app.post("/v1/settings/models/add")
    def settings_models_add(body: dict) -> dict[str, Any]:
        return manager.add_model((body or {}).get("model", ""))

    @app.post("/v1/settings/models/remove")
    def settings_models_remove(body: dict) -> dict[str, Any]:
        return manager.remove_model((body or {}).get("model", ""))

    @app.post("/v1/settings/onboarded")
    def settings_set_onboarded(body: dict) -> dict[str, Any]:
        return manager.set_onboarded(bool((body or {}).get("value", True)))

    @app.post("/v1/settings/experimental-connectors")
    def settings_set_experimental(body: dict) -> dict[str, Any]:
        return manager.set_experimental_connectors(bool((body or {}).get("value")))

    @app.post("/v1/settings/surfaces")
    def settings_set_surfaces(body: dict) -> dict[str, Any]:
        b = body or {}
        return manager.set_surfaces(chat=b.get("chat"), code=b.get("code"))

    @app.post("/v1/settings/scratch-base")
    def settings_set_scratch_base(body: dict) -> dict[str, Any]:
        return manager.set_scratch_base(str((body or {}).get("path", "")))

    @app.post("/v1/settings/nav-layout")
    def settings_set_nav_layout(body: dict) -> dict[str, Any]:
        return manager.set_nav_layout(str((body or {}).get("nav_layout", "")))

    @app.post("/v1/settings/sessions-peek")
    def settings_set_sessions_peek(body: dict) -> dict[str, Any]:
        # Sidebar: sessions shown per group before "Show more" (owner ask, 2026-07-03).
        return manager.set_sessions_peek((body or {}).get("sessions_peek", 5))

    @app.post("/v1/settings/pdf")
    def settings_set_pdf(body: dict) -> dict[str, Any]:
        # Token savings (owner ask, 2026-07-17): fallback mode for models without native
        # PDF support + attach-time page/size thresholds.
        b = body or {}
        return manager.set_pdf_settings(
            fallback=b.get("pdf_fallback"),
            max_pages=b.get("pdf_max_pages"),
            max_mb=b.get("pdf_max_mb"),
        )

    @app.post("/v1/attachments/inspect-pdf")
    def attachments_inspect_pdf(body: dict) -> dict[str, Any]:
        # Attach-time page/size probe for the composer's threshold check. Local only.
        from ..pdf_support import inspect

        return inspect(str((body or {}).get("data_url", "")))

    # -- direct-message routing -------------------------------------------------
    @app.get("/v1/messaging/dm-route")
    def dm_route_get() -> dict[str, Any]:
        return {"dm_session": manager.dm_session()}

    @app.post("/v1/messaging/dm-route")
    def dm_route_set(body: dict) -> dict[str, Any]:
        # A falsy session_id clears the designation (DMs then park as unrouted).
        return manager.set_dm_session((body or {}).get("session_id", ""))

    if os.environ.get("COWORKER_DEBUG_INJECT") == "1":
        # Dev-only (env-gated, localhost): feed a message through the real inbound path so the
        # messaging stack can be exercised without a live bot connection. Not registered otherwise.
        @app.post("/v1/_debug/inject_inbound")
        async def debug_inject_inbound(body: dict) -> dict[str, Any]:
            from ..connectors.base import MessageEvent, SessionSource

            event = MessageEvent(
                text=str((body or {}).get("text", "")),
                source=SessionSource(
                    platform=str(body.get("platform", "slack")),
                    chat_id=str(body.get("chat_id", "C0BD7KZ1AH5")),
                    user_id=str(body.get("user_id", "U07JK68S4BH")),
                    user_name=str(body.get("user_name", "tester")),
                    chat_type=str(body.get("chat_type", "channel")),
                    chat_name=str(body.get("chat_name", "")) or None,
                    thread_id=str(body.get("thread_ts", "")) or None,
                    team_id=str(body.get("team_id", "")) or None,
                ),
                message_id=str(body.get("ts", "")) or None,
                # §31 mention router: the flag is normally computed from the raw Slack text
                # at mapping time; the injector sets it directly.
                mentions_me=bool(body.get("mentions_me")),
            )
            await manager._dispatch_inbound(event)
            return {"ok": True}

    # -- automations (scheduled tasks) ------------------------------------------
    @app.get("/v1/automations")
    def automations_list() -> dict[str, Any]:
        return manager.list_automations()

    @app.post("/v1/automations")
    def automations_create(body: dict) -> dict[str, Any]:
        return manager.create_automation(body or {})

    @app.get("/v1/automations/{task_id}")
    def automation_get(task_id: str) -> dict[str, Any]:
        return manager.get_automation(task_id)

    @app.patch("/v1/automations/{task_id}")
    def automation_update(task_id: str, body: dict) -> dict[str, Any]:
        return manager.update_automation(task_id, body or {})

    @app.delete("/v1/automations/{task_id}")
    def automation_delete(task_id: str) -> dict[str, Any]:
        return manager.delete_automation(task_id)

    @app.post("/v1/automations/{task_id}/seen")
    def automations_seen(task_id: str) -> dict[str, Any]:
        return manager.mark_automation_seen(task_id)

    @app.post("/v1/automations/{task_id}/run")
    def automation_run(task_id: str) -> dict[str, Any]:
        # Prepare a live manual run; the GUI opens the returned session and drives it.
        return manager.prepare_manual_run(task_id)

    @app.post("/v1/automations/{task_id}/runs/{run_id}/finalize")
    def automation_run_finalize(task_id: str, run_id: str) -> dict[str, Any]:
        return manager.finalize_manual_run(task_id, run_id)

    @app.websocket("/ws/session/{session_id}")
    async def ws_session(ws: WebSocket, session_id: str) -> None:
        if not _websocket_authenticated(ws):
            await ws.close(code=1008)
            return
        # CORS never gates WebSockets, so a cross-site page could otherwise open this socket
        # and drive the session into tool calls. Reject a disallowed browser Origin before
        # accepting the handshake (1008 = policy violation).
        if not _origin_allowed(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept(subprotocol="qunwork" if api_token else None)
        agent = ws.query_params.get("agent") or "code"

        # All four interactive prompts (approval / question / directory / plan) are parked as Inbox
        # items and awaited via inbox.wait — so they survive a dropped socket (redelivered on
        # reconnect) and can be resolved from any surface. `visibility` decides where they SHOW:
        # Unattended → the cross-session Inbox; attended → inline in this session only. The agent
        # stays blocked until the item is resolved (live WS response, REST, or a bound channel).
        def _visibility() -> str:
            return (
                VIS_INBOX
                if manager.unattended.is_unattended(session_id)
                else VIS_INLINE
            )

        async def _mirror(item) -> None:
            # Unattended items mirror to a bound channel as buttons (see mirror_inbox_item).
            await manager.mirror_inbox_item(item)

        def _route() -> str:
            return manager.inbox_routing.route_for(session_id, agent)

        async def approver(_request) -> ApprovalOutcome:
            # The engine has already emitted PERMISSION_REQUIRED (the live inline card). Park the
            # item so the answer can also come from the Inbox / a reconnect / after a restart.
            item = manager.inbox.add_approval(
                session_id,
                f"Run `{_request.tool_name}`?",
                body="\n".join(
                    p
                    for p in (
                        (getattr(_request, "reason", "") or "").strip(),
                        args_preview(getattr(_request, "arguments", None)),
                    )
                    if p
                ),
                inbox=_route(),
                visibility=_visibility(),
                # Automation-run context (manual "Run now" rides this socket): lets the
                # card offer the task-persistent "Allow every time" (§25). {} elsewhere.
                data=manager.approval_prompt_data(session_id, _request),
                tool_call_id=getattr(_request, "tool_call_id", None),
            )
            if (
                item.state == "pending"
            ):  # freshly raised (not a durable-resume re-raise)
                manager.persist_session(
                    session_id
                )  # the pending tool call is now on disk
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resolution = await manager.inbox.wait(item.id)
            # Accept every vocabulary: the live card sends once/always_tool/always_command/
            # always_task/deny; the Inbox / a channel send allow/always/deny.
            return manager.approval_outcome(resolution, _request, session_id)

        async def question_asker(args: dict, tool_call_id=None) -> dict:
            # ask_user (engine does NOT emit the event — we do, only when attended).
            item = manager.inbox.add_question(
                session_id,
                str(args.get("question", "")),
                inbox=_route(),
                visibility=_visibility(),
                options=list(args.get("options") or []),
                allow_text=bool(args.get("allow_text", True)),
                multi=bool(args.get("multi", False)),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
                else:
                    await ws.send_json(
                        {
                            "type": "question_requested",
                            "data": {
                                "question": item.title,
                                "options": item.options,
                                "allow_text": item.allow_text,
                                "multi": item.multi,
                                "header": str(args.get("header", "")),
                            },
                        }
                    )
            return {"answer": await manager.inbox.wait(item.id)}

        async def directory_requester(args: dict, tool_call_id=None) -> dict:
            # The engine has already emitted DIRECTORY_REQUESTED. Park, await, then apply the grant.
            item = manager.inbox.add_directory(
                session_id,
                "Grant access to a folder?",
                body=str(args.get("reason", "")),
                inbox=_route(),
                visibility=_visibility(),
                data={
                    "path": str(args.get("path", "")),
                    "writable": bool(args.get("writable", False)),
                },
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resp = _parse_json(
                await manager.inbox.wait(item.id)
            )  # {granted, path, writable}
            if not resp.get("granted"):
                return {"granted": False, "reason": "the user declined the request"}
            path = (resp.get("path") or args.get("path") or "").strip()
            if not path:
                return {"granted": False, "error": "no directory was provided"}
            writable = bool(resp.get("writable", args.get("writable", False)))
            res = manager.add_root(session_id, path, writable)
            if not res.get("ok"):
                return {
                    "granted": False,
                    "error": res.get("error", "could not grant access"),
                }
            primary = next(
                (
                    r
                    for r in res.get("roots", [])
                    if r.get("path")
                    and Path(r["path"]).expanduser().resolve()
                    == Path(path).expanduser().resolve()
                ),
                None,
            )
            return {
                "granted": True,
                "path": (primary or {}).get("path", path),
                "writable": writable,
            }

        async def plan_approver(_args: dict, tool_call_id=None) -> dict:
            # The engine has already emitted PLAN_PROPOSED. Park, await the verdict.
            item = manager.inbox.add_plan(
                session_id,
                "Approve the plan?",
                body=str(_args.get("plan", "")),
                inbox=_route(),
                visibility=_visibility(),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resp = _parse_json(
                await manager.inbox.wait(item.id)
            )  # {approved, mode, feedback}
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user rejected the plan",
                }
            return {"approved": True, "mode": resp.get("mode") or "interactive"}

        async def _apply_model(model: Optional[str]) -> None:
            # Mid-session rebind is allowed (roadmap item 3, supersedes the 2026-07-04
            # lock): history is canonical and providers convert per call. A real switch
            # appends a persisted notice; broadcast it so live views render the marker
            # and update their header. Never rebind mid-turn — the running loop reads
            # `engine.model` per iteration and a mixed turn is exactly the breakage the
            # old lock existed to prevent.
            if not model or manager.is_running(session_id):
                return
            notice = engine.switch_model(model)
            if notice is None:  # same model, or first bind on a fresh session
                return
            manager.persist_session(session_id)
            await manager.broadcast_session(
                session_id,
                {"type": "model_changed", "data": {"model": model, "text": notice}},
            )

        def _resolve_pending(resolution: str) -> None:
            # Live WS responses resolve THE session's single pending prompt (one at a time, since the
            # agent blocks). Reconnect / Inbox resolve by id via REST instead.
            pend = manager.inbox.pending(session_id)
            if pend:
                manager.inbox.resolve(pend[0].id, resolution)

        workspace = ws.query_params.get("workspace")
        mcp_tools = await manager.prepare_mcp_tools(
            session_id, workspace=workspace, agent=agent
        )
        engine = manager.get_engine(
            session_id,
            workspace=workspace,
            agent=agent,
            approver=approver,
            extra_tools=mcp_tools,
            directory_requester=directory_requester,
            plan_approver=plan_approver,
            question_asker=question_asker,
        )
        if engine is None:
            await ws.send_json(
                {
                    "type": "error",
                    "data": {
                        "error": "no valid workspace — choose a project folder first"
                    },
                }
            )
            await ws.close()
            return
        await ws.send_json(
            {
                "type": "ready",
                "data": {
                    "session_id": session_id,
                    "agent": getattr(engine, "agent_name", "code"),
                    "model": engine.model,
                    "mode": engine.permissions.mode.value,
                    "workspace": (
                        str(getattr(engine, "executor").cwd)
                        if getattr(engine, "executor", None)
                        else None
                    ),
                    "command_trust": manager.workspace_command_trust(
                        str(getattr(engine, "audit_context", {}).get("workspace", ""))
                    ),
                },
            }
        )

        # Checkpoint events: persist mid-turn so a crash/quit can't eat the conversation.
        # turn_start = the user message just landed (a brand-new session gets its row here,
        # not at connect — empty never-used sessions shouldn't appear in Recents);
        # permission_required/directory_requested = parked indefinitely on the user;
        # iteration_end = a model response + its tool results completed.
        _CHECKPOINTS = {
            "turn_start",
            "permission_required",
            "directory_requested",
            "plan_proposed",
            "iteration_end",
        }

        async def run_turn(content, *, retry: bool = False) -> None:
            # The receive loop atomically claims this session before scheduling the task.
            # Keeping the claim outside prevents two back-to-back frames from both starting.
            try:
                events = engine.retry() if retry else engine.run(content)
                async for event in events:
                    # Broadcast to every socket viewing this session (this socket included — it's a
                    # registered client), so a second view of the same session stays in sync too.
                    await manager.broadcast_session(
                        session_id, {"type": event.type.value, "data": event.data}
                    )
                    if event.type.value in _CHECKPOINTS:
                        manager.save(session_id, engine)
            finally:
                manager.mark_idle(session_id)
                manager.save(session_id, engine)
                await manager.broadcast_session(
                    session_id, {"type": "turn_done", "data": {}}
                )

        # This socket is now a live view of the session; background turns (channel delivery,
        # self-wake, durable resume) broadcast here too, not just locally driven run_turns.
        manager.register_session_client(session_id, ws.send_json)
        inbound_times: deque[float] = deque()

        async def reject_input(reason: str) -> None:
            # Input validation failures are not provider failures and must not offer "Retry"
            # or flush an in-progress assistant stream in the GUI.
            await ws.send_json({"type": "input_rejected", "data": {"error": reason}})

        async def claim_turn(*, retry: bool = False, content=None) -> None:
            if not manager.try_mark_running(session_id):
                await reject_input(
                    "This session is already running a turn. Wait for it to finish or stop it."
                )
                return
            asyncio.create_task(run_turn(content, retry=retry))

        try:
            while True:
                try:
                    message = await ws.receive_json()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    await reject_input("Invalid WebSocket message: expected JSON.")
                    continue

                now = asyncio.get_running_loop().time()
                while (
                    inbound_times
                    and now - inbound_times[0] > _WS_RATE_LIMIT_WINDOW_SECONDS
                ):
                    inbound_times.popleft()
                if len(inbound_times) >= _WS_RATE_LIMIT_COUNT:
                    await reject_input("Too many WebSocket messages; reconnect and try again.")
                    await ws.close(code=1008)
                    return
                inbound_times.append(now)

                if not isinstance(message, dict):
                    await reject_input("Invalid WebSocket message: expected an object.")
                    continue
                kind = message.get("type")
                if not isinstance(kind, str):
                    await reject_input("Invalid WebSocket message: missing string type.")
                    continue
                if kind == "approval":
                    _resolve_pending(message.get("decision", "deny"))
                elif kind == "directory_response":
                    _resolve_pending(
                        json.dumps(
                            {
                                "granted": bool(message.get("granted")),
                                "path": message.get("path", ""),
                                "writable": bool(message.get("writable", False)),
                            }
                        )
                    )
                elif kind == "plan_response":
                    _resolve_pending(
                        json.dumps(
                            {
                                "approved": bool(message.get("approved")),
                                "mode": message.get("mode", "interactive"),
                                "feedback": message.get("feedback", ""),
                            }
                        )
                    )
                elif kind == "question_response":
                    _resolve_pending(str(message.get("answer", "")))
                elif kind == "interrupt":
                    engine.request_interrupt()
                elif kind == "retry":
                    # Re-run after a provider error (engine guards on the error-notice
                    # tail, so a stray frame is a no-op that still ends with turn_done).
                    await claim_turn(retry=True)
                elif kind == "set_mode":
                    try:
                        engine.permissions.mode = Mode(message.get("mode"))
                    except (TypeError, ValueError):
                        pass
                elif kind == "set_model":
                    model = message.get("model")
                    if model is not None and not isinstance(model, str):
                        await reject_input("Invalid model: expected a string.")
                    else:
                        await _apply_model(model)
                elif kind == "user_message":
                    raw_text = message.get("text")
                    if raw_text is None:
                        raw_text = ""
                    if not isinstance(raw_text, str):
                        await reject_input("Invalid message text: expected a string.")
                        continue
                    text = raw_text.strip()
                    raw_attachments = message.get("attachments")
                    attachments = [] if raw_attachments is None else raw_attachments
                    # Reject an oversized frame instead of buffering it into a turn. Send a
                    # visible error so the surface can tell the user, and drop the message.
                    if not isinstance(attachments, list):
                        await reject_input("Invalid attachments: expected a list.")
                        continue
                    reject = None
                    if len(text) > _MAX_MESSAGE_TEXT_CHARS:
                        reject = (
                            f"Message too long ({len(text)} chars; "
                            f"limit {_MAX_MESSAGE_TEXT_CHARS})."
                        )
                    elif len(attachments) > _MAX_ATTACHMENTS:
                        reject = (
                            f"Too many attachments ({len(attachments)}; "
                            f"limit {_MAX_ATTACHMENTS})."
                        )
                    elif any(not isinstance(a, dict) for a in attachments):
                        reject = "Invalid attachment: expected an object."
                    elif _json_value_size(attachments) > _MAX_ATTACHMENTS_BYTES:
                        reject = "Attachments too large (limit 15 MB per message)."
                    else:
                        for attachment in attachments:
                            attachment_kind = attachment.get("kind")
                            name = attachment.get("name")
                            mime = attachment.get("mime")
                            if attachment_kind not in {"image", "pdf", "text"}:
                                reject = "Invalid attachment kind."
                            elif name is not None and (
                                not isinstance(name, str) or len(name) > 1024
                            ):
                                reject = "Invalid attachment name."
                            elif mime is not None and (
                                not isinstance(mime, str) or len(mime) > 255
                            ):
                                reject = "Invalid attachment MIME type."
                            elif attachment_kind == "image":
                                data = attachment.get("data_url")
                                if (
                                    not isinstance(data, str)
                                    or not data.startswith("data:image/")
                                    or ";base64," not in data
                                ):
                                    reject = "Invalid image attachment."
                                elif len(data) > MAX_IMAGE_CHARS:
                                    # 明确报错而非静默丢弃(owner bug 2026-08-18: 大图
                                    # 被静默跳过 → 用户以为"LLM 没反应")。
                                    img_name = attachment.get("name") or "image"
                                    mb = len(data) / (1024 * 1024)
                                    limit_mb = MAX_IMAGE_CHARS / (1024 * 1024)
                                    reject = (
                                        f'Image "{img_name}" is too large to attach '
                                        f"({mb:.1f} MB encoded; limit ≈{limit_mb:.0f} MB). "
                                        "The image was NOT sent — your text (if any) was "
                                        "still sent. Compress the image or pick a smaller one."
                                    )
                            elif attachment_kind == "pdf":
                                data = attachment.get("data_url")
                                if (
                                    not isinstance(data, str)
                                    or not data.startswith(
                                        "data:application/pdf;base64,"
                                    )
                                    or len(data) > MAX_PDF_CHARS
                                ):
                                    reject = "Invalid or oversized PDF attachment."
                            else:
                                body = attachment.get("text")
                                if (
                                    not isinstance(body, str)
                                    or len(body) > MAX_TEXT_CHARS
                                ):
                                    reject = "Invalid or oversized text attachment."
                            if reject is not None:
                                break
                    if reject is not None:
                        await reject_input(reject)
                        continue
                    # The composer sends its visible model with every message — the FIRST
                    # one binds the session (race-proof across reconnects; see api.ts
                    # Session.userMessage), later ones may switch it (notice persisted).
                    model = message.get("model")
                    if model is not None and not isinstance(model, str):
                        await reject_input("Invalid model: expected a string.")
                        continue
                    await _apply_model(model)
                    if text or attachments:
                        # Image handling (fix 2026-08): the image_url part is ALWAYS
                        # kept in the message (GUI history renders it, vision models
                        # read it, and the engine converts it to a placeholder for
                        # text-only models per call). We ALSO save the image files to
                        # the session workspace (.qunwork_attachments/) and add a
                        # "[image: path]" text part so the image-understanding / OCR
                        # skill can analyze them by path when the model is text-only.
                        _rec = manager.session_store.load(session_id)
                        _ws_dir = (
                            _rec.workspace if _rec and _rec.workspace else manager.default_workspace
                        )
                        _img_dir = (
                            Path(_ws_dir).expanduser() / ".qunwork_attachments"
                            if _ws_dir
                            else None
                        )
                        content = build_user_content(
                            text,
                            attachments,
                            save_images_to=_img_dir,
                        )
                        if manager.is_running(session_id):
                            # Running turn: accept the draft as a SUPPLEMENT instead of
                            # rejecting it. manager._engines[session_id] is the session-wide
                            # shared engine (all views of a session hold the same instance),
                            # so the injection lands on the turn that is actually running.
                            # queue_steering appends it as a user message before the next
                            # model round — and if the run is about to end, the engine's
                            # steering path forces one more round to honour it.
                            _running = manager.get_engine(session_id)
                            if _running is not None:
                                _running.queue_steering(
                                    content,
                                    source={"supplement": True, "display": "supplement"},
                                )
                                await ws.send_json(
                                    {
                                        "type": "supplement_accepted",
                                        "data": {"text": text},
                                    }
                                )
                            else:
                                await reject_input(
                                    "This session is busy and has no live engine to "
                                    "accept a supplement."
                                )
                        else:
                            await claim_turn(content=content)
                else:
                    await reject_input(f"Unknown WebSocket message type: {kind}.")
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            # 未捕获异常不应导致 sidecar 崩溃(WebSocket 断连/模型调用异常等)
            import logging
            logging.getLogger("qunwork.ws").error(
                "WebSocket handler error (session %s): %s", session_id, exc, exc_info=True
            )
        finally:
            manager.unregister_session_client(session_id, ws.send_json)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        """App-wide event stream (session-independent): the GUI keeps one open for
        pushes like automation_run_started (the UX-026 toast). Read-only — inbound
        frames are ignored; the receive loop just detects disconnect."""
        if not _websocket_authenticated(ws):
            await ws.close(code=1008)
            return
        if not _origin_allowed(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept(subprotocol="qunwork" if api_token else None)
        manager.register_event_client(ws.send_json)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.unregister_event_client(ws.send_json)

    return app


def _parse_json(s: str) -> dict[str, Any]:
    """Parse a structured Inbox resolution (directory/plan carry their reply as a JSON string)."""
    try:
        v = json.loads(s) if s else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _openai_response(model: str, turn: AssistantTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in turn.tool_calls
        ]
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:12],
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": turn.finish_reason or "stop",
            }
        ],
    }
