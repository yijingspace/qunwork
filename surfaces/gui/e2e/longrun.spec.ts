import { expect } from "@playwright/test";
import { test } from "./fixtures";

// 7×24 长程任务管理控制台 e2e 回归 — 验证四块面板 + 关键操作在真实 GUI 链路
// (mock API) 下渲染与交互正常。配合 fixtures.ts 的 /v1/7x24/* mock。
// 运行: pnpm e2e -- --grep "7x24"
// vite 冷启动首次编译可能 >30s (依赖预构建) — goto 放宽到 60s。
test.setTimeout(90_000);

const GOTO: { waitUntil: "domcontentloaded"; timeout: number } = {
  waitUntil: "domcontentloaded",
  timeout: 60_000,
};

test("7x24 management: renders all four panels with data", async ({ page }) => {
  await page.goto("/", GOTO);
  // 侧边栏 → 7×24 管理。
  await page.getByTestId("nav-longrun").click();

  // 健康面板: 心跳任务数 / 自动化 / 卡死检测。
  await expect(page.getByTestId("health-heartbeat-tasks")).toHaveText("2");
  await expect(page.getByTestId("health-automations")).toHaveText("4/5");
  await expect(page.getByTestId("health-detection")).toHaveText("25s");

  // 遥测面板: 降级轨迹 + 收敛历史。
  await expect(page.getByText("研究报告")).toBeVisible();

  // 检查点面板: 会话列表。
  await expect(page.getByText("会话一")).toBeVisible();

  // 存储面板: 大小 + 记忆。
  await expect(page.getByText("2.4 MB")).toBeVisible();

  // 信息素总线面板 (QunMesh): 四信道总览 + 网格拓扑健康 + mesh_mode 档位选择。
  await expect(page.getByTestId("pheromone-bus-badge")).toContainText("StigmergyBus");
  await expect(page.getByTestId("pheromone-task-signals")).toContainText("3");
  await expect(page.getByTestId("mesh-topology")).toContainText("2 · 2 agents · 1");
  await expect(page.getByTestId("mesh-topology")).toContainText("task-a → task-b");
  await expect(page.getByTestId("mesh-mode-off")).toHaveClass(/bg-ink/);
  await page.getByTestId("mesh-mode-full").click();
  await expect(page.getByTestId("mesh-mode-full")).toHaveClass(/bg-ink/);

  // 聚合告警 + 跨周对比图。
  await expect(page.getByText("task-b").first()).toBeVisible();
  await expect(page.getByTestId("week-compare-chart")).toBeVisible();
});

test("7x24 management: checkpoint chain, restore drill and rollback", async ({ page }) => {
  await page.goto("/", GOTO);
  await page.getByTestId("nav-longrun").click();

  // 选择会话 → 检查点链。
  await page.getByText("会话一").click();
  await expect(page.getByTestId("restore-drill")).toBeVisible();
  await expect(page.getByText("#1")).toBeVisible();

  // 恢复演练 (只读)。
  await page.getByTestId("restore-drill").click();
  await expect(page.getByTestId("restore-result")).toBeVisible();
  await expect(page.getByTestId("restore-result")).toContainText("500 msgs");

  // 回滚按钮存在 (confirm 拦截)。
  page.on("dialog", (d) => d.accept());
  await expect(page.getByTestId("rollback-btn")).toBeVisible();
});

test("7x24 management: channel probe and alert archive actions", async ({ page }) => {
  await page.goto("/", GOTO);
  await page.getByTestId("nav-longrun").click();

  // 渠道健康探针 → 结果展示。
  await page.getByTestId("channels-probe").click();
  await expect(page.getByTestId("probe-results")).toBeVisible();
  await expect(page.getByText("120.5ms")).toBeVisible();

  // 告警归档 → 结果提示。
  await page.getByTestId("archive-alerts-btn").click();
  await expect(page.getByTestId("archive-msg")).toBeVisible();
  await expect(page.getByTestId("archive-msg")).toContainText("3");

  // 审计列表展示。
  await expect(page.getByText("回滚会话 s1 到恢复前备份 (10 条)")).toBeVisible();
});
