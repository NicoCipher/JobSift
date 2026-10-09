import { expect, test } from "@playwright/test";

test("production operator opens Operations and hides obsolete demo routes", async ({ page }) => {
  test.skip(
    process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR !== "1",
    "Run with NEXT_PUBLIC_JOBSIFT_OPERATOR=1 to verify production operator routing.",
  );

  await page.goto("/");
  await expect(page).toHaveURL(/\/operations\/?$/);
  const nav = page.getByRole("navigation", { name: "Primary navigation" });
  await expect(nav.getByRole("link", { name: "Operations" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(nav.locator("a")).toHaveText([
    "Operations",
    "Clients",
    "Review",
    "Settings",
  ]);
  await expect(page.getByRole("link", { name: "JobSift" })).toHaveAttribute(
    "href",
    "/operations",
  );

  await page.goto("/clients");
  await page.getByRole("link", { name: "JobSift" }).click();
  await expect(page).toHaveURL(/\/operations\/?$/);

  await page.goto("/jobs");
  await expect(page).toHaveURL(/\/operations\/?$/);
  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/operations\/?$/);
});

test("fixture development mode retains its authored Jobs test surface", async ({ page }) => {
  test.skip(
    process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1" || process.env.NEXT_PUBLIC_JOBSIFT_LIVE === "1",
    "This assertion only applies to the isolated fixture development mode.",
  );
  await page.goto("/");
  await expect(page).toHaveURL(/\/jobs\/?$/);
});
