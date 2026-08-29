import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LongRunView } from "./LongRunView";

const HEALTH = {
  heartbeat: { tasks: 3, alive: ["a", "b"], unhealthy: ["c"], detection_time_seconds: 25.0 },
  automation: { total: 10, enabled: 8, failed_recent: 1, run_count_total: 42 },
  wakes: { pending: 2, due: 1 },
  detection_time_seconds: 25.0,
};

const TELEMETRY = {
  degradations: [
    { task_id: "t1", level: 2, action: "downgrade_model", fidelity: 0.85, ts: 1 },
  ],
  convergence_history: [
    {
      run_id: "r1",
      intent: "研究报告",
      status: "completed",
      report: { gap: 0.5712, theoretical_rounds: 9, iterations: 9, convergence_curve: [0.1, 1], final_convergence: 1, converged: true, stalled: false },
    },
  ],
};

const CHECKPOINTS = {
  sessions: [
    { session_id: "s1", title: "会话一", message_count: 500, archived: true },
    { session_id: "s2", title: "会话二", message_count: 20, archived: false },
  ],
};

const DETAIL = {
  ok: true,
  session_id: "s1",
  chain: [{ seq: 1, n_layer: 1, granularity: "full", created_at: 1000, expires_at: null }],
  count: 1,
  latest_restorable: true,
};

const RESTORE = {
  ok: true,
  session_id: "s1",
  restore_ms: 1.2,
  applied: false,
  summary: { messages: 500, tasks: 3, result: "partial-after-phase-3", phase: 3, keys: ["messages", "tasks", "result"] },
};

const RESTORE_APPLIED = { ...RESTORE, applied: true };

const ALERTS = {
  ok: true,
  count: 2,
  alerts: [
    { id: 2, kind: "heartbeat_stalled", task_id: "t2", message: "任务 t2 心跳停滞", ts: 2000 },
    { id: 1, kind: "heartbeat_stalled", task_id: "t1", message: "任务 t1 心跳停滞", ts: 1000 },
  ],
};

const TELEMETRY_MULTI = {
  degradations: [
    { task_id: "t1", level: 1, action: "continue", fidelity: 1.0, ts: 1 },
    { task_id: "t2", level: 3, action: "narrow_scope", fidelity: 0.7, ts: 2 },
    { task_id: "t3", level: 5, action: "checkpoint_pause", fidelity: 0.0, ts: 3 },
  ],
  convergence_history: [
    {
      run_id: "r1",
      intent: "第一次",
      status: "completed",
      report: { gap: 0.5712, theoretical_rounds: 9, iterations: 7, convergence_curve: [0.1, 1], final_convergence: 1, converged: true, stalled: false },
    },
    {
      run_id: "r2",
      intent: "第二次",
      status: "completed",
      report: { gap: 0.5712, theoretical_rounds: 9, iterations: 5, convergence_curve: [0.1, 1], final_convergence: 1, converged: true, stalled: false },
    },
  ],
};

const STORAGE = {
  sessions: {
    count: 2,
    jsonl_total_bytes: 2_500_000,
    archived_sessions: 1,
    archive_bytes: 50_000,
    top: [{ session_id: "s1", jsonl_bytes: 2_000_000, archived: true }],
  },
  memory: { count: 7, stale: 2 },
};

const EMPTY_ALERTS = { ok: true, alerts: [], count: 0 };

const EMPTY_AGGREGATIONS = { ok: true, aggregations: [], count: 0 };

const AGGREGATIONS = {
  ok: true,
  count: 2,
  aggregations: [
    { id: 2, task_id: "task-x", kind: "heartbeat_stalled", level: "critical", started_at: 1000, updated_at: 7200, count: 5, resolved: 0, silenced_until: 9_999_999_999 },
    { id: 1, task_id: "task-y", kind: "heartbeat_stalled", level: "critical", started_at: 1000, updated_at: 2000, count: 2, resolved: 1 },
  ],
};

const AGG_STATS = {
  ok: true,
  days: [
    { alerts: 1, resolved: 0, tasks: 1 },
    { alerts: 3, resolved: 1, tasks: 2 },
    { alerts: 5, resolved: 0, tasks: 1 },
  ],
  top_tasks: [["task-x", 5], ["task-y", 2]],
};

const AUDIT = {
  ok: true,
  count: 2,
  audit: [
    { id: 2, kind: "audit", task_id: "s1", message: "回滚会话 s1 到恢复前备份 (10 条)", ts: 3000, payload: {} },
    { id: 1, kind: "audit", message: "更新告警渠道配置: 启用 ['email']", ts: 2000, payload: {} },
  ],
};

const WEEK_COMPARE = {
  ok: true,
  labels: ["08-22", "08-23", "08-24", "08-25", "08-26", "08-27", "08-28"],
  this_week: [1, 0, 2, 0, 3, 0, 5],
  last_week: [2, 0, 1, 0, 2, 0, 3],
  total_this: 11,
  total_last: 8,
  delta_pct: 37.5,
};

const SETTINGS = {
  ok: true,
  settings: {
    silence_after: 3,
    silence_seconds: 600,
    archive_keep_days: 30,
    health_thresholds: { good: 80, warn: 50 },
    probe_history_keep_days: 30,
    probe_history_keep_count: 10000,
  },
};

const ARCHIVED = {
  ok: true,
  count: 2,
  total_archived: 2,
  archived: [
    { id: 10, kind: "heartbeat_stalled", task_id: "old-t", message: "旧告警记录", ts: 1000 },
    { id: 11, kind: "audit", message: "旧审计记录", ts: 500 },
  ],
};

const PROBE_SCHEDULE = { ok: true, schedule: { enabled: true, interval_minutes: 60, last_probe_ts: null } };

const CHANNEL_HEALTH = {
  ok: true,
  thresholds: { good: 80, warn: 50 },
  channels: {
    email: { ok_count: 5, total: 5, success_rate: 1, health_score: 100, rating: "good", avg_ms: 102,
             trend: [{ ts: 1, ok: true, ms: 100 }, { ts: 2, ok: true, ms: 104 }] },
    telegram: { ok_count: 4, total: 5, success_rate: 0.8, health_score: 80, rating: "good", avg_ms: 202,
                trend: [{ ts: 1, ok: true, ms: 200 }, { ts: 2, ok: false, ms: null }] },
    wecom: { ok_count: 0, total: 3, success_rate: 0, health_score: 0, rating: "bad", avg_ms: null,
             trend: [{ ts: 1, ok: false, ms: null }] },
  },
};

const CHANNELS = {
  ok: true,
  enabled: ["email"],
  channels: {
    email: { enabled: true, smtp_host: "smtp.example.com", username: "u", password: "••••" },
    telegram: { enabled: false },
    feishu: { enabled: false },
    dingtalk: { enabled: false },
    wecom: { enabled: false },
  },
};

function stubFetch(routes: { match: string | RegExp; method?: string; json: unknown }[]) {
  const calls: { url: string; method: string; body?: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: init?.body });
    const path = url.split("?")[0];
    const route = routes.find(
      (r) =>
        (r.method ?? "GET").toUpperCase() === method &&
        (r.match instanceof RegExp ? r.match.test(url) : path.endsWith(String(r.match))),
    );
    if (!route) {
      // 默认回退: alert-channels / aggregations 未 stub 时返回空配置 (refresh 总会请求)。
      if (path.endsWith("/v1/7x24/alert-channels")) {
        return { ok: true, status: 200, json: async () => CHANNELS, text: async () => JSON.stringify(CHANNELS) } as Response;
      }
      if (path.endsWith("/v1/7x24/alerts/aggregations")) {
        return { ok: true, status: 200, json: async () => EMPTY_AGGREGATIONS, text: async () => JSON.stringify(EMPTY_AGGREGATIONS) } as Response;
      }
      if (path.endsWith("/v1/7x24/alerts/aggregations/stats")) {
        return { ok: true, status: 200, json: async () => ({ ok: true, days: [], top_tasks: [] }), text: async () => "{}" } as Response;
      }
      if (path.endsWith("/v1/7x24/audit")) {
        return { ok: true, status: 200, json: async () => ({ ok: true, audit: [], count: 0 }), text: async () => "[]" } as Response;
      }
      if (path.endsWith("/v1/7x24/alert-settings")) {
        return { ok: true, status: 200, json: async () => SETTINGS, text: async () => JSON.stringify(SETTINGS) } as Response;
      }
      if (path.endsWith("/v1/7x24/alerts/aggregations/week-compare")) {
        return { ok: true, status: 200, json: async () => WEEK_COMPARE, text: async () => JSON.stringify(WEEK_COMPARE) } as Response;
      }
      if (path.endsWith("/v1/7x24/alerts/archived")) {
        return { ok: true, status: 200, json: async () => ARCHIVED, text: async () => JSON.stringify(ARCHIVED) } as Response;
      }
      if (path.endsWith("/v1/7x24/probe-schedule")) {
        return { ok: true, status: 200, json: async () => PROBE_SCHEDULE, text: async () => JSON.stringify(PROBE_SCHEDULE) } as Response;
      }
      if (path.endsWith("/v1/7x24/channel-health")) {
        return { ok: true, status: 200, json: async () => CHANNEL_HEALTH, text: async () => JSON.stringify(CHANNEL_HEALTH) } as Response;
      }
      if (path.endsWith("/v1/7x24/channel-health/export")) {
        return { ok: true, status: 200, text: async () => "id,channel,ok,ms,error,ts\n", json: async () => "{}" } as Response;
      }
      return { ok: false, status: 404, json: async () => ({ error: "not found" }), text: async () => "not found" } as Response;
    }
    return {
      ok: true,
      status: 200,
      json: async () => route.json,
      text: async () => (typeof route.json === "string" ? route.json : JSON.stringify(route.json)),
    } as Response;
  }));
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("LongRunView", () => {
  it("renders four management panels with backend data", async () => {
    stubFetch([
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/checkpoints/s1", json: DETAIL },
    ]);
    render(<LongRunView onBack={() => {}} />);

    // 健康面板: 心跳/自动化/唤醒/检测时间。
    await waitFor(() => expect(screen.getByText("3")).toBeTruthy());
    expect(screen.getByText("8/10")).toBeTruthy();
    expect(screen.getByText("25s")).toBeTruthy();
    // 遥测面板: 降级轨迹 + 收敛历史。
    await waitFor(() => expect(screen.getByText("85%")).toBeTruthy()); // fidelity 0.85
    expect(screen.getByText("研究报告")).toBeTruthy();
    // 存储面板: 会话数/大小。
    expect(screen.getByText("2.4 MB")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
  });

  it("shows checkpoint chain when a session is selected", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/checkpoints/s1", json: DETAIL },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("会话一")).toBeTruthy());
    fireEvent.click(screen.getByText("会话一"));
    await waitFor(() => expect(screen.getByText(/#1/)).toBeTruthy());
    expect(screen.getByText(/restorable/)).toBeTruthy();
  });

  it("runs maintenance and shows result", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/maintenance",
        method: "POST",
        json: { sessions_archive: { archived: [1], skipped: 3 }, memories_dedupe: {} },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("maintenance-run")).toBeTruthy());
    fireEvent.click(screen.getByTestId("maintenance-run"));
    await waitFor(() => expect(screen.getByTestId("maintenance-result")).toBeTruthy());
    expect(screen.getByTestId("maintenance-result").textContent).toContain("archived 1");
    const post = calls.find((c) => c.method === "POST" && c.url.includes("/v1/7x24/maintenance"));
    expect(post).toBeTruthy();
    expect(post!.body).toContain('"dry_run":false');
  });

  it("shows stalled-task alert banner", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH }, // unhealthy: ["c"]
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("alert-banner")).toBeTruthy());
    expect(screen.getByTestId("alert-banner").textContent).toContain("c");
  });

  it("runs checkpoint restore drill", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/checkpoints/s1", json: DETAIL },
      { match: "/v1/7x24/checkpoints/s1/restore", method: "POST", json: RESTORE },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("会话一")).toBeTruthy());
    fireEvent.click(screen.getByText("会话一"));
    await waitFor(() => expect(screen.getByTestId("restore-drill")).toBeTruthy());
    fireEvent.click(screen.getByTestId("restore-drill"));
    await waitFor(() => expect(screen.getByTestId("restore-result")).toBeTruthy());
    expect(screen.getByTestId("restore-result").textContent).toContain("500 msgs");
    expect(screen.getByTestId("restore-result").textContent).toContain("partial-after-phase-3");
  });

  it("renders telemetry trend charts", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY_MULTI },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("convergence-trend")).toBeTruthy());
    // 降级分布: L1/L3/L5 各一次。
    expect(screen.getByTestId("degradation-bar-L1")).toBeTruthy();
    expect(screen.getByTestId("degradation-bar-L3")).toBeTruthy();
    expect(screen.getByTestId("degradation-bar-L5")).toBeTruthy();
  });

  it("shows persisted alert history", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: ALERTS },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("任务 t2 心跳停滞")).toBeTruthy());
    expect(screen.getByText("任务 t1 心跳停滞")).toBeTruthy();
  });

  it("runs apply-restore (write) and marks result APPLIED", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/checkpoints/s1", json: DETAIL },
      { match: "/v1/7x24/checkpoints/s1/restore", method: "POST", json: RESTORE_APPLIED },
    ]);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("会话一")).toBeTruthy());
    fireEvent.click(screen.getByText("会话一"));
    await waitFor(() => expect(screen.getByTestId("restore-apply")).toBeTruthy());
    fireEvent.click(screen.getByTestId("restore-apply"));
    await waitFor(() => expect(screen.getByTestId("restore-result")).toBeTruthy());
    expect(screen.getByTestId("restore-result").textContent).toContain("APPLIED");
    const post = calls.find((c) => c.method === "POST" && c.url.includes("/restore"));
    expect(post).toBeTruthy();
    expect(post!.url).toContain("apply=true");
    confirmSpy.mockRestore();
  });

  it("scales convergence trend via window buttons", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY_MULTI },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("trend-window-5")).toBeTruthy());
    fireEvent.click(screen.getByTestId("trend-window-5"));
    await waitFor(() => expect(screen.getByTestId("trend-window-all")).toBeTruthy());
    expect(screen.getByTestId("convergence-trend")).toBeTruthy();
  });

  it("saves alert channels and shows enabled state", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/alert-channels",
        method: "PUT",
        json: { ok: true, enabled: ["email", "telegram"], channels: CHANNELS.channels },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    // 渠道面板渲染 (默认回退 stub 提供 CHANNELS)。
    await waitFor(() => expect(screen.getByTestId("channel-toggle-email")).toBeTruthy());
    // 勾选 telegram → 保存。
    fireEvent.click(screen.getByTestId("channel-toggle-telegram"));
    fireEvent.click(screen.getByTestId("channels-save"));
    await waitFor(() => expect(screen.getByTestId("channel-msg")).toBeTruthy());
    const put = calls.find((c) => c.method === "PUT" && c.url.includes("/alert-channels"));
    expect(put).toBeTruthy();
    expect(put!.body).toContain("telegram");
  });

  it("filters alert history by time range and shows frequency chart", async () => {
    const manyAlerts = {
      ok: true,
      count: 3,
      alerts: [
        { id: 3, kind: "heartbeat_stalled", task_id: "c", message: "告警c", ts: 3000 },
        { id: 2, kind: "heartbeat_stalled", task_id: "b", message: "告警b", ts: 2000 },
        { id: 1, kind: "heartbeat_stalled", task_id: "a", message: "告警a", ts: 1000 },
      ],
    };
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: manyAlerts },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("告警a")).toBeTruthy());
    // 时间范围按钮存在; 频率图 (3 条告警跨 3 小时 → ≥2 桶)。
    expect(screen.getByTestId("alert-range-1h")).toBeTruthy();
    expect(screen.getByTestId("alert-range-7d")).toBeTruthy();
    fireEvent.click(screen.getByTestId("alert-range-1h"));
    await waitFor(() => expect(screen.getByTestId("alert-range-all")).toBeTruthy());
  });

  it("shows backup note after apply restore", async () => {
    const RESTORE_WITH_BACKUP = {
      ...RESTORE_APPLIED,
      backup_seq: "seq123",
      backup_key: "backup:s1",
    };
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/checkpoints/s1", json: DETAIL },
      { match: "/v1/7x24/checkpoints/s1/restore", method: "POST", json: RESTORE_WITH_BACKUP },
    ]);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("会话一")).toBeTruthy());
    fireEvent.click(screen.getByText("会话一"));
    await waitFor(() => expect(screen.getByTestId("restore-apply")).toBeTruthy());
    fireEvent.click(screen.getByTestId("restore-apply"));
    await waitFor(() => expect(screen.getByTestId("restore-result")).toBeTruthy());
    expect(screen.getByTestId("restore-result").textContent).toContain("可回滚");
    confirmSpy.mockRestore();
  });

  it("routes channels by level via level checkboxes", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/alert-channels",
        method: "PUT",
        json: { ok: true, enabled: ["telegram"], channels: CHANNELS.channels },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("channel-toggle-telegram")).toBeTruthy());
    // 勾选 telegram 的 critical 级别 → 保存。
    fireEvent.click(screen.getByTestId("channel-toggle-telegram"));
    fireEvent.click(screen.getByTestId("channel-level-telegram-critical"));
    fireEvent.click(screen.getByTestId("channels-save"));
    await waitFor(() => expect(screen.getByTestId("channel-msg")).toBeTruthy());
    const put = calls.find((c) => c.method === "PUT" && c.url.includes("/alert-channels"));
    expect(put).toBeTruthy();
    expect(put!.body).toContain("critical");
  });

  it("rolls back to pre-restore backup", async () => {
    const ROLLBACK = { ok: true, session_id: "s1", rolled_back_messages: 10, restore_ms: 0.8 };
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/checkpoints/s1", json: DETAIL },
      { match: "/v1/7x24/checkpoints/s1/rollback", method: "POST", json: ROLLBACK },
    ]);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("会话一")).toBeTruthy());
    fireEvent.click(screen.getByText("会话一"));
    await waitFor(() => expect(screen.getByTestId("rollback-btn")).toBeTruthy());
    fireEvent.click(screen.getByTestId("rollback-btn"));
    await waitFor(() => expect(screen.getByTestId("restore-result")).toBeTruthy());
    expect(screen.getByTestId("restore-result").textContent).toContain("ROLLED BACK");
    const post = calls.find((c) => c.method === "POST" && c.url.includes("/rollback"));
    expect(post).toBeTruthy();
    confirmSpy.mockRestore();
  });

  it("shows alert aggregations with count and duration", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/alerts/aggregations", json: AGGREGATIONS },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("task-x")).toBeTruthy());
    expect(screen.getByText("×5")).toBeTruthy();
    expect(screen.getByText("task-y")).toBeTruthy();
    expect(screen.getByText(/已解决|resolved/)).toBeTruthy();
  });

  it("shows silenced state on aggregation", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/alerts/aggregations", json: AGGREGATIONS },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("task-x")).toBeTruthy());
    // task-x 有 silenced_until (未来) → 静默标记。
    expect(screen.getByTestId("silenced-task-x")).toBeTruthy();
  });

  it("shows daily alert trend chart", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/alerts/aggregations", json: AGGREGATIONS },
      { match: "/v1/7x24/alerts/aggregations/stats", json: AGG_STATS },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("alert-trend-chart")).toBeTruthy());
  });

  it("shows operation audit log", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/audit", json: AUDIT },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText("回滚会话 s1 到恢复前备份 (10 条)")).toBeTruthy());
    expect(screen.getByText(/更新告警渠道配置/)).toBeTruthy();
  });

  it("exports audit via CSV/JSON buttons", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/audit", json: AUDIT },
      { match: "/v1/7x24/audit/export", json: "id,ts,task_id,message,payload\n1,x,y,z,{}\n" },
    ]);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("audit-export-csv")).toBeTruthy());
    fireEvent.click(screen.getByTestId("audit-export-csv"));
    await waitFor(() => expect(screen.getByTestId("export-msg")).toBeTruthy());
    expect(screen.getByTestId("export-msg").textContent).toContain("CSV");
    const get = calls.find((c) => c.method === "GET" && c.url.includes("/export"));
    expect(get).toBeTruthy();
    expect(get!.url).toContain("format=csv");
    clickSpy.mockRestore();
  });

  it("saves alert silence settings", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/alert-settings",
        method: "PUT",
        json: { ok: true, settings: { silence_after: 5, silence_seconds: 1800, archive_keep_days: 90 } },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("silence-after-input")).toBeTruthy());
    fireEvent.change(screen.getByTestId("silence-after-input"), { target: { value: "5" } });
    fireEvent.change(screen.getByTestId("silence-seconds-input"), { target: { value: "1800" } });
    fireEvent.change(screen.getByTestId("archive-keep-days-input"), { target: { value: "90" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(screen.getByTestId("settings-msg")).toBeTruthy());
    expect(screen.getByTestId("settings-msg").textContent).toContain("5");
    const put = calls.find((c) => c.method === "PUT" && c.url.includes("/alert-settings"));
    expect(put).toBeTruthy();
    expect(put!.body).toContain("1800");
    expect(put!.body).toContain("90");
  });

  it("saves health thresholds and probe history retention window", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/alert-settings",
        method: "PUT",
        json: {
          ok: true,
          settings: {
            silence_after: 3,
            silence_seconds: 600,
            archive_keep_days: 30,
            health_thresholds: { good: 90, warn: 60 },
            probe_history_keep_days: 14,
            probe_history_keep_count: 5000,
          },
        },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("health-good-input")).toBeTruthy());
    fireEvent.change(screen.getByTestId("health-good-input"), { target: { value: "90" } });
    fireEvent.change(screen.getByTestId("health-warn-input"), { target: { value: "60" } });
    fireEvent.change(screen.getByTestId("probe-keep-days-input"), { target: { value: "14" } });
    fireEvent.change(screen.getByTestId("probe-keep-count-input"), { target: { value: "5000" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(screen.getByTestId("settings-msg")).toBeTruthy());
    const put = calls.find((c) => c.method === "PUT" && c.url.includes("/alert-settings"));
    expect(put).toBeTruthy();
    const body = JSON.parse(String(put!.body));
    expect(body.settings.health_thresholds).toEqual({ good: 90, warn: 60 });
    expect(body.settings.probe_history_keep_days).toBe(14);
    expect(body.settings.probe_history_keep_count).toBe(5000);
    // 保存后输入框显示返回的新值。
    await waitFor(() =>
      expect((screen.getByTestId("health-good-input") as HTMLInputElement).value).toBe("90"),
    );
  });

  it("prunes probe history via button", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/probe-history/prune",
        method: "POST",
        json: { ok: true, removed: 12, kept: 88, keep_days: 30, keep_count: 10000 },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("probe-prune")).toBeTruthy());
    fireEvent.click(screen.getByTestId("probe-prune"));
    await waitFor(() => expect(screen.getByTestId("settings-msg")).toBeTruthy());
    expect(screen.getByTestId("settings-msg").textContent).toContain("12");
    expect(
      calls.some((c) => c.method === "POST" && c.url.includes("/probe-history/prune")),
    ).toBe(true);
  });

  it("shows week compare chart", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/alerts/aggregations/week-compare", json: WEEK_COMPARE },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("week-compare-chart")).toBeTruthy());
    expect(screen.getByText(/37.5%/)).toBeTruthy();
  });

  it("runs channel health probe and shows per-channel results", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/alert-channels/probe",
        method: "POST",
        json: {
          ok: true,
          healthy: ["email"],
          results: {
            email: { ok: true, ms: 120.5 },
            telegram: { ok: false, error: "incomplete config" },
          },
        },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("channels-probe")).toBeTruthy());
    fireEvent.click(screen.getByTestId("channels-probe"));
    await waitFor(() => expect(screen.getByTestId("probe-results")).toBeTruthy());
    expect(screen.getByText("120.5ms")).toBeTruthy();
    expect(screen.getByText(/incomplete config/)).toBeTruthy();
    const post = calls.find((c) => c.method === "POST" && c.url.includes("/probe"));
    expect(post).toBeTruthy();
  });

  it("archives old alerts and shows result", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/alerts/archive",
        method: "POST",
        json: { ok: true, archived: 3, kept: 2, archived_total: 5 },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("archive-alerts-btn")).toBeTruthy());
    fireEvent.click(screen.getByTestId("archive-alerts-btn"));
    await waitFor(() => expect(screen.getByTestId("archive-msg")).toBeTruthy());
    expect(screen.getByTestId("archive-msg").textContent).toContain("3");
    const post = calls.find((c) => c.method === "POST" && c.url.includes("/alerts/archive"));
    expect(post).toBeTruthy();
    expect(post!.body).toContain("30");
  });

  it("views archived alerts and restores one", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/alerts/archived", json: ARCHIVED },
      {
        match: "/v1/7x24/alerts/archived/10/restore",
        method: "POST",
        json: { ok: true, restored: ARCHIVED.archived[0] },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("view-archived-btn")).toBeTruthy());
    fireEvent.click(screen.getByTestId("view-archived-btn"));
    await waitFor(() => expect(screen.getByTestId("archived-list")).toBeTruthy());
    expect(screen.getByText("旧告警记录")).toBeTruthy();
    fireEvent.click(screen.getByTestId("restore-archived-10"));
    await waitFor(() => expect(screen.getByTestId("archive-msg")).toBeTruthy());
    expect(screen.getByTestId("archive-msg").textContent).toContain("已恢复归档告警 #10");
    const post = calls.find((c) => c.method === "POST" && c.url.includes("/archived/10/restore"));
    expect(post).toBeTruthy();
  });

  it("saves probe schedule settings", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      {
        match: "/v1/7x24/probe-schedule",
        method: "PUT",
        json: { ok: true, schedule: { enabled: true, interval_minutes: 30 } },
      },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("probe-interval")).toBeTruthy());
    fireEvent.change(screen.getByTestId("probe-interval"), { target: { value: "30" } });
    fireEvent.click(screen.getByTestId("probe-schedule-save"));
    await waitFor(() => expect(screen.getByTestId("archive-msg")).toBeTruthy());
    expect(screen.getByTestId("archive-msg").textContent).toContain("30");
    const put = calls.find((c) => c.method === "PUT" && c.url.includes("/probe-schedule"));
    expect(put).toBeTruthy();
    expect(put!.body).toContain("30");
  });

  it("shows channel health score and trend", async () => {
    stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/channel-health", json: CHANNEL_HEALTH },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("channel-health-email")).toBeTruthy());
    expect(screen.getByTestId("channel-health-email").textContent).toContain("100");
    expect(screen.getByTestId("channel-health-telegram").textContent).toContain("80");
    expect(screen.getByTestId("channel-health-email").textContent).toContain("5/5");
    // 降级提示: wecom rating=bad → 严重; telegram=good 无提示。
    expect(screen.getByTestId("channel-degrade-wecom")).toBeTruthy();
    expect(screen.queryByTestId("channel-degrade-email")).toBeNull();
  });

  it("exports probe history via CSV button", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/channel-health/export", json: "id,channel,ok,ms,error,ts\n1,email,1,100,,1\n" },
    ]);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("channels-export-csv")).toBeTruthy());
    fireEvent.click(screen.getByTestId("channels-export-csv"));
    await waitFor(() => expect(screen.getByTestId("channel-msg")).toBeTruthy());
    expect(screen.getByTestId("channel-msg").textContent).toContain("CSV");
    const get = calls.find((c) => c.method === "GET" && c.url.includes("/channel-health/export"));
    expect(get).toBeTruthy();
    clickSpy.mockRestore();
  });

  it("saves per-channel archive keep days", async () => {
    const calls = stubFetch([
      { match: "/v1/7x24/checkpoints", json: CHECKPOINTS },
      { match: "/v1/7x24/health", json: HEALTH },
      { match: "/v1/7x24/telemetry", json: TELEMETRY },
      { match: "/v1/7x24/storage", json: STORAGE },
      { match: "/v1/7x24/alerts", json: EMPTY_ALERTS },
      { match: "/v1/7x24/alert-channels", method: "PUT", json: { ok: true, enabled: ["email"], channels: CHANNELS.channels } },
      { match: "/v1/7x24/alert-settings", method: "PUT", json: { ok: true, settings: { silence_after: 3, silence_seconds: 600, archive_keep_days: 30 } } },
    ]);
    render(<LongRunView onBack={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("channel-archive-email")).toBeTruthy());
    fireEvent.change(screen.getByTestId("channel-archive-email"), { target: { value: "60" } });
    fireEvent.click(screen.getByTestId("channels-save"));
    await waitFor(() => expect(screen.getByTestId("channel-msg")).toBeTruthy());
    const put = calls.find((c) => c.method === "PUT" && c.url.includes("/alert-settings"));
    expect(put).toBeTruthy();
    expect(put!.body).toContain("60");
  });
});
