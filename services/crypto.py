# services/crypto.py
#
# MT5 계좌 비밀번호를 암호화해서 DB에 저장하기 위한 키.
#   - .env 의 FERNET_KEY 가 있으면 그걸 사용 (지금 개발 PC·라이브 서버)
#   - 없으면 data/secret.key 를 쓰고, 그것도 없으면 새로 만들어 저장 (배포판 첫 실행)
#   ※ 키를 잃어버리면 저장된 비밀번호를 복호화할 수 없으니 data/secret.key 를 지우지 말 것

import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.join(BASE_DIR, "data", "secret.key")


def _load_key() -> bytes:
    load_dotenv()
    env_key = os.getenv("FERNET_KEY", "").strip()
    if env_key:
        return env_key.encode()
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)
    print(f"[crypto] 암호화 키를 새로 만들었습니다: {KEY_PATH}")
    return key


class CryptoService:
    def __init__(self):
        self.cipher = Fernet(_load_key())

    def encrypt(self, text: str) -> str:
        return self.cipher.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self.cipher.decrypt(token.encode()).decode()
