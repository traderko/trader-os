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

    def send(self, title: str, body: str, buttons: list | None = None, kind: str | None = None) -> None:
        """비동기(백그라운드 스레드)로 전송. 텔레그램이 느리거나 실패해도 호출한 쪽을 막지 않는다.
        buttons: 메시지 아래 버튼 [[{"text": .., "callback_data": ..}, ...], ...] (누르면 services/telegram_live.py 가 처리)
        kind: 어떤 알림인지 - 중요도(🔴 긴급 · 🟠 중요 · ⚪ 보통 · 🔕 조용)에 따라 꾸밈 (services/alert_level.py)"""
        if not self.enabled:
            return
        threading.Thread(target=self._send_sync, args=(title, body, buttons, kind), daemon=True).start()

    def _send_sync(self, title: str, body: str, buttons: list | None = None, kind: str | None = None) -> None:
        level, quiet, chats = "normal", False, [None]
        try:
            from services import alert_level
            level, title, body = alert_level.decorate_plain(kind, title, body)
            quiet = level == "quiet"
            if level == "urgent":
                text = f"{title}\n{body}" if title else body
                aid = alert_level.register(text, None, buttons)
                buttons = [[alert_level.ack_button(aid)]] + list(buttons or [])
                chats = alert_level.chats() or [None]
        except Exception as e:
            print(f"[telegram] 중요도 적용 실패 (그냥 보냄): {e}")
        for cid in chats:
            ok, msg = self.send_now(title, body, buttons, chat_id=cid, quiet=quiet)
            if not ok:
                print(f"[telegram] {msg}")

    def send_now(self, title: str, body: str, buttons: list | None = None, chat_id: str | None = None,
                 quiet: bool = False) -> tuple[bool, str]:
        """바로 보내고 (성공 여부, 설명) 반환. 관리 화면의 [테스트 전송] 버튼이 쓴다.
        chat_id: 비우면 설정의 대화방 · quiet: 알림음 없이"""
        cfg = telegram_cfg()
        if not (cfg["bot_token"] and cfg["chat_id"]):
            return False, "봇 토큰과 chat id를 모두 입력해야 합니다."
        cid = chat_id or cfg["chat_id"]

        text = f"{title}\n{body}" if title else body
        try:
            res = requests.post(
                TELEGRAM_API.format(token=cfg["bot_token"]),
                json={"chat_id": cid, "text": text, "disable_web_page_preview": True,
                      **({"disable_notification": True} if quiet else {}),
                      **({"reply_markup": {"inline_keyboard": buttons}} if buttons else {})},
                timeout=10,
            )
        except Exception as e:
            # 오류 메시지에 URL(=봇 토큰)이 그대로 찍히므로 가림
            return False, f"전송 오류: {str(e).replace(cfg['bot_token'], '***')}"

        if res.status_code == 200:
            try:   # 실시간 현황 '맨 아래 유지' 방식이 이 알림 아래로 현황을 다시 내리도록
                from services.telegram_live import note_message
                if str(cid) == str(cfg["chat_id"]):
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
