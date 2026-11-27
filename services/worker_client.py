# services/worker_client.py
#
# 계좌별 워커(트레이드락+MT5 미니 API, :9001~)에 요청을 포워딩하는 공용 헬퍼.
# main.py나 다른 라우터가 서로를 import하지 않도록, 아무것도 의존하지 않는
# 독립 모듈로 둔다 (순환 import 방지).

import asyncio

from fastapi import HTTPException
import httpx

WORKER_PORT_BASE = 9000

# 워커 요청용 클라이언트는 하나를 계속 씀 (이벤트 루프마다 하나)
#   - 예전: 요청마다 httpx.AsyncClient 를 새로 만들었는데, 만들 때마다 SSL 인증서를 읽느라(ssl.create_default_context)
#     Windows 에서 0.5~1초씩 서버 전체가 멈춤 → 요청이 많으면 몇 초씩 멈춰 EA 체결 보고가 늦게 도착 (2026-11 /묶기 로 찾음)
#   - 워커는 http://127.0.0.1 이라 SSL 이 필요 없음 (verify=False 로 인증서도 안 읽음)
_clients: dict[int, httpx.AsyncClient] = {}


def shared_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    c = _clients.get(id(loop))
    if c is None or c.is_closed:
        c = httpx.AsyncClient(verify=False, timeout=10.0,
                              limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
        _clients[id(loop)] = c
    return c


def worker_port_for(account_id: int) -> int:
    return WORKER_PORT_BASE + account_id


def worker_base_url(account_id: int) -> str:
    return f"http://127.0.0.1:{worker_port_for(account_id)}"


async def worker_get(account_id: int, path: str, params: dict | None = None, timeout: float = 10.0):
    """timeout: 기본 10초. MT5 전체 기록을 읽는 요청(청산 직전 잔고 채우기 등)은 길게 줌"""
    try:
        res = await shared_client().get(f"{worker_base_url(account_id)}{path}", params=params, timeout=timeout)
    except httpx.ConnectError:
        raise HTTPException(503, f"워커(account_id={account_id})에 연결할 수 없습니다.")
    if res.status_code != 200:
        raise HTTPException(res.status_code, res.text)
    data = res.json()
    if path == "/account-info":           # 평가금 1분 기록 (평가금 기준 최대 낙폭)
        try:
            from services.equity_log import observe as eq_observe
            eq_observe(account_id, data)
        except Exception:
            pass
    if path == "/positions-all":          # SL/TP 를 언제 고쳤는지 기록 (맞추기에서 '최근 고침')
        try:
            from services.sltp import observe
            from services.symbols import remember
            observe(account_id, data)
            for p in data or []:
                remember(p.get("symbol", ""))
        except Exception:
            pass
    return data


async def worker_post(account_id: int, path: str, json: dict, timeout: float = 10.0):
    try:
        res = await shared_client().post(f"{worker_base_url(account_id)}{path}", json=json, timeout=timeout)
    except httpx.ConnectError:
        raise HTTPException(503, f"워커(account_id={account_id})에 연결할 수 없습니다.")
    if res.status_code != 200:
        raise HTTPException(res.status_code, res.text)
    return res.json()
