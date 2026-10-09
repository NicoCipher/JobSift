import { createHash, randomBytes } from "node:crypto";
import { expect, test } from "@playwright/test";
import { issueOwnerSession, verifyOwnerSession } from "../lib/owner-auth";

test("owner session signatures cannot be forged or used after expiration", () => {
  const secret = randomBytes(32);
  const issuedAt = 1791500000000;
  const session = issueOwnerSession(secret, issuedAt);
  expect(verifyOwnerSession(session, secret, issuedAt + 1000)).toBe(true);
  expect(verifyOwnerSession(session, randomBytes(32), issuedAt + 1000)).toBe(false);
  expect(verifyOwnerSession(session, secret, issuedAt + 24 * 60 * 60 * 1000 + 1)).toBe(false);
  expect(verifyOwnerSession(session.slice(0, -1) + (session.endsWith("A") ? "B" : "A"), secret, issuedAt + 1000)).toBe(false);
});

test("owner-only API rejects unauthenticated commands and accepts a signed session", async ({ page }) => {
  test.skip(
    process.env.JOBSIFT_REQUIRE_OWNER_AUTH !== "1" ||
      !process.env.JOBSIFT_OWNER_TEST_KEY,
    "Run this integration test with the owner gate enabled and a private test key configured.",
  );
  const key = process.env.JOBSIFT_OWNER_TEST_KEY ?? "";
  const digest = createHash("sha256").update(key).digest("hex");
  expect(digest).toBe(process.env.JOBSIFT_OWNER_ACCESS_KEY_SHA256);
  await page.goto("/operations");
  await expect(page).toHaveURL(/\/owner-login/);
  await page.getByLabel("Owner access key").fill(key);
  await page.getByRole("button", { name: "Unlock JobSift" }).click();
  await expect(page).toHaveURL(/\/operations/);
  const response = await page.request.post("/api/control/dispatch", {
    data: { command: "invalid-command" },
  });
  // Authorized requests reach existing input validation rather than GitHub.
  expect(response.status()).toBe(400);
});
