import { expect, test } from "playwright/test";

test("unauthenticated visitors are sent to sign in", async ({ page }) => {
  await page.goto("/runs");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByLabel("Email")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});

test("the console exposes the live run control-plane routes", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("access_token", "placeholder.session.token"));
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();
  await page.goto("/runs/new");
  await expect(page.getByRole("heading", { name: "Start a run", exact: true })).toBeVisible();
  await expect(page.getByLabel("Target ID")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create run" })).toBeVisible();
});
