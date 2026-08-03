import os
import uuid
from pathlib import Path


class SecretBox:
    """Encrypt small application secrets with a key held in the Docker data volume."""

    def __init__(self, key_path: str | Path):
        self.key_path = Path(key_path)

    def _fernet(self):
        from cryptography.fernet import Fernet

        if self.key_path.is_file():
            key = self.key_path.read_bytes().strip()
        else:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            temporary = self.key_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(key)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            temporary.replace(self.key_path)
        return Fernet(key)

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except Exception as exc:
            raise RuntimeError("无法解密已保存的 SMTP 密码；通知密钥可能已丢失") from exc
