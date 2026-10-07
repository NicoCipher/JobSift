import {
  createCipheriv,
  createDecipheriv,
  createHash,
  randomBytes,
} from "node:crypto";

const HANDLE_TTL_MS = 10 * 60 * 1000;

function key(secret: string) {
  return createHash("sha256")
    .update("jobsift-sheet-handle-v1\0")
    .update(secret)
    .digest();
}

export function issueSheetHandle(
  secret: string,
  profileId: string,
  sheetUrl: string,
  now = Date.now(),
): string | null {
  const normalizedProfile = profileId.trim().toLowerCase();
  const url = sheetUrl.trim();
  if (
    !secret.trim() ||
    !/^[0-9a-f]{16}$/.test(normalizedProfile) ||
    !/^https:\/\/docs\.google\.com\/spreadsheets\//i.test(url)
  ) {
    return null;
  }
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key(secret), iv);
  const payload = JSON.stringify({
    profile_id: normalizedProfile,
    sheet_url: url,
    expires_at: now + HANDLE_TTL_MS,
  });
  const ciphertext = Buffer.concat([
    cipher.update(payload, "utf8"),
    cipher.final(),
  ]);
  return [
    "v1",
    iv.toString("base64url"),
    ciphertext.toString("base64url"),
    cipher.getAuthTag().toString("base64url"),
  ].join(".");
}

export function readSheetHandle(
  secret: string,
  handle: string,
  now = Date.now(),
): { profile_id: string; sheet_url: string } | null {
  const [version, ivRaw, ciphertextRaw, tagRaw] = handle.trim().split(".");
  if (
    !secret.trim() ||
    version !== "v1" ||
    !ivRaw ||
    !ciphertextRaw ||
    !tagRaw
  ) {
    return null;
  }
  try {
    const iv = Buffer.from(ivRaw, "base64url");
    const ciphertext = Buffer.from(ciphertextRaw, "base64url");
    const tag = Buffer.from(tagRaw, "base64url");
    if (iv.length !== 12 || tag.length !== 16) return null;
    const decipher = createDecipheriv("aes-256-gcm", key(secret), iv);
    decipher.setAuthTag(tag);
    const value = JSON.parse(
      Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString(
        "utf8",
      ),
    ) as Record<string, unknown>;
    const profileId =
      typeof value.profile_id === "string" ? value.profile_id : "";
    const sheetUrl =
      typeof value.sheet_url === "string" ? value.sheet_url : "";
    const expiresAt =
      typeof value.expires_at === "number" ? value.expires_at : 0;
    if (
      !/^[0-9a-f]{16}$/.test(profileId) ||
      !/^https:\/\/docs\.google\.com\/spreadsheets\//i.test(sheetUrl) ||
      expiresAt < now ||
      expiresAt > now + HANDLE_TTL_MS
    ) {
      return null;
    }
    return { profile_id: profileId, sheet_url: sheetUrl };
  } catch {
    return null;
  }
}
