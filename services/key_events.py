# services/key_events.py
#
# 주요 지표 강조 - 지표 이름에 관리 화면에서 정한 단어(FOMC, 실업수당, 원유, 비농업 …)가 들어 있으면 '주요 지표'.
#   관리 화면 대시보드 '이번 주 지표'에서 ⭐ 로 강조
#   텔레그램 '경제지표 발표 예정' 알림 제목을 ⭐ 주요 지표로
#   트레이딩 지침 '지표·장 시작 전 포지션 보유' 알림에도 ⭐
# 대소문자 구분 없음. einfomax(한글)와 Forex Factory(영어) 이름 둘 다 보도록 기본 단어에 두 가지를 넣어 둠.

from services.settings_service import get_settings


def words() -> list[str]:
    return [w for w in ((get_settings().get("events") or {}).get("highlight") or []) if w.strip()]


def is_key(name: str) -> bool:
    n = (name or "").lower()
    return any(w.lower() in n for w in words())


def star(name: str) -> str:
    """주요 지표면 앞에 ⭐"""
    return f"⭐ {name}" if is_key(name) else name
