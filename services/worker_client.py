# services/worker_client.py
#
# 계좌별 워커(트레이드락+MT5 미니 API, :9001~)에 요청을 포워딩하는 공용 헬퍼.
# main.py나 다른 라우터가 서로를 import하지 않도록, 아무것도 의존하지 않는
# 독립 모듈로 둔다 (순환 import 방지).

from fastapi import HTTPException
import httpx

WORKER_PORT_BASE = 9000


def worker_port_for(account_id: int) -> int:
    return WORKER_PORT_BASE + account_id


def worker_base_url(account_id: int) -> str:
    return f"http://127.0.0.1:{worker_port_for(account_id)}"


async def worker_get(account_id: int, path: str, params: dict | None = None):
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(f"{worker_base_url(account_id)}{path}", params=params)
        except httpx.ConnectError:
            raise HTTPException(503, f"워커(account_id={account_id})에 연결할 수 없습니다.")
    if res.status_code != 200:
        raise HTTPException(res.status_code, res.text)
    return res.json()


async def worker_post(account_id: int, path: str, json: dict):
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.post(f"{worker_base_url(account_id)}{path}", json=json)
        except httpx.ConnectError:
            raise HTTPException(503, f"워커(account_id={account_id})에 연결할 수 없습니다.")
    if res.status_code != 200:
        raise HTTPException(res.status_code, res.text)
    return res.json()