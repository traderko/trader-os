# routers/mt5_gateway_router.py
# 메인 FastAPI(:8000, Flutter/EA가 접근하는 공개 API)에서
# account_id를 보고 해당 워커(:9001, :9002, ...)로 요청을 그대로 전달한다.
#
# WORKER_PORTS는 main.py의 lifespan에서 DB 조회 후 채워짐 (하드코딩 아님).

from fastapi import APIRouter, HTTPException
import httpx
import asyncio

router = APIRouter(prefix="/mt5", tags=["mt5_gateway"])

WORKER_PORTS: dict[int, int] = {}  # account_id -> port, main.py가 startup시 채움


def _worker_base_url(account_id: int) -> str:
    port = WORKER_PORTS.get(account_id)
    if port is None:
        raise HTTPException(404, f"account_id={account_id}에 대한 워커가 없습니다.")
    return f"http://127.0.0.1:{port}"


@router.get("/{account_id}/positions")
async def positions(account_id: int):
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            res = await client.get(f"{_worker_base_url(account_id)}/positions")
        except httpx.ConnectError:
            raise HTTPException(503, f"워커(account_id={account_id})에 연결할 수 없습니다.")
    return res.json()


@router.post("/{account_id}/positions/{ticket}/close")
async def close_position(account_id: int, ticket: int):
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.post(f"{_worker_base_url(account_id)}/positions/{ticket}/close")
        except httpx.ConnectError:
            raise HTTPException(503, f"워커(account_id={account_id})에 연결할 수 없습니다.")
    if res.status_code != 200:
        raise HTTPException(res.status_code, res.text)
    return res.json()


@router.get("/positions/all")
async def positions_all():
    async def fetch(account_id: int):
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                res = await client.get(f"{_worker_base_url(account_id)}/positions")
                return account_id, res.json()
            except Exception as e:
                return account_id, {"error": str(e)}

    results = await asyncio.gather(*[fetch(aid) for aid in WORKER_PORTS.keys()])
    return {aid: data for aid, data in results}