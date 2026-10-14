"""可选的 Novel Agent 备份加密封装。"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENCRYPTED_BACKUP_MAGIC = b"NOVEL_AGENT_ENCRYPTED_BACKUP\x01\n"
_AAD = b"novel-agent-backup"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_BYTES = 32


def is_encrypted_backup(data: bytes) -> bool:
    return bytes(data).startswith(ENCRYPTED_BACKUP_MAGIC)


def _derive_key(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    if not password:
        raise ValueError("备份密码不能为空")
    if len(salt) != _SALT_BYTES or (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
        raise ValueError("备份 KDF 参数不受支持")
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_KEY_BYTES,
    )


def encrypt_backup(data: bytes, password: str) -> bytes:
    """将 ZIP 备份包封装为带认证的加密载荷。"""
    if not password:
        return data
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    key = _derive_key(password, salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    ciphertext = AESGCM(key).encrypt(nonce, bytes(data), _AAD)
    header = {
        "schema_version": "novel-agent-encrypted-backup-v1",
        "kdf": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
    }
    return (
        ENCRYPTED_BACKUP_MAGIC
        + json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
        + ciphertext
    )


def decrypt_backup(data: bytes, password: str) -> bytes:
    """解密并认证备份；密码错误或内容被篡改时统一报错。"""
    if not is_encrypted_backup(data):
        return data
    if not password:
        raise ValueError("该备份受密码保护，请提供密码")
    payload = bytes(data)[len(ENCRYPTED_BACKUP_MAGIC) :]
    try:
        raw_header, ciphertext = payload.split(b"\n\n", 1)
        header = json.loads(raw_header)
        if header.get("schema_version") != "novel-agent-encrypted-backup-v1":
            raise ValueError("不支持的加密备份版本")
        salt = base64.urlsafe_b64decode(str(header["salt"]).encode("ascii"))
        nonce = base64.urlsafe_b64decode(str(header["nonce"]).encode("ascii"))
        if len(nonce) != _NONCE_BYTES:
            raise ValueError("备份 nonce 无效")
        key = _derive_key(
            password,
            salt,
            n=int(header.get("n", 0)),
            r=int(header.get("r", 0)),
            p=int(header.get("p", 0)),
        )
        return AESGCM(key).decrypt(nonce, ciphertext, _AAD)
    except (InvalidTag, KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("不支持的加密备份版本"):
            raise
        raise ValueError("备份密码错误或文件已损坏") from exc
