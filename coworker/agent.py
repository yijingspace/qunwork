"""Engine assembly from an Agent (Code / Chat / …).

Wires the agent's base tools + permissions + AGENTS.md (workspace agents) + memory +
the skill catalog (progressive disclosure) + load_skill into a TurnEngine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .agents import Agent, AgentContext, code_agent
from .automation import scheduling_tools
from .selfwake import selfwake_tools
from .subscriptions import subscription_tools
from .config import load_config
from .connectors import (
    connector_list,
    load_settings,
    make_integration_tools,
    make_send_file_tool,
    make_send_message_tool,
)
from .engine import Approver, TurnEngine
from .environment import environment_context
from .memory import MemoryStore, Scope, format_memories, memory_tools
from .permissions import Mode, PermissionEngine
from .project import load_agents_md
from .roots import RootDir, normalize_roots, render_context
from .providers import ProviderClient, ProviderRouter
from .overrides import RiskOverrideStore
from .secrets import SecretStore, state_dir
from .skills import SkillLoader, skill_catalog_text, skill_tools
from .tools import ToolRegistry
from .tools.ask import ask_user_tool
from .tools.directories import request_directory_tool
from .tools.plan import propose_plan_tool
from .orchestrator import orchestration_tools
from .tools.subagent import explorer_tools
from .web import make_web_fetch_tool, make_web_search_tool
from .workspace_trust import WorkspaceTrustStore
from .tools.shell import LocalExecutor
from .tools.todo import TodoList

# Appended each turn while discuss mode is active: enforcement-only read-only, with no
# pressure toward a plan proposal (that's what distinguishes it from plan mode).
_DISCUSS_MODE_CONTEXT = """\
Discuss mode is active: write and shell tools are disabled. Explore and answer freely; if
the user asks for a change, describe it in chat instead of attempting it (they can switch
to plan or approval mode to have you make it)."""

# Appended to the latest user message every turn while plan mode is active. The mode can
# flip mid-session (plan approval), so this can't live in the static instructions.
_PLAN_MODE_CONTEXT = """\
Plan mode is active: write and shell tools are blocked. Explore read-only and design an
approach. When you've committed to one, present it with `propose_plan` (what you'll change,
in which files, how you'll verify) — don't describe edits as if you were making them. If
the plan is approved, this same session switches to execution and you implement it; if
rejected, revise the plan using the feedback."""

# When-to-remember rules, injected only when a memory store is wired. Without these,
# models either never call `remember` or save noise the repo already records.
_MEMORY_GUIDANCE = """\
Memory:
- You have persistent memory across sessions. Use `remember` for durable facts: the user's \
corrections and stated preferences (include the why), and project context you couldn't \
rederive from the code. Don't save what the repo already records (code structure, git \
history, AGENTS.md) or details that only matter to the current task. Use absolute dates, \
never "yesterday".
- Before saving, check the known-memories list: if an entry already covers it, revise that \
entry with `memory_update` instead of adding a near-duplicate; retire wrong or obsolete \
entries with `memory_forget`.
- Memories reflect when they were written. If one names a file, flag, or URL, verify it \
still exists before relying on it."""

# UX-015 (§33): the GUI interleaves these status lines with humanized tool rows inside a
# collapsed "turn" — they're what the user reads while the agent works. Universal (appended
# for every persona); models that ignore it degrade gracefully to a turn with no narration.
_NARRATION_GUIDANCE = """\
Narration: before each batch of tool calls, write ONE short plain sentence saying what \
you're doing and why (e.g. "Checking what merged since yesterday's digest."). It is shown \
to the user as live progress. Don't narrate trivial single-call follow-ups, don't repeat \
the previous line, and never let narration replace your final answer."""


def _enabled_connector_tools(secrets: SecretStore) -> tuple[set[str], set[str]]:
    connectors = {c["name"]: c for c in connector_list(secrets)}
    enabled_connectors = {
        name
        for name, c in connectors.items()
        if c.get("connected") and c.get("enabled")
    }
    enabled_tools = {
        tool["name"]
        for c in connectors.values()
        if c.get("name") in enabled_connectors
        for tool in c.get("tools", [])
        if tool.get("enabled")
    }
    return enabled_connectors, enabled_tools


def _skill_dirs(workspace: Optional[Path]) -> list[Path]:
    # Built-in skills ship inside the app bundle (coworker/skills — read-only
    # layer, e.g. vision/image-understanding). User skills live in the state
    # dir and (workspace-scoped) .coworker/skills — scanned later so they win
    # on name conflicts.
    dirs = [Path(__file__).resolve().parent / "skills"]
    dirs.append(state_dir() / "skills")
    if workspace is not None:
        dirs.append(workspace / ".coworker" / "skills")
    return dirs


def text_stats_tool() -> Any:
    """UTF-8-safe Chinese text statistics — workers must use this instead of
    PowerShell inline scripts (ANSI mojibake has repeatedly stalled them)."""
    import aisuite as ai

    def text_stats(text: str) -> dict:
        """Count characters in a string safely (no shell). Useful to verify a
        Chinese deliverable's length. Returns han_chars (CJK), total_chars,
        lines, and a boolean has_non_ascii."""
        han = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        return {
            "han_chars": han,
            "total_chars": len(text),
            "lines": text.count("\n") + 1,
            "has_non_ascii": any(ord(c) > 127 for c in text),
        }

    return ai.tool(
        text_stats,
        metadata=ai.ToolMetadata(
            category="text", risk_level="low", capabilities=["text_stats"]
        ),
    )


def build_engine(
    *,
    agent: Agent,
    workspace: Optional[str | Path] = None,
    model: str = "gpt-5.6-sol",
    mode: Mode = Mode.INTERACTIVE,
    approver: Optional[Approver] = None,
    provider: Optional[ProviderClient] = None,
    allowed_commands: Optional[list[str]] = None,
    max_iterations: Optional[int] = None,
    model_settings: Optional[dict[str, Any]] = None,
    memory_store: Optional[MemoryStore] = None,
    messages: Optional[list[dict[str, Any]]] = None,
    extra_tools: Optional[list[Any]] = None,
    secrets: Optional[SecretStore] = None,
    task_store: Optional[Any] = None,
    wake_store: Optional[Any] = None,
    session_id: Optional[str] = None,
    audit_sink: Optional[Any] = None,
    roots: Optional[list] = None,
    directory_requester: Optional[Any] = None,
    plan_approver: Optional[Any] = None,
    question_asker: Optional[Any] = None,
    subscription_store: Optional[Any] = None,
    channel_buffer: Optional[Any] = None,
    routing_targets: Optional[list[str]] = None,
    connector_filter: Optional[set[str]] = None,
    # The knowledge-library SQLite file the agent's `knowledge_search` reads. Must match
    # the SessionManager's store or UI-added entries never surface for the agent.
    # Defaults to the single source of truth (workspace `.coworker/knowledge.db`).
    knowledge_db_path: Optional[str | Path] = None,
    usage_sink: Optional[Callable[[dict, None]]] = None,
    persist_callback: Optional[Callable[[], None]] = None,
    # P1-5 零信任能力袋: persona scope 检查器 (可选)。
    scope_store: Optional[Any] = None,
) -> TurnEngine:
    ws = Path(workspace).expanduser().resolve() if workspace else None
    if agent.needs_workspace and ws is None:
        raise ValueError(f"agent '{agent.name}' requires a workspace")

    # The session's directories. Explicit `roots` (orphan Cowork: scratch + added folders) wins;
    # otherwise the single workspace is the sole writable root. One shared, mutable list flows to
    # the file tools, the permission engine, and the context injector so add/remove is seen by all.
    if roots:
        root_list: list[RootDir] = normalize_roots(roots)
    elif ws is not None:
        root_list = [RootDir(path=ws, writable=True)]
    else:
        root_list = []

    workspace_trusted = bool(ws and WorkspaceTrustStore().is_trusted(ws))
    config = load_config(ws, workspace_trusted=workspace_trusted)
    executor = (
        LocalExecutor(cwd=ws) if (agent.needs_workspace and ws is not None) else None
    )
    todo = TodoList()
    context = AgentContext(
        workspace=ws, executor=executor, todo=todo, roots=root_list or None
    )

    registry = ToolRegistry()
    registry.register_all(agent.build_tools(context))
    # MCP / connector tools (supplied by the manager) carry their own metadata + schema.
    if extra_tools:
        registry.register_all(extra_tools)
    # Messaging personas (Cowork / Ops / MyHelper) expose send_message; MyHelper also uses it as
    # the reply path for inbound Telegram/Slack super-agent sessions.
    secrets = secrets or SecretStore()
    if agent.messaging and any(s.enabled for s in load_settings(secrets).values()):
        registry.register(make_send_message_tool(secrets))
        # send_file (§34): hand deliverables into the chat — same targets, but its OWN
        # approval surface (a thread's standing send_message grant never covers uploads).
        registry.register(
            make_send_file_tool(secrets, workspace=ws, roots=root_list or None)
        )
        # Channel subscriptions (inbound): listen to a channel, catch up, (un)subscribe. The agent
        # obtains a channel via ask_user or from a channel message it's reacting to.
        if subscription_store is not None and channel_buffer is not None and session_id:
            registry.register_all(
                subscription_tools(
                    subscription_store,
                    session_id,
                    channel_buffer,
                    routing_targets=routing_targets,
                )
            )
    # Knowledge surfaces with a multi-root workspace can ask the user mid-task for another folder.
    if agent.family == "knowledge" and root_list:
        registry.register(request_directory_tool())
    if agent.connectors:
        enabled_connectors, enabled_tools = _enabled_connector_tools(secrets)
        # Per-session connection hierarchy (UI-REFRESH §4.3): when the caller supplies the session's
        # effective connector set, intersect it so only effective-enabled connectors expose tools.
        # Default None preserves CLI / direct callers (no per-session restriction).
        if connector_filter is not None:
            enabled_connectors = enabled_connectors & connector_filter
        registry.register_all(
            make_integration_tools(
                secrets,
                enabled_connectors=enabled_connectors,
                enabled_tools=enabled_tools,
                roots=root_list or None,
            )
        )
    # Web search + fetch: research tools for every agent (keyless DuckDuckGo default).
    registry.register(make_web_search_tool(secrets))
    registry.register(make_web_fetch_tool())
    # ask_user: the universal human-in-the-loop Q&A primitive (every agent; engine-intercepted).
    if question_asker is not None:
        registry.register(ask_user_tool())
    # Route by the model's `provider:` prefix (OpenAI default, Ollama, …). The manager normally
    # passes its shared router; this fallback covers the TUI / direct build_engine() callers.
    # Resolved here (not at engine construction) because the explorer subagent captures it.
    provider = provider or ProviderRouter(secrets, default_provider="openai")
    # Code-family personas can fan broad research out to read-only explorer subagents, keeping
    # their own context for the actual change.
    if agent.family == "code" and ws is not None:
        registry.register_all(
            explorer_tools(
                workspace=ws,
                provider=provider,
                model=model,
                model_settings=model_settings,
            )
        )
    # Scheduling: knowledge surfaces with a workspace can set up scheduled tasks (origin = this
    # session). Code stays out (it fans out to explorers instead).
    if task_store is not None and ws is not None and agent.family == "knowledge":
        origin = {
            "surface": agent.name,
            "session_id": session_id or "",
            "workspace": str(ws),
            "agent": agent.name,
        }
        registry.register_all(
            scheduling_tools(task_store, origin=origin, default_workspace=str(ws))
        )
    # Self-wake: knowledge surfaces can suspend + schedule their own resumption (timer /
    # on-completion / on-event). The scheduler tick resumes due wakes.
    if wake_store is not None and session_id and agent.family == "knowledge":
        registry.register_all(selfwake_tools(wake_store, session_id))

    # G1 (dev-plan 2026-08-05): the swarm is a first-class capability for cowork too
    # (cowork/knowledge/chat all share the knowledge family in agents.py, but the
    # product never *told* cowork sessions they can delegate to a swarm — that's the
    # "easter egg" gap the plan calls out). Scheduling / self-wake stay knowledge-only.
    swarm_enabled = ws is not None and agent.family in ("knowledge", "cowork")
    if swarm_enabled:
        registry.register_all(
            orchestration_tools(
                workspace=ws,
                provider=provider,
                model=model,
                model_settings=model_settings,
                usage_sink=usage_sink,
            )
        )
        # UTF-8-safe Chinese text stats — replaces workers' fragile PowerShell
        # inline-script attempts (ANSI mojibake burned whole task budgets).
        registry.register(text_stats_tool())
        # Periodic closed-loop toolkit (dev-plan T1): exact Pisano/Fibonacci-mods
        # lookups + FPA recurrence verification, so workers/reviewers can catch
        # arithmetic & recurrence hallucinations with math instead of guessing.
        from .periodic import periodic_tools

        registry.register_all(periodic_tools())
        # Knowledge file library: agent can search the workspace's indexed docs
        # and manual knowledge entries via knowledge_search.
        from .knowledge import knowledge_tools, resolve_knowledge_db_path

        registry.register_all(
            knowledge_tools(
                workspace=ws,
                db_path=knowledge_db_path
                or resolve_knowledge_db_path(workspace=str(ws)),
            )
        )

    instructions = f"{agent.system_prompt}\n\n{_NARRATION_GUIDANCE}"
    if ws is not None:
        instructions = f"{instructions}\n\n{environment_context(ws)}"
        conventions = load_agents_md(ws)
        if conventions:
            instructions = f"{instructions}\n\n{conventions}"
    if swarm_enabled and agent.name == "cowork":
        instructions = (
            f"{instructions}\n\n"
            "SWARM: you can delegate a whole goal to a worker swarm. When a task has "
            "several independent work streams (research + write + verify, or multiple "
            "documents to produce), call the `orchestrate` tool instead of doing it all "
            "inline — the swarm plans, splits, executes in parallel, and converges the "
            "deliverable, with progress reported as it runs. You stay the single point "
            "of contact for the user."
        )

    if memory_store is not None:
        registry.register_all(
            memory_tools(memory_store, workspace=str(ws) if ws else None)
        )
        instructions = f"{instructions}\n\n{_MEMORY_GUIDANCE}"
        remembered = memory_store.list(scope=Scope.GLOBAL)
        if ws is not None:
            remembered += memory_store.list(scope=Scope.WORKSPACE, workspace=str(ws))
        block = format_memories(remembered)
        if block:
            instructions = f"{instructions}\n\n{block}"

    # 工具自治 (Self-made tools) 指引: Agent 在任务中发现没有可用工具时,
    # 应自己创造工具 (DSH 愿景) — 工具不足是能力缺口, 不是任务失败。
    if ws is not None:
        instructions = (
            f"{instructions}\n\n"
            "Tool autonomy: when the task needs a capability no existing tool "
            "provides (an unknown tool error, or repeated failures from missing "
            "functionality), CREATE the tool yourself with `create_selfmade_tool` "
            "— write a short Python snippet (TOOL_NAME / TOOL_DESCRIPTION / "
            "TOOL_PARAMETERS / def run(**kwargs)), it is validated, registered "
            "live for this session, and persisted for future sessions. Prefer "
            "building on existing tools first; self-create only for genuine "
            "capability gaps."
        )

    skill_loader = SkillLoader(_skill_dirs(ws), readonly_dirs=[_skill_dirs(ws)[0]])
    registry.register_all(skill_tools(skill_loader))

    # 工具自治 (Self-made tools, DSH 愿景): Agent 发现工具不足时自造新工具。
    # 提供 create_selfmade_tool (需审批) 让 Agent 写 Python 实现并即时注册;
    # 同时加载 workspace 已持久化的自造工具 (跨会话复用, 自进化资产)。
    if ws is not None:
        from .tools.selfmade import load_tools, make_selfmade_tool_tools

        try:
            registry.register_all(make_selfmade_tool_tools(ws, registry))
            persisted = load_tools(ws)
            if persisted:
                registry.register_all(persisted)
        except Exception:
            import logging

            logging.getLogger("coworker.agent").exception(
                "selfmade tools setup failed (best-effort)"
            )

    # User-local risk overrides (mainly to relax MCP's conservative default). Empty store →
    # no-op; never written by persona loading (the no-self-grant rule).
    risk_overrides = RiskOverrideStore(state_dir() / "risk_overrides.json").resolver()
    permissions = PermissionEngine(
        workspace_root=ws or (root_list[0].path if root_list else Path.cwd()),
        mode=mode,
        # `[]` is an explicit deny-by-default override, not a request to fall back to config.
        allowed_commands=(
            allowed_commands if allowed_commands is not None else config.allowed_commands
        ),
        auto_allow_tools=set(config.auto_allow),
        roots=root_list or None,
        risk_overrides=risk_overrides,
    )
    # The plan-mode exit door. Always registered (surfaces can flip a live session into
    # plan mode via set_mode, and the registry is fixed at build); the engine rejects the
    # call whenever the session isn't actually in plan mode.
    registry.register(propose_plan_tool())

    # Per-turn ephemeral context, appended to the latest user message since mid-thread system
    # messages aren't reliable across providers. Two producers: the plan-mode reminder (mode can
    # flip mid-session, so it's checked each turn, not baked into the instructions) and the live
    # directory list (orphan Cowork can gain folders mid-session; Cowork/MyHelper only).
    roots_context = (
        (lambda: render_context(root_list))
        if root_list and agent.family == "knowledge"
        else None
    )

    def context_provider() -> str:
        parts = []
        if permissions.mode is Mode.PLAN:
            parts.append(_PLAN_MODE_CONTEXT)
        elif permissions.mode is Mode.DISCUSS:
            parts.append(_DISCUSS_MODE_CONTEXT)
        if roots_context is not None:
            ctx = roots_context()
            if ctx:
                parts.append(ctx)
        # Skills are progressive-disclosure: only the catalog (name+description) rides in,
        # refreshed every turn so skills created/imported mid-session appear immediately.
        catalog = skill_catalog_text(skill_loader)
        if catalog:
            parts.append(catalog)
        return "\n\n".join(parts)

    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model=model,
        instructions=instructions,
        approver=approver,
        # Stop kills the in-flight foreground shell command, not just the loop.
        interrupt_hooks=[executor.interrupt_now] if executor is not None else None,
        max_iterations=(
            max_iterations if max_iterations is not None else config.max_iterations
        ),
        model_settings=model_settings,
        messages=messages,
        audit_sink=audit_sink,
        usage_sink=usage_sink,
        context_provider=context_provider,
        directory_requester=directory_requester,
        plan_approver=plan_approver,
        question_asker=question_asker,
        skill_loader=skill_loader,
        persist_callback=persist_callback,
        scope_store=scope_store,
        trim_tool_outputs=config.trim_tool_outputs,
    )
    engine.executor = executor  # type: ignore[attr-defined]
    engine.todo = todo  # type: ignore[attr-defined]
    engine.agent_name = agent.name  # type: ignore[attr-defined]
    engine.roots = root_list  # type: ignore[attr-defined]  # shared list; Slice C mutates in place
    engine.audit_context = {
        "session_id": session_id or "",
        "agent": agent.name,
        "workspace": str(ws) if ws else "",
    }
    return engine


def build_code_engine(**kwargs: Any) -> TurnEngine:
    """Back-compat shim: build the Code agent's engine."""
    return build_engine(agent=code_agent(), **kwargs)
