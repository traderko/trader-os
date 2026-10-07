# services/telegram_service.py
#
# 텔레그램 봇으로 알림 보내기. FCMService.send()가 푸시와 함께 이걸 호출한다.
#
# 설정: 관리 화면(/admin)의 "텔레그램" 탭 → data/settings.json
#   비어 있으면 .env의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID를 대신 씀.
#   봇 토큰은 @BotFather에서 /newbot으로 만들고, chat id는 봇에게 말을 건 뒤 getUpdates로 확인.
# 둘 중 하나라도 비어 있으면 아무것도 안 보냄 (FCM만 동작).
# 매번 설정을 다시 읽으므로 화면에서 바꾸면 재시작 없이 바로 적용된다.
#
# 기존 main.py의 Telethon(TELEGRAM_APP_API_ID/HASH)은 "내 계정으로 채널을 읽는" 용도라 별개.

import os
import threading

import requests

from services.settings_service import telegram_cfg

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramService:
    @property
    def token(self) -> str:
        return telegram_cfg()["bot_token"]

    @property
    def chat_id(self) -> str:
        return telegram_cfg()["chat_id"]

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, title: str, body: str) -> None:
        """비동기(백그라운드 스레드)로 전송. 텔레그램이 느리거나 실패해도 호출한 쪽을 막지 않는다."""
        if not self.enabled:
            return
        threading.Thread(target=self._send_sync, args=(title, body), daemon=True).start()

    def _send_sync(self, title: str, body: str) -> None:
        ok, msg = self.send_now(title, body)
        if not ok:
            print(f"[telegram] {msg}")

    def send_now(self, title: str, body: str) -> tuple[bool, str]:
        """바로 보내고 (성공 여부, 설명) 반환. 관리 화면의 [테스트 전송] 버튼이 쓴다."""
        cfg = telegram_cfg()
        if not (cfg["bot_token"] and cfg["chat_id"]):
            return False, "봇 토큰과 chat id를 모두 입력해야 합니다."

        text = f"{title}\n{body}" if title else body
        try:
            res = requests.post(
                TELEGRAM_API.format(token=cfg["bot_token"]),
                json={"chat_id": cfg["chat_id"], "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
        except Exception as e:
            # 오류 메시지에 URL(=봇 토큰)이 그대로 찍히므로 가림
            return False, f"전송 오류: {str(e).replace(cfg['bot_token'], '***')}"

        if res.status_code == 200:
            try:   # 실시간 현황 '맨 아래 유지' 방식이 이 알림 아래로 현황을 다시 내리도록
                from services.telegram_live import note_message
                note_message(cfg["chat_id"], res.json()["result"]["message_id"])
            except Exception:
                pass
            return True, "전송 성공"
        try:
            desc = res.json().get("description", res.text)
        except ValueError:
            desc = res.text
        hint = ""
        if res.status_code == 401:
            hint = " (봇 토큰이 잘못됐습니다)"
        elif res.status_code in (400, 403):
            hint = " (chat id가 틀렸거나, 봇에게 먼저 메시지를 보내지 않았습니다)"
        return False, f"전송 실패 {res.status_code}: {desc[:200]}{hint}"
