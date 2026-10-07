import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
} from "node:crypto";

function keyBytes(raw: string): Buffer | null {
  try {
    const value = Buffer.from(raw.trim(), "base64url");
    return value.length === 32 ? value : null;
  } catch {
    return null;
  }
}

export function validProvisioningKey(raw: string | null | undefined): boolean {
  return Boolean(raw && keyBytes(raw));
}

export function encryptProvisioningPayload(
  rawKey: string,
  plaintext: string,
): string | null {
  const key = keyBytes(rawKey);
  if (!key) return null;
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const ciphertext = Buffer.concat([
    cipher.update(plaintext, "utf8"),
    cipher.final(),
  ]);
  const tag = cipher.getAuthTag();
  return [
    "v1",
    iv.toString("base64url"),
    ciphertext.toString("base64url"),
    tag.toString("base64url"),
  ].join(".");
}

export function decryptProvisioningPayload(
  rawKey: string,
  envelope: string,
): string | null {
  const key = keyBytes(rawKey);
  const [version, ivRaw, ciphertextRaw, tagRaw] = envelope.split(".");
  if (!key || version !== "v1" || !ivRaw || !ciphertextRaw || !tagRaw) {
    return null;
  }
  try {
    const iv = Buffer.from(ivRaw, "base64url");
    const ciphertext = Buffer.from(ciphertextRaw, "base64url");
    const tag = Buffer.from(tagRaw, "base64url");
    if (iv.length !== 12 || tag.length !== 16) return null;
    const decipher = createDecipheriv("aes-256-gcm", key, iv);
    decipher.setAuthTag(tag);
    return Buffer.concat([
      decipher.update(ciphertext),
      decipher.final(),
    ]).toString("utf8");
  } catch {
    return null;
  }
}

export const decryptProvisioningPayloadForTest = decryptProvisioningPayload;
