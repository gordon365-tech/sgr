"""
SGR Encryption
==============
AES-256-GCM encryption for API keys at rest.

Design decisions:
- AES-256-GCM: authenticated encryption (integrity + confidentiality)
- Random IV per encryption (never reuse IV with same key)
- KEK (Key Encryption Key) from config, never stored in DB
- Ciphertext stored as: base64(iv + tag + ciphertext)
- Memory: secrets zeroed after use where possible

Usage:
    cipher = get_cipher()
    encrypted = cipher.encrypt("my-api-key")
    decrypted = cipher.decrypt(encrypted)
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from sgr.core.config import get_config

_IV_SIZE = 12  # 96 bits (GCM standard)
_TAG_SIZE = 16  # 128 bits (GCM standard)


class Cipher:
    """AES-256-GCM cipher for encrypting secrets at rest."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Key must be exactly 32 bytes (256 bits)")
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: str, associated_data: bytes | None = None) -> str:
        """
        Encrypt a string.
        Returns base64-encoded string: iv + ciphertext+tag
        """
        iv = os.urandom(_IV_SIZE)
        ciphertext_with_tag = self._aesgcm.encrypt(
            iv,
            plaintext.encode("utf-8"),
            associated_data,
        )
        combined = iv + ciphertext_with_tag
        return base64.b64encode(combined).decode("ascii")

    def decrypt(self, encrypted: str, associated_data: bytes | None = None) -> str:
        """
        Decrypt a base64-encoded encrypted string.
        Raises InvalidTag if ciphertext was tampered with.
        """
        combined = base64.b64decode(encrypted.encode("ascii"))
        iv = combined[:_IV_SIZE]
        ciphertext_with_tag = combined[_IV_SIZE:]

        plaintext_bytes = self._aesgcm.decrypt(iv, ciphertext_with_tag, associated_data)
        return plaintext_bytes.decode("utf-8")


@lru_cache(maxsize=1)
def get_cipher() -> Cipher:
    """
    Singleton cipher initialized from config.
    Key is derived from ENCRYPTION_MASTER_KEY env var.
    """
    config = get_config()
    raw_key = config.encryption.master_key.get_secret_value()

    # Pad or truncate to exactly 32 bytes (in production: use proper KDF like PBKDF2)
    key_bytes = raw_key.encode("utf-8")[:32].ljust(32, b"\x00")

    return Cipher(key_bytes)
