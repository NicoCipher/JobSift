import {
  constants,
  createCipheriv,
  createPublicKey,
  publicEncrypt,
  randomBytes,
  X509Certificate,
} from "node:crypto";

const GOOGLE_CERTS_BASE = "https://www.googleapis.com/robot/v1/metadata/x509/";
const AAD = Buffer.from("jobsift-client-registration-v1", "utf8");
const CERT_TTL_MS = 30 * 60 * 1000;

type CertificateMap = Record<string, string>;

type CacheEntry = {
  expiresAt: number;
  certificates: CertificateMap;
};

let cachedCertificates: CacheEntry | null = null;

function b64url(value: Buffer) {
  return value.toString("base64url");
}

function publicKey(pem: string) {
  try {
    return new X509Certificate(pem).publicKey;
  } catch {
    return createPublicKey(pem);
  }
}

export function encryptClientRegistration(
  payload: Record<string, unknown>,
  certificates: CertificateMap,
) {
  const entries = Object.entries(certificates).filter(
    ([keyId, pem]) => Boolean(keyId.trim()) && pem.includes("BEGIN"),
  );
  if (!entries.length) {
    throw new Error("No Google service-account encryption certificates are available.");
  }

  const plaintext = Buffer.from(JSON.stringify(payload), "utf8");
  if (plaintext.length > 24_000) {
    throw new Error("Client registration payload is too large.");
  }

  const key = randomBytes(32);
  const nonce = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, nonce);
  cipher.setAAD(AAD);
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();

  const keys = Object.fromEntries(
    entries.map(([keyId, pem]) => [
      keyId,
      b64url(
        publicEncrypt(
          {
            key: publicKey(pem),
            padding: constants.RSA_PKCS1_OAEP_PADDING,
            oaepHash: "sha256",
          },
          key,
        ),
      ),
    ]),
  );

  return JSON.stringify({
    version: "jobsift-onboarding-v1",
    algorithm: "RSA-OAEP-SHA256+A256GCM",
    nonce: b64url(nonce),
    ciphertext: b64url(ciphertext),
    tag: b64url(tag),
    keys,
  });
}

export async function googleServiceAccountCertificates(email: string) {
  const value = email.trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.iam\.gserviceaccount\.com$/.test(value)) {
    throw new Error("Google service-account email is invalid.");
  }

  const now = Date.now();
  if (cachedCertificates && cachedCertificates.expiresAt > now) {
    return cachedCertificates.certificates;
  }

  const response = await fetch(GOOGLE_CERTS_BASE + encodeURIComponent(value), {
    cache: "no-store",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) {
    throw new Error("Could not load Google service-account encryption certificates.");
  }
  const body = (await response.json()) as unknown;
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("Google service-account certificate response is invalid.");
  }

  const certificates = Object.fromEntries(
    Object.entries(body as Record<string, unknown>).flatMap(([keyId, pem]) =>
      typeof pem === "string" && pem.includes("BEGIN CERTIFICATE")
        ? [[keyId, pem]]
        : [],
    ),
  );
  if (!Object.keys(certificates).length) {
    throw new Error("Google service-account encryption certificates are unavailable.");
  }

  cachedCertificates = {
    expiresAt: now + CERT_TTL_MS,
    certificates,
  };
  return certificates;
}
