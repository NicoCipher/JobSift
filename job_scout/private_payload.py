"""Decrypt private client-setup payloads carried through public workflow inputs."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any


class PrivatePayloadError(ValueError):
    """Encrypted setup payload could not be validated or decrypted."""


_AAD = b"jobsift-client-registration-v1"


def _decode(value: object, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise PrivatePayloadError(f"encrypted payload field {field!r} is invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise PrivatePayloadError(
            f"encrypted payload field {field!r} is not valid base64url"
        ) from error


def decrypt_registration_envelope(
    envelope: str | bytes,
    service_account: str | bytes,
) -> dict[str, Any]:
    """Decrypt one hybrid RSA-OAEP/AES-GCM registration envelope."""

    try:
        wrapped = json.loads(envelope)
        credentials = json.loads(service_account)
    except (json.JSONDecodeError, TypeError) as error:
        raise PrivatePayloadError("encrypted registration context is not valid JSON") from error

    if not isinstance(wrapped, dict) or wrapped.get("version") != "jobsift-onboarding-v1":
        raise PrivatePayloadError("unsupported encrypted registration version")
    if wrapped.get("algorithm") != "RSA-OAEP-SHA256+A256GCM":
        raise PrivatePayloadError("unsupported encrypted registration algorithm")

    key_id = credentials.get("private_key_id")
    private_key_pem = credentials.get("private_key")
    if not isinstance(key_id, str) or not key_id:
        raise PrivatePayloadError("Google service account private key ID is unavailable")
    if not isinstance(private_key_pem, str) or not private_key_pem:
        raise PrivatePayloadError("Google service account private key is unavailable")

    keys = wrapped.get("keys")
    if not isinstance(keys, dict) or key_id not in keys:
        raise PrivatePayloadError(
            "encrypted registration does not target the active Google service-account key"
        )

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:
        raise RuntimeError(
            "install job-scout[sheets] for encrypted client onboarding"
        ) from error

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )
    aes_key = private_key.decrypt(
        _decode(keys[key_id], "keys"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    if len(aes_key) != 32:
        raise PrivatePayloadError("decrypted onboarding key has an invalid size")

    nonce = _decode(wrapped.get("nonce"), "nonce")
    ciphertext = _decode(wrapped.get("ciphertext"), "ciphertext")
    tag = _decode(wrapped.get("tag"), "tag")
    if len(nonce) != 12 or len(tag) != 16:
        raise PrivatePayloadError("encrypted registration nonce/tag is invalid")

    try:
        plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext + tag, _AAD)
        payload = json.loads(plaintext)
    except Exception as error:  # cryptography intentionally hides authentication details.
        raise PrivatePayloadError("encrypted registration authentication failed") from error
    if not isinstance(payload, dict):
        raise PrivatePayloadError("decrypted registration must be a JSON object")
    return payload


def decrypt_registration_file(
    *,
    envelope_path: Path,
    service_account_path: Path,
    output_path: Path,
) -> None:
    payload = decrypt_registration_envelope(
        envelope_path.read_bytes(),
        service_account_path.read_bytes(),
    )
    output_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    output_path.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["decrypt-registration"])
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--service-account", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    decrypt_registration_file(
        envelope_path=args.envelope,
        service_account_path=args.service_account,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
