"""
Tests for API key encryption.
Round-trip, tamper detection, uniqueness of ciphertext.
"""

from __future__ import annotations

import base64

import pytest

from sgr.core.encryption import Cipher


class TestCipher:
    def _make_cipher(self) -> Cipher:
        return Cipher(b"A" * 32)

    def test_roundtrip(self) -> None:
        cipher = self._make_cipher()
        plaintext = "my-secret-api-key-12345"
        encrypted = cipher.encrypt(plaintext)
        decrypted = cipher.decrypt(encrypted)
        assert decrypted == plaintext

    def test_encrypted_differs_from_plaintext(self) -> None:
        cipher = self._make_cipher()
        plaintext = "secret"
        encrypted = cipher.encrypt(plaintext)
        assert encrypted != plaintext

    def test_different_iv_each_time(self) -> None:
        """Same plaintext → different ciphertext each time (random IV)."""
        cipher = self._make_cipher()
        e1 = cipher.encrypt("same-plaintext")
        e2 = cipher.encrypt("same-plaintext")
        assert e1 != e2

    def test_tamper_detection(self) -> None:
        """Flipping a bit in ciphertext must raise (GCM tag check)."""
        from cryptography.exceptions import InvalidTag

        cipher = self._make_cipher()
        encrypted = cipher.encrypt("sensitive-data")
        raw = bytearray(base64.b64decode(encrypted))
        raw[-1] ^= 0xFF  # flip last byte
        tampered = base64.b64encode(bytes(raw)).decode("ascii")
        with pytest.raises(InvalidTag):
            cipher.decrypt(tampered)

    def test_short_key_raises(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            Cipher(b"tooshort")

    def test_associated_data(self) -> None:
        """AAD must match on decrypt or raise."""
        from cryptography.exceptions import InvalidTag

        cipher = self._make_cipher()
        aad = b"user-id-123"
        encrypted = cipher.encrypt("api-key", associated_data=aad)
        # Correct AAD → works
        assert cipher.decrypt(encrypted, associated_data=aad) == "api-key"
        # Wrong AAD → raises
        with pytest.raises(InvalidTag):
            cipher.decrypt(encrypted, associated_data=b"wrong-user")
