"""봉인 정보 대칭 암호화. 목적은 실수로 열람·grep 되는 것을 막는 것이지 강한 보안이 아니다."""
import os
from pathlib import Path

from cryptography.fernet import Fernet

KEY_FILE = Path("~/.mockdrill/key").expanduser()


def _key() -> bytes:
    if env := os.environ.get("MOCK_SEAL_KEY"):
        return env.encode()
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_bytes(Fernet.generate_key())
        KEY_FILE.chmod(0o600)
    return KEY_FILE.read_bytes().strip()


def seal(plaintext: str, key: bytes | None = None) -> bytes:
    return Fernet(key or _key()).encrypt(plaintext.encode("utf-8"))


def unseal(token: bytes, key: bytes | None = None) -> str:
    return Fernet(key or _key()).decrypt(token).decode("utf-8")
