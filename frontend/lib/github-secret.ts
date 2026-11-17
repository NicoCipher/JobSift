import sodium from "libsodium-wrappers";

export class InvalidGithubActionsPublicKeyError extends Error {
  constructor() {
    super("Invalid GitHub Actions secrets public key.");
    this.name = "InvalidGithubActionsPublicKeyError";
  }
}

export async function encryptGithubActionSecret(
  secret: string,
  publicKeyBase64: string,
): Promise<string> {
  await sodium.ready;
  let publicKey: Uint8Array;
  try {
    publicKey = sodium.from_base64(
      publicKeyBase64,
      sodium.base64_variants.ORIGINAL,
    );
  } catch {
    throw new InvalidGithubActionsPublicKeyError();
  }
  if (publicKey.length !== sodium.crypto_box_PUBLICKEYBYTES) {
    throw new InvalidGithubActionsPublicKeyError();
  }
  const encrypted = sodium.crypto_box_seal(
    sodium.from_string(secret),
    publicKey,
  );
  return sodium.to_base64(encrypted, sodium.base64_variants.ORIGINAL);
}
