import { test, expect } from "@playwright/test";
import { canonicalTimeZone, validBatchId, validGenerationId, validRemovedOrdinals } from "../lib/control-validation";
import { isOperatorProfileAllowed, parseOperatorProfiles } from "../lib/operator-profiles";
test("Jobs opens and closes with Enter/Esc, restores focus, and guards typing", async ({
  page,
}) => {
  await page.goto("/jobs");
  const rows = page.locator(".jobs-table .job-link");
  await expect(rows).toHaveCount(40);
  await rows.first().focus();
  await page.keyboard.press("k");
  await expect(rows.first()).toBeFocused();
  await page.keyboard.press("j");
  await expect(rows.nth(1)).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#detail-title")).toBeFocused();
  await expect(
    page.getByRole("heading", { name: "Why it matched" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".detail")).toHaveCount(0);
  await expect(rows.nth(1)).toBeFocused();
  await page.keyboard.press("/");
  const search = page.getByRole("searchbox");
  await expect(search).toBeFocused();
  await page.keyboard.type("jk");
  await expect(search).toHaveValue("jk");
  await expect(search).toBeFocused();
});
test("fixture search, filters, empty state, and cursor pagination work", async ({
  page,
}) => {
  await page.goto("/jobs");
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(4);
  await page.getByRole("button", { name: "Previous", exact: true }).click();
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
  await page.getByRole("button", { name: "Filters", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Match decision", exact: true })
    .selectOption("needs_review");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(4);
  await page.locator(".jobs-table .job-link").first().click();
  await expect(
    page.getByRole("heading", { name: "Why it needs review" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close detail" }).click();
  await page.getByRole("searchbox").fill("nonexistent-example");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "No groups match these filters" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Clear filters", exact: true })
    .first()
    .click();
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
});
test("read-only capabilities expose no mutation controls and URLs keep their kind", async ({
  page,
}) => {
  await page.goto("/jobs");
  const rows = page.locator(".jobs-table .job-link");
  await rows.first().click();
  await expect(
    page.getByRole("link", { name: /Open application/ }),
  ).toHaveAttribute("rel", "noopener noreferrer");
  await page.getByRole("button", { name: "Close detail" }).click();
  await rows.nth(1).click();
  await expect(page.getByRole("link", { name: /Open listing/ })).toBeVisible();
  await expect(
    page.getByText(
      "Previously delivered to Example delivery destination on Sep 8, 2026.",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close detail" }).click();
  await rows.nth(2).click();
  await expect(
    page.getByText("Application URL unavailable", { exact: true }),
  ).toBeVisible();
  for (const route of ["/jobs", "/briefs", "/runs", "/settings"]) {
    await page.goto(route);
    await expect(
      page.getByRole("button", {
        name: /^(Start run|Retry run|Cancel run|Edit outcome|Applied|Not Applied|Create brief revision|Activate brief|Manage client)$/i,
      }),
    ).toHaveCount(0);
  }
});
test("operations control fails closed without server command credentials", async ({ page }) => {
  await page.goto("/operations");
  await expect(page.getByRole("heading", { name: "Operations" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "What do you want to do?" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Find Jobs" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Clients & Sheets" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "System Status" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run sourcing now" })).toBeDisabled();

  await page.getByText("Advanced sourcing controls", { exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "Yield-aware bonus targets" }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: "Pause automatic sourcing" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Resume automatic sourcing" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Run custom sourcing" })).toBeDisabled();

  await expect(page.getByRole("button", { name: "Check Sheet" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Disable Sheet" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Re-enable Sheet" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Check status" })).toBeDisabled();
});

test("control validation canonicalizes timezones and generated batch IDs", () => {
  expect(canonicalTimeZone("america/new_york")).toBe("America/New_York");
  expect(canonicalTimeZone(" Africa/Lagos ")).toBe("Africa/Lagos");
  expect(canonicalTimeZone("\"; echo pwned; #")).toBeNull();
  expect(validBatchId("01234567-89ab-5cde-8fab-0123456789ab")).toBe(true);
  expect(validBatchId("\"; echo pwned; #")).toBe(false);
  expect(validGenerationId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")).toBe(true);
  expect(validGenerationId("01234567-89ab-5cde-8fab-0123456789ab")).toBe(false);
  expect(validRemovedOrdinals("")).toBe(true);
  expect(validRemovedOrdinals("1")).toBe(true);
  expect(validRemovedOrdinals("1,2,17")).toBe(true);
  expect(validRemovedOrdinals("\\d")).toBe(false);
  expect(validRemovedOrdinals("1,0x2")).toBe(false);
});

test("operator profile catalogue rejects unlisted production controls", () => {
  const raw = JSON.stringify([
    {
      profile_id: "AD763A0336D92204",
      client_name: "Paying Client",
      destination_name: "Client Jobs",
    },
    {
      profile_id: "ad763a0336d92204",
      client_name: "Duplicate",
      destination_name: "Ignored",
    },
    {
      profile_id: "not-a-profile",
      client_name: "Invalid",
      destination_name: "Ignored",
    },
  ]);
  expect(parseOperatorProfiles(raw)).toEqual([
    {
      profile_id: "ad763a0336d92204",
      client_name: "Paying Client",
      destination_name: "Client Jobs",
    },
  ]);
  expect(isOperatorProfileAllowed(raw, "ad763a0336d92204")).toBe(true);
  expect(isOperatorProfileAllowed(raw, "0123456789abcdef")).toBe(false);
  expect(isOperatorProfileAllowed(undefined, "0123456789abcdef")).toBe(false);
});

test("control API rejects unconfigured, invalid, and cross-origin mutations", async ({ request }) => {
  const unconfigured = await request.post("/api/control/dispatch", {
    data: {
      command: "inventory-refresh",
      workday_targets: "1",
      workday_detail_concurrency: "4",
    },
  });
  expect(unconfigured.status()).toBe(503);

  const invalidYieldBudget = await request.post("/api/control/dispatch", {
    data: {
      command: "inventory-refresh",
      workday_targets: "1",
      workday_detail_concurrency: "4",
      yield_extra_budget: "999",
    },
  });
  expect(invalidYieldBudget.status()).toBe(400);

  const invalidProfile = await request.post("/api/control/dispatch", {
    data: {
      command: "client-control",
      operation: "pause",
      profile_id: "not-a-control-id",
    },
  });
  expect(invalidProfile.status()).toBe(400);

  const maliciousTimezone = await request.post("/api/control/dispatch", {
    data: {
      command: "client-control",
      operation: "set-timezone",
      profile_id: "0123456789abcdef",
      timezone: "\"; echo pwned; #",
    },
  });
  expect(maliciousTimezone.status()).toBe(403);

  const maliciousBatch = await request.post("/api/control/dispatch", {
    data: {
      command: "client-control",
      operation: "release-batch",
      profile_id: "0123456789abcdef",
      batch_id: "\"; echo pwned; #",
    },
  });
  expect(maliciousBatch.status()).toBe(403);

  const crossOrigin = await request.post("/api/control/dispatch", {
    headers: { Origin: "https://example.invalid" },
    data: {
      command: "inventory-refresh",
      workday_targets: "1",
      workday_detail_concurrency: "4",
    },
  });
  expect(crossOrigin.status()).toBe(403);
});

test("operator command center exposes pending review batch without opaque IDs", async ({ page }) => {
  await page.route("**/api/control/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: {
            name: "Inventory",
            state: "active",
            url: "https://example.invalid/inventory",
            runs: [],
          },
          delivery: {
            name: "Delivery",
            state: "active",
            url: "https://example.invalid/delivery",
            runs: [],
          },
          operator_snapshot: {
            run: {
              id: 43,
              run_number: 43,
              status: "completed",
              conclusion: "success",
              created_at: "2026-10-06T18:59:46Z",
              updated_at: "2026-10-06T19:12:25Z",
              url: "https://example.invalid/run/43",
            },
            profiles: [
              {
                action: "awaiting_release",
                profile_id: "ad763a0336d92204",
                batch_id: "f96331fa-7c62-5793-b2e5-ea395286d416",
                batch_status: "prepared",
                requested_quota: 100,
                selected_count: 1,
                shortfall: 99,
                fresh_eligible_employers: 1,
                match_eligible_postings: 30,
                needs_review_postings: 0,
                selection_eligible_postings: 30,
                stale_posting_suppressed_groups: 29,
                company_cap_suppressed_groups: 0,
                pending_items: [
                  {
                    ordinal: 1,
                    title: "Software Engineer",
                    company: "Acme",
                    link: "https://example.invalid/job-1",
                    platform: "Greenhouse",
                  },
                ],
                pending_items_truncated: false,
                client_funnel: null,
              },
            ],
          },
        },
      }),
    });
  });

  const dispatched: Record<string, unknown>[] = [];
  await page.route("**/api/control/dispatch", async (route) => {
    dispatched.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ data: { accepted: true } }),
    });
  });

  page.on("dialog", (dialog) => void dialog.accept());
  await page.goto("/operations");

  await expect(page.getByRole("heading", { name: "Right now" })).toBeVisible();
  await expect(page.getByText("1 job is waiting for approval")).toBeVisible();
  await expect(page.getByText("Software Engineer", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open job" })).toHaveAttribute(
    "href",
    "https://example.invalid/job-1",
  );
  await expect(
    page.locator(".operator-batch-card").getByText(
      "Example client — Example delivery destination",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByText(/pending review batch stopped a new client evaluation/)).toBeVisible();

  const release = page.getByRole("button", { name: "Release 1 job" });
  await release.click();
  await expect.poll(() => dispatched.length).toBe(1);
  await expect(release).toBeDisabled();
  expect(dispatched[0]).toMatchObject({
    command: "client-control",
    operation: "release-batch",
    profile_id: "ad763a0336d92204",
    batch_id: "f96331fa-7c62-5793-b2e5-ea395286d416",
  });
});

test("pending batch actions fail closed when the client label is unavailable", async ({ page }) => {
  await page.route("**/api/control/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Inventory", state: "active", url: "https://example.invalid/inventory", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.invalid/delivery", runs: [] },
          operator_snapshot: {
            run: {
              id: 44,
              run_number: 44,
              status: "completed",
              conclusion: "success",
              created_at: "2026-10-06T19:00:00Z",
              updated_at: "2026-10-06T19:10:00Z",
              url: "https://example.invalid/run/44",
            },
            profiles: [
              {
                action: "awaiting_release",
                profile_id: "0123456789abcdef",
                batch_id: "f96331fa-7c62-5793-b2e5-ea395286d416",
                batch_status: "prepared",
                requested_quota: 100,
                selected_count: 1,
                shortfall: 99,
                fresh_eligible_employers: 1,
                match_eligible_postings: 30,
                needs_review_postings: 0,
                selection_eligible_postings: 30,
                stale_posting_suppressed_groups: 29,
                company_cap_suppressed_groups: 0,
                pending_items: [
                  {
                    ordinal: 1,
                    title: "Software Engineer",
                    company: "Acme",
                    link: "https://example.invalid/job-1",
                    platform: "Greenhouse",
                  },
                ],
                pending_items_truncated: false,
                client_funnel: null,
              },
            ],
          },
        },
      }),
    });
  });
  await page.goto("/operations");
  await expect(
    page.locator(".operator-batch-card").getByText("Client name unavailable", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Release 1 job" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Discard" })).toBeDisabled();
  await expect(page.getByText(/Batch actions are locked/)).toBeVisible();
});

test("journaled delivery recovery is visible and cannot be discarded", async ({ page }) => {
  await page.route("**/api/control/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Inventory", state: "active", url: "https://example.invalid/inventory", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.invalid/delivery", runs: [] },
          operator_snapshot: {
            run: null,
            truncated: false,
            profiles: [
              {
                action: "reconciliation_required",
                profile_id: "ad763a0336d92204",
                profile_status: "active",
                delivery_mode: "review",
                daily_quota: 100,
                sheet_status: "ready",
                delivered_today: 0,
                batch_id: "f96331fa-7c62-5793-b2e5-ea395286d416",
                batch_status: "failed",
                requested_quota: 100,
                selected_count: 1,
                shortfall: 99,
                fresh_eligible_employers: 1,
                match_eligible_postings: 1,
                needs_review_postings: 0,
                selection_eligible_postings: 1,
                stale_posting_suppressed_groups: 0,
                company_cap_suppressed_groups: 0,
                pending_items: [],
                pending_items_truncated: false,
                recovery_required: true,
                client_funnel: null,
              },
            ],
          },
        },
      }),
    });
  });

  await page.goto("/operations");
  await expect(page.getByText("A Sheet delivery needs safe recovery")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry safe delivery" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Discard" })).toBeDisabled();
  await expect(page.getByText(/Discard is locked because this batch has a delivery journal/)).toBeVisible();
});

test("unverified latest operator state locks state-dependent controls", async ({ page }) => {
  const dispatched: Record<string, unknown>[] = [];
  await page.route("**/api/control/dispatch", async (route) => {
    dispatched.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          accepted: true,
          control_request_id: "33333333-3333-4333-8333-333333333333",
        },
      }),
    });
  });

  await page.route("**/api/control/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: {
            name: "Inventory",
            state: "active",
            url: "https://example.invalid/inventory",
            runs: [],
          },
          delivery: {
            name: "Delivery",
            state: "active",
            url: "https://example.invalid/delivery",
            runs: [],
          },
          operator_snapshot: {
            complete: false,
            state_error:
              "Latest JobSift mutation completed, but its authoritative state snapshot is unavailable.",
            run: {
              id: 45,
              run_number: 45,
              status: "completed",
              conclusion: "failure",
              created_at: "2026-10-06T20:00:00Z",
              updated_at: "2026-10-06T20:05:00Z",
              url: "https://example.invalid/run/45",
              kind: "delivery",
            },
            truncated: false,
            profiles: [],
          },
        },
      }),
    });
  });

  await page.goto("/operations");
  await expect(
    page.locator(".operator-command-center").getByRole("alert"),
  ).toContainText("Client state could not be verified");
  await expect(page.getByText(/No review batch is blocking/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run sourcing now" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Check Sheet", exact: true }).last()).toBeDisabled();
  await expect(page.getByLabel("What do you want to do?")).toBeDisabled();

  const sync = page.getByRole("button", { name: "Sync current state" });
  await expect(sync).toBeEnabled();
  await sync.click();
  await expect.poll(() => dispatched.length).toBe(1);
  expect(dispatched[0]).toMatchObject({
    command: "client-control",
    operation: "list",
  });
});

test("failed status refresh locks actions from the previous verified snapshot", async ({ page }) => {
  let failStatus = false;
  await page.route("**/api/control/status", async (route) => {
    if (failStatus) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: { message: "Could not load JobSift workflow status." },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: {
            name: "Inventory",
            state: "active",
            url: "https://example.invalid/inventory",
            runs: [],
          },
          delivery: {
            name: "Delivery",
            state: "active",
            url: "https://example.invalid/delivery",
            runs: [],
          },
          operator_snapshot: {
            complete: true,
            state_error: null,
            run: null,
            truncated: false,
            profiles: [
              {
                action: "awaiting_release",
                profile_id: "ad763a0336d92204",
                profile_status: "active",
                delivery_mode: "review",
                daily_quota: 100,
                sheet_status: "ready",
                delivered_today: 0,
                batch_id: "f96331fa-7c62-5793-b2e5-ea395286d416",
                batch_status: "prepared",
                requested_quota: 100,
                selected_count: 1,
                shortfall: 99,
                fresh_eligible_employers: 1,
                match_eligible_postings: 1,
                needs_review_postings: 0,
                selection_eligible_postings: 1,
                stale_posting_suppressed_groups: 0,
                company_cap_suppressed_groups: 0,
                pending_items: [],
                pending_items_truncated: false,
                recovery_required: false,
                client_funnel: null,
              },
            ],
          },
        },
      }),
    });
  });

  await page.goto("/operations");
  const release = page.getByRole("button", { name: "Release 1 job" });
  await expect(release).toBeEnabled();

  failStatus = true;
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(page.getByText("Could not load JobSift workflow status.")).toBeVisible();
  await expect(release).toBeDisabled();
});

test("a newer authoritative snapshot can prove a dispatched command was superseded", async ({ page }) => {
  const requestedId = "11111111-1111-4111-8111-111111111111";
  const newerId = "22222222-2222-4222-8222-222222222222";
  let commandAccepted = false;

  await page.route("**/api/control/status*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: {
            name: "Inventory",
            state: "active",
            url: "https://example.invalid/inventory",
            runs: [],
          },
          delivery: {
            name: "Delivery",
            state: "active",
            url: "https://example.invalid/delivery",
            runs: [],
          },
          operator_snapshot: {
            complete: true,
            observed_at: commandAccepted
              ? "2026-10-06T20:10:00Z"
              : "2026-10-06T20:00:00Z",
            control_request_id: commandAccepted ? newerId : null,
            confirmed_control_request_id: commandAccepted ? requestedId : null,
            state_error: null,
            run: {
              id: commandAccepted ? 2 : 1,
              run_number: commandAccepted ? 2 : 1,
              status: "completed",
              conclusion: "success",
              created_at: "2026-10-06T20:00:00Z",
              updated_at: commandAccepted
                ? "2026-10-06T20:10:00Z"
                : "2026-10-06T20:00:00Z",
              url: "https://example.invalid/state",
              kind: "inventory",
            },
            truncated: false,
            profiles: [],
          },
        },
      }),
    });
  });

  await page.route("**/api/control/dispatch", async (route) => {
    commandAccepted = true;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          accepted: true,
          control_request_id: requestedId,
        },
      }),
    });
  });

  await page.goto("/operations");
  const runNow = page.getByRole("button", { name: "Run sourcing now" });
  await expect(runNow).toBeEnabled();
  await runNow.click();
  await expect(runNow).toBeDisabled();

  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(runNow).toBeEnabled();
});

test("truncated operator state never claims all clients are clear", async ({ page }) => {
  await page.route("**/api/control/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Inventory", state: "active", url: "https://example.invalid/inventory", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.invalid/delivery", runs: [] },
          operator_snapshot: {
            run: null,
            truncated: true,
            profiles: [
              {
                action: "ready",
                profile_id: "ad763a0336d92204",
                profile_status: "active",
                delivery_mode: "review",
                daily_quota: 100,
                sheet_status: "ready",
                delivered_today: 0,
                batch_id: null,
                batch_status: null,
                requested_quota: null,
                selected_count: null,
                shortfall: null,
                fresh_eligible_employers: null,
                match_eligible_postings: null,
                needs_review_postings: null,
                selection_eligible_postings: null,
                stale_posting_suppressed_groups: null,
                company_cap_suppressed_groups: null,
                pending_items: [],
                pending_items_truncated: false,
                recovery_required: false,
                client_funnel: null,
              },
            ],
          },
        },
      }),
    });
  });

  await page.goto("/operations");
  await expect(page.getByText(/Client state is incomplete/)).toBeVisible();
  await expect(page.getByText(/No review batch is blocking/)).toHaveCount(0);
});

test("yield-aware scheduling is visible and dispatches guarded bonus capacity", async ({ page }) => {
  await page.route("**/api/control/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: {
            name: "Inventory",
            state: "active",
            url: "https://example.invalid/inventory",
            runs: [],
          },
          delivery: {
            name: "Delivery",
            state: "active",
            url: "https://example.invalid/delivery",
            runs: [],
          },
        },
      }),
    });
  });

  const dispatched: Record<string, unknown>[] = [];
  await page.route("**/api/control/dispatch", async (route) => {
    dispatched.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ data: { accepted: true } }),
    });
  });

  await page.goto("/operations");
  const statusSection = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "System Status" }) });
  await expect(statusSection.getByText("Yield-aware scheduling", { exact: true })).toBeVisible();
  await expect(statusSection.getByText(/72-hour evidence window/)).toBeVisible();
  await expect(statusSection.getByText(/100 scheduled bonus targets/)).toBeVisible();

  const findJobsSection = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Find Jobs" }) });
  await findJobsSection.getByText("Advanced sourcing controls", { exact: true }).click();
  await findJobsSection
    .getByRole("combobox", { name: "Yield-aware bonus targets" })
    .selectOption("50");
  await findJobsSection.getByRole("button", { name: "Run custom sourcing" }).click();

  await expect.poll(() => dispatched.length).toBe(1);
  expect(dispatched[0]).toMatchObject({
    command: "inventory-refresh",
    workday_targets: "1",
    workday_detail_concurrency: "4",
    yield_extra_budget: "50",
  });
});

test("client Sheet controls keep listed and manual targets explicit", async ({ page }) => {
  let stateVersion = 0;
  let lastControlRequestId: string | null = null;
  await page.route("**/api/control/status*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { name: "Inventory", state: "active", url: "https://example.invalid/inventory", runs: [] },
          delivery: { name: "Delivery", state: "active", url: "https://example.invalid/delivery", runs: [] },
          operator_snapshot: {
            complete: true,
            observed_at: `2026-10-06T20:00:0${stateVersion}Z`,
            control_request_id: lastControlRequestId,
            confirmed_control_request_id: lastControlRequestId,
            state_error: null,
            run: {
              id: 100 + stateVersion,
              run_number: 100 + stateVersion,
              status: "completed",
              conclusion: "success",
              created_at: "2026-10-06T20:00:00Z",
              updated_at: `2026-10-06T20:00:0${stateVersion}Z`,
              url: "https://example.invalid/state",
              kind: "delivery",
            },
            truncated: false,
            profiles: [],
          },
        },
      }),
    });
  });

  const dispatched: Record<string, unknown>[] = [];
  await page.route("**/api/control/dispatch", async (route) => {
    dispatched.push(route.request().postDataJSON() as Record<string, unknown>);
    stateVersion += 1;
    lastControlRequestId =
      `00000000-0000-4000-8000-${String(stateVersion).padStart(12, "0")}`;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          accepted: true,
          control_request_id: lastControlRequestId,
        },
      }),
    });
  });

  await page.goto("/operations");
  const deliveryProfile = page.getByRole("combobox", { name: "Delivery profile", exact: true });
  const clientSheet = page.getByRole("combobox", { name: "Client Sheet", exact: true });
  await expect(deliveryProfile).toHaveValue("ad763a0336d92204");
  await expect(clientSheet).toHaveValue("ad763a0336d92204");
  await expect(deliveryProfile.locator("option").first()).toHaveText(
    "Example client — Example delivery destination",
  );

  const dialogs: string[] = [];
  page.on("dialog", async (dialog) => {
    dialogs.push(dialog.message());
    await dialog.accept();
  });

  await page.getByRole("combobox", { name: "What do you want to do?" }).selectOption("pause");
  await page.getByRole("button", { name: "Pause delivery" }).click();
  await expect.poll(() => dialogs.length).toBe(1);
  expect(dialogs[0]).toContain("Example client — Example delivery destination");
  expect(dialogs[0]).toContain("ad763a0336d92204");
  await expect.poll(() => dispatched.length).toBe(1);
  expect(dispatched[0]?.profile_id).toBe("ad763a0336d92204");
  expect(JSON.stringify(dispatched[0])).not.toContain("example-client");
  expect(JSON.stringify(dispatched[0])).not.toContain("example-destination");
  await page.getByRole("button", { name: "Refresh status" }).click();

  const sheetBlock = page
    .locator(".operations-client-block")
    .filter({ has: page.getByRole("heading", { name: "Google Sheet" }) });
  await sheetBlock.getByText("Advanced target", { exact: true }).click();
  const sheetTarget = sheetBlock.getByRole("combobox", { name: "Target by" });
  await expect(sheetTarget).toBeEnabled();
  await sheetTarget.selectOption("manual");
  await sheetBlock.getByRole("textbox", { name: "Profile ID" }).fill("0123456789abcdef");
  await sheetBlock.getByRole("button", { name: "Check Sheet" }).click();
  await expect.poll(() => dispatched.length).toBe(2);
  expect(dispatched[1]?.profile_id).toBe("0123456789abcdef");
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(sheetTarget).toBeEnabled();

  await sheetTarget.selectOption("listed");
  await expect(sheetBlock.getByRole("textbox", { name: "Profile ID" })).toHaveCount(0);
  await sheetBlock.getByRole("button", { name: "Check Sheet" }).click();
  await expect.poll(() => dispatched.length).toBe(3);
  expect(dispatched[2]?.profile_id).toBe("ad763a0336d92204");
  await page.getByRole("button", { name: "Refresh status" }).click();

  const deliveryBlock = page
    .locator(".operations-client-block")
    .filter({ has: page.getByRole("heading", { name: "Delivery" }) });
  await deliveryBlock.getByText("Advanced target", { exact: true }).click();
  const deliveryTarget = deliveryBlock.getByRole("combobox", { name: "Target client by" });
  await expect(deliveryTarget).toBeEnabled();
  await deliveryTarget.selectOption("manual");
  await deliveryBlock.getByRole("textbox", { name: "Profile ID" }).fill("fedcba9876543210");
  await deliveryBlock.getByRole("combobox", { name: "What do you want to do?" }).selectOption("set-quota");
  await deliveryBlock.getByRole("button", { name: "Save daily limit" }).click();
  await expect.poll(() => dispatched.length).toBe(4);
  expect(dispatched[3]?.profile_id).toBe("fedcba9876543210");
  await page.getByRole("button", { name: "Refresh status" }).click();
  await expect(deliveryTarget).toBeEnabled();

  await deliveryTarget.selectOption("listed");
  await expect(deliveryBlock.getByRole("textbox", { name: "Profile ID" })).toHaveCount(0);
  await deliveryBlock.getByRole("button", { name: "Save daily limit" }).click();
  await expect.poll(() => dispatched.length).toBe(5);
  expect(dispatched[4]?.profile_id).toBe("ad763a0336d92204");
});

test("partial run is data, not a service failure; missing and zero are explicit", async ({
  page,
}) => {
  await page.goto("/runs");
  await expect(
    page.getByRole("heading", {
      name: "Partial — retained results with a source failure",
    }),
  ).toBeVisible();
  await expect(page.getByText("Workday · network_failure")).toBeVisible();
  await expect(
    page.getByRole("row").filter({ hasText: "CSV rows appended" }),
  ).toContainText("0");
  await expect(
    page.getByRole("row").filter({ hasText: "Normalized" }),
  ).toContainText("Not reported");
});
test("baseline fixture preserves 62 postings and 56 equivalent groups", async ({
  page,
}) => {
  await page.goto("/diagnostics");
  await expect(
    page.getByRole("row").filter({ hasText: "Eligible postings" }),
  ).toContainText("62");
  await expect(
    page.getByRole("row").filter({ hasText: "Equivalent fresh groups" }),
  ).toContainText("56");
  await expect(
    page.getByRole("heading", { name: /Frozen V2 equivalent baseline/ }),
  ).toBeVisible();
  await expect(page.getByText("62 fresh groups", { exact: false })).toHaveCount(
    0,
  );
});
test("all routes render one main heading and no browser errors", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const route of [
    "/dashboard",
    "/operations",
    "/jobs",
    "/review",
    "/briefs",
    "/runs",
    "/history",
    "/diagnostics",
    "/settings",
  ]) {
    await page.goto(route);
    await expect(
      page.getByRole("main").getByRole("heading", { level: 1 }),
    ).toHaveCount(1);
    await expect(
      page.getByText("Development fixtures", { exact: true }),
    ).toBeVisible();
  }
  expect(errors).toEqual([]);
});
for (const width of [320, 768, 1024, 1440])
  test(`responsive ${width}px and readable detail`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/jobs");
    await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    if (width < 1024) {
      await expect(
        page.getByRole("button", { name: "Menu", exact: true }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Menu", exact: true }).click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(
        page.getByRole("button", { name: "Menu", exact: true }),
      ).toBeFocused();
    } else {
      await expect(
        page.getByRole("columnheader", { name: "Role", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("columnheader", { name: "Company", exact: true }),
      ).toBeVisible();
    }
    const link = page
      .locator(
        width < 1024 ? ".mobile-jobs .job-link" : ".jobs-table .job-link",
      )
      .first();
    await link.click();
    await expect(page.locator("#detail-title")).toBeFocused();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.getByRole("button", { name: "Close detail" }).click();
    await expect(link).toBeFocused();
  });
test("1440×900 meets default density; theme and shortcut preferences persist", async ({
  page,
}) => {
  await page.goto("/jobs");
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
  const measurement = await page
    .locator(".jobs-table tbody tr")
    .evaluateAll((rows) => ({
      visible: rows.filter(
        (r) =>
          r.getBoundingClientRect().top >= 0 &&
          r.getBoundingClientRect().bottom <= innerHeight,
      ).length,
      height: rows[0].getBoundingClientRect().height,
    }));
  console.log("1440x900 default density", measurement);
  expect(measurement.visible).toBeGreaterThanOrEqual(16);
  expect(measurement.height).toBeGreaterThanOrEqual(40);
  await page.goto("/settings");
  await page
    .getByRole("combobox", { name: "Theme", exact: true })
    .selectOption("dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page
    .getByRole("combobox", {
      name: "Character keyboard shortcuts",
      exact: true,
    })
    .selectOption("disabled");
  await page.goto("/jobs");
  await page.locator(".jobs-table .job-link").first().focus();
  await page.keyboard.press("/");
  await expect(page.getByRole("searchbox")).not.toBeFocused();
});

test("browser Back preserves the second cursor page and snapshot", async ({
  page,
}) => {
  await page.goto("/jobs");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  const rows = page.locator(".jobs-table .job-link");
  await expect(rows).toHaveCount(4);
  const title = await rows.first().textContent();
  await rows.first().click();
  await expect(page.locator("#detail-title")).toBeFocused();
  await page.goBack();
  await expect(page.locator(".detail")).toHaveCount(0);
  await expect(rows).toHaveCount(4);
  await expect(rows.first()).toHaveText(title!);
});

test("mobile filter dialog restores focus after Escape", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto("/jobs");
  const trigger = page.getByRole("button", { name: "Filters", exact: true });
  await trigger.click();
  await expect(page.getByRole("dialog", { name: "Job filters" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test("slash preserves native editing for all contenteditable forms", async ({ page }) => {
  await page.goto("/jobs");
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
  for (const value of ["", "plaintext-only", "true"]) {
    await page.evaluate((attribute) => {
      document.querySelector("#review-editor")?.remove();
      const editor = document.createElement("div");
      editor.id = "review-editor";
      editor.setAttribute("contenteditable", attribute);
      document.querySelector("main")!.prepend(editor);
      editor.focus();
    }, value);
    await page.keyboard.type("/");
    await expect.soft(page.locator("#review-editor")).toBeFocused();
    await expect.soft(page.locator("#review-editor")).toHaveText("/");
  }
});

test("200 percent text keeps match status inside its table cell", async ({ page }) => {
  await page.goto("/jobs");
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  const geometry = await page.locator(".jobs-table tbody tr").first().evaluate((row) => ({
    statusRight: row.querySelector(".status")!.getBoundingClientRect().right,
    cellRight: row.querySelector(".match-col")!.getBoundingClientRect().right,
  }));
  expect(geometry.statusRight).toBeLessThanOrEqual(geometry.cellRight);
});

test("copied and new-tab detail links preserve snapshot identity and recover explicitly", async ({ page, context }) => {
  await page.goto("/jobs");
  const row = page.locator(".jobs-table .job-link").first();
  await expect(row).toBeVisible();
  const href = (await row.getAttribute("href"))!;
  const copied = new URL(href, page.url());
  const snapshot = copied.searchParams.get("snapshot_id");
  expect(snapshot).toBeTruthy();
  await row.click();
  await expect(page.locator(".detail")).toBeVisible();
  expect(new URL(page.url()).searchParams.get("snapshot_id")).toBe(snapshot);
  const tab = await context.newPage();
  await tab.goto(copied.toString());
  // Fixture snapshots are session-local: a new tab must not replace missing evidence silently.
  await expect(tab.locator("main [role=alert]")).toContainText("snapshot has expired");
  expect(new URL(tab.url()).searchParams.get("snapshot_id")).toBe(snapshot);
  await expect(tab.locator(".detail")).toHaveCount(0);
  await tab.getByRole("button", { name: "Refresh Jobs" }).click();
  await expect(tab.locator(".jobs-table tbody tr")).toHaveCount(40);
  await tab.close();
  copied.searchParams.delete("snapshot_id");
  await page.goto(copied.toString());
  await expect(page.locator("main [role=alert]")).toContainText("Snapshot unavailable");
  await expect(page.locator(".detail")).toHaveCount(0);
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(0);
  await page.getByRole("button", { name: "Refresh Jobs" }).click();
  await expect(page.locator(".jobs-table tbody tr")).toHaveCount(40);
});

test("nested editable descendants keep slash, j/k and detail Escape native", async ({ page }) => {
  await page.goto("/jobs");
  await page.locator(".jobs-table .job-link").first().click();
  await expect(page.locator("#detail-title")).toBeFocused();
  await page.evaluate(() => {
    const container = document.createElement("div");
    container.id = "nested-container";
    container.contentEditable = "true";
    const child = document.createElement("span");
    child.id = "nested-editor";
    child.textContent = "Seed";
    container.append(child);
    document.querySelector(".detail")!.append(container);
    container.focus();
    const range = document.createRange();
    range.selectNodeContents(child);
    range.collapse(false);
    window.getSelection()!.removeAllRanges();
    window.getSelection()!.addRange(range);
  });
  const prevented = await page.locator("#nested-editor").evaluate((child) =>
    ["/", "j", "k"].map((key) => {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      child.dispatchEvent(event);
      return event.defaultPrevented;
    })
  );
  expect(prevented).toEqual([false, false, false]);
  await page.keyboard.type("/jk");
  await expect(page.locator("#nested-container")).toBeFocused();
  await expect(page.locator("#nested-editor")).toHaveText("Seed/jk");
  await page.keyboard.press("Escape");
  await expect(page.locator(".detail")).toBeVisible();
  await page.locator("#detail-title").focus();
  await page.keyboard.press("Escape");
  await expect(page.locator(".detail")).toHaveCount(0);
  await page.keyboard.press("/");
  await expect(page.getByRole("searchbox")).toBeFocused();
});


test("Clients page shows operator state without exposing opaque identifiers", async ({ page }) => {
  await page.route("**/api/control/status*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { state: "active" },
          operator_snapshot: {
            complete: true,
            observed_at: "2026-10-07T05:30:00Z",
            confirmed_control_request_id: null,
            state_error: null,
            run: null,
            truncated: false,
            profiles: [
              {
                action: "awaiting_release",
                profile_id: "ad763a0336d92204",
                profile_status: "active",
                delivery_mode: "review",
                daily_quota: 100,
                sheet_status: "ready",
                delivered_today: 17,
                batch_id: "f96331fa-7c62-5793-b2e5-ea395286d416",
                batch_status: "prepared",
                requested_quota: 83,
                selected_count: 2,
                shortfall: 81,
                pending_items: [],
                pending_items_truncated: false,
                recovery_required: false,
                client_funnel: {
                  overall: {
                    confirmed_matches_0_24h: 63,
                  },
                  age_buckets: {},
                  delivery: {},
                },
              },
            ],
          },
        },
      }),
    });
  });

  await page.goto("/clients");
  await expect(page.getByRole("heading", { name: "Clients", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Example client" })).toBeVisible();
  await expect(page.getByText("Active", { exact: true })).toBeVisible();
  await expect(page.locator(".client-card").getByText("Review", { exact: true })).toBeVisible();
  await expect(page.getByText("17", { exact: true })).toBeVisible();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  await expect(page.getByText("63 jobs", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Review 2 jobs" })).toBeVisible();
  await expect(page.getByText("ad763a0336d92204")).toHaveCount(0);
  await expect(page.getByText("f96331fa-7c62-5793-b2e5-ea395286d416")).toHaveCount(0);
});

test("Review shows authoritative evidence and sends only kept jobs", async ({ page }) => {
  const batchId = "f96331fa-7c62-5793-b2e5-ea395286d416";
  const controlRequestId = "11111111-1111-4111-8111-111111111111";
  await page.route("**/api/control/status*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { state: "active" },
          operator_snapshot: {
            complete: true,
            observed_at: "2026-10-07T05:30:00Z",
            confirmed_control_request_id: null,
            state_error: null,
            run: null,
            truncated: false,
            profiles: [
              {
                action: "awaiting_release",
                profile_id: "ad763a0336d92204",
                profile_status: "active",
                delivery_mode: "review",
                daily_quota: 100,
                sheet_status: "ready",
                delivered_today: 17,
                batch_id: batchId,
                batch_status: "prepared",
                requested_quota: 83,
                selected_count: 2,
                shortfall: 81,
                pending_items: [],
                pending_items_truncated: false,
                recovery_required: false,
                client_funnel: null,
              },
            ],
          },
        },
      }),
    });
  });

  await page.route("**/api/control/review**", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          data: { state: "queued", control_request_id: controlRequestId },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          state: "ready",
          review: {
            observed_at: "2026-10-07T05:30:00Z",
            profile_id: "ad763a0336d92204",
            batch_id: batchId,
            generation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            batch_status: "prepared",
            selected_count: 2,
            requested_quota: 83,
            freshness_limit_hours: 24,
            safe_to_release: true,
            recovery_required: false,
            error: null,
            items: [
              {
                ordinal: 1,
                title: "Software Engineer",
                company: "Acme",
                application_link: "https://example.invalid/job-1",
                source: "Greenhouse",
                posted_at: "2026-10-07T02:30:00Z",
                age_hours: 3,
                location: "United States",
                remote_status: "remote",
                decision: "strong_match",
                matched_reasons: ["role title matched", "remote policy satisfied"],
                review_reasons: [],
                evidence_verified: true,
                release_ready: true,
                warnings: [],
              },
              {
                ordinal: 2,
                title: "Backend Developer",
                company: "Beta",
                application_link: "https://example.invalid/job-2",
                source: "Ashby",
                posted_at: "2026-10-07T01:30:00Z",
                age_hours: 4,
                location: "United States",
                remote_status: "remote",
                decision: "possible_match",
                matched_reasons: ["software role matched"],
                review_reasons: [],
                evidence_verified: true,
                release_ready: true,
                warnings: [],
              },
            ],
          },
        },
      }),
    });
  });

  const dispatched: Record<string, unknown>[] = [];
  await page.route("**/api/control/dispatch", async (route) => {
    dispatched.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          accepted: true,
          control_request_id: "22222222-2222-4222-8222-222222222222",
        },
      }),
    });
  });

  page.on("dialog", (dialog) => void dialog.accept());
  await page.goto("/review");
  await page.getByRole("button", { name: "Load jobs to review" }).click();

  await expect(page.getByRole("heading", { name: "Software Engineer" })).toBeVisible();
  await expect(page.getByText("3h old", { exact: true })).toBeVisible();
  await expect(page.getByText("Greenhouse", { exact: true })).toBeVisible();
  await expect(page.getByText("Role title matched", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open job" }).first()).toHaveAttribute(
    "href",
    "https://example.invalid/job-1",
  );

  const firstJob = page.locator(".review-job").first();
  await firstJob.getByRole("button", { name: "Remove" }).click();
  await expect(firstJob.getByText("Will not be sent")).toBeVisible();

  const send = page.getByRole("button", {
    name: "Send 1 job to Example client's Sheet",
  });
  await expect(send).toBeEnabled();
  await send.click();

  await expect.poll(() => dispatched.length).toBe(1);
  expect(dispatched[0]).toMatchObject({
    command: "client-control",
    operation: "release-selection",
    profile_id: "ad763a0336d92204",
    batch_id: batchId,
    removed_ordinals: "1",
    expected_generation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  });
  await expect(page.getByText("ad763a0336d92204")).toHaveCount(0);
  await expect(page.getByText(batchId)).toHaveCount(0);
});

test("Review locks unsafe kept evidence until the operator removes it", async ({ page }) => {
  const batchId = "f96331fa-7c62-5793-b2e5-ea395286d416";
  await page.route("**/api/control/status*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          control_ready: true,
          inventory: { state: "active" },
          operator_snapshot: {
            complete: true,
            observed_at: "2026-10-07T05:30:00Z",
            confirmed_control_request_id: null,
            state_error: null,
            run: null,
            truncated: false,
            profiles: [{
              action: "awaiting_release",
              profile_id: "ad763a0336d92204",
              profile_status: "active",
              delivery_mode: "review",
              daily_quota: 100,
              sheet_status: "ready",
              delivered_today: 0,
              batch_id: batchId,
              batch_status: "prepared",
              requested_quota: 2,
              selected_count: 2,
              shortfall: 0,
              pending_items: [],
              pending_items_truncated: false,
              recovery_required: false,
              client_funnel: null,
            }],
          },
        },
      }),
    });
  });
  await page.route("**/api/control/review**", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          data: {
            state: "queued",
            control_request_id: "11111111-1111-4111-8111-111111111111",
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        data: {
          state: "ready",
          review: {
            observed_at: "2026-10-07T05:30:00Z",
            profile_id: "ad763a0336d92204",
            batch_id: batchId,
            generation_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            batch_status: "prepared",
            selected_count: 2,
            requested_quota: 2,
            freshness_limit_hours: 24,
            safe_to_release: false,
            recovery_required: false,
            error: null,
            items: [
              {
                ordinal: 1,
                title: "Safe role",
                company: "Acme",
                application_link: "https://example.invalid/safe",
                source: "Greenhouse",
                posted_at: "2026-10-07T04:30:00Z",
                age_hours: 1,
                location: "United States",
                remote_status: "remote",
                decision: "strong_match",
                matched_reasons: ["role title matched"],
                review_reasons: [],
                evidence_verified: true,
                release_ready: true,
                warnings: [],
              },
              {
                ordinal: 2,
                title: "Changed role",
                company: "Beta",
                application_link: "https://example.invalid/changed",
                source: "Ashby",
                posted_at: "2026-10-05T04:30:00Z",
                age_hours: 49,
                location: "United States",
                remote_status: "remote",
                decision: "strong_match",
                matched_reasons: [],
                review_reasons: [],
                evidence_verified: false,
                release_ready: false,
                warnings: ["Job or match evidence changed after this review batch was prepared."],
              },
            ],
          },
        },
      }),
    });
  });

  await page.goto("/review");
  await page.getByRole("button", { name: "Load jobs to review" }).click();
  const send = page.getByRole("button", {
    name: "Send 2 jobs to Example client's Sheet",
  });
  await expect(send).toBeDisabled();
  await expect(page.getByText(/At least one kept job is no longer safe/)).toBeVisible();

  await page.getByRole("button", { name: "Approve all eligible" }).click();
  await expect(
    page.getByRole("button", { name: "Send 1 job to Example client's Sheet" }),
  ).toBeEnabled();
});
