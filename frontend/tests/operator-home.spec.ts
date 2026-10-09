import { expect, test } from "@playwright/test";

test("production operator opens Operations and hides obsolete demo routes", async ({ page }) => {
  test.skip(
    process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR !== "1",
    "Run with NEXT_PUBLIC_JOBSIFT_OPERATOR=1 to verify production operator routing.",
  );

  const ownerTestKey = process.env.JOBSIFT_OWNER_TEST_KEY;
  expect(ownerTestKey, "Set a temporary owner key for operator browser tests").toMatch(/^[A-Za-z0-9_-]{43}$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/owner-login/);
  await page.getByLabel("Owner access key").fill(ownerTestKey!);
  await page.getByRole("button", { name: "Unlock JobSift" }).click();
  await expect(page).toHaveURL(/\/operations\/?$/);
  await expect(page.locator(".operator-driving-home")).toBeVisible();
  await expect(page.locator("#manual-controls")).not.toHaveAttribute("open", "");
  await expect(page.locator(".operations-primary-action")).toBeHidden();
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

test("the guided home does not invent figures when client state is unverified", async ({ page }) => {
  test.skip(process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1", "Runs in isolated fixture mode.");
  await page.route("**/api/control/status**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Sourcing", state: "active", url: "https://example.com", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.com", runs: [] },
          operator_snapshot: {
            complete: false, observed_at: null, control_request_id: null,
            confirmed_control_request_id: null, state_error: "Authoritative state is unavailable.",
            run: null, profiles: [], truncated: false,
          },
        },
      }),
    }),
  );

  await page.goto("/operations");
  const home = page.locator(".operator-driving-home");
  await expect(home.getByRole("heading", { name: "Waiting for a verified update" })).toBeVisible();
  await expect(home.locator(".operator-driving-metrics dd")).toHaveText(["—", "—", "—"]);
  await expect(page.locator(".operations-primary-action")).toBeHidden();
  await expect(home.getByRole("button", { name: "Check status" })).toBeVisible();
});

test("the guided home prioritises a real review batch and keeps tuning hidden", async ({ page }) => {
  test.skip(process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1", "Runs in isolated fixture mode.");
  await page.route("**/api/control/status**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Sourcing", state: "active", url: "https://example.com", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.com", runs: [] },
          operator_snapshot: {
            complete: true, observed_at: "2026-10-09T12:00:00Z", control_request_id: null,
            confirmed_control_request_id: null, state_error: null, run: null, truncated: false,
            profiles: [{
              action: "status", profile_id: "0123456789abcdef", profile_status: "active",
              delivery_mode: "review", daily_quota: 100, sheet_status: "ready",
              delivered_today: 7, batch_id: "test-batch", batch_status: "pending",
              requested_quota: 100, selected_count: 2, shortfall: 98,
              fresh_eligible_employers: 2, match_eligible_postings: 2,
              needs_review_postings: 0, selection_eligible_postings: 2,
              stale_posting_suppressed_groups: 0, company_cap_suppressed_groups: 0,
              pending_items: [{ ordinal: 0, title: "Software Engineer",
                company: "Test Company", link: "https://example.com/job", platform: "test" }],
              pending_items_truncated: true, recovery_required: false,
              client_funnel: null, operator_managed: true, client_name: "Test Client",
              destination_name: "Test Sheet", sheet_handle: null, control_capability: null,
            }],
          },
        },
      }),
    }),
  );

  await page.goto("/operations");
  const home = page.locator(".operator-driving-home");
  await expect(home.getByRole("heading", { name: "Jobs are waiting for your decision" })).toBeVisible();
  await expect(home.locator(".operator-driving-metrics dd")).toHaveText(["1", "7", "1"]);
  await expect(home.getByRole("link", { name: "Review waiting jobs" })).toHaveAttribute("href", "#review-queue");
  await expect(page.getByRole("heading", { name: "2 jobs are waiting for approval" })).toBeVisible();
  await expect(page.locator("#manual-controls")).not.toHaveAttribute("open", "");
});


test("empty client state is not zero jobs delivered", async ({ page }) => {
  test.skip(process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1", "Runs in isolated fixture mode.");
  await page.route("**/api/control/status**", (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Sourcing", state: "active", url: "https://example.com", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.com", runs: [] },
          operator_snapshot: {
            complete: true, observed_at: "2026-10-09T12:00:00Z", control_request_id: null,
            confirmed_control_request_id: null, state_error: null, run: null,
            profiles: [], truncated: false,
          },
        },
      }),
    }),
  );

  await page.goto("/operations");
  const home = page.locator(".operator-driving-home");
  await expect(home.getByRole("heading", { name: "No client delivery state reported yet" })).toBeVisible();
  await expect(home.locator(".operator-driving-metrics dd")).toHaveText(["0", "—", "0"]);
  await expect(home.getByRole("link", { name: "Open clients" })).toHaveAttribute("href", "/clients");
});

test("failed operation gives one direct link to its recorded activity", async ({ page }) => {
  test.skip(process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1", "Runs in isolated fixture mode.");
  await page.route("**/api/control/status**", (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Sourcing", state: "active", url: "https://example.com", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.com", runs: [] },
          operator_snapshot: {
            complete: true, observed_at: "2026-10-09T12:00:00Z", control_request_id: null,
            confirmed_control_request_id: null, state_error: null, profiles: [], truncated: false,
            run: { id: 14, run_number: 14, status: "completed", conclusion: "failure",
              kind: "inventory", created_at: "2026-10-09T11:55:00Z",
              updated_at: "2026-10-09T12:00:00Z", url: "https://example.com/failure" },
          },
        },
      }),
    }),
  );

  await page.goto("/operations");
  const home = page.locator(".operator-driving-home");
  const activity = page.locator("#last-activity");
  await expect(home.getByRole("heading", { name: "The last reported operation failed" })).toBeVisible();
  await expect(activity).not.toHaveAttribute("open", "");
  await home.getByRole("link", { name: "View last activity" }).click();
  await expect(activity).toHaveAttribute("open", "");
  await expect(activity.getByText("failure")).toBeVisible();
  await expect(page.locator("#manual-controls")).not.toHaveAttribute("open", "");
});

test("mobile owner overview stays readable without exposing manual controls", async ({ page }) => {
  test.skip(process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1", "Runs in isolated fixture mode.");
  await page.setViewportSize({ width: 360, height: 800 });
  await page.route("**/api/control/status**", (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Sourcing", state: "active", url: "https://example.com", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.com", runs: [] },
          operator_snapshot: {
            complete: false, observed_at: null, control_request_id: null,
            confirmed_control_request_id: null, state_error: "State unavailable.",
            run: null, profiles: [], truncated: false,
          },
        },
      }),
    }),
  );
  await page.goto("/operations");
  await expect(page.locator(".operator-driving-home")).toBeVisible();
  await expect(page.locator(".operations-primary-action")).toBeHidden();
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(360);
});


test("paused delivery is explained before suggesting review of waiting jobs", async ({ page }) => {
  test.skip(process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1", "Runs in isolated fixture mode.");
  const profile = (id: string, client: string, status: string, batchId: string | null) => ({
    action: "status", profile_id: id, profile_status: status,
    delivery_mode: "review", daily_quota: 100, sheet_status: "ready",
    delivered_today: 2, batch_id: batchId, batch_status: batchId ? "pending" : null,
    requested_quota: 100, selected_count: batchId ? 2 : null, shortfall: batchId ? 98 : null,
    fresh_eligible_employers: 2, match_eligible_postings: 2, needs_review_postings: 0,
    selection_eligible_postings: 2, stale_posting_suppressed_groups: 0,
    company_cap_suppressed_groups: 0, pending_items: [], pending_items_truncated: false,
    recovery_required: false, client_funnel: null, operator_managed: true,
    client_name: client, destination_name: "Client Sheet", sheet_handle: null,
    control_capability: null,
  });
  await page.route("**/api/control/status**", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      data: {
        control_ready: true,
        inventory: { name: "Sourcing", state: "active", url: "https://example.com", runs: [] },
        delivery: { name: "Delivery", state: "active", url: "https://example.com", runs: [] },
        operator_snapshot: {
          complete: true, observed_at: "2026-10-09T12:00:00Z",
          control_request_id: null, confirmed_control_request_id: null,
          state_error: null, run: null, truncated: false,
          profiles: [
            profile("0123456789abcdef", "Paused Client", "paused", "batch-paused"),
            profile("fedcba9876543210", "Active Client", "active", "batch-active"),
          ],
        },
      },
    }),
  }));

  await page.goto("/operations");
  const home = page.locator(".operator-driving-home");
  await expect(home.getByRole("heading", { name: "A client is paused" })).toBeVisible();
  await expect(home.getByRole("link", { name: "View paused clients" })).toHaveAttribute("href", "/clients");
  await expect(home.getByRole("link", { name: "Review waiting jobs" })).toHaveCount(0);
});

test("client details and maintenance actions stay closed by default", async ({ page }) => {
  test.skip(process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1", "Runs in isolated fixture mode.");
  await page.route("**/api/control/status**", (route) => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({
      data: {
        control_ready: true,
        inventory: { name: "Sourcing", state: "active", url: "https://example.com", runs: [] },
        delivery: { name: "Delivery", state: "active", url: "https://example.com", runs: [] },
        operator_snapshot: {
          complete: true, observed_at: "2026-10-09T12:00:00Z",
          confirmed_control_request_id: null, state_error: null, run: null,
          truncated: false, profiles: [{
            action: "status", profile_id: "0123456789abcdef", profile_status: "active",
            delivery_mode: "review", daily_quota: 100, sheet_status: "ready",
            delivered_today: 7, batch_id: null, batch_status: null,
            requested_quota: null, selected_count: null, shortfall: null,
            pending_items: [], pending_items_truncated: false, recovery_required: false,
            client_funnel: null, operator_managed: true, client_name: "Test Client",
            destination_name: "Test Sheet", sheet_handle: null, control_capability: null,
          }],
        },
      },
    }),
  }));

  await page.goto("/clients");
  const card = page.locator(".client-card").filter({ hasText: "Test Client" });
  await expect(card).toBeVisible();
  await expect(card.getByText("Jobs sent today")).toBeVisible();
  await expect(card.getByText("Google Sheet", { exact: true })).toBeVisible();
  const actions = card.locator(".client-more-actions");
  await expect(actions).not.toHaveAttribute("open", "");
  await expect(actions.getByRole("button", { name: "Check Google Sheet" })).toBeHidden();
  await actions.locator("summary").click();
  await expect(actions.getByRole("button", { name: "Check Google Sheet" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Find jobs now" })).toBeHidden();
});
