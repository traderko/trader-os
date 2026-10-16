# api/grid_router.py
#
# 구간매매 (웹 화면 '구간' 탭). 계산·주문은 services/grid.py (텔레그램 /grid 도 같은 함수), 실제 주문은 계좌 워커.
#
#   GET  /accounts/{번호}/grid/ktr?symbol=nasdaq&source=mt5|oanda&tf=2m&session=auto|asia|euro|us
#   GET  /accounts/{번호}/grid/ktr-table?symbol=nasdaq&source=mt5   장 3개 × 시간봉 5개 표
#   POST /accounts/{번호}/grid/plan      미리보기 (주문 안 함). 일반 물타기는 n 을 비우면 자동, 막히면 {invalid, options, n_auto}
#   POST /accounts/{번호}/grid/execute   주문 - 거래 잠금 중이면 거절, 지금 시세로 다시 계산해서 넣음
#   GET  /accounts/{번호}/grid/list      진행 중인 구간매매
#   GET  /accounts/{번호}/grid/detail?magic=…  묶음 상세 (구간마다 보유·대기·청산, 손절·다음 구간 손익)
#   POST /accounts/{번호}/grid/close     {magic, pending_only}

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from services import grid
from services.account import get_account_or_default

router = APIRouter(prefix="/accounts")


async def _account(db, account_number: int):
    acc = await get_account_or_default(db, account_number)
    if acc is None:
        raise HTTPException(404, f"계좌를 찾지 못했습니다: {account_number}")
    return acc


async def _run(coro, keep_invalid: bool = False):
    try:
        return await coro
    except grid.GridPlanInvalid as e:
        if keep_invalid:          # 미리보기: 막힌 이유 + N별 비교표를 그대로 돌려줌 (주문 실행은 400)
            return {"invalid": str(e), **e.data}
        raise HTTPException(400, str(e))
    except grid.GridLocked as e:
        raise HTTPException(423, str(e))
    except grid.GridError as e:
        raise HTTPException(400, str(e))


@router.get("/{account_number}/grid/ktr")
async def grid_ktr(account_number: int, symbol: str, source: str = "mt5", tf: str = "2m",
                   session: str = "auto", db: AsyncSession = Depends(get_db)):
    acc = await _account(db, account_number)
    return await _run(grid.compute_ktr(acc.id, symbol, symbol, "oanda" if source == "oanda" else "mt5", tf,
                                       None if session == "auto" else session))


@router.get("/{account_number}/grid/ktr-table")
async def grid_ktr_table(account_number: int, symbol: str, source: str = "mt5", db: AsyncSession = Depends(get_db)):
    acc = await _account(db, account_number)
    return await _run(grid.ktr_table(acc.id, symbol, "oanda" if source == "oanda" else "mt5"))


@router.post("/{account_number}/grid/plan")
async def grid_plan(account_number: int, body: dict, db: AsyncSession = Depends(get_db)):
    acc = await _account(db, account_number)
    return await _run(grid.make_plan(acc.id, body), keep_invalid=True)


@router.post("/{account_number}/grid/execute")
async def grid_execute(account_number: int, body: dict, db: AsyncSession = Depends(get_db)):
    acc = await _account(db, account_number)
    return await _run(grid.execute(db, acc, body, "웹"))


@router.get("/{account_number}/grid/list")
async def grid_list(account_number: int, db: AsyncSession = Depends(get_db)):
    acc = await _account(db, account_number)
    return await _run(grid.grids(acc.id))


@router.get("/{account_number}/grid/detail")
async def grid_detail(account_number: int, magic: int, db: AsyncSession = Depends(get_db)):
    acc = await _account(db, account_number)
    return await _run(grid.detail(db, acc.id, magic))


@router.post("/{account_number}/grid/close")
async def grid_close(account_number: int, body: dict, db: AsyncSession = Depends(get_db)):
    acc = await _account(db, account_number)
    return await _run(grid.close(acc.id, int(body.get("magic") or 0), bool(body.get("pending_only"))))
