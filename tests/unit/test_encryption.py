"""Unit tests for token encryption and decryption."""

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from meta_bot.errors import EncryptionError
from meta_bot.infra.db.encryption import TokenEncryptor


def test_encryption_roundtrip(valid_fernet_key: str) -> None:
    """Test encrypting and decrypting returns original plaintext token."""
    encryptor = TokenEncryptor(SecretStr(valid_fernet_key))
    original = "EAAB1234567890abcdef_test_access_token_long_string"

    ciphertext = encryptor.encrypt(original)
    assert ciphertext != original
    assert len(ciphertext) > 0

    decrypted = encryptor.decrypt(ciphertext)
    assert decrypted == original


def test_encryption_empty_token(valid_fernet_key: str) -> None:
    """Test that encrypting empty token raises EncryptionError."""
    encryptor = TokenEncryptor(SecretStr(valid_fernet_key))
    with pytest.raises(EncryptionError, match="Cannot encrypt empty"):
        encryptor.encrypt("")


def test_decryption_empty_ciphertext(valid_fernet_key: str) -> None:
    """Test that decrypting empty ciphertext raises EncryptionError."""
    encryptor = TokenEncryptor(SecretStr(valid_fernet_key))
    with pytest.raises(EncryptionError, match="Cannot decrypt empty"):
        encryptor.decrypt("")


def test_decryption_corrupt_ciphertext(valid_fernet_key: str) -> None:
    """Test that decrypting corrupt ciphertext raises EncryptionError."""
    encryptor = TokenEncryptor(SecretStr(valid_fernet_key))
    with pytest.raises(EncryptionError, match="Invalid ciphertext"):
        encryptor.decrypt("not_a_valid_fernet_ciphertext_token")


def test_decryption_with_wrong_key(valid_fernet_key: str) -> None:
    """Test that ciphertext cannot be decrypted with a different key."""
    encryptor1 = TokenEncryptor(SecretStr(valid_fernet_key))
    other_key = Fernet.generate_key().decode()
    encryptor2 = TokenEncryptor(SecretStr(other_key))

    ciphertext = encryptor1.encrypt("my_secret_token")
    with pytest.raises(
        EncryptionError, match="Invalid ciphertext or encryption key mismatch"
    ):
        encryptor2.decrypt(ciphertext)
