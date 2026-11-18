# api/hook_router.py
#
# 폰 알림 받기 - 안드로이드 MacroDroid 가 토스 앱 알림을 그대로 보내면 토스뱅크 잔고(은행 '직접 입력' 칸)를 갱신
#   POST /hook/bank?token=…&name=토스뱅크   본문: 알림 제목 + 내용 (그냥 글자)
#   token: 관리 화면 '지갑' 탭에 보이는 값 (settings "banks.hook_token") - 틀리면 403
#   /hook/ 은 '비밀번호 로그인(public)' 방식에서도 쿠키 없이 들어오게 열어 둠 (대신 token 으로 막음)
#
# 토스 알림 모양 (2026-11 실제 화면 기준)
#   토스뱅크 체크카드 : "6,000원 결제 | (주)공차코리아 잔액 2,664,301원"  ← 잔액이 있으면 그 값으로 맞춤
#                       "3,900원 결제 | 메가커피(송정점)"                  ← 잔액 없으면 빼기
#                       "100원 캐시백"                                     ← 더하기
#   입출금            : "40,000원 출금 | 내 토스뱅크 통장 → 고예준"         ← 빼기
#                       "116,036원 입금 | PAYOUT → 내 토스뱅크 통장"        ← 더하기
#   토스페이          : "13,900원 결제 완료 토스뱅크 · 맥도날드 …"          ← 토스뱅크에서 나간 것만 빼기
#   그 밖(소비 분석 · 현금영수증 · 복권 …)은 넘김
#   더하기·빼기는 오차가 쌓일 수 있어서, '잔액'이 찍힌 알림이 오면 그 값으로 다시 맞춤
#   같은 금액이 2분 안에 또 오면(카드·토스페이 알림이 겹치는 경우) 한 번만 셈
import hmac
import re
import time

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

_N = r"([0-9][0-9,]*)\s*원"
_BAL = re.compile(r"잔액\s*[:：]?\s*" + _N)
_RULES = [  # (정규식, 부호, 이름) - 위에서부터 처음 맞는 것 하나
    (re.compile(_N + r"\s*(?:결제\s*)?취소"), +1, "결제 취소"),
    (re.compile(_N + r"\s*캐시백"), +1, "캐시백"),
    (re.compile(_N + r"\s*입금"), +1, "입금"),
    (re.compile(_N + r"\s*출금(?!\s*안내)"), -1, "출금"),
    (re.compile(_N + r"\s*결제\s*완료"), -1, "토스페이 결제"),     # 토스뱅크에서 나간 것만 (아래에서 확인)
    (re.compile(_N + r"\s*결제(?!\s*(?:완료|취소|예정|일))"), -1, "카드 결제"),
]
_recent: list[tuple[float, int, float]] = []          # (시각, 부호, 금액) - 겹치는 알림 거르기
DUP_SEC = 120


def parse_toss(text: str) -> tuple[str, float, str] | None:
    """('set', 잔액, 이유) | ('add', ±금액, 이유) | None"""
    t = " ".join(text.split())
    m = _BAL.findall(t)
    if m:
        return "set", float(m[-1].replace(",", "")), "잔액"
    for rx, sign, why in _RULES:
        mm = rx.search(t)
        if not mm:
            continue
        if why == "토스페이 결제" and "토스뱅크" not in t:
            return None                                # 다른 카드·계좌로 낸 토스페이 결제
        return "add", sign * float(mm.group(1).replace(",", "")), why
    return None


@router.post("/hook/bank")
async def hook_bank(request: Request, token: str = "", name: str = "토스뱅크"):
    from services import banks
    good = banks.hook_token()
    if not token or not hmac.compare_digest(token, good):
        raise HTTPException(403, "token 이 맞지 않습니다")
    body = (await request.body()).decode("utf-8", "replace")[:2000]
    p = parse_toss(body)
    if not p:
        return {"ok": False, "reason": "잔고와 관계없는 알림이라 넘김"}
    kind, v, why = p
    name = (name or "토스뱅크").strip()[:20]
    cur = next((m for m in banks.cfg()["manual"] if m["name"] == name), None)
    if kind == "set":
        amount = v
        t = " ".join(body.split())                     # 같은 결제의 토스페이 알림이 뒤따라와도 또 빼지 않게
        for rx, sign, _ in _RULES:
            mm = rx.search(t)
            if mm:
                _recent.append((time.time(), sign, float(mm.group(1).replace(",", ""))))
                break
    else:
        now = time.time()
        _recent[:] = [x for x in _recent if now - x[0] < DUP_SEC]
        sign = 1 if v > 0 else -1
        if any(s == sign and abs(a - abs(v)) < 0.5 for _, s, a in _recent):
            return {"ok": False, "reason": f"{why} {abs(v):,.0f}원 - 2분 안에 같은 금액 알림이 이미 있어 한 번만 셈"}
        _recent.append((now, sign, abs(v)))
        if cur is None:
            return {"ok": False, "reason": f"'{name}' 잔액을 아직 모름 - 관리 화면에 한 번 넣거나 '잔액'이 찍힌 알림을 기다림"}
        amount = cur["amount"] + v
    banks.set_manual_one(name, amount, via="알림")
    try:
        from services import wallets
        wallets._cache.clear()
    except Exception:
        pass
    return {"ok": True, "name": name, "amount": amount, "why": why, "change": None if kind == "set" else v}
