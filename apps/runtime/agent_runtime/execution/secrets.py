"""Decryption for provider credentials sealed into immutable task snapshots."""

import base64
import binascii

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

_AAD = b"agent-platform:execution-credential:v1"


def _key_bytes(key: SecretStr) -> bytes:
    value = key.get_secret_value().strip()
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        decoded = b""
    if len(decoded) != 32:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            decoded = b""
    if len(decoded) != 32:
        raise ValueError("execution encryption key must encode 32 bytes")
    return decoded


def decrypt_execution_credential(envelope: str, key: SecretStr) -> str:
    try:
        sealed = base64.b64decode(envelope, validate=True)
        nonce_size = 12
        if len(sealed) <= nonce_size:
            raise ValueError
        plaintext = AESGCM(_key_bytes(key)).decrypt(sealed[:nonce_size], sealed[nonce_size:], _AAD)
        return plaintext.decode("utf-8")
    except (binascii.Error, InvalidTag, UnicodeDecodeError, ValueError):
        raise ValueError("execution credential envelope is invalid") from None
