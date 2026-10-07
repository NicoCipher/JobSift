import { createHmac, timingSafeEqual } from "node:crypto";

const CAPABILITY_TTL_MS = 5 * 60 * 1000;

function signature(secret: string, profileId: string, expiresAt: string) {
  return createHmac("sha256", secret)
    .update("jobsift-operator-profile-v1:" + profileId + ":" + expiresAt)
    .digest("base64url");
}

export function issueOperatorCapability(
  secret: string,
  profileId: string,
  now = Date.now(),
): string | null {
  const key = secret.trim();
  const normalized = profileId.trim().toLowerCase();
  if (!key || !/^[0-9a-f]{16}$/.test(normalized)) return null;
  const expiresAt = String(now + CAPABILITY_TTL_MS);
  return normalized + "." + expiresAt + "." + signature(key, normalized, expiresAt);
}

export function verifyOperatorCapability(
  secret: string,
  profileId: string,
  capability: string,
  now = Date.now(),
): boolean {
  const key = secret.trim();
  const normalized = profileId.trim().toLowerCase();
  const parts = capability.trim().split(".");
  if (!key || !/^[0-9a-f]{16}$/.test(normalized) || parts.length !== 3) {
    return false;
  }
  const [claimedProfile, expiresAt, supplied] = parts;
  if (
    claimedProfile !== normalized ||
    !/^\d{13}$/.test(expiresAt) ||
    Number(expiresAt) < now ||
    Number(expiresAt) > now + CAPABILITY_TTL_MS
  ) {
    return false;
  }
  const expected = signature(key, normalized, expiresAt);
  const expectedBytes = Buffer.from(expected);
  const suppliedBytes = Buffer.from(supplied);
  return (
    expectedBytes.length === suppliedBytes.length &&
    timingSafeEqual(expectedBytes, suppliedBytes)
  );
}
