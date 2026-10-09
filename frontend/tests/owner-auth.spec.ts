import { createHash, randomBytes } from "node:crypto";
import { expect, test } from "@playwright/test";
import { NextRequest } from "next/server";
import { OWNER_COOKIE, issueOwnerSession, validOwnerLoginOrigin, verifyOwnerSession } from "../lib/owner-auth";
import { POST as ownerLogin } from "../app/api/owner/session/route";

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

test("owner login checks real request Host and Origin without trusting a reconstructed URL", () => {
  const request = (host: string, origin?: string, site?: string) => new NextRequest(
    "http://localhost:3100/api/owner/session",
    { method: "POST", headers: {
      host,
      ...(origin ? { origin } : {}),
      ...(site ? { "sec-fetch-site": site } : {}),
    } },
  );

  // CI starts Next on 127.0.0.1 even when NextRequest uses localhost internally.
  expect(validOwnerLoginOrigin(request("127.0.0.1:3100", "http://127.0.0.1:3100", "same-origin"))).toBe(true);
  expect(validOwnerLoginOrigin(request("jobsift.example", "https://jobsift.example", "same-origin"))).toBe(true);
  expect(validOwnerLoginOrigin(request("127.0.0.1:3100", "https://evil.example", "cross-site"))).toBe(false);
  expect(validOwnerLoginOrigin(request("127.0.0.1:3100", "http://localhost:3100"))).toBe(false);
  expect(validOwnerLoginOrigin(request("jobsift.example", "http://jobsift.example"))).toBe(false);
  expect(validOwnerLoginOrigin(request("jobsift.example"))).toBe(false);
});

test("owner login redirects use the validated browser origin, not the normalized request URL", async () => {
  const key = randomBytes(32).toString("base64url");
  const oldDigest = process.env.JOBSIFT_OWNER_ACCESS_KEY_SHA256;
  const oldSecret = process.env.JOBSIFT_OWNER_SESSION_SECRET;
  process.env.JOBSIFT_OWNER_ACCESS_KEY_SHA256 = createHash("sha256").update(key).digest("hex");
  process.env.JOBSIFT_OWNER_SESSION_SECRET = randomBytes(32).toString("base64url");

  const signIn = (origin: string, host: string, accessKey: string) =>
    ownerLogin(new NextRequest("http://localhost:3100/api/owner/session", {
      method: "POST",
      headers: {
        host,
        origin,
        "sec-fetch-site": "same-origin",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({ access_key: accessKey }).toString(),
    }));

  try {
    // NextRequest's internal URL can be localhost even for a 127.0.0.1 browser.
    const allowed = await signIn("http://127.0.0.1:3100", "127.0.0.1:3100", key);
    expect(allowed.status).toBe(303);
    expect(allowed.headers.get("location")).toBe("http://127.0.0.1:3100/operations");
    expect(allowed.cookies.get(OWNER_COOKIE)?.value).toBeTruthy();
    expect(allowed.cookies.get(OWNER_COOKIE)?.secure).toBe(false);

    const denied = await signIn("http://127.0.0.1:3100", "127.0.0.1:3100", "wrong-key");
    expect(denied.status).toBe(303);
    expect(denied.headers.get("location")).toBe("http://127.0.0.1:3100/owner-login?error=invalid");
    expect(denied.cookies.get(OWNER_COOKIE)).toBeUndefined();

    // A validated HTTPS origin must also produce a Secure session cookie.
    const https = await signIn("https://jobsift.example", "jobsift.example", key);
    expect(https.status).toBe(303);
    expect(https.headers.get("location")).toBe("https://jobsift.example/operations");
    expect(https.cookies.get(OWNER_COOKIE)?.secure).toBe(true);
  } finally {
    if (oldDigest === undefined) delete process.env.JOBSIFT_OWNER_ACCESS_KEY_SHA256;
    else process.env.JOBSIFT_OWNER_ACCESS_KEY_SHA256 = oldDigest;
    if (oldSecret === undefined) delete process.env.JOBSIFT_OWNER_SESSION_SECRET;
    else process.env.JOBSIFT_OWNER_SESSION_SECRET = oldSecret;
  }
});
