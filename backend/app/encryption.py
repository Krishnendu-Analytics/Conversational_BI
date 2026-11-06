"""Encrypt and decrypt customer database credentials at rest (Fernet)."""

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CredentialEncryption:
  """Symmetric encrypt/decrypt for connection URLs and passwords."""

  def __init__(self) -> None:
    key = get_settings().fernet_secret_key
    if not key:
      raise RuntimeError("FERNET_SECRET_KEY is not configured")
    try:
      self._fernet = Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
      raise RuntimeError("FERNET_SECRET_KEY is invalid") from exc

  def encrypt(self, plaintext: str) -> str:
    """Return url-safe base64 ciphertext for storage in connections.encrypted_url."""
    return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

  def decrypt(self, ciphertext: str) -> str:
    """Decrypt stored ciphertext; raises ValueError on wrong key or tampered data."""
    try:
      return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
      raise ValueError("Failed to decrypt credential") from exc