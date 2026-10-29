# services/sltp.py
#
# 여러 포지션의 SL/TP 한꺼번에 바꾸기 (웹 포지션 탭 · 텔레그램 🛑 SL / 🎯 TP 버튼이 같이 씀)
#
# MT5 는 포지션 하나씩 주문을 보내야 해서 50개가 넘으면 한 번의 요청이 10초를 넘겨 '시간 초과'가 났다.
# (워커는 시간 초과 뒤에도 계속 바꾸고 있어서, 실제로는 대부분 적용됐는데 '실패'로 보이기도 함)
#   → CHUNK 개씩 나눠 보내고 (요청마다 넉넉한 시간), 진행 상황을 알려 주고,
#     그래도 시간 초과가 난 묶음은 마지막에 포지션을 다시 읽어 실제로 바뀌었는지 확인한다.

import asyncio
from typing import Awaitable, Callable

from services.worker_client import worker_get, worker_post

CHUNK = 10               # 한 번에 보낼 포지션 수
SEC_PER_TICKET = 3.0     # 요청 시간 한도 = 10초 + 포지션당 이만큼


def _same(a, b) -> bool:
    a, b = float(a or 0), float(b or 0)
    return abs(a - b) <= max(1e-9, abs(b) * 1e-7)


async def apply_sltp(account_id: int, field: str, tickets: list[int], price: float,
                     progress: Callable[[int, int], Awaitable[None]] | None = None) -> dict:
    """field: 'sl' | 'tp'. 반환 {"success": [티켓], "failed": [{"ticket", "reason"}]} (워커와 같은 모양)"""
    path = "/positions/set-sl" if field == "sl" else "/positions/set-tp"
    key = "sl_price" if field == "sl" else "tp_price"
    tickets = list(dict.fromkeys(int(t) for t in tickets))
    ok: list[int] = []
    failed: dict[int, str] = {}
    unsure: list[int] = []           # 시간 초과 - 워커가 계속 처리 중일 수 있음
    done = 0
    for i in range(0, len(tickets), CHUNK):
        part = tickets[i:i + CHUNK]
        try:
            res = await worker_post(account_id, path, {"tickets": part, key: price},
                                    timeout=10 + SEC_PER_TICKET * len(part))
            ok += [int(t) for t in res.get("success") or []]
            for f in res.get("failed") or []:
                failed[int(f.get("ticket"))] = str(f.get("reason") or "")
        except Exception as e:
            if _is_timeout(e):
                unsure += part
            else:
                for t in part:
                    failed[t] = _reason(e)
        done += len(part)
        if progress:
            try:
                await progress(done, len(tickets))
            except Exception:
                pass
    if unsure:
        # 늦게 끝난 것까지 기다렸다가 실제 값으로 확인 (최대 약 20초)
        left = set(unsure)
        for wait in (2, 3, 5, 10):
            await asyncio.sleep(wait)
            try:
                rows = await worker_get(account_id, "/positions-all")
            except Exception:
                continue
            now = {int(p["ticket"]): p for p in rows}
            for t in list(left):
                p = now.get(t)
                if p is None:
                    failed[t] = "포지션을 찾을 수 없음 (청산됨?)"
                    left.discard(t)
                elif _same(p.get(field), price):
                    ok.append(t)
                    left.discard(t)
            if not left:
                break
        for t in left:
            failed[t] = "시간 초과 - 적용됐는지 확인 안 됨"
    return {"success": ok, "failed": [{"ticket": t, "reason": r} for t, r in failed.items() if t not in ok]}


def _is_timeout(e: Exception) -> bool:
    import httpx
    return isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)) or "timeout" in type(e).__name__.lower()


def _reason(e: Exception) -> str:
    d = getattr(e, "detail", None)
    return str(d or e or type(e).__name__)[:200]
