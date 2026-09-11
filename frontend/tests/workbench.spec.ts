import { test, expect } from "@playwright/test";
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
