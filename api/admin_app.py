# api/admin_app.py
#
# 관리 화면 전용 서버. main.py의 lifespan이 공개 API(:8000)와 같은 프로세스·같은 이벤트 루프에서
# 127.0.0.1:8100 으로 하나 더 띄운다.
#   - 같은 루프라 DB 엔진(asyncpg)을 그대로 같이 쓸 수 있다.
#   - 기본은 127.0.0.1에만 열리므로 다른 PC나 nginx를 거친 요청은 닿지 않는다.
#   - '관리 화면 원격 접속'(services/admin_remote.py)을 켜면 0.0.0.0 으로 다시 열고, 로그인·네트워크 검사를 한다.
#     켜고 끌 때 관리 서버만 다시 연다 (메인 서버·워커는 그대로).
# 포트는 .env 의 ADMIN_PORT 로 바꿀 수 있다.

import asyncio
import contextlib
import os
import socket
import sys
import time
import webbrowser

import uvicorn
from fastapi import FastAPI

from api import admin_router

ADMIN_HOST = "127.0.0.1"
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8100"))
# 서버를 켜면 관리 화면을 브라우저로 자동으로 연다. 끄려면 .env 에 TRADEROS_OPEN_BROWSER=0
OPEN_BROWSER = os.getenv("TRADEROS_OPEN_BROWSER", "1").strip().lower() not in ("0", "false", "no", "off")
ALREADY_OPEN_WAIT = 8   # 이미 열려 있던 관리 화면 탭이 다시 붙는지 기다리는 시간(초)

admin_app = FastAPI(title="TraderOS 관리", docs_url=None, redoc_url=None, openapi_url=None)
admin_app.include_router(admin_router.login_router)   # 로그인 화면·로그인 (원격일 때 로그인 전에도 열림)
admin_app.include_router(admin_router.router)

_current: dict = {"server": None, "task": None, "host": None}


class _EmbeddedServer(uvicorn.Server):
    """Ctrl+C 같은 종료 신호는 메인 서버(:8000)가 받게 하고, 이 서버는 신호 처리기를 건드리지 않음."""

    def install_signal_handlers(self) -> None:  # uvicorn 0.29 미만
        pass

    @contextlib.contextmanager
    def capture_signals(self):  # uvicorn 0.29 이상
        yield


def _bind_host() -> str:
    from services import admin_remote
    return "0.0.0.0" if admin_remote.cfg()["enabled"] else ADMIN_HOST


def _make_socket(host: str) -> socket.socket:
    """직접 포트를 열어 uvicorn 에 넘김 - 포트를 못 열어도 uvicorn 이 프로세스를 끝내지(sys.exit) 않게"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)   # 다른 프로그램이 같은 포트를 가로채지 못하게
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, ADMIN_PORT))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.setblocking(False)
    return sock


def _serve(host: str) -> tuple[_EmbeddedServer | None, asyncio.Task | None]:
    try:
        sock = _make_socket(host)
    except OSError as e:
        print(f"[admin] 관리 화면 포트 {ADMIN_PORT} 을(를) 열지 못했습니다: {e} (다른 프로그램이 쓰는 중인지 확인)")
        return None, None
    config = uvicorn.Config(admin_app, log_level="warning", lifespan="off")
    server = _EmbeddedServer(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    _current.update(server=server, task=task, host=host)
    return server, task


def start_admin_server() -> tuple[_EmbeddedServer, asyncio.Task]:
    host = _bind_host()
    server, task = _serve(host)
    print(f"[admin] 관리 화면: http://{ADMIN_HOST}:{ADMIN_PORT}/"
          + ("  (원격 접속 켜짐 - 같은 네트워크·Tailscale 에서 비밀번호로)" if host != ADMIN_HOST else ""))
    if OPEN_BROWSER and server:
        asyncio.create_task(_open_browser_when_ready(server))
    return server, task


async def rebind() -> None:
    """원격 접속을 켜고 끌 때 관리 서버만 다시 연다"""
    host = _bind_host()
    if host == _current.get("host"):
        return
    await asyncio.sleep(0.5)                  # 설정 저장 응답이 먼저 나가게
    old_server, old_task = _current.get("server"), _current.get("task")
    if old_server:
        old_server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(old_task, timeout=5)
    for _ in range(10):
        server, _task = _serve(host)
        if server:
            print(f"[admin] 관리 화면을 {host}:{ADMIN_PORT} 로 다시 열었습니다"
                  + (" (원격 접속 켜짐)" if host != ADMIN_HOST else " (이 PC에서만)"))
            return
        await asyncio.sleep(1)
    print("[admin] ⚠️ 관리 화면을 다시 열지 못했습니다 - 서버를 재시작하세요.")


async def _open_browser_when_ready(server: "_EmbeddedServer") -> None:
    """관리 서버가 뜨면 브라우저로 연다.
    서버를 재시작할 때마다 탭이 늘어나지 않게, 이미 열려 있던 관리 화면이 몇 초 안에
    다시 접속해 오면(화면이 4초마다 ping) 새로 열지 않는다."""
    for _ in range(100):                 # 최대 10초 - 포트를 못 열었으면 그냥 포기
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.1)
    else:
        return
    since = time.time()
    await asyncio.sleep(ALREADY_OPEN_WAIT)
    if (admin_router.RUNTIME.get("page_seen_at") or 0) >= since:
        return
    url = f"http://{ADMIN_HOST}:{ADMIN_PORT}/"
    try:
        await asyncio.to_thread(webbrowser.open, url)
    except Exception as e:
        print(f"[admin] 브라우저를 열지 못했습니다 ({e}). 직접 {url} 을 여세요.")


async def stop_admin_server(server: _EmbeddedServer, task: asyncio.Task) -> None:
    # 원격 접속을 켜고 끄면 서버가 바뀌므로 지금 떠 있는 것을 끈다
    server, task = _current.get("server") or server, _current.get("task") or task
    if not server:
        return
    server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, timeout=5)
