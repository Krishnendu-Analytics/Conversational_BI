import os

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

TEST_FERNET_KEY = os.environ["FERNET_SECRET_KEY"]

from app.config import get_settings

get_settings.cache_clear()

from app.encryption import CredentialEncryption


def test_encrypt_decrypt_round_trip() -> None:
    enc = CredentialEncryption()
    plaintext = "postgresql://user:secret@localhost:5432/sales"
    ciphertext = enc.encrypt(plaintext)

    assert ciphertext != plaintext
    assert enc.decrypt(ciphertext) == plaintext


def test_decrypt_wrong_key_raises() -> None:
    enc = CredentialEncryption()
    ciphertext = enc.encrypt("postgresql://user:pass@host/db")

    os.environ["FERNET_SECRET_KEY"] = Fernet.generate_key().decode()
    get_settings.cache_clear()

    other = CredentialEncryption()
    try:
        other.decrypt(ciphertext)
        assert False, "expected ValueError"
    except ValueError:
        pass
    finally:
        os.environ["FERNET_SECRET_KEY"] = TEST_FERNET_KEY
        get_settings.cache_clear()


def test_missing_key_raises() -> None:
    os.environ["FERNET_SECRET_KEY"] = ""
    get_settings.cache_clear()

    try:
        CredentialEncryption()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "FERNET_SECRET_KEY" in str(exc)
    finally:
        os.environ["FERNET_SECRET_KEY"] = TEST_FERNET_KEY
        get_settings.cache_clear()
