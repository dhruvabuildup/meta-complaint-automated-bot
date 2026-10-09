"""Encryption helpers for protecting access tokens at rest using Fernet symmetric cryptography."""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from meta_bot.errors import EncryptionError


class TokenEncryptor:
    """Handles symmetric encryption and decryption of tokens using Fernet."""

    def __init__(self, key: SecretStr) -> None:
        try:
            self._fernet = Fernet(key.get_secret_value().encode("utf-8"))
        except Exception as exc:
            raise EncryptionError(f"Invalid Fernet encryption key: {exc}") from exc

    def encrypt(self, plain_token: str) -> str:
        """Encrypt plaintext token string into URL-safe base64 ciphertext string."""
        if not plain_token:
            raise EncryptionError("Cannot encrypt empty token")
        try:
            encrypted_bytes = self._fernet.encrypt(plain_token.encode("utf-8"))
            return encrypted_bytes.decode("utf-8")
        except Exception as exc:
            raise EncryptionError(f"Token encryption failed: {exc}") from exc

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt URL-safe base64 ciphertext string back to plaintext token string."""
        if not ciphertext:
            raise EncryptionError("Cannot decrypt empty ciphertext")
        try:
            decrypted_bytes = self._fernet.decrypt(ciphertext.encode("utf-8"))
            return decrypted_bytes.decode("utf-8")
        except InvalidToken as exc:
            raise EncryptionError(
                "Invalid ciphertext or encryption key mismatch"
            ) from exc
        except Exception as exc:
            raise EncryptionError(f"Token decryption failed: {exc}") from exc
