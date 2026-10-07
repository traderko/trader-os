# services/phrase_defaults.py
#
# 확인 문구 기본값. 서버가 켜질 때 종류별로 "한 번만" 넣는다.
#   - 그 종류의 문구가 이미 있으면 넣지 않음 (기존 사용자의 문구는 그대로)
#   - 넣은 기록은 data/settings.json 의 seeded_phrase_types 에 남겨서,
#     사용자가 일부러 다 지운 뒤 재시작해도 다시 생기지 않게 함
# 쉼표·특수기호 없이 써서 입력 실수를 줄임.

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.trade_lock_phrase import TradeLockPhrase
from services.settings_service import get_settings, update_settings

DEFAULT_PHRASES: dict[str, list[str]] = {
    # 정기 재확인
    "checkin": [
        "나는 트레이딩 지침을 확인했고 뇌동매매를 하지 않겠습니다",
    ],
    # 연속 손절 잠금
    "consec_loss": [
        "연속 손절은 시장이 내 생각과 다르다는 신호이니 지금은 한 발 물러서겠습니다",
        "잃은 돈을 바로 되찾으려고 진입하지 않고 계획된 자리만 기다리겠습니다",
        "손절가를 넓히거나 물을 타지 않고 정한 손절을 그대로 지키겠습니다",
        "다음 진입은 평소보다 작은 랏으로 하겠습니다",
        "지금 진입하려는 이유가 계획인지 복구 욕심인지 먼저 확인했습니다",
    ],
    # 집중 구간 공통
    "session": [
        "지표 발표일과 목요일 금요일 아시아장은 되돌림이 클 수 있음을 인지했습니다",
        "오늘은 변동성이 큰 날이므로 계획한 자리에서 계획한 랏만 진입하겠습니다",
        "주간 고가에서 추격 매수하지 않고 주간 저가에서 추격 매도하지 않겠습니다",
    ],
}


async def seed_default_phrases(db: AsyncSession) -> dict[str, int]:
    """종류별로 아직 넣은 적 없고 문구도 없으면 기본 문구를 넣는다. {종류: 넣은 개수}"""
    seeded = set(get_settings().get("seeded_phrase_types") or [])
    added: dict[str, int] = {}

    for ptype, phrases in DEFAULT_PHRASES.items():
        if ptype in seeded:
            continue
        has_any = (await db.execute(
            select(TradeLockPhrase.id)
            .where(TradeLockPhrase.phrase_type == ptype, TradeLockPhrase.is_active == True)
            .limit(1)
        )).first()
        if not has_any:
            existing = set((await db.execute(select(TradeLockPhrase.phrase))).scalars().all())
            new = [p for p in phrases if p not in existing]   # phrase 컬럼이 unique
            for p in new:
                db.add(TradeLockPhrase(phrase=p, phrase_type=ptype, is_active=True))
            added[ptype] = len(new)
        seeded.add(ptype)

    await db.commit()
    if seeded != set(get_settings().get("seeded_phrase_types") or []):
        update_settings({"seeded_phrase_types": sorted(seeded)})
    return added
