import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

export const OWNER_COOKIE = "jobsift_owner_session";
const SESSION_DURATION_MS = 24 * 60 * 60 * 1000;

export function ownerAuthRequired(): boolean {
  // Never rely on an operator-mode UI switch to protect production API credentials.
  return (
    process.env.NODE_ENV === "production" ||
    process.env.VERCEL === "1" ||
    process.env.NEXT_PUBLIC_JOBSIFT_OPERATOR === "1" ||
    process.env.JOBSIFT_REQUIRE_OWNER_AUTH === "1"
  );
}

function sessionSecret(): Buffer | null {
  const value = process.env.JOBSIFT_OWNER_SESSION_SECRET ?? "";
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) return null;
  const secret = Buffer.from(value, "base64url");
  return secret.length === 32 && secret.toString("base64url") === value ? secret : null;
}

function expectedAccessKeyHash(): Buffer | null {
  const value = process.env.JOBSIFT_OWNER_ACCESS_KEY_SHA256 ?? "";
  return /^[0-9a-f]{64}$/.test(value) ? Buffer.from(value, "hex") : null;
}

export function ownerAuthConfigured(): boolean {
  return sessionSecret() !== null && expectedAccessKeyHash() !== null;
}

export function validOwnerAccessKey(supplied: string): boolean {
  const expected = expectedAccessKeyHash();
  if (!expected || !/^[A-Za-z0-9_-]{43}$/.test(supplied)) return false;
  const bytes = Buffer.from(supplied, "base64url");
  if (bytes.length !== 32 || bytes.toString("base64url") !== supplied) return false;
  const actual = createHash("sha256").update(supplied).digest();
  return timingSafeEqual(expected, actual);
}

function mac(secret: Buffer, unsigned: string): string {
  return createHmac("sha256", secret)
    .update("jobsift-owner-session-v1\0" + unsigned)
    .digest("base64url");
}

export function issueOwnerSession(secret: Buffer, now = Date.now()): string {
  const issued = String(now);
  const expires = String(now + SESSION_DURATION_MS);
  const nonce = randomBytes(24).toString("hex");
  const unsigned = `v1.${issued}.${expires}.${nonce}`;
  return `${unsigned}.${mac(secret, unsigned)}`;
}

export function verifyOwnerSession(
  cookie: string | undefined,
  secret: Buffer | null,
  now = Date.now(),
): boolean {
  if (!secret || !cookie || cookie.length > 200) return false;
  const match = /^v1\.(\d{13})\.(\d{13})\.([0-9a-f]{48})\.([A-Za-z0-9_-]{43})$/.exec(cookie);
  if (!match) return false;
  const issued = Number(match[1]);
  const expires = Number(match[2]);
  if (issued > now + 60_000 || expires <= now || expires <= issued ||
      expires - issued > SESSION_DURATION_MS) return false;
  const unsigned = `v1.${match[1]}.${match[2]}.${match[3]}`;
  const expected = Buffer.from(mac(secret, unsigned), "ascii");
  const supplied = Buffer.from(match[4], "ascii");
  return expected.length === supplied.length && timingSafeEqual(expected, supplied);
}

export function newOwnerSession(): string | null {
  const secret = sessionSecret();
  return secret ? issueOwnerSession(secret) : null;
}

export function isOwner(request: NextRequest): boolean {
  return verifyOwnerSession(request.cookies.get(OWNER_COOKIE)?.value, sessionSecret());
}

export function ownerApiGuard(request: NextRequest): NextResponse | null {
  if (!ownerAuthRequired()) return null;
  if (!ownerAuthConfigured()) {
    return NextResponse.json(
      { error: { code: "OWNER_AUTH_NOT_CONFIGURED", message: "Owner authentication is not configured." } },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (isOwner(request)) return null;
  return NextResponse.json(
    { error: { code: "OWNER_AUTH_REQUIRED", message: "Sign in as the owner to use JobSift." } },
    { status: 401, headers: { "Cache-Control": "no-store" } },
  );
}
