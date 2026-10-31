# routers/trade_lock_router.py
# GET  /lock/status?account_id=1
# POST /lock/confirm  {"account_id": 1, "phrase": "..."}

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.account import Account
from db.session import get_db
from services import ea_status
from services import trade_lock_service as svc

router = APIRouter(prefix="/lock", tags=["trade_lock"])

EXPECTED_API_KEY = None  # 예: os.environ.get("TRADE_LOCK_API_KEY")


def verify_api_key(authorization: str | None = Header(default=None)):
    if EXPECTED_API_KEY is None:
        return
    if authorization != f"Bearer {EXPECTED_API_KEY}":
        raise HTTPException(status_code=401, detail="인증 실패")
 
 
async def _resolve_account_id(db: AsyncSession, account_number: int) -> int:
    result = await db.execute(select(Account).where(Account.account_number == str(account_number)))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(404, f"account_number={account_number}에 해당하는 계좌가 없습니다.")
    return account.id
 
 
class ConfirmRequest(BaseModel):
    account_number: int
    phrase: str
 
 
class ManualLockRequest(BaseModel):
    account_number: int
    minutes: int
 
 
@router.get("/status")
async def lock_status(request: Request, account_number: int, ea: str | None = None, src: str | None = None,
                      db: AsyncSession = Depends(get_db),
                      _=Depends(verify_api_key)):
    account_id = await _resolve_account_id(db, account_number)
    acc = await db.get(Account, account_id)
    # MT5 실행을 꺼 둔 계좌(직접 띄운 MT5)는 서버가 재시작할 수 없으니 '예전 EA - MT5 재시작' 알림을 보내지 않음
    # EA 가 부른 것만 EA 로 셈. 웹 화면(src=web)·브라우저·앱도 같은 주소로 잠금 상태를 읽는데,
    # 버전(ea)을 안 보내서 '예전 EA' 로 잘못 잡혔었음. 예전 EA 는 ea 없이 MT5 WebRequest 로 옴 (브라우저·앱이 아님)
    ua = (request.headers.get("user-agent") or "").lower()
    browser = any(x in ua for x in ("mozilla", "dart", "okhttp", "cfnetwork"))
    if src != "web" and (ea or not browser):
        ea_status.mark(account_id, ea, str(account_number), notify=bool(acc and acc.lock_enabled),
                       ip=request.client.host if request.client else "")   # 관리 화면의 "EA 연결됨 · 버전" 표시용 (ea = EA 버전, 예전 EA 는 안 보냄)
    return await svc.get_lock_state(db, account_id)
 
 
@router.post("/confirm")
async def lock_confirm(req: ConfirmRequest, db: AsyncSession = Depends(get_db), _=Depends(verify_api_key)):
    account_id = await _resolve_account_id(db, req.account_number)
    try:
        return await svc.confirm_unlock(db, account_id, str(req.account_number), req.phrase)
    except svc.PhraseMismatchError as e:
        if e.args and e.args[0] == svc.PHRASE_TYPE_SESSION:
            raise HTTPException(status_code=400, detail="집중 구간입니다. 전용 문구를 정확히 입력해야 합니다.")
        if e.args and e.args[0] == svc.PHRASE_TYPE_CONSEC:
            raise HTTPException(status_code=400, detail="연속 손절 잠금입니다. 손절 문구를 정확히 입력해야 합니다.")
        raise HTTPException(status_code=400, detail="문구가 정확히 일치하지 않습니다.")
    except svc.ManualLockActiveError:
        raise HTTPException(status_code=403, detail="수동 잠금은 확인 문구로 해제할 수 없습니다. 설정한 시간이 지나야 자동 해제됩니다.")
 
 
@router.post("/manual-lock")
async def manual_lock(req: ManualLockRequest, db: AsyncSession = Depends(get_db), _=Depends(verify_api_key)):
    """사용자가 스스로 거는 락. 확인 문구로 못 풂 - 시간 지나야만 자동 해제."""
    account_id = await _resolve_account_id(db, req.account_number)
    try:
        return await svc.set_manual_lock(db, account_id, str(req.account_number), req.minutes)
    except svc.InvalidLockDurationError as e:
        raise HTTPException(status_code=400, detail=str(e))
 