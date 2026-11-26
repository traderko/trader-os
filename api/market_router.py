# api/market_router.py
#
# 거래 시간 (휴장 · 조기 종료 알림) - 계산 · 알림은 services/market_watch.py
#   POST /market/sessions                    SessionReporter EA(mt5_ea/sessionReporter.mq5)가 10분마다 보냄
#        {"account_number", "server", "offset", "now_utc", "symbols": {종목: [[시작, 끝] UTC epoch, ...]}}
#   GET  /market/sessions?account_number=    받은 거래 시간과 앞으로 3일 안 종료 (확인용)

import time

from fastapi import APIRouter, Depends, Request

from api.trade_lock_router import verify_api_key

router = APIRouter(prefix="/market", tags=["market"])


@router.post("/sessions")
async def post_sessions(request: Request, _=Depends(verify_api_key)):
    from services import market_watch
    payload = await request.json()
    n = market_watch.put_sessions(payload)
    return {"ok": True, "symbols": n}


@router.get("/sessions")
async def get_sessions(account_number: str):
    from services import market_watch
    market_watch._load()
    now = time.time()
    out = {}
    for key, s in market_watch._sessions.items():
        an, sym = key.split("|", 1)
        if an != str(account_number):
            continue
        out[sym] = {"updated": s["at"], "closes": market_watch.closes(s["w"], now, 3 * 86400)}
    return {"account_number": account_number, "symbols": out}
