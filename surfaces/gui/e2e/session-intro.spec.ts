// Start-screen template tasks (§27, card-grid edition): three concrete starter cards plus the
// "Custom template" ghost cell, with user templates in their own labeled group. Sub-lines are
// outcome-voiced; connection state lives in the dots + the trailing action. Gated card (source
// not live for this session) → "Configure ›" visible AT REST and expands the rail's Access
// section (§32); ready card → hover reveals "Start →", click prefills the composer with the
// template stem. Template cards are a real <button> body + SIBLING delete button (a11y).
import { expect } from "@playwright/test";
import { test } from "./fixtures";

test("starter cards + ghost add cell + template group; gated card shows Configure › and opens Access", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("What task can I help you solve?")).toBeVisible();

  // The three starter cards, the dashed add cell, and the seeded template card.
  await expect(page.getByTestId("intro-task-folder")).toBeVisible();
  await expect(page.getByTestId("intro-task-hubspot")).toBeVisible();
  await expect(page.getByTestId("intro-task-github-slack")).toBeVisible();
  await expect(page.getByTestId("intro-task-add")).toBeVisible();
  await expect(page.getByTestId("intro-task-custom-101")).toBeVisible();
  await expect(page.getByText("Set me up (optional)")).toHaveCount(0);

  // Fixture session state: slack + github live, hubspot not → the HubSpot card is gated,
  // with the Configure affordance visible AT REST (no hover needed — it IS the card's action);
  // the github+slack automation card has everything it needs.
  const hs = page.getByTestId("intro-task-hubspot");
  await expect(hs).toContainText("Configure ›");
  await expect(hs.locator(".task-card-act")).toHaveCSS("opacity", "1");
  await expect(page.getByTestId("intro-task-github-slack")).toContainText("Start →");

  // Sub-lines describe the task's outcome, never connection state.
  await expect(hs).toContainText("Sources, stages, and who needs follow-up");
  await expect(hs).not.toContainText(/connect/i);

  // Configure → the rail's Access section expands (§32), not a bespoke setup surface.
  await hs.click();
  await expect(page.getByRole("region", { name: "Session access" })).toBeVisible();
  // No composer prefill happened on the gated click.
  await expect(page.getByPlaceholder(/Ask the coworker/)).toHaveValue("");
});

test("ready cards reveal Start → on hover and prefill the composer", async ({ page }) => {
  // Make every source live for this session (registered after the fixture's routes → wins).
  await page.route("**/v1/sessions/*/connections*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        connected: [
          { connector: "hubspot", enabled: true, detail: "" },
          { connector: "github", enabled: true, detail: "" },
          { connector: "slack", enabled: true, detail: "" },
        ],
        recommended: [],
        attention: 0,
      }),
    }),
  );
  await page.goto("/");

  const hs = page.getByTestId("intro-task-hubspot");
  await expect(hs).toContainText("Start →");
  // The action is hover-revealed on ready cards (hidden at rest).
  await expect(hs.locator(".task-card-act")).toHaveCSS("opacity", "0");
  await hs.hover();
  await expect(hs.locator(".task-card-act")).toHaveCSS("opacity", "1");

  await hs.click();
  await expect(page.getByPlaceholder(/Ask the coworker/)).toHaveValue(/HubSpot leads/);

  // Both sources live → the automation card is ready too; its prefill is the recipe stem.
  const gh = page.getByTestId("intro-task-github-slack");
  await expect(gh).toContainText("Start →");
  await gh.click();
  await expect(page.getByPlaceholder(/Ask the coworker/)).toHaveValue(/weekly progress report/);
});

test("folder card opens the add-folder panel; adding a folder prefills the composer", async ({ page }) => {
  await page.goto("/");

  // No shared folder yet (the fixture root is the primary scratch) → the card opens the form.
  await page.getByTestId("intro-task-folder").click();
  const path = page.getByPlaceholder("Choose or paste a folder path…");
  await expect(path).toBeVisible();
  await path.fill("/Users/me/Reports");
  await page.getByRole("button", { name: "Add", exact: true }).click();

  await expect(page.getByPlaceholder(/Ask the coworker/)).toHaveValue(
    /Analyze the files in this folder/,
  );
});

test("template card prefills the composer; × deletes it; the add form creates one", async ({ page }) => {
  await page.goto("/");

  // The seeded template prefills on click.
  await page.getByTestId("intro-task-custom-101").click();
  await expect(page.getByPlaceholder(/Ask the coworker/)).toHaveValue(/weekly digest/i);

  // × is a sibling BUTTON (never nested interactives) and removes the card.
  const card = page.getByTestId("intro-task-custom-101");
  await card.hover();
  await card.getByRole("button", { name: "Delete template" }).click();
  await expect(page.getByTestId("intro-task-custom-101")).toHaveCount(0);

  // The ghost cell opens the form; saving creates a new card that prefills on click.
  await page.getByTestId("intro-task-add").click();
  await page.getByPlaceholder("Template title").fill("Release notes");
  await page.getByPlaceholder("The task prompt").fill("Draft release notes from recent commits.");
  await page.getByRole("button", { name: "Save template" }).click();
  const made = page.locator('[data-testid^="intro-task-custom-"]', { hasText: "Release notes" });
  await expect(made).toBeVisible();
  await made.click();
  await expect(page.getByPlaceholder(/Ask the coworker/)).toHaveValue(/release notes/i);
});
