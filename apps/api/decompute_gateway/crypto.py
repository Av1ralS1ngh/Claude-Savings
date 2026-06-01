from __future__ import annotations

from dataclasses import dataclass
from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: bytes
    key_id: str


class CCRCrypto:
    """Small envelope-encryption wrapper for CCR payloads.

    V0 uses a single configured Fernet key as the envelope key. The key_id is stored with
    each row so production can later rotate keys or move to KMS-backed data keys without
    changing the DB contract.
    """

    def __init__(self, key: str | None, key_id: str = "local-v1") -> None:
        self.key = key
        self.key_id = key_id
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> EncryptedPayload:
        if not self._fernet:
            raise RuntimeError("CCR encryption key is not configured")
        return EncryptedPayload(ciphertext=self._fernet.encrypt(plaintext.encode("utf-8")), key_id=self.key_id)

    def decrypt(self, ciphertext: bytes, key_id: str | None = None) -> str:
        if not self._fernet:
            raise RuntimeError("CCR decryption requested but encryption key is not configured")
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("CCR ciphertext could not be decrypted with configured key") from exc
