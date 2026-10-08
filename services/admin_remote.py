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
#
# :8000/admin/ 으로도 열 수 있다 (main.py 가 관리 앱을 /admin 에 붙임 - scope["traderos_via"] = "main").
#   8000 은 nginx 같은 리버스 프록시 뒤에 있을 수 있어서, 이 경로로 온 요청은 "이 PC에서 왔다"고 믿지 않는다:
#   이 PC에서 열어도 비밀번호 로그인이 필요하고, 프록시가 넘겨준 주소(X-Forwarded-For)가 인터넷이면 거절.
#   (이 PC에서 비밀번호 없이 쓰려면 127.0.0.1:8100)

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


def via_main(request) -> bool:
    """:8000/admin/ 으로 온 요청인지"""
    return request.scope.get("traderos_via") == "main"


def is_local(request) -> bool:
    """비밀번호 없이 통과시켜도 되는 요청 - :8100 에 이 PC에서 직접 온 것만"""
    if via_main(request):
        return False
    host = request.client.host if request.client else ""
    return host in LOCAL_HOSTS and not any(h in request.headers for h in PROXY_HEADERS)


def remote_ip(request) -> str:
    host = request.client.host if request.client else ""
    if via_main(request) and host in LOCAL_HOSTS:
        # 8000 앞의 리버스 프록시가 넘겨준 실제 주소 (없으면 이 PC)
        fwd = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")
        if fwd:
            return fwd.split(",")[0].strip()
    # 8100 에 직접 붙은 원격 접속은 프록시 헤더를 믿지 않음
    return host


def network_ok(ip: str) -> bool:
    return ip in LOCAL_HOSTS or access.is_lan(ip) or access.is_tailscale(ip)


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
        if via_main(request):
            return False, 403, ("/admin 으로 열려면 서버 PC의 관리 화면(http://127.0.0.1:8100/) '접속' 탭에서 "
                                "'관리 화면 원격 접속'을 켜세요.")
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
    if any(p in request.url.path for p in LOCAL_ONLY_PATHS):
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
