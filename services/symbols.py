# services/symbols.py
#
# 종목 묶음 - 브로커마다 같은 종목 이름이 달라서 (XAUUSD / XAUUSD+ / GOLD, NAS100+ / US100.b / USTEC ...)
# 이름을 정규화(대문자, 영문·숫자만)해서 아래 접두어로 시작하면 같은 묶음으로 본다.
#
#   웹 화면 종목 버튼, 포지션 조회(symbol=gold 처럼 묶음 key로 조회), 텔레그램 실시간 현황이 같이 씀.
#
# 바꾸고 싶으면 data/settings.json 에 "symbols": [...] 를 같은 모양으로 넣으면 그걸 씀.
#   point: 1랏이 가격 1만큼 움직일 때 손익(달러) - MT5가 계약 크기를 알려주면 그 값을 먼저 쓰고, 이건 예비값

import re

DEFAULT_GROUPS = [
    {"key": "gold", "label": "골드", "icon": "🧈", "point": 100, "names": ["XAUUSD", "GOLD"]},
    {"key": "nasdaq", "label": "나스닥", "icon": "🗽", "point": 1,
     "names": ["NAS100", "US100", "USTEC", "USTECH", "NDX100", "NQ100"]},
    {"key": "oil", "label": "오일", "icon": "🛢️", "point": 100,
     "names": ["USOIL", "USOUSD", "XTIUSD", "WTI", "CRUDE"]},
    {"key": "btc", "label": "비트", "icon": "₿", "point": 1, "names": ["BTCUSD", "BITCOIN", "BTC"]},
]


def norm(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (symbol or "").upper())


def groups() -> list[dict]:
    try:
        from services.settings_service import get_settings
        custom = get_settings().get("symbols")
        if isinstance(custom, list) and custom and all(isinstance(g, dict) and g.get("key") and g.get("names") for g in custom):
            return custom
    except Exception:
        pass
    return DEFAULT_GROUPS


def group_by_key(key: str) -> dict | None:
    return next((g for g in groups() if g["key"] == key), None)


# 묶음마다 이 브로커에서 실제로 쓰는 종목 이름 (포지션을 읽을 때 기억 - worker_client) → 텔레그램 실시간 현황 표시용
_broker_name: dict[str, str] = {}


def remember(symbol: str) -> None:
    g = group_of(symbol)
    if g and symbol:
        _broker_name[g["key"]] = symbol


def display_name(key: str) -> str:
    """종목명 (예: XAUUSD+, NAS100+) - 포지션에서 본 브로커 이름, 없으면 묶음의 대표 이름"""
    if key in _broker_name:
        return _broker_name[key]
    g = group_by_key(key)
    return (g.get("names") or [key])[0] if g else key


def group_of(symbol: str) -> dict | None:
    n = norm(symbol)
    if not n:
        return None
    for g in groups():
        if any(n.startswith(norm(x)) for x in g["names"]):
            return g
    return None
