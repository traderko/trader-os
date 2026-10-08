# services/admin_remote.py
#
# 관리 화면(:8100)을 다른 기기에서 열기 - 기본은 꺼짐 (이 PC에서만).
#
#   켜는 곳: 이 PC의 관리 화면 '접속' 탭 → "관리 화면 원격 접속" (켜기·비밀번호는 이 PC에서만 바꿀 수 있음)
#   켜면  : 관리 서버가 0.0.0.0:8100 으로 다시 열리고, 아래 조건을 모두 만족해야 들어옴
#     1) 같은 공유기(사설 IP) 또는 Tailscale 에서 온 접속 - 인터넷에서 바로 온 접속은 비밀번호가 맞아도 거절
#     2) 관리자 비밀번호로 로그인 (웹 화면 '공개 서버' 비밀번호와 따로)
#     3) 바꾸는 요청(POST·PUT…)은 같은 주소에서 연 관리 화면에서 온 것만 (다른 사이트가 몰래 보내는 요청 차단)
#   원격에서도 막는 것: 원격 접속 설정 자체, 봇 토큰이 주소에 들어가는 getUpdates 바로가기
#
# 로그인 쿠키는 웹 화면(access_service)과 같은 방식 "만료시각.서명", 키만 따로 (data/admin_session.key)

import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import urlparse

from services import access_service as access
from services.settings_service import DATA_DIR, get_settings, update_settings

COOKIE = "traderos_admin"
KEY_PATH = os.path.join(DATA_DIR, "admin_session.key")
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")

# 원격에서는 열지 않는 경로 (설정 자체, 토큰이 주소에 들어가는 바로가기)
LOCAL_ONLY_PATHS = ("/api/admin-remote", "/api/telegram/updates")


def cfg() -> dict:
    a = get_settings().get("admin_remote") or {}
    return {
        "enabled": bool(a.get("enabled")),
        "password_hash": a.get("password_hash") or "",
        "session_days": int(a.get("session_days") or 7),
    }


def is_local(request) -> bool:
    host = request.client.host if request.client else ""
    return host in LOCAL_HOSTS and not any(h in request.headers for h in PROXY_HEADERS)


def remote_ip(request) -> str:
    # 원격 접속은 프록시 헤더를 믿지 않음 (직접 붙은 주소만)
    return request.client.host if request.client else ""


def network_ok(ip: str) -> bool:
    return access.is_lan(ip) or access.is_tailscale(ip)


# ── 비밀번호 ──
def set_password(pw: str) -> None:
    if len(pw) < 8:
        raise ValueError("관리자 비밀번호는 8자 이상으로 정하세요.")
    update_settings({"admin_remote": {"password_hash": access.hash_password(pw)}})
    rotate_key()


def verify(pw: str) -> bool:
    h = cfg()["password_hash"]
    return bool(h) and access.verify_password(pw, h)


# ── 로그인 쿠키 ──
_key_lock = threading.Lock()


def _key() -> bytes:
    with _key_lock:
        try:
            k = open(KEY_PATH, "rb").read()
            if len(k) >= 32:
                return k
        except OSError:
            pass
        return _write_key()


def _write_key() -> bytes:
    k = secrets.token_bytes(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = KEY_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(k)
    os.replace(tmp, KEY_PATH)
    return k


def rotate_key() -> None:
    """원격으로 로그인한 기기 모두 로그아웃"""
    with _key_lock:
        _write_key()


def make_session() -> tuple[str, int]:
    days = cfg()["session_days"]
    exp = str(int(time.time() + days * 86400))
    return f"{exp}.{hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()}", days * 86400


def check_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    return hmac.compare_digest(hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest(), sig)


# ── 판정 ──
def same_origin(request) -> bool:
    """바꾸는 요청이 이 관리 화면에서 온 것인지 (Origin/Referer 의 주소:포트 == Host)"""
    host = request.headers.get("host", "")
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return False
    return urlparse(src).netloc == host


def check_network(request) -> tuple[bool, int, str]:
    """로그인 전 단계 - 원격 접속이 켜져 있고, 집 네트워크·Tailscale 에서 왔는지"""
    if is_local(request):
        return True, 0, ""
    if not cfg()["enabled"]:
        return False, 403, "관리 화면은 서버가 돌고 있는 PC에서만 열 수 있습니다. (원격 접속 꺼짐)"
    ip = remote_ip(request)
    if not network_ok(ip):
        return False, 403, f"관리 화면은 같은 네트워크(공유기)나 Tailscale 에서만 열 수 있습니다. (접속 주소 {ip})"
    return True, 0, ""


def check(request) -> tuple[bool, int, str]:
    """관리 API 요청 판정 (통과?, 상태코드, 이유)"""
    if is_local(request):
        return True, 0, ""
    ok, code, msg = check_network(request)
    if not ok:
        return ok, code, msg
    if request.url.path.startswith(LOCAL_ONLY_PATHS):
        return False, 403, "이 항목은 서버 PC에서만 열 수 있습니다."
    if not check_session(request.cookies.get(COOKIE)):
        return False, 401, "관리자 로그인이 필요합니다."
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
        return False, 403, "다른 페이지에서 보낸 요청은 받지 않습니다."
    return True, 0, ""


def lan_ips() -> list[str]:
    """이 PC의 공유기 내부 주소 (안내용)"""
    try:
        import psutil
        out = []
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                ip = a.address.split("%")[0]
                if ":" not in ip and access.is_lan(ip):
                    out.append(ip)
        return sorted(set(out))
    except Exception:
        return []
