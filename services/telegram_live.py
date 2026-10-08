# services/telegram_live.py
#
# 텔레그램 "실시간 현황" - 봇 대화방 맨 위에 고정한 메시지 하나를 몇 초마다 고쳐 쓴다.
# (안드로이드 앱의 고정 알림 대신. 고쳐 쓸 때는 알림이 울리지 않음)
#
#   📌 TraderOS 실시간 · 20:31
#   12345678  🔓 거래 가능 · 52분 뒤 재확인
#   평가금 10,385.60 · 손익 +385.00 · 증거금 12.9%
#   🧈 XAUUSD+  BUY 0.40 @4,466.27  +383.80
#      SL시 -200.20 (3/3) · TP시 +245.00 (1/3)
#
# 같이 하는 일 (봇에게 온 메시지를 2초마다 확인 - getUpdates, 서버가 밖으로만 요청하므로 공유기·Tailscale 설정 불필요):
#   - [🔄 새로고침] 버튼 → 바로 갱신
#   - /start, /id → 이 대화의 chat id를 답장 (관리 화면에 넣을 값)
#   - [🔓 잠금 풀기] 버튼 · /unlock → 웹·EA와 같은 규칙으로 해제: 사유에 맞는 문구를 보여주고,
#     그 문구를 띄어쓰기까지 똑같이 직접 쳐서 답장하면 풀림 (5분 안에, 너무 빨리 오면 붙여넣기로 보고 거절)
#     관리 화면에 등록한 chat id 대화에서만 받음 - 봇을 찾은 다른 사람은 못 풂. 수동 잠금은 여기서도 못 풂
#   - 진입 알림의 [🛑 SL] [🎯 TP] [⚖️ 본전 SL] 버튼 · /pos → 포지션 SL/TP 설정 (가격을 답장으로 입력, 0 이면 해제)
#   - 관리 화면 [chat id 찾기] → 최근 봇에게 말을 건 대화 목록(recent_chats)
#
# 설정: 관리 화면 텔레그램 탭 (data/settings.json 의 telegram.live_enabled / live_interval_sec)
# 상태: data/telegram_live.json (고정 메시지 번호, getUpdates offset) - 서버를 재시작해도 같은 메시지를 이어서 고침

import asyncio
import html
import json
import os
import random
import time
from datetime import datetime

import httpx
from sqlalchemy import select

from db.models.account import Account
from db.models.trade_lock_phrase import TradeLockPhrase
from db.session import AsyncSessionLocal
from services import trade_lock_service as lock_svc
from services.settings_service import telegram_cfg
from services.worker_client import worker_get
from services import symbols

STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "telegram_live.json")
API = "https://api.telegram.org/bot{token}/{method}"
POLL_SEC = 2
FORCE_EDIT_SEC = 60          # 내용이 같아도 이 간격으로는 시각을 갱신
UNLOCK_TTL = 300             # 잠금 풀기 문구를 보여준 뒤 답장을 기다리는 시간(초)
TYPE_SEC_PER_CHAR = 0.15     # 이보다 빨리 온 답장은 붙여넣기로 보고 거절 (한글 직접 입력은 보통 글자당 0.2초 이상)

_REASON = {"restricted_window": "정기 재확인", "consec_loss": "연속 손절", "manual": "수동 잠금"}

recent_chats: list[dict] = []      # 관리 화면 [chat id 찾기] 용
# 대화방별로 마지막에 올라온 메시지 번호 - "맨 아래 유지" 방식에서 현황이 위로 밀렸는지 판단
latest_message: dict[str, int] = {}


def note_message(chat_id, message_id) -> None:
    """이 대화방에 새 메시지가 올라왔다고 알림 (알림 전송·사용자 메시지 등에서 호출)"""
    try:
        k, m = str(chat_id), int(message_id)
    except (TypeError, ValueError):
        return
    if m > latest_message.get(k, 0):
        latest_message[k] = m
# 관리 화면에 보여줄 봇 연결 상태
status: dict = {"has_token": False, "last_poll_ok": None, "error": None, "bot": None}


def _sym_info(symbol: str):
    """(아이콘, 1랏 1포인트 가치 예비값) - 종목 묶음은 services/symbols.py"""
    g = symbols.group_of(symbol)
    return (g.get("icon") or "•", g.get("point")) if g else ("•", None)


def _f(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}"


def _s(v: float) -> str:
    return ("+" if v > 0 else "") + _f(v)


def _hm(sec: int) -> str:
    sec = max(0, int(sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}시간 {m}분" if h else f"{m}분"


def _lock_line(s: dict | None) -> str:
    if not s:
        return "⚠️ 잠금 상태 확인 실패"
    reason = s.get("reason") or ""
    if reason.startswith("session_window"):
        names = (s.get("session") or {}).get("names") or []
        why = ("·".join(names) + " " if names else "") + "집중 구간"
    else:
        why = next((v for k, v in _REASON.items() if reason.startswith(k)), reason)
    d = s.get("daily_loss") or {}
    daily = f" · 오늘 {-d.get('pct', 0):+.1f}%" if d.get("enabled") else ""   # 일일 손실 한도를 켠 경우
    if s.get("locked"):
        if reason == "manual":
            what = "손실 한도 잠금" if s.get("manual_kind") == "loss_limit" else "수동 잠금"
            return f"🔒 {what} · {_hm(s.get('remaining_sec', 0))} 뒤 해제{daily}"
        return f"🔒 잠김 · {html.escape(why)}{daily}"
    return f"🔓 거래 가능 · {_hm(s.get('remaining_sec', 0))} 뒤 재확인{daily}"


def _positions_block(positions: list[dict]) -> list[str]:
    if not positions:
        return ["포지션 없음"]
    by_sym: dict[str, list[dict]] = {}
    for p in positions:
        by_sym.setdefault(p["symbol"], []).append(p)
    out = []
    for sym, ps in sorted(by_sym.items()):
        icon, pv = _sym_info(sym)
        pv = next((p.get("contract_size") for p in ps if p.get("contract_size")), None) or pv   # MT5가 알려준 계약 크기 우선
        profit = sum(p["profit"] + (p.get("swap") or 0) for p in ps)
        sides = []
        for t, label in ((0, "BUY"), (1, "SELL")):
            side = [p for p in ps if p["type"] == t]
            if side:
                vol = sum(p["volume"] for p in side)
                avg = sum(p["price_open"] * p["volume"] for p in side) / vol if vol else 0
                sides.append(f"{label} {_f(vol)} @{_f(avg)}")
        out.append(f"{icon} <b>{html.escape(sym)}</b>  {' · '.join(sides)}  <b>{_s(profit)}</b>")
        if pv is not None:
            proj = []
            for key, label in (("sl", "SL시"), ("tp", "TP시")):
                w = [p for p in ps if p.get(key)]
                if w:
                    total = sum(((p[key] - p["price_open"]) if p["type"] == 0 else (p["price_open"] - p[key])) * p["volume"] * pv for p in w)
                    proj.append(f"{label} {_s(total)} ({len(w)}/{len(ps)})")
            if proj:
                out.append("    " + " · ".join(proj))
    return out


class TelegramLive:
    def __init__(self):
        self.state = self._load()
        self.last_text = None
        self.last_edit = 0.0
        self.force = asyncio.Event()
        self.seen: dict[str, int] = {}   # 봇별로 이미 처리한 마지막 update_id
        self.locked: list[tuple[int, str]] = []   # 문구로 풀 수 있는 잠금 계좌 (id, 계좌번호) - 버튼용
        self.pending: dict[str, dict] = {}        # 대화방별 '문구 입력 기다리는 중'

    # ── 상태 파일 ──
    def _load(self) -> dict:
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            json.dump(self.state, open(tmp, "w", encoding="utf-8"))
            os.replace(tmp, STATE_PATH)
        except OSError:
            pass

    # ── 텔레그램 API ──
    async def _call(self, client: httpx.AsyncClient, token: str, method: str, **params) -> dict:
        try:
            r = await client.post(API.format(token=token, method=method), json=params, timeout=15)
            return r.json()
        except Exception as e:
            return {"ok": False, "description": str(e).replace(token, "***")}

    # ── 현황 만들기 ──
    async def build_text(self) -> str:
        async with AsyncSessionLocal() as db:
            accounts = (await db.execute(
                select(Account).where(Account.lock_enabled.is_(True)).order_by(Account.id)
            )).scalars().all()
            locks = {}
            for a in accounts:
                try:
                    locks[a.id] = await lock_svc.get_lock_state(db, a.id)
                except Exception:
                    locks[a.id] = None

        async def one(a: Account) -> list[str]:
            lines = [f"<b>{html.escape(a.account_number)}</b>  {_lock_line(locks.get(a.id))}"]
            try:
                info, positions = await asyncio.gather(
                    asyncio.wait_for(worker_get(a.id, "/account-info"), 5),
                    asyncio.wait_for(worker_get(a.id, "/positions-all"), 5),
                )
            except Exception:
                lines.append("MT5 연결 안 됨 (워커가 꺼져 있거나 시작 중)")
                return lines
            eq, margin = info.get("equity") or 0, info.get("margin") or 0
            util = (margin / eq * 100) if eq > 0 else 0
            lines.append(f"평가금 {_f(eq)} · 손익 <b>{_s(info.get('profit') or 0)}</b> · 증거금 {util:.1f}%")
            lines += _positions_block(positions)
            return lines

        self.locked = [(a.id, a.account_number) for a in accounts
                       if (locks.get(a.id) or {}).get("locked") and locks[a.id].get("reason") != "manual"]
        blocks = await asyncio.gather(*(one(a) for a in accounts)) if accounts else []
        body = "\n\n".join("\n".join(b) for b in blocks) or "잠금을 켠 계좌가 없습니다."
        return f"📌 <b>TraderOS 실시간</b> · {datetime.now().strftime('%H:%M:%S')}\n\n{body}"

    # ── 고정 메시지 갱신 ──
    async def update(self, client: httpx.AsyncClient, token: str, chat_id: str, force: bool = False,
                     position: str = "top") -> None:
        """position
          top    : 메시지 하나를 맨 위에 고정해 두고 계속 고쳐 씀 (기존 방식)
          bottom : 다른 메시지가 올라와 현황이 위로 밀리면, 예전 현황을 지우고 맨 아래에 새로 보냄.
                   고정은 하지 않음 (고정할 때마다 '메시지를 고정했습니다' 줄이 쌓이므로)"""
        text = await self.build_text()
        core = text.split("\n", 1)[1] if "\n" in text else text      # 시각 줄을 뺀 내용
        mid = self.state.get("message_id") if self.state.get("chat_id") == chat_id else None
        # 방식을 바꿨으면 내용이 그대로여도 고정 상태부터 맞춤
        if mid and position == "top" and not self.state.get("pinned", True):   # 예전 상태 파일엔 pinned가 없음 = 고정돼 있음
            await self._pin(client, token, chat_id, mid)          # 맨 아래 → 위쪽 고정으로 바꾼 경우
        if mid and position == "bottom" and self.state.get("pinned", True):
            await self._call(client, token, "unpinChatMessage", chat_id=chat_id, message_id=mid)
            self.state["pinned"] = False
            self._save()

        pushed_up = position == "bottom" and mid and latest_message.get(chat_id, 0) > mid
        if not force and not pushed_up and core == self.last_text and time.time() - self.last_edit < FORCE_EDIT_SEC:
            return
        markup = self._markup()

        if pushed_up:
            # 밀려난 예전 현황은 지움 (48시간 지난 메시지는 못 지우는데, 그럴 땐 그냥 둠)
            await self._call(client, token, "deleteMessage", chat_id=chat_id, message_id=mid)
            mid = None
        if mid:
            r = await self._call(client, token, "editMessageText", chat_id=chat_id, message_id=mid, text=text,
                                 parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
            desc = (r.get("description") or "").lower()
            if r.get("ok") or "not modified" in desc:
                self.last_text, self.last_edit = core, time.time()
                return
            if "too many requests" in desc:
                return
            print(f"[telegram-live] 메시지 고치기 실패 - 새로 보냅니다: {r.get('description')}")

        r = await self._call(client, token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                             disable_web_page_preview=True, disable_notification=True, reply_markup=markup)
        if not r.get("ok"):
            print(f"[telegram-live] 보내기 실패: {r.get('description')}")
            return
        mid = r["result"]["message_id"]
        latest_message[str(chat_id)] = mid          # 방금 보낸 현황이 맨 아래
        self.state.update(chat_id=chat_id, message_id=mid, pinned=False)
        self._save()
        self.last_text, self.last_edit = core, time.time()
        if position == "top":
            await self._pin(client, token, chat_id, mid)
        # bottom 은 고정하지 않음 - 고정하면 '고정했습니다' 안내 줄이 현황 아래에 생겨 맨 아래가 아니게 됨

    def _markup(self) -> dict:
        rows = [[{"text": f"🔓 {num} 잠금 풀기", "callback_data": f"unlock:{aid}"}] for aid, num in self.locked]
        rows.append([{"text": "🔄 새로고침", "callback_data": "live:refresh"}])
        return {"inline_keyboard": rows}

    async def _send(self, client, token, chat_id, text: str, **extra) -> dict:
        r = await self._call(client, token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                             disable_web_page_preview=True, **extra)
        if r.get("ok"):
            note_message(chat_id, r["result"]["message_id"])
        else:
            print(f"[telegram-live] 보내기 실패: {r.get('description')}")
        return r

    # ── 잠금 풀기 ──
    async def start_unlock(self, client, token, chat_id: str, account_id: int | None) -> None:
        """사유에 맞는 문구를 하나 골라 보여주고 답장을 기다림 (account_id 없으면 잠긴 계좌를 고르게 함)"""
        async with AsyncSessionLocal() as db:
            if account_id is None:
                accs = (await db.execute(select(Account).where(Account.lock_enabled.is_(True)).order_by(Account.id))).scalars().all()
                cands = []
                for a in accs:
                    st = await lock_svc.get_lock_state(db, a.id)
                    if st.get("locked"):
                        cands.append((a, st))
                if not cands:
                    await self._send(client, token, chat_id, "🔓 잠긴 계좌가 없습니다.")
                    return
                if len(cands) > 1:
                    kb = [[{"text": f"🔓 {a.account_number}", "callback_data": f"unlock:{a.id}"}] for a, _ in cands]
                    await self._send(client, token, chat_id, "어느 계좌의 잠금을 풀까요?", reply_markup={"inline_keyboard": kb})
                    return
                acc, st = cands[0]
            else:
                acc = await db.get(Account, account_id)
                if acc is None:
                    await self._send(client, token, chat_id, "계좌를 찾을 수 없습니다.")
                    return
                st = await lock_svc.get_lock_state(db, acc.id)
            num = html.escape(acc.account_number)
            if not st.get("locked"):
                await self._send(client, token, chat_id, f"🔓 {num} 은(는) 지금 잠겨 있지 않습니다.")
                return
            if st.get("reason") == "manual":
                await self._send(client, token, chat_id,
                                 f"🔒 {num} 은(는) 수동 잠금입니다. 문구로 풀 수 없고 {_hm(st.get('remaining_sec', 0))} 뒤에 풀립니다.")
                return
            types = st.get("phrase_types") or [st.get("phrase_type")]
            rows = (await db.execute(select(TradeLockPhrase.phrase).where(
                TradeLockPhrase.is_active == True, TradeLockPhrase.phrase_type.in_(types)))).scalars().all()
        if not rows:
            await self._send(client, token, chat_id, "이 잠금에 쓸 문구가 없습니다. 관리 화면 확인 문구 탭에서 먼저 추가하세요.")
            return
        phrase = random.choice(rows)
        why = _lock_line(st).replace("🔒 잠김 · ", "")
        r = await self._send(
            client, token, chat_id,
            f"🔒 <b>{num}</b> · {why} 잠금\n\n"
            f"아래 문구를 띄어쓰기까지 똑같이 <b>직접 입력</b>해서 보내세요. (5분 안에, 복사·붙여넣기 말고)\n\n"
            f"<blockquote>{html.escape(phrase)}</blockquote>\n그만두려면 /cancel",
            reply_markup={"force_reply": True, "input_field_placeholder": "문구를 직접 입력"},
        )
        if r.get("ok"):
            self.pending[chat_id] = {"account_id": acc.id, "account_number": acc.account_number,
                                     "phrase": phrase, "at": time.time()}

    async def try_unlock(self, client, token, chat_id: str, text: str, sent_at: int) -> None:
        p = self.pending.get(chat_id)
        if time.time() - p["at"] > UNLOCK_TTL:
            self.pending.pop(chat_id, None)
            await self._send(client, token, chat_id, "⏱ 5분이 지났습니다. 다시 [🔓 잠금 풀기] 를 누르거나 /unlock 을 보내세요.")
            return
        need = max(2, int(len(p["phrase"]) * TYPE_SEC_PER_CHAR))
        if sent_at - int(p["at"]) < need:
            await self._send(client, token, chat_id, "너무 빨리 왔습니다. 붙여넣기 말고 직접 입력해서 다시 보내세요.")
            p["at"] = time.time()          # 기다리는 시간을 새로 시작
            return
        try:
            async with AsyncSessionLocal() as db:
                res = await lock_svc.confirm_unlock(db, p["account_id"], p["account_number"], text.strip())
        except lock_svc.PhraseMismatchError:
            await self._send(client, token, chat_id, "❌ 문구가 다릅니다. 띄어쓰기까지 똑같이 다시 입력하세요. (그만두려면 /cancel)")
            return
        except lock_svc.ManualLockActiveError:
            self.pending.pop(chat_id, None)
            await self._send(client, token, chat_id, "수동 잠금은 문구로 풀 수 없습니다. 설정한 시간이 지나야 풀립니다.")
            return
        except Exception as e:
            # 해제는 저장된 뒤 알림 보내기에서 실패했을 수도 있음 - 상태를 다시 봄
            print(f"[telegram-live] 잠금 해제 중 오류: {e}")
            res = None
            async with AsyncSessionLocal() as db:
                if not (await lock_svc.get_lock_state(db, p["account_id"])).get("locked"):
                    res = {"status": "unlocked"}
            if res is None:
                await self._send(client, token, chat_id, "⚠️ 잠금 해제 중 오류가 났습니다. 잠시 뒤 다시 해보세요.")
                return
        self.pending.pop(chat_id, None)
        if res.get("status") == "not_needed":
            await self._send(client, token, chat_id, "🔓 이미 풀려 있습니다.")
        # 해제되면 '거래 잠금 해제됨' 알림이 따로 옴 (웹·EA에서 풀 때와 같음)
        self.force.set()

    # ── 포지션 SL/TP (진입 알림 버튼, /pos) ──
    async def _targets(self, aid: int, ticket: int, scope: str) -> tuple[dict | None, list[dict]]:
        """(기준 포지션, 바꿀 포지션들). scope o=그 포지션만, a=같은 종목·방향 전체"""
        try:
            rows = await asyncio.wait_for(worker_get(aid, "/positions-all"), 5)
        except Exception:
            return None, []
        me = next((p for p in rows if p.get("ticket") == ticket), None)
        if me is None:
            return None, []
        if scope == "a":
            return me, [p for p in rows if p["symbol"] == me["symbol"] and p["type"] == me["type"]]
        return me, [me]

    @staticmethod
    def _fmt_price(v, digits) -> str:
        if not v:
            return "없음"
        d = digits if isinstance(digits, int) else (2 if abs(v) >= 100 else 5)
        return f"{v:,.{d}f}"

    def _describe(self, me: dict, targets: list[dict]) -> str:
        side = "BUY" if me["type"] == 0 else "SELL"
        vol = sum(p["volume"] for p in targets)
        avg = sum(p["price_open"] * p["volume"] for p in targets) / vol if vol else me["price_open"]
        d = me.get("digits")
        what = f"#{me['ticket']}" if len(targets) == 1 else f"{len(targets)}개"
        return f"{html.escape(me['symbol'])} {side} {vol:g}lot @{self._fmt_price(avg, d)} ({what})"

    async def pos_button(self, client, token, chat_id: str, data: str) -> str:
        """pos:<sl|tp|be>:<o|a>:<계좌id>:<티켓> 버튼. 콜백 답(짧은 글) 을 돌려줌"""
        try:
            _, field, scope, aid, ticket = data.split(":")
            aid, ticket = int(aid), int(ticket)
        except ValueError:
            return "잘못된 버튼"
        me, targets = await self._targets(aid, ticket, scope)
        if me is None:
            await self._send(client, token, chat_id, "이 포지션은 이미 청산됐거나 MT5에 연결되지 않았습니다.")
            return "포지션 없음"
        if field == "be":
            vol = sum(p["volume"] for p in targets)
            price = sum(p["price_open"] * p["volume"] for p in targets) / vol
            if isinstance(me.get("digits"), int):
                price = round(price, me["digits"])
            await self._apply_level(client, token, chat_id, aid, "sl", price, targets, me)
            return "본전 SL 설정"
        cur = targets[0].get(field) if len(targets) == 1 else None
        label = "SL(손절)" if field == "sl" else "TP(익절)"
        r = await self._send(
            client, token, chat_id,
            f"{self._describe(me, targets)}\n{label} 가격을 보내세요.\n"
            f"현재가 {self._fmt_price(me.get('price_current'), me.get('digits'))}"
            + (f" · 지금 {label.split('(')[0]} {self._fmt_price(cur, me.get('digits'))}" if len(targets) == 1 else "")
            + "\n0 을 보내면 해제 · 그만두려면 /cancel",
            reply_markup={"force_reply": True, "input_field_placeholder": "예: 3990.5"},
        )
        if r.get("ok"):
            self.pending[chat_id] = {"type": "pos", "field": field, "aid": aid, "ticket": ticket, "scope": scope,
                                     "at": time.time()}
        return "가격을 입력하세요"

    async def try_pos(self, client, token, chat_id: str, text: str) -> None:
        p = self.pending.get(chat_id)
        if time.time() - p["at"] > UNLOCK_TTL:
            self.pending.pop(chat_id, None)
            await self._send(client, token, chat_id, "⏱ 5분이 지났습니다. 버튼을 다시 눌러 주세요.")
            return
        t = text.strip().replace(",", "")
        if t in ("없음", "해제"):
            t = "0"
        try:
            price = float(t)
        except ValueError:
            await self._send(client, token, chat_id, "숫자로 보내 주세요. (예: 3990.5, 해제는 0 · 그만두려면 /cancel)")
            return
        me, targets = await self._targets(p["aid"], p["ticket"], p["scope"])
        self.pending.pop(chat_id, None)
        if me is None:
            await self._send(client, token, chat_id, "이 포지션은 이미 청산됐거나 MT5에 연결되지 않았습니다.")
            return
        await self._apply_level(client, token, chat_id, p["aid"], p["field"], price, targets, me)

    async def _apply_level(self, client, token, chat_id, aid, field, price, targets, me) -> None:
        cur = me.get("price_current") or 0
        buy = me["type"] == 0
        label = "SL" if field == "sl" else "TP"
        # 방향이 맞는지 먼저 확인 (BUY 의 SL 은 현재가 아래, TP 는 위 / SELL 은 반대)
        if price and cur:
            below = price < cur
            ok = (below if field == "sl" else not below) if buy else (not below if field == "sl" else below)
            if not ok:
                where = ("아래" if field == "sl" else "위") if buy else ("위" if field == "sl" else "아래")
                await self._send(client, token, chat_id,
                                 f"❌ {'BUY' if buy else 'SELL'} 포지션의 {label}은(는) 현재가({self._fmt_price(cur, me.get('digits'))})보다 {where}여야 합니다.")
                return
        path = "/positions/set-sl" if field == "sl" else "/positions/set-tp"
        body = {"tickets": [p["ticket"] for p in targets], ("sl_price" if field == "sl" else "tp_price"): price}
        try:
            from services.worker_client import worker_post
            res = await asyncio.wait_for(worker_post(aid, path, body), 15)
        except Exception as e:
            await self._send(client, token, chat_id, f"⚠️ {label} 설정 실패: {html.escape(str(e))[:200]}")
            return
        okn, failed = len(res.get("success") or []), res.get("failed") or []
        what = "해제" if not price else self._fmt_price(price, me.get("digits"))
        msg = f"{'✅' if not failed else '⚠️'} {self._describe(me, targets)}\n{label} {what} · {okn}/{len(targets)}개 적용"
        if failed:
            reasons = {f.get("reason", "") for f in failed}
            msg += "\n실패: " + html.escape(", ".join(sorted(reasons))[:200])
            if any("10016" in r for r in reasons):
                msg += "\n(가격이 현재가에 너무 가깝거나 방향이 맞지 않습니다)"
        await self._send(client, token, chat_id, msg)
        self.force.set()     # 실시간 현황의 SL/TP 예상 손익도 바로 갱신

    async def show_positions(self, client, token, chat_id: str) -> None:
        """/pos - 종목·방향별로 SL/TP 버튼"""
        async with AsyncSessionLocal() as db:
            accs = (await db.execute(select(Account).where(Account.lock_enabled.is_(True)).order_by(Account.id))).scalars().all()
        sent = False
        for a in accs:
            try:
                rows = await asyncio.wait_for(worker_get(a.id, "/positions-all"), 5)
            except Exception:
                continue
            groups: dict[tuple, list] = {}
            for p in rows:
                groups.setdefault((p["symbol"], p["type"]), []).append(p)
            for (sym, typ), ps in sorted(groups.items()):
                me = ps[0]
                key = f"{a.id}:{me['ticket']}"
                sl = {p.get("sl") for p in ps}
                tp = {p.get("tp") for p in ps}
                d = me.get("digits")
                info = (f"<b>{html.escape(a.account_number)}</b>  {self._describe(me, ps)}\n"
                        f"SL {self._fmt_price(sl.pop(), d) if len(sl) == 1 else '제각각'} · "
                        f"TP {self._fmt_price(tp.pop(), d) if len(tp) == 1 else '제각각'}")
                await self._send(client, token, chat_id, info, reply_markup={"inline_keyboard": [[
                    {"text": "🛑 SL", "callback_data": f"pos:sl:a:{key}"},
                    {"text": "🎯 TP", "callback_data": f"pos:tp:a:{key}"},
                    {"text": "⚖️ 평단 SL", "callback_data": f"pos:be:a:{key}"}]]})
                sent = True
        if not sent:
            await self._send(client, token, chat_id, "열린 포지션이 없습니다.")

    async def _pin(self, client, token, chat_id, mid) -> None:
        p = await self._call(client, token, "pinChatMessage", chat_id=chat_id, message_id=mid, disable_notification=True)
        if p.get("ok"):
            self.state["pinned"] = True
            self._save()
            # 고정 안내 줄('봇이 메시지를 고정했습니다')은 현황보다 나중 메시지지만, 위쪽 고정 방식에선 상관없음
        else:
            print(f"[telegram-live] 메시지 고정 실패: {p.get('description')}")

    # ── 봇에게 온 메시지·버튼 ──
    async def poll(self, client: httpx.AsyncClient, token: str, confirm: bool) -> None:
        """봇에게 온 메시지 확인.
        confirm=False (chat id를 아직 안 정했을 때): 텔레그램 쪽에서 '읽음 처리'를 하지 않는다.
          → 브라우저로 getUpdates 주소를 열어 chat id를 찾는 예전 방식도 그대로 쓸 수 있음.
        읽은 위치(offset)는 봇마다 따로 저장 - 봇을 바꾸면 번호가 달라서 새 봇 메시지를 못 보는 일 방지."""
        key = token.split(":", 1)[0]
        offsets = self.state.setdefault("offsets", {})
        seen = self.seen.get(key, offsets.get(key, 0) - 1)
        params = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if confirm:
            params["offset"] = max(offsets.get(key, 0), seen + 1)
        r = await self._call(client, token, "getUpdates", **params)
        if not r.get("ok"):
            desc = r.get("description") or "알 수 없는 오류"
            if "webhook" in desc.lower():
                # 이 봇에 웹훅이 걸려 있으면 getUpdates를 못 씀 → 웹훅을 지움 (이 봇은 TraderOS 전용이어야 함)
                d = await self._call(client, token, "deleteWebhook")
                desc = "웹훅이 걸려 있어 지웠습니다" if d.get("ok") else f"웹훅 해제 실패: {d.get('description')}"
            elif r.get("error_code") == 401 or "unauthorized" in desc.lower():
                desc = "봇 토큰이 잘못됐습니다"
            elif r.get("error_code") == 409:
                desc = "같은 봇을 다른 프로그램(다른 서버 등)도 쓰고 있습니다 - 봇은 서버 하나에서만 쓰세요"
            if desc != status["error"]:
                print(f"[telegram-live] 봇 메시지 확인 실패: {desc}")
            status["error"] = desc
            return
        status["last_poll_ok"], status["error"] = time.time(), None
        changed = False
        for u in r.get("result", []):
            if u["update_id"] <= seen:
                continue   # 읽음 처리 안 하는 동안 다시 받은 것 - 이미 처리함
            seen = u["update_id"]
            if confirm:
                offsets[key] = seen + 1
                changed = True
            msg = u.get("message")
            if msg and msg.get("chat"):
                chat = msg["chat"]
                if not msg.get("pinned_message"):          # 고정 안내 줄은 무시
                    note_message(chat["id"], msg.get("message_id"))
                name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
                recent_chats[:] = [c for c in recent_chats if c["chat_id"] != str(chat["id"])][-4:]
                recent_chats.append({"chat_id": str(chat["id"]), "name": name, "at": time.time()})
                raw = (msg.get("text") or "").strip()
                text = raw.lower()
                cid = str(chat["id"])
                mine = cid == str(telegram_cfg()["chat_id"] or "")
                if mine and text.startswith("/unlock"):
                    await self.start_unlock(client, token, cid, None)
                elif mine and text.startswith("/pos"):
                    await self.show_positions(client, token, cid)
                elif mine and text.startswith("/cancel"):
                    p = self.pending.pop(cid, None)
                    if p:
                        await self._send(client, token, cid, "SL/TP 설정을 그만뒀습니다." if p.get("type") == "pos" else "잠금 풀기를 그만뒀습니다.")
                elif mine and raw and not raw.startswith("/") and cid in self.pending:
                    if self.pending[cid].get("type") == "pos":
                        await self.try_pos(client, token, cid, raw)
                    else:
                        await self.try_unlock(client, token, cid, raw, int(msg.get("date") or time.time()))
                elif text.startswith("/start") or text.startswith("/id"):
                    a = await self._call(client, token, "sendMessage", chat_id=chat["id"],
                                         text=f"이 대화의 chat id: {chat['id']}\n관리 화면 텔레그램 탭의 chat id 칸에 넣으세요.")
                    if a.get("ok"):
                        note_message(chat["id"], a["result"]["message_id"])
                    if not a.get("ok"):
                        print(f"[telegram-live] /start 답장 실패: {a.get('description')}")
                    else:
                        print(f"[telegram-live] /start 받음 - chat id {chat['id']} ({name})")
            cb = u.get("callback_query")
            if cb:
                data = cb.get("data") or ""
                cid = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
                answer = ""
                if data == "live:refresh":
                    self.force.set()
                    answer = "갱신합니다"
                elif data.startswith("unlock:"):
                    if cid != str(telegram_cfg()["chat_id"] or ""):
                        answer = "등록된 대화에서만 잠금을 풀 수 있습니다"
                    else:
                        answer = "문구를 보냈습니다"
                elif data.startswith("pos:"):
                    if cid != str(telegram_cfg()["chat_id"] or ""):
                        answer = "등록된 대화에서만 쓸 수 있습니다"
                    else:
                        answer = await self.pos_button(client, token, cid, data)
                await self._call(client, token, "answerCallbackQuery", callback_query_id=cb["id"], text=answer)
                if data.startswith("unlock:") and answer == "문구를 보냈습니다":
                    try:
                        await self.start_unlock(client, token, cid, int(data.split(":", 1)[1]))
                    except ValueError:
                        pass
        self.seen[key] = seen
        if changed:
            self.state.pop("offset", None)   # 예전 형식 (봇 구분 없는 offset)
            self._save()

    async def run(self) -> None:
        print("[telegram-live] 시작")
        next_update = 0.0
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    cfg = telegram_cfg()
                    token, chat_id = cfg["bot_token"], cfg["chat_id"]
                    status["has_token"] = bool(token)
                    if not token:
                        await asyncio.sleep(5)
                        continue
                    if status["bot"] is None:
                        me = await self._call(client, token, "getMe")
                        if me.get("ok"):
                            status["bot"] = "@" + (me["result"].get("username") or "")
                            print(f"[telegram-live] 봇 {status['bot']} 연결됨 - 봇에게 /start 를 보내면 chat id를 알려줍니다")
                            await self._call(client, token, "setMyCommands", commands=[
                                {"command": "unlock", "description": "거래 잠금 풀기 (문구 직접 입력)"},
                                {"command": "pos", "description": "포지션 SL/TP 설정"},
                                {"command": "cancel", "description": "잠금 풀기 그만두기"},
                                {"command": "id", "description": "이 대화의 chat id 보기"},
                            ])
                    await self.poll(client, token, confirm=bool(chat_id))
                    forced = self.force.is_set()
                    if chat_id and cfg["live_enabled"] and (forced or time.time() >= next_update):
                        self.force.clear()
                        await self.update(client, token, chat_id, force=forced, position=cfg["live_position"])
                        next_update = time.time() + cfg["live_interval_sec"]
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[telegram-live] 오류: {e}")
                try:
                    await asyncio.wait_for(self.force.wait(), POLL_SEC)
                except asyncio.TimeoutError:
                    pass


live = TelegramLive()
