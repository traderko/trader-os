# services/access_service.py
#
# 메인 서버(:8000)에 바깥에서 들어오는 요청을 막는 규칙. main.py 의 AccessGuard 가 쓴다.
#
#   이 PC에서 온 요청 (127.0.0.1, 프록시 헤더 없음) → 항상 통과
#       MT5 EA(잠금 확인·체결 기록), 워커, 관리 화면이 이 경로라 어느 방식이든 그대로 동작
#
#   access.mode (관리 화면 '접속' 탭에서 선택, data/settings.json)
#     open      : 제한 없음 (예전 동작)
#     tailscale : Tailscale 주소(100.64.0.0/10, fd7a:115c:a1e0::/48)에서 온 요청만 - 내 PC에서 돌릴 때
#     lan       : 같은 공유기(사설 IP: 192.168.x, 10.x, 172.16~31.x)와 Tailscale 에서 온 요청 - 집 와이파이의 폰·다른 PC, 맥의 가상머신 등
#     public    : 비밀번호로 로그인한 브라우저만 - 도메인·고정 IP 서버(예: Vultr)에서 돌릴 때
#                 nginx·Caddy 같은 HTTPS 리버스 프록시 뒤에 두는 걸 전제로 함
#
# 로그인 쿠키: "만료시각.서명" (서명 = HMAC-SHA256, 키는 data/session.key - 처음 쓸 때 만들어짐)
#   비밀번호를 바꾸면 키도 바꿔서 기존에 로그인한 기기는 모두 로그아웃된다.
# 비밀번호 저장: PBKDF2-SHA256 (salt 포함), 평문은 어디에도 저장하지 않음.

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time

from services.settings_service import DATA_DIR, get_settings, update_settings

COOKIE = "traderos_session"
KEY_PATH = os.path.join(DATA_DIR, "session.key")
PBKDF2_ROUNDS = 200_000

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_PROXY_HEADERS = (b"x-forwarded-for", b"x-real-ip", b"forwarded")
_TAILSCALE_NETS = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]
_LAN_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fe80::/10", "fc00::/7")]

# 로그인 없이 열리는 경로 (public 방식) - 로그인 화면을 그리는 데 필요한 것만
PUBLIC_PATHS = ("/auth/",)
PUBLIC_PREFIXES_STATIC = ("/app",)   # 웹 화면 파일(HTML) - 데이터는 API에서 막힘


def access_cfg() -> dict:
    a = get_settings().get("access") or {}
    return {
        "mode": a.get("mode") or "open",
        "password_hash": a.get("password_hash") or "",
        "session_days": int(a.get("session_days") or 30),
    }


# ── 주소 판별 ──
def _headers(scope) -> dict:
    return {k.lower(): v for k, v in scope.get("headers") or []}


def is_local(scope) -> bool:
    """이 PC 안에서 직접 온 요청인지 (리버스 프록시를 거친 요청은 아님)"""
    host = (scope.get("client") or ("", 0))[0]
    if host not in _LOOPBACK:
        return False
    h = _headers(scope)
    return not any(p in h for p in _PROXY_HEADERS)


def client_ip(scope) -> str:
    """실제 접속 주소. 이 PC의 리버스 프록시가 넘겨준 경우엔 X-Forwarded-For / X-Real-IP 를 믿음."""
    host = (scope.get("client") or ("", 0))[0]
    if host in _LOOPBACK:
        h = _headers(scope)
        fwd = h.get(b"x-forwarded-for") or h.get(b"x-real-ip")
        if fwd:
            return fwd.decode("latin-1").split(",")[0].strip()
    return host


def is_tailscale(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in _TAILSCALE_NETS)


def is_lan(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in _LAN_NETS)


def tailscale_ips() -> list[str]:
    """이 PC의 Tailscale 주소 (관리 화면 안내용)"""
    try:
        import psutil
        out = []
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                ip = a.address.split("%")[0]
                if is_tailscale(ip):
                    out.append(ip)
        return sorted(set(out), key=lambda x: (":" in x, x))
    except Exception:
        return []


# ── 비밀번호 ──
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, rounds, salt, dk = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


def set_password(pw: str) -> None:
    if len(pw) < 8:
        raise ValueError("비밀번호는 8자 이상으로 정하세요.")
    update_settings({"access": {"password_hash": hash_password(pw)}})
    rotate_key()


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
    """모든 로그인 무효화 (비밀번호 변경·전체 로그아웃)"""
    with _key_lock:
        _write_key()


def make_session(days: int) -> str:
    exp = str(int(time.time() + days * 86400))
    sig = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


def session_from_scope(scope) -> str | None:
    raw = _headers(scope).get(b"cookie")
    if not raw:
        return None
    for part in raw.decode("latin-1").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return None


# ── 로그인 실패 제한 (주소별로 5번 틀리면 10분 차단) ──
_fails: dict[str, list[float]] = {}
MAX_FAILS, WINDOW, BLOCK = 5, 600, 600


def login_blocked(ip: str) -> int:
    """차단 중이면 남은 초, 아니면 0"""
    now = time.time()
    ts = [t for t in _fails.get(ip, []) if now - t < WINDOW]
    _fails[ip] = ts
    if len(ts) >= MAX_FAILS:
        return int(BLOCK - (now - ts[-1])) + 1
    return 0


def login_failed(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def login_ok(ip: str) -> None:
    _fails.pop(ip, None)


# ── 요청 판정 ──
def normalize_path(path: str) -> str:
    """라이브 nginx 처럼 /trading-api 를 붙여서 넘기는 경우도 같은 규칙으로"""
    return path[len("/trading-api"):] or "/" if path.startswith("/trading-api/") or path == "/trading-api" else path


def decide(scope) -> tuple[bool, int, str]:
    """(통과?, 막을 때 상태코드, 이유)"""
    if is_local(scope):
        return True, 0, ""
    cfg = access_cfg()
    mode = cfg["mode"]
    if mode == "open":
        return True, 0, ""
    if mode == "tailscale":
        ip = client_ip(scope)
        if is_tailscale(ip):
            return True, 0, ""
        return False, 403, f"이 서버는 Tailscale로만 접속할 수 있습니다. (접속 주소 {ip}) - 같은 와이파이에서 쓰려면 관리 화면 '접속' 탭에서 '같은 네트워크'로 바꾸세요."
    if mode == "lan":
        ip = client_ip(scope)
        if is_lan(ip) or is_tailscale(ip):
            return True, 0, ""
        return False, 403, f"이 서버는 같은 네트워크(공유기)와 Tailscale에서만 접속할 수 있습니다. (접속 주소 {ip})"
    # public
    path = normalize_path(scope.get("path", ""))
    if path.startswith(PUBLIC_PATHS):
        return True, 0, ""
    if any(path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES_STATIC):
        return True, 0, ""
    if check_session(session_from_scope(scope)):
        return True, 0, ""
    return False, 401, "로그인이 필요합니다."
