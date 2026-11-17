import sodium from "libsodium-wrappers";

export async function encryptGithubActionSecret(
  secret: string,
  publicKeyBase64: string,
): Promise<string> {
  await sodium.ready;
  const publicKey = sodium.from_base64(
    publicKeyBase64,
    sodium.base64_variants.ORIGINAL,
  );
  if (publicKey.length !== sodium.crypto_box_PUBLICKEYBYTES) {
    throw new Error("Invalid GitHub Actions secrets public key.");
  }
  const encrypted = sodium.crypto_box_seal(
    sodium.from_string(secret),
    publicKey,
  );
  return sodium.to_base64(encrypted, sodium.base64_variants.ORIGINAL);
}
