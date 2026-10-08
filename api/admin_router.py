# api/admin_router.py
#
# 웹 관리 화면.  브라우저에서  http://127.0.0.1:8100/  로 연다.  (api/admin_app.py가 띄움)
#
#   GET  /                      관리 화면 (admin_ui/index.html)
#   GET  /api/status            서버·계좌·워커·잠금 상태 한 번에
#   GET  /api/settings          설정 (텔레그램 토큰은 가려서)
#   PUT  /api/settings          설정 일부 저장 → 재시작 없이 바로 반영
#   POST /api/telegram/test     텔레그램 테스트 메시지
#   GET  /api/phrases           확인 문구 목록 (checkin + session)
#   POST /api/phrases           문구 추가
#   DELETE /api/phrases/{id}    문구 삭제(비활성화)
#   GET  /api/accounts          계좌 목록            POST /api/accounts        계좌 추가
#   PATCH /api/accounts/{id}    잠금 켜기/끄기, 기본 계좌, 비밀번호 변경
#   DELETE /api/accounts/{id}   계좌 삭제 (거래 기록이 없을 때만)
#   GET  /api/brokers           등록된 브로커 서버 목록
#   GET  /api/accounts/{id}/unlock-phrase   잠금 해제에 쓸 문구 하나 (지금 잠금 사유에 맞는 종류에서 무작위)
#   POST /api/accounts/{id}/unlock          문구를 입력해 잠금 해제 (EA·앱과 같은 규칙)
#   GET  /api/logs              로그 파일 목록
#   GET  /api/logs/{name}       로그 끝부분
#
#   GET/PUT /api/admin-remote   관리 화면 원격 접속 설정 (이 PC에서만)
#   POST /api/admin-login, /api/admin-logout   원격 로그인
#
# 보안: 공개 API(:8000)와 다른 포트(:8100)에 기본은 127.0.0.1로만 연다.
#   '관리 화면 원격 접속'을 켜면 같은 네트워크·Tailscale 에서 관리자 비밀번호로 들어올 수 있다 (services/admin_remote.py).
#   요청마다 admin_guard 가 확인한다.

import asyncio

import httpx
import os
import random
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models.account import Account
from db.models.broker import Broker
from db.models.trade import Trade
from db.models.trade_lock import TradeLock
from db.models.trade_lock_phrase import TradeLockPhrase
from db.session import get_db
from services import trade_lock_service as lock_svc
from services.settings_service import WEEKDAY_KR, SettingsError, get_settings, update_settings
from services.telegram_service import TelegramService
from services import ea_installer
from services.crypto import CryptoService
from services import ea_status
from services.worker_client import worker_get

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_HTML = os.path.join(BASE_DIR, "admin_ui", "index.html")

# main.py의 lifespan이 채움 (main을 import하면 순환참조라 이렇게 주입받음)
RUNTIME: dict = {
    "started_at": time.time(),
    "worker_procs": {},   # account_id -> subprocess.Popen
    "log_dir": os.path.join(BASE_DIR, "logs"),
    "start_account": None,   # async (account_id) -> None : 터미널·워커 실행
    "stop_account": None,    # async (account_id) -> None : 워커 종료
    "restart_terminal": None,  # async (account_id) -> None : MT5 껐다 켜기 + 워커 다시 실행
    "starting": set(),       # 지금 켜는 중인 account_id
    "start_errors": {},      # account_id -> 마지막 실행 오류
    "terminal_dir_for": None,  # (계좌번호) -> MT5 설치 폴더 (예: C:\\mt5\\12345678)
}


def mt5_login_state(account_number: str, info: dict | None = None) -> str:
    """이 계좌의 MT5가 로그인되어 있는지.
    ok: 로그인됨(또는 로그인 기록 있음) / needed: 터미널은 떴지만 로그인 기록 없음 / unknown: 판단 불가

    1순위는 워커가 MT5에서 직접 읽은 계좌 정보. MT5는 로그인 기록(common.ini 의 Login)을
    터미널을 닫을 때에야 파일에 쓰기 때문에, 처음 로그인한 직후에는 파일만 보면
    로그인돼 있어도 '필요'로 잘못 나온다."""
    if info and str(info.get("login") or "") == str(account_number):
        return "ok"
    fn = RUNTIME.get("terminal_dir_for")
    if fn is None:
        return "unknown"
    try:
        data_dir = ea_installer.find_data_dir(fn(account_number))
    except Exception:
        return "unknown"
    if not data_dir:
        return "unknown"
    return "ok" if ea_installer.has_logged_in(data_dir) else "needed"

def admin_guard(request: Request):
    from services import admin_remote
    ok, code, msg = admin_remote.check(request)
    if not ok:
        raise HTTPException(code, msg)


router = APIRouter(tags=["admin"], dependencies=[Depends(admin_guard)])
login_router = APIRouter(tags=["admin"])


# ── 화면 · 원격 로그인 ───────────────────────────────────────────────
_LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TraderOS 관리 - 로그인</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mute:#6b7280;--line:#d9dde3;--acc:#2f6fed;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e6e8eb;--mute:#9aa1ab;--line:#30353d;--acc:#5b8cff;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Malgun Gothic",sans-serif;padding:16px}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:100%;max-width:340px}
h1{font-size:18px;margin:0 0 4px}p{color:var(--mute);margin:0 0 16px;font-size:13px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
#err{color:var(--bad);font-size:13px;min-height:1.5em;margin:8px 0 0}
</style></head><body>
<form id="f"><h1>TraderOS 관리 화면</h1><p>관리자 비밀번호를 입력하세요.</p>
<input type="password" id="pw" autocomplete="current-password" autofocus required>
<button>로그인</button><div id="err"></div></form>
<script>
document.getElementById('f').onsubmit=async(e)=>{e.preventDefault();const err=document.getElementById('err');err.textContent='';
try{const r=await fetch('api/admin-login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({password:document.getElementById('pw').value})});
if(r.ok)return location.reload();const d=await r.json().catch(()=>({}));err.textContent=d.detail||('오류 '+r.status);}
catch(x){err.textContent='서버에 연결하지 못했습니다.';}};
</script></body></html>"""


@login_router.get("/", include_in_schema=False)
async def admin_page(request: Request):
    from fastapi.responses import HTMLResponse
    from services import admin_remote
    ok, code, msg = admin_remote.check_network(request)
    if not ok:
        raise HTTPException(code, msg)
    if not admin_remote.is_local(request) and not admin_remote.check_session(request.cookies.get(admin_remote.COOKIE)):
        return HTMLResponse(_LOGIN_HTML, headers={"Cache-Control": "no-store"})
    return FileResponse(ADMIN_HTML, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


class AdminLoginIn(BaseModel):
    password: str


@login_router.post("/api/admin-login")
async def admin_login(body: AdminLoginIn, request: Request):
    from fastapi.responses import JSONResponse
    from services import access_service, admin_remote
    ok, code, msg = admin_remote.check_network(request)
    if not ok:
        raise HTTPException(code, msg)
    if not admin_remote.same_origin(request):
        raise HTTPException(403, "다른 페이지에서 보낸 요청은 받지 않습니다.")
    key = "admin:" + admin_remote.remote_ip(request)
    wait = access_service.login_blocked(key)
    if wait:
        raise HTTPException(429, f"비밀번호를 여러 번 틀렸습니다. {wait // 60 + 1}분 뒤에 다시 시도하세요.")
    # 비밀번호 확인은 무거우므로(PBKDF2) 다른 요청을 막지 않게 스레드에서
    if not await asyncio.to_thread(admin_remote.verify, body.password):
        access_service.login_failed(key)
        print(f"[admin] 원격 로그인 실패 - {admin_remote.remote_ip(request)}")
        raise HTTPException(401, "비밀번호가 맞지 않습니다.")
    access_service.login_ok(key)
    token, max_age = admin_remote.make_session()
    print(f"[admin] 원격 로그인 - {admin_remote.remote_ip(request)}")
    res = JSONResponse({"ok": True})
    res.set_cookie(admin_remote.COOKIE, token, max_age=max_age, httponly=True, samesite="strict",
                   secure=request.url.scheme == "https", path="/")
    return res


@login_router.post("/api/admin-logout")
async def admin_logout():
    from fastapi.responses import JSONResponse
    from services import admin_remote
    res = JSONResponse({"ok": True})
    res.delete_cookie(admin_remote.COOKIE, path="/")
    return res


# ── 상태 ────────────────────────────────────────────────────────────
async def _account_info(account_id: int) -> dict | None:
    try:
        return await asyncio.wait_for(worker_get(account_id, "/account-info"), timeout=4)
    except Exception:
        return None


@router.get("/api/ping")
async def ping(request: Request):
    """관리 화면이 열려 있는지 알리는 용도 (서버 재시작 때 브라우저를 또 열지 않기 위해)"""
    from services import admin_remote
    local = admin_remote.is_local(request)
    if local:
        RUNTIME["page_seen_at"] = time.time()
    return {"ok": True, "remote": not local}


@router.get("/api/status")
async def status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Account).options(selectinload(Account.broker)).order_by(Account.id))
    accounts = result.scalars().all()

    procs = RUNTIME.get("worker_procs") or {}

    async def one(acc: Account) -> dict:
        proc = procs.get(acc.id)
        worker = "off"
        if acc.lock_enabled:
            worker = "running" if proc is not None and proc.poll() is None else "stopped"
            if acc.id in RUNTIME["starting"]:
                worker = "starting"

        info = await _account_info(acc.id) if worker == "running" else None

        lock = None  # 잠금을 안 쓰는 계좌는 잠금 기록을 새로 만들지 않음
        if acc.lock_enabled:
            try:
                lock = await lock_svc.get_lock_state(db, acc.id)
            except Exception as e:
                lock = {"error": str(e)}

        return {
            "id": acc.id,
            "account_number": acc.account_number,
            "broker": acc.broker.name if acc.broker else None,
            "is_default": acc.is_default,
            "lock_enabled": acc.lock_enabled,
            "worker": worker,
            "start_error": RUNTIME["start_errors"].get(acc.id),
            "mt5_login": mt5_login_state(acc.account_number, info) if acc.lock_enabled else None,
            "ea": ea_status.state(acc.id, RUNTIME["started_at"]) if acc.lock_enabled else None,
            "mt5": None if info is None else {
                "balance": info.get("balance"),
                "equity": info.get("equity"),
                "margin": info.get("margin"),
                "margin_free": info.get("margin_free"),
                "margin_level": info.get("margin_level"),
                "currency": info.get("currency"),
            },
            "lock": lock,
        }

    # 같은 DB 세션을 여러 코루틴이 동시에 쓰면 안 되므로 순서대로
    items = [await one(a) for a in accounts]

    return {
        "now": datetime.now(lock_svc.KST).isoformat(),
        "uptime_sec": int(time.time() - RUNTIME["started_at"]),
        "web_app": RUNTIME.get("web_app"),   # {"port": 8000, "path": "/trader-os/"} - 플러터 웹 빌드가 있을 때만
        "accounts": items,
        "telegram_enabled": TelegramService().enabled,
        "version": _version(),
    }


def _version() -> dict:
    from services import updater
    v = updater.version_label()
    s = updater.summary()
    v["update_available"] = s["available"]
    v["latest"] = (s.get("latest") or {}).get("version") if s["available"] else None
    return v


# ── 설정 ────────────────────────────────────────────────────────────
def _mask(token: str) -> str:
    if not token:
        return ""
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "…"


def _public_settings() -> dict:
    s = get_settings()
    token = s["telegram"]["bot_token"]
    s["telegram"] = {
        "bot_token_masked": _mask(token),
        "has_bot_token": bool(token),
        "chat_id": s["telegram"]["chat_id"],
        "live_enabled": s["telegram"].get("live_enabled", True),
        "live_interval_sec": s["telegram"].get("live_interval_sec", 10),
        "live_position": s["telegram"].get("live_position", "top"),
        "env_fallback": not token and bool(os.getenv("TELEGRAM_BOT_TOKEN")),
    }
    s.pop("admin_remote", None)                       # 관리자 비밀번호 해시 - /api/admin-remote 에서 따로
    if isinstance(s.get("access"), dict):
        s["access"] = {k: v for k, v in s["access"].items() if k != "password_hash"}
    s["weekday_names"] = WEEKDAY_KR
    return s


@router.get("/api/settings")
async def read_settings():
    return _public_settings()


@router.put("/api/settings")
async def write_settings(body: dict):
    patch: dict = {}

    if "lock" in body:
        if not isinstance(body["lock"], dict):
            raise HTTPException(400, "lock 형식이 잘못됐습니다.")
        patch["lock"] = body["lock"]

    if "telegram" in body:
        tg = body["telegram"] or {}
        patch["telegram"] = {}
        # 토큰은 화면에 가려서 보여주므로, 새로 입력했을 때만 바꾸고 비워 두면 기존 값 유지
        if tg.get("bot_token"):
            patch["telegram"]["bot_token"] = tg["bot_token"]
        if tg.get("clear_bot_token"):
            patch["telegram"]["bot_token"] = ""
        if "chat_id" in tg:
            patch["telegram"]["chat_id"] = tg["chat_id"]
        for k in ("live_enabled", "live_interval_sec", "live_position"):
            if k in tg:
                patch["telegram"][k] = tg[k]

    try:
        update_settings(patch)
    except SettingsError as e:
        raise HTTPException(400, str(e))
    return _public_settings()


# ── 바깥 접속 방식 (services/access_service.py) ──
class AccessIn(BaseModel):
    mode: str | None = None
    password: str | None = None
    session_days: int | None = None
    logout_all: bool = False


def _access_out() -> dict:
    from services import access_service as access
    cfg = access.access_cfg()
    return {"mode": cfg["mode"], "has_password": bool(cfg["password_hash"]), "session_days": cfg["session_days"],
            "tailscale_ips": access.tailscale_ips(), "port": int(os.getenv("TRADEROS_PORT", "8000"))}


@router.get("/api/access")
async def read_access():
    return _access_out()


@router.put("/api/access")
async def write_access(body: AccessIn):
    from services import access_service as access
    try:
        if body.password:
            access.set_password(body.password)          # 바꾸면 기존 로그인 모두 풀림
        patch = {}
        if body.mode is not None:
            patch["mode"] = body.mode
        if body.session_days is not None:
            patch["session_days"] = body.session_days
        if patch:
            update_settings({"access": patch})
        if body.logout_all:
            access.rotate_key()
    except (ValueError, SettingsError) as e:
        raise HTTPException(400, str(e))
    return _access_out()


@router.get("/api/telegram/chats")
async def telegram_chats():
    """최근 봇에게 말을 건 대화 (chat id 찾기). 봇 토큰을 저장한 뒤 봇에게 /start 를 보내면 2초 안에 나타남."""
    from services.telegram_live import recent_chats, status
    from services.settings_service import telegram_cfg
    chats = list(reversed(recent_chats))
    token = telegram_cfg()["bot_token"]
    if token:
        # 예전 방식(getUpdates)을 서버가 대신 해 봄 - 읽음 처리는 하지 않음
        try:
            async with httpx.AsyncClient() as c:
                r = (await c.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)).json()
            for u in reversed(r.get("result", [])):
                m = u.get("message") or u.get("edited_message") or (u.get("callback_query") or {}).get("message")
                ch = (m or {}).get("chat")
                if ch and all(x["chat_id"] != str(ch["id"]) for x in chats):
                    name = ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("username") or ""
                    chats.append({"chat_id": str(ch["id"]), "name": name, "at": m.get("date")})
        except Exception:
            pass
    return {"chats": chats, "status": status}


@router.get("/api/telegram/updates", include_in_schema=False)
async def telegram_updates_page():
    """예전 방식: 브라우저로 getUpdates 결과를 직접 보기 (이 PC에서만 열리는 관리 화면이라 토큰이 밖으로 안 나감)"""
    from services.settings_service import telegram_cfg
    token = telegram_cfg()["bot_token"]
    if not token:
        raise HTTPException(400, "봇 토큰을 먼저 저장하세요.")
    return RedirectResponse(f"https://api.telegram.org/bot{token}/getUpdates")


@router.post("/api/telegram/test")
async def telegram_test():
    ok, msg = await asyncio.to_thread(
        TelegramService().send_now, "✅ TraderOS 테스트", "관리 화면에서 보낸 테스트 메시지입니다."
    )
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


# ── 관리 화면 원격 접속 (services/admin_remote.py) - /api/admin-remote 는 원격에서 막힘 (이 PC에서만) ──
class AdminRemoteIn(BaseModel):
    enabled: bool | None = None
    password: str | None = None
    session_days: int | None = None
    logout_all: bool = False


def _admin_remote_out(request: Request) -> dict:
    from api import admin_app
    from services import access_service, admin_remote
    c = admin_remote.cfg()
    return {"enabled": c["enabled"], "has_password": bool(c["password_hash"]), "session_days": c["session_days"],
            "listening": admin_app._current.get("host"), "port": admin_app.ADMIN_PORT,
            "main_port": int(os.getenv("TRADEROS_PORT", "8000")),
            "lan_ips": admin_remote.lan_ips(), "tailscale_ips": access_service.tailscale_ips()}


@router.get("/api/admin-remote")
async def read_admin_remote(request: Request):
    return _admin_remote_out(request)


@router.put("/api/admin-remote")
async def write_admin_remote(body: AdminRemoteIn, request: Request):
    from api import admin_app
    from services import admin_remote
    try:
        if body.password:
            admin_remote.set_password(body.password)       # 바꾸면 원격 로그인 모두 풀림
        patch = {}
        if body.enabled is not None:
            patch["enabled"] = body.enabled
        if body.session_days is not None:
            patch["session_days"] = body.session_days
        if patch:
            update_settings({"admin_remote": patch})
        if body.logout_all:
            admin_remote.rotate_key()
    except (ValueError, SettingsError) as e:
        raise HTTPException(400, str(e))
    asyncio.create_task(admin_app.rebind())            # 켜고 끈 경우에만 실제로 다시 엶
    out = _admin_remote_out(request)
    out["listening"] = "0.0.0.0" if out["enabled"] else admin_app.ADMIN_HOST
    return out


# ── 확인 문구 ───────────────────────────────────────────────────────
class PhraseIn(BaseModel):
    phrase: str
    phrase_type: str = "checkin"


@router.get("/api/phrases")
async def list_phrases(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TradeLockPhrase).where(TradeLockPhrase.is_active == True).order_by(TradeLockPhrase.id)
    )
    return [
        {"id": p.id, "phrase": p.phrase, "phrase_type": p.phrase_type}
        for p in result.scalars().all()
    ]


@router.post("/api/phrases")
async def add_phrase(body: PhraseIn, db: AsyncSession = Depends(get_db)):
    phrase = body.phrase.strip()
    if not phrase:
        raise HTTPException(400, "문구를 입력하세요.")
    ptype = body.phrase_type
    if ptype.startswith(lock_svc.PHRASE_TYPE_SESSION + ":"):
        sid = ptype.split(":", 1)[1]
        if sid not in {x["id"] for x in get_settings()["lock"]["sessions"]}:
            raise HTTPException(400, "없는 집중 구간입니다. 구간을 먼저 저장하세요.")
    elif ptype not in (lock_svc.PHRASE_TYPE_CHECKIN, lock_svc.PHRASE_TYPE_CONSEC, lock_svc.PHRASE_TYPE_SESSION):
        raise HTTPException(400, "phrase_type은 checkin, consec_loss, session, session:<구간id> 중 하나여야 합니다.")

    # 삭제(비활성화)했던 같은 문구가 있으면 되살림 - phrase 컬럼이 unique라서
    existing = (await db.execute(select(TradeLockPhrase).where(TradeLockPhrase.phrase == phrase))).scalar_one_or_none()
    if existing is not None:
        if existing.is_active and existing.phrase_type == body.phrase_type:
            raise HTTPException(400, "이미 등록된 문구입니다.")
        existing.is_active = True
        existing.phrase_type = body.phrase_type
        row = existing
    else:
        row = TradeLockPhrase(phrase=phrase, phrase_type=body.phrase_type, is_active=True)
        db.add(row)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "이미 등록된 문구입니다.")
    await db.refresh(row)
    return {"id": row.id, "phrase": row.phrase, "phrase_type": row.phrase_type}


@router.delete("/api/phrases/{phrase_id}")
async def delete_phrase(phrase_id: int, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(TradeLockPhrase).where(TradeLockPhrase.id == phrase_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "문구를 찾을 수 없습니다.")
    row.is_active = False
    await db.commit()
    return {"deleted": True}


# ── 계좌 ────────────────────────────────────────────────────────────
class AccountIn(BaseModel):
    account_number: str
    password: str
    server: str                  # MT5 로그인 창의 서버 이름 (예: InfinoxLimited-MT5Live)
    lock_enabled: bool = True
    is_default: bool = False


class AccountPatch(BaseModel):
    lock_enabled: bool | None = None
    is_default: bool | None = None
    password: str | None = None


def _account_out(acc: Account, info: dict | None = None) -> dict:
    return {
        "id": acc.id,
        "account_number": acc.account_number,
        "broker": acc.broker.name if acc.broker else None,
        "server": acc.broker.server if acc.broker else None,
        "is_default": acc.is_default,
        "lock_enabled": acc.lock_enabled,
        "has_password": bool(acc.password_encrypted),
        # 지금 암호화 키로 풀리는지 - 키가 바뀌었으면(예: .env FERNET_KEY 삭제) 비밀번호를 다시 넣어야 함
        "password_ok": CryptoService().can_decrypt(acc.password_encrypted),
        "mt5_login": mt5_login_state(acc.account_number, info),
    }


async def _get_account(db: AsyncSession, account_id: int) -> Account:
    acc = (await db.execute(
        select(Account).options(selectinload(Account.broker)).where(Account.id == account_id)
    )).scalar_one_or_none()
    if acc is None:
        raise HTTPException(404, "계좌를 찾을 수 없습니다.")
    return acc


async def _make_default(db: AsyncSession, acc: Account) -> None:
    # 기본 계좌는 하나만 (PostgreSQL에 부분 unique 인덱스가 있어서 기존 것을 먼저 끔)
    await db.execute(update(Account).where(Account.id != acc.id).values(is_default=False))
    await db.flush()
    acc.is_default = True


_bg_tasks: set = set()


def _run_in_background(account_id: int, start: bool, action: str | None = None) -> None:
    """터미널 실행·MT5 설치본 복사는 오래 걸릴 수 있어서 응답을 먼저 보내고 뒤에서 처리.
    action: 'restart_terminal' 이면 MT5 재시작 (start=True 처럼 '켜는 중'으로 표시)"""
    fn = RUNTIME.get(action or ("start_account" if start else "stop_account"))
    if fn is None:
        return  # 관리 화면만 따로 띄운 경우(테스트 등)

    if start:
        RUNTIME["starting"].add(account_id)   # 응답 직후 상태 조회에서도 "켜는 중"으로 보이게
    RUNTIME["start_errors"].pop(account_id, None)

    async def job():
        try:
            await fn(account_id)
        except Exception as e:
            RUNTIME["start_errors"][account_id] = str(e)
            print(f"[admin] account_id={account_id} {'MT5 재시작' if action else ('실행' if start else '중지')} 실패: {e}")
        finally:
            RUNTIME["starting"].discard(account_id)

    task = asyncio.create_task(job())
    _bg_tasks.add(task)                      # 참조를 잡아둬야 중간에 사라지지 않음
    task.add_done_callback(_bg_tasks.discard)


@router.get("/api/brokers")
async def list_brokers(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Broker).order_by(Broker.id))).scalars().all()
    return [{"id": b.id, "name": b.name, "server": b.server} for b in rows]


@router.get("/api/accounts")
async def list_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Account).options(selectinload(Account.broker)).order_by(Account.id)
    )).scalars().all()
    procs = RUNTIME.get("worker_procs") or {}

    async def one(a: Account) -> dict:
        p = procs.get(a.id)
        running = a.lock_enabled and p is not None and p.poll() is None
        return _account_out(a, await _account_info(a.id) if running else None)

    return list(await asyncio.gather(*(one(a) for a in rows)))


@router.post("/api/accounts")
async def add_account(body: AccountIn, db: AsyncSession = Depends(get_db)):
    number = body.account_number.strip()
    server = body.server.strip()
    if not number.isdigit():
        raise HTTPException(400, "계좌번호는 숫자만 입력하세요.")
    if not body.password:
        raise HTTPException(400, "비밀번호를 입력하세요.")
    if not server:
        raise HTTPException(400, "서버 이름을 입력하세요. MT5 로그인 창에 나오는 이름 그대로 넣으면 됩니다.")

    if (await db.execute(select(Account).where(Account.account_number == number))).scalar_one_or_none():
        raise HTTPException(400, "이미 등록된 계좌번호입니다.")

    broker = (await db.execute(select(Broker).where(Broker.server == server))).scalar_one_or_none()
    if broker is None:
        # 브로커 이름은 서버 이름의 앞부분 (예: InfinoxLimited-MT5Live → InfinoxLimited)
        base = server.split("-")[0] or server
        name, n = base, 2
        while (await db.execute(select(Broker).where(Broker.name == name))).scalar_one_or_none():
            name, n = f"{base}{n}", n + 1
        broker = Broker(name=name, server=server)
        db.add(broker)
        await db.flush()

    acc = Account(
        account_number=number,
        password_encrypted=CryptoService().encrypt(body.password),
        broker_id=broker.id,
        lock_enabled=body.lock_enabled,
        is_default=False,
    )
    db.add(acc)
    await db.flush()

    has_default = (await db.execute(
        select(func.count()).select_from(Account).where(Account.is_default == True)
    )).scalar()
    if body.is_default or not has_default:   # 첫 계좌는 자동으로 기본 계좌
        await _make_default(db, acc)

    await db.commit()
    acc = await _get_account(db, acc.id)

    if acc.lock_enabled:
        _run_in_background(acc.id, start=True)
    return _account_out(acc)


@router.patch("/api/accounts/{account_id}")
async def edit_account(account_id: int, body: AccountPatch, db: AsyncSession = Depends(get_db)):
    acc = await _get_account(db, account_id)
    turned_on = turned_off = False

    if body.password:
        acc.password_encrypted = CryptoService().encrypt(body.password)
    if body.is_default:
        await _make_default(db, acc)
    if body.lock_enabled is not None and body.lock_enabled != acc.lock_enabled:
        acc.lock_enabled = body.lock_enabled
        turned_on, turned_off = body.lock_enabled, not body.lock_enabled

    await db.commit()
    if turned_on:
        _run_in_background(account_id, start=True)
    if turned_off:
        _run_in_background(account_id, start=False)
    return _account_out(await _get_account(db, account_id))


# ── 자동 업데이트 (services/updater.py) ─────────────────────────────
@router.get("/api/update")
async def update_status(check: bool = False):
    from services import updater
    if check:
        return await updater.check(notify=False)
    return updater.summary()


@router.post("/api/update/apply")
async def update_apply():
    from services import updater
    try:
        return updater.apply_in_background()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.post("/api/accounts/{account_id}/restart-mt5")
async def restart_mt5(account_id: int, db: AsyncSession = Depends(get_db)):
    """MT5를 정상 종료 후 다시 띄우고 워커도 다시 실행 (서버 재시작 없이)"""
    acc = await _get_account(db, account_id)
    if not acc.lock_enabled:
        raise HTTPException(400, "MT5 실행이 꺼진 계좌입니다. 먼저 MT5 실행을 켜세요.")
    if account_id in RUNTIME["starting"]:
        raise HTTPException(409, "이미 켜는 중입니다. 잠시 기다려 주세요.")
    if RUNTIME.get("restart_terminal") is None:
        raise HTTPException(503, "이 서버에서는 MT5 재시작을 쓸 수 없습니다.")
    _run_in_background(account_id, start=True, action="restart_terminal")
    return {"ok": True}


@router.delete("/api/accounts/{account_id}")
async def remove_account(account_id: int, db: AsyncSession = Depends(get_db)):
    acc = await _get_account(db, account_id)
    trades = (await db.execute(
        select(func.count()).select_from(Trade).where(Trade.account_id == account_id)
    )).scalar()
    if trades:
        raise HTTPException(400, f"거래 기록이 {trades}건 있어 삭제할 수 없습니다. 대신 잠금을 끄세요.")

    was_default = acc.is_default
    await db.execute(delete(TradeLock).where(TradeLock.account_id == account_id))
    await db.delete(acc)
    await db.flush()
    if was_default:   # 다른 계좌가 있으면 그중 첫 번째를 기본으로
        other = (await db.execute(select(Account).order_by(Account.id).limit(1))).scalar_one_or_none()
        if other is not None:
            other.is_default = True
    await db.commit()

    _run_in_background(account_id, start=False)
    return {"deleted": True}


# ── 잠금 해제 (EA 창·앱과 같은 규칙: 지금 사유에 맞는 문구를 정확히 입력해야 풀림) ──
class UnlockIn(BaseModel):
    phrase: str


@router.get("/api/accounts/{account_id}/unlock-phrase")
async def unlock_phrase(account_id: int, db: AsyncSession = Depends(get_db)):
    await _get_account(db, account_id)
    state = await lock_svc.get_lock_state(db, account_id)
    if not state["locked"]:
        return {"locked": False}
    if state["reason"] == "manual":
        return {"locked": True, "reason": "manual", "phrase": None,
                "message": "수동 잠금은 문구로 풀 수 없습니다. 설정한 시간이 지나야 풀립니다."}

    types = state.get("phrase_types") or [state["phrase_type"]]
    rows = (await db.execute(
        select(TradeLockPhrase.phrase)
        .where(TradeLockPhrase.is_active == True, TradeLockPhrase.phrase_type.in_(types))
    )).scalars().all()
    if not rows:
        return {"locked": True, "reason": state["reason"], "phrase": None,
                "message": "이 잠금에 쓸 문구가 없습니다. 확인 문구 탭에서 먼저 추가하세요."}
    return {"locked": True, "reason": state["reason"], "phrase": random.choice(rows)}


@router.post("/api/accounts/{account_id}/unlock")
async def unlock(account_id: int, body: UnlockIn, db: AsyncSession = Depends(get_db)):
    acc = await _get_account(db, account_id)
    try:
        return await lock_svc.confirm_unlock(db, account_id, acc.account_number, body.phrase.strip())
    except lock_svc.PhraseMismatchError:
        raise HTTPException(400, "문구가 정확히 일치하지 않습니다. 띄어쓰기까지 똑같이 입력하세요.")
    except lock_svc.ManualLockActiveError:
        raise HTTPException(403, "수동 잠금은 문구로 풀 수 없습니다. 설정한 시간이 지나야 풀립니다.")


# ── 로그 ────────────────────────────────────────────────────────────
def _log_dir() -> str:
    return RUNTIME.get("log_dir") or os.path.join(BASE_DIR, "logs")


@router.get("/api/logs")
async def list_logs():
    d = _log_dir()
    if not os.path.isdir(d):
        return []
    files = []
    for name in sorted(os.listdir(d)):
        path = os.path.join(d, name)
        if os.path.isfile(path) and name.endswith(".log"):
            st = os.stat(path)
            files.append({"name": name, "size": st.st_size, "modified": int(st.st_mtime)})
    return files


@router.get("/api/logs/{name}")
async def read_log(name: str, lines: int = 300):
    # 경로 조작 방지: 로그 폴더 안의 .log 파일 이름만 허용
    if os.path.basename(name) != name or not name.endswith(".log"):
        raise HTTPException(400, "잘못된 파일 이름")
    path = os.path.join(_log_dir(), name)
    if not os.path.isfile(path):
        raise HTTPException(404, "로그 파일이 없습니다.")

    lines = max(10, min(lines, 2000))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 400_000))  # 끝 400KB만 읽음
        text = f.read().decode("utf-8", errors="replace")
    return {"name": name, "lines": text.splitlines()[-lines:]}
