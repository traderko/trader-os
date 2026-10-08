# services/fcm_service.py
#
# 알림 보내기. 텔레그램(관리 화면에서 봇 설정)은 항상, 안드로이드 앱 푸시(Firebase FCM)는 설정돼 있을 때만.
#
# Firebase는 선택 사항:
#   - .env 의 FIREBASE_CREDENTIALS 에 서비스 계정 키(json) 경로, 또는
#   - secrets\ 폴더에 *firebase*.json 파일이 있으면 사용
#   - 둘 다 없으면 (배포판 기본) 텔레그램으로만 보냄. firebase-admin 패키지도 필요 없음.

import glob
import os
from functools import lru_cache

from services.telegram_service import TelegramService

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_telegram = TelegramService()


def _firebase_key_path() -> str | None:
    env = os.getenv("FIREBASE_CREDENTIALS", "").strip()
    if env:
        return env if os.path.isabs(env) else os.path.join(BASE_DIR, env)
    found = sorted(glob.glob(os.path.join(BASE_DIR, "secrets", "*firebase*.json")))
    return found[0] if found else None


@lru_cache
def init_firebase():
    """Firebase 앱 (없으면 None). 여러 번 불러도 처음 한 번만 초기화."""
    path = _firebase_key_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials
        return firebase_admin.initialize_app(credentials.Certificate(path))
    except Exception as e:
        print(f"[fcm] Firebase 초기화 실패 - 앱 푸시 없이 텔레그램으로만 보냅니다: {e}")
        return None


def get_fcm_service() -> "FCMService":
    init_firebase()
    return FCMService()


class FCMService:
    def send(self, title: str, body: str, patloadJsonStr: str = '{}', buttons: list | None = None) -> str | None:
        # 텔레그램은 백그라운드로 먼저 보냄 - FCM이 실패해도 텔레그램은 도착하게 (buttons: 텔레그램 메시지 아래 버튼)
        _telegram.send(title, body, buttons)

        if init_firebase() is None:
            return None
        try:
            from firebase_admin import messaging
            return messaging.send(messaging.Message(
                data={"title": title, "body": body, "payload": patloadJsonStr},
                topic="all",
            ))
        except Exception as e:
            print(f"[fcm] 앱 푸시 실패 (텔레그램은 보냄): {e}")
            return None
