from __future__ import annotations

import base64
import json
import os

import pytest

from job_scout.private_payload import PrivatePayloadError, decrypt_registration_envelope

AAD = b"jobsift-client-registration-v1"


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def envelope(payload: dict[str, object], *, key_id: str, public_key) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(
        nonce,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        AAD,
    )
    wrapped_key = public_key.encrypt(
        key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return json.dumps(
        {
            "version": "jobsift-onboarding-v1",
            "algorithm": "RSA-OAEP-SHA256+A256GCM",
            "nonce": b64(nonce),
            "ciphertext": b64(encrypted[:-16]),
            "tag": b64(encrypted[-16:]),
            "keys": {key_id: b64(wrapped_key)},
        }
    )


def credentials(key_id: str, private_key) -> str:
    from cryptography.hazmat.primitives import serialization

    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps({"private_key_id": key_id, "private_key": pem})


def test_decrypt_registration_envelope_round_trips_private_sheet_context():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = {
        "client_id": "opaque-client",
        "destination_id": "sheet-abc",
        "display_name": "Client Jobs",
        "spreadsheet": "https://docs.google.com/spreadsheets/d/example",
        "tab": "Jobs",
        "column_mapping": {
            "Job Link": "URL",
            "Job Title": "Role",
            "Company Name": "Company",
        },
    }

    encrypted = envelope(payload, key_id="kid-1", public_key=private_key.public_key())
    assert decrypt_registration_envelope(
        encrypted,
        credentials("kid-1", private_key),
    ) == payload


def test_decrypt_registration_envelope_fails_closed_for_wrong_service_account_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encrypted = envelope(
        {"client_id": "opaque-client"},
        key_id="kid-1",
        public_key=private_key.public_key(),
    )

    with pytest.raises(PrivatePayloadError, match="does not target"):
        decrypt_registration_envelope(
            encrypted,
            credentials("kid-2", other_key),
        )
