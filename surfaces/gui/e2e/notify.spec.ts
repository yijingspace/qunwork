// Task-completion alerts (owner ask 2026-09-13): both a normal turn and a swarm run must
// announce themselves when they finish — a short chime, plus an opt-in desktop popup. The
// desktop channel goes through the Web Notification API, which the specs stub so the
// assertions can see exactly what would have popped up.
import { expect, type Page } from "@playwright/test";
import { test } from "./fixtures";

/** Install a Notification stub + enable both channels before the app boots. */
async function armAlerts(page: Page, prefs: { sound: boolean; desktop: boolean } = { sound: true, desktop: true }) {
  await page.addInitScript((p) => {
    localStorage.setItem("qunwork-notify", JSON.stringify(p));
    (window as unknown as { __notified: Array<{ title: string; body: string }> }).__notified = [];
    class FakeNotification {
      static permission = "granted";
      static requestPermission = async () => "granted";
      onclick: (() => void) | null = null;
      constructor(title: string, opts?: { body?: string }) {
        (window as unknown as { __notified: Array<{ title: string; body: string }> }).__notified.push({
          title,
          body: opts?.body ?? "",
        });
      }
      close() {}
    }
    (window as unknown as { Notification: unknown }).Notification = FakeNotification;
  }, prefs);
}

const notified = (page: Page) =>
  page.evaluate(() => (window as unknown as { __notified: Array<{ title: string; body: string }> }).__notified);

test("a finished normal turn raises a desktop alert carrying the reply", async ({ page }) => {
  await armAlerts(page);
  await page.goto("/");

  await page.getByPlaceholder(/Ask the coworker/).fill("hello agent");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(/Echo: hello agent/)).toBeVisible();

  // The alert lands on turn_done (not before) and summarizes the assistant's answer.
  await expect
    .poll(async () => (await notified(page)).length, { timeout: 5_000 })
    .toBeGreaterThan(0);
  const alerts = await notified(page);
  expect(alerts.some((a) => a.body.includes("Echo: hello agent"))).toBe(true);
});

test("a finished swarm run raises an alert too", async ({ page }) => {
  await armAlerts(page);
  await page.goto("/");

  // Swarm mode — entering it swaps the composer placeholder, and submitting from there
  // is what launches the run.
  await page.getByTestId("task-mode-menu").click();
  await page.getByRole("menuitem", { name: /Swarm mode/ }).click();
  await page.getByPlaceholder(/Describe a goal for the swarm/).fill("Draft the launch note");
  await page.getByRole("button", { name: "Send" }).click();

  // The fixture run is already terminal, so the first poll lands the alert.
  await expect
    .poll(async () => (await notified(page)).length, { timeout: 10_000 })
    .toBeGreaterThan(0);
  const alerts = await notified(page);
  expect(alerts.some((a) => a.body.includes("Draft the launch note"))).toBe(true);
});

test("the settings card switches each channel on and remembers it", async ({ page }) => {
  // Granted permission + the shipped default prefs, so the test can turn desktop ON.
  await armAlerts(page, { sound: true, desktop: false });
  await page.goto("/");
  await page.getByTestId("account-row").click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();

  const card = page.getByTestId("notify-card");
  await expect(card).toBeVisible();
  const sound = page.getByTestId("notify-sound");
  const desktop = page.getByTestId("notify-desktop");
  // Sound on, desktop off by default: a chime is unmissable, an OS popup is opt-in.
  await expect(sound).toBeChecked();
  await expect(desktop).not.toBeChecked();

  await sound.uncheck();
  expect(
    await page.evaluate(() => JSON.parse(localStorage.getItem("qunwork-notify") || "{}").sound),
  ).toBe(false);

  // Desktop needs the OS permission, granted for this context by the stub.
  await desktop.check();
  await expect(desktop).toBeChecked();

  // The test button reports what actually went out.
  await page.getByTestId("notify-test").click();
  await expect(card).toContainText("Alert sent.");
});
