// Swarm run visualization: the three-lane DAG (plan → parallel execution → review/convergence),
// the governance metric cards fed by the engine's OWN values + thresholds, the per-node thought
// chain, and honest event-log replay. The fixture run has a 3-task dependency chain, two
// governance checks (the second tripping the red line) and per-task worker thoughts.
import { expect, type Page } from "@playwright/test";
import { test } from "./fixtures";

/** Enter swarm mode via the Composer's task-mode dropdown, then open the fixture run. */
async function openSwarmRun(page: Page) {
  await page.goto("/");
  await page.getByTestId("task-mode-menu").click();
  await page.getByRole("menuitem", { name: /Swarm mode/ }).click();
  await page.getByTestId("history-run-swarm-1").click();
  await expect(page.getByTestId("dag-node-t1")).toBeVisible();
}

test("the DAG renders plan, execution and review lanes from the run's own plan", async ({ page }) => {
  await openSwarmRun(page);

  await expect(page.getByTestId("dag-node-t1")).toBeVisible();
  await expect(page.getByTestId("dag-node-t2")).toBeVisible();
  await expect(page.getByTestId("dag-node-t3")).toBeVisible();
  // Lane headings + the plan/review summary nodes.
  await expect(page.getByText("PARALLEL EXECUTION")).toBeVisible();
  await expect(page.getByText("REVIEW & CONVERGENCE").first()).toBeVisible();
  await expect(page.getByText("3 tasks")).toBeVisible();
  await expect(page.getByText("2/3 accepted")).toBeVisible();
  await expect(page.getByText("已收敛").or(page.getByText("converged"))).toBeVisible();
});

test("governance cards show the engine's values, thresholds and the tripped red line", async ({ page }) => {
  await openSwarmRun(page);

  await expect(page.getByTestId("governance-metrics")).toBeVisible();
  // Latest check (step 3) — the values the engine actually computed.
  await expect(page.getByTestId("gov-metric-viscosity")).toContainText("0.44");
  await expect(page.getByTestId("gov-metric-drift")).toContainText("0.72");
  await expect(page.getByTestId("gov-metric-autonomy")).toContainText("66%");
  // …and the thresholds they were judged against, shipped on the event.
  await expect(page.getByTestId("gov-metric-viscosity")).toContainText("0.4");
  await expect(page.getByTestId("gov-metric-viscosity")).toContainText("0.66");
  // Step 3 tripped the red line.
  await expect(page.getByTestId("gov-metric-redline")).toContainText("tripped");
  await expect(page.getByTestId("gov-metric-redline")).toContainText("2 governance checks");
});

test("hovering a task node reveals that worker's thought chain and dependencies", async ({ page }) => {
  await openSwarmRun(page);

  await page.getByTestId("dag-node-t2").hover();
  const detail = page.getByTestId("dag-node-detail");
  await expect(detail).toBeVisible();
  await expect(detail).toContainText("Draft the launch note");
  await expect(detail).toContainText("Outlining: what shipped");
  await expect(detail).toContainText("depends on");
  await expect(detail).toContainText("t1");

  // A different node shows ITS own worker's thought chain, not the previous one's.
  await page.getByTestId("dag-node-t3").hover();
  await expect(detail).toContainText("Cross-checking each claim");
  await expect(detail).not.toContainText("Outlining: what shipped");
});

test("the coordination report can be exported as a redacted sample", async ({ page }) => {
  await openSwarmRun(page);

  await page.getByTestId("swarm-report").click();
  await expect(page.getByTestId("swarm-report-body")).toBeVisible();
  // The raw sample still carries the host path + the address (it's the local report).
  await expect(page.getByTestId("swarm-report-body")).toContainText("rohit@openworker.com");

  await page.getByTestId("swarm-report-redact").click();
  const sample = page.getByTestId("swarm-report-redacted");
  await expect(sample).toBeVisible();
  await expect(sample).toContainText("coordination-report-run-swarm-1-redacted.md");
  // …and the publishable copy has them masked.
  await expect(sample).not.toContainText("rohit@openworker.com");
  await expect(sample).toContainText("<email>");
});

test("a run whose storage died says so at once and can be closed out", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("task-mode-menu").click();
  await page.getByRole("menuitem", { name: /Swarm mode/ }).click();
  await page.getByTestId(`history-${"run-swarm-diskfull"}`).click();

  // The reason comes straight from the server, not a generic "quiet" notice.
  const banner = page.getByTestId("swarm-storage-error");
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("database or disk is full");
  await expect(banner).toContainText("Free up disk space");
  // …and it does not double up with the vague silence notice.
  await expect(page.getByTestId("swarm-stale")).toHaveCount(0);

  // The owner's way out of a frozen run.
  await page.getByTestId("swarm-abandon").click();
  await expect(banner).toHaveCount(0);
});

test("replay rewinds the event log frame by frame and restores the live frame", async ({ page }) => {
  await openSwarmRun(page);

  await page.getByTestId("dag-replay").click();
  const scrub = page.getByTestId("dag-replay-scrub");
  await expect(scrub).toBeVisible();

  // Take manual control (scrubbing pauses auto-play) and rewind to the very first frame.
  await scrub.focus();
  await scrub.press("Home");
  await expect(page.getByTestId("dag-empty-frame")).toBeVisible();
  await expect(page.getByTestId("dag-node-t1")).toHaveCount(0);

  // Frame 1 = run_started only; frame 2 adds the plan → the three nodes appear.
  await scrub.press("ArrowRight");
  await expect(page.getByTestId("dag-node-t1")).toHaveCount(0);
  await scrub.press("ArrowRight");
  await expect(page.getByTestId("dag-node-t1")).toBeVisible();
  await expect(page.getByTestId("dag-node-t3")).toBeVisible();

  // Early frames carry the FIRST governance check (nominal), not the later red-line one.
  await scrub.press("End");
  await expect(page.getByTestId("gov-metric-viscosity")).toContainText("0.44");

  // Stop hands the live frame back.
  await page.getByTestId("dag-replay-stop").click();
  await expect(page.getByTestId("dag-replay")).toBeVisible();
  await expect(page.getByTestId("dag-node-t1")).toBeVisible();
  await expect(page.getByTestId("gov-metric-autonomy")).toContainText("66%");
});
