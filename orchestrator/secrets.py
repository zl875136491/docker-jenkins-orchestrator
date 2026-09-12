import base64
import json

from cryptography.fernet import Fernet, InvalidToken


class SecretError(ValueError):
    pass


class SecretBox:
    """Encrypts application environment variables before repository persistence."""

    def __init__(self, key: str | None, require_encryption: bool = False) -> None:
        if require_encryption and not key:
            raise SecretError("ORCHESTRATOR_DATA_ENCRYPTION_KEY is required for MongoDB storage")
        self._fernet: Fernet | None = None
        if key:
            try:
                self._fernet = Fernet(key.encode("utf-8"))
            except (ValueError, TypeError) as exc:
                raise SecretError("ORCHESTRATOR_DATA_ENCRYPTION_KEY must be a valid Fernet key") from exc

    def encrypt_environment(self, environment: dict[str, str]) -> str:
        payload = json.dumps(environment, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if self._fernet is not None:
            return "fernet:" + self._fernet.encrypt(payload).decode("utf-8")
        return "plain:" + base64.urlsafe_b64encode(payload).decode("ascii")

    def decrypt_environment(self, ciphertext: str) -> dict[str, str]:
        try:
            scheme, payload = ciphertext.split(":", 1)
        except ValueError as exc:
            raise SecretError("Invalid persisted environment format") from exc
        try:
            if scheme == "fernet":
                if self._fernet is None:
                    raise SecretError("Encrypted environment cannot be read without a key")
                data = self._fernet.decrypt(payload.encode("utf-8"))
            elif scheme == "plain" and self._fernet is None:
                data = base64.urlsafe_b64decode(payload.encode("ascii"))
            else:
                raise SecretError("Unsupported persisted environment encryption scheme")
            decoded = json.loads(data.decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretError("Unable to decrypt persisted environment") from exc
        if not isinstance(decoded, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()):
            raise SecretError("Persisted environment has an invalid shape")
        return decoded
