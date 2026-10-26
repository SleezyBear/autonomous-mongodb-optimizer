import { expect, test } from "playwright/test";

const required = [
  "R24_OPERATOR_EMAIL", "R24_APPROVER_EMAIL", "R24_PASSWORD", "R24_TARGET_ID",
  "R24_SUCCESS_RUN_ID", "R24_NOOP_RUN_ID", "R24_ROLLBACK_RUN_ID", "R24_PENDING_RUN_ID",
] as const;

const configured = required.every((name) => Boolean(process.env[name]));

async function login(page: import("playwright/test").Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(process.env.R24_PASSWORD!);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/$/);
}

test.describe("R24 real persisted stack", () => {
  test.skip(!configured, "R24 real-stack environment is not configured");

  test("authenticated UI creates a durable run and observes live terminal evidence", async ({ page }) => {
    await login(page, process.env.R24_OPERATOR_EMAIL!);
    await page.goto("/runs/new");
    await page.getByLabel("Target ID").selectOption(process.env.R24_TARGET_ID!);
    await page.getByRole("button", { name: "Create run" }).click();
    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+$/);
    await expect(page.getByLabel("Run status")).toHaveText("Queued");

    await page.goto(`/runs/${process.env.R24_SUCCESS_RUN_ID}`);
    await expect(page.getByText("Deployment completed successfully.")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Deployment", exact: true })).toBeVisible();

    await page.goto(`/runs/${process.env.R24_NOOP_RUN_ID}`);
    await expect(page.getByText("Completed safely: no candidates were generated.")).toBeVisible();

    await page.goto(`/runs/${process.env.R24_ROLLBACK_RUN_ID}`);
    await expect(page.getByLabel("Run status")).toHaveText("Rolled back");
    await expect(page.getByRole("heading", { name: "Rollback", exact: true })).toBeVisible();
  });

  test("real approver action is durable and unsafe authentication fails visibly", async ({ page }) => {
    await login(page, process.env.R24_APPROVER_EMAIL!);
    await page.goto(`/runs/${process.env.R24_PENDING_RUN_ID}`);
    await expect(page.getByRole("heading", { name: "Approval required" })).toBeVisible();
    await page.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByLabel("Run status")).toHaveText("Approved");

    await page.evaluate(() => window.localStorage.setItem("access_token", "tampered.jwt.value"));
    await page.goto("/runs");
    await expect(page.getByText("API request failed (401)")).toBeVisible();
  });
});
