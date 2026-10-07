# services/trade_lock_service.py
# 락 상태 판단, 확인 문구 검증. 전부 AsyncSession 기반.
#
# MT5 연속손절 조회는 여기서 하지 않는다 - trade_lock_worker.py가 계좌별
# 독립 프로세스에서 지속 연결을 유지하며 계산해 DB(consec_loss_active)에 기록하고,
# 이 파일은 요청이 올 때마다 그 DB 값 + 실시간 시간대 판단만 조합해서 응답한다.

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.economic_event import EconomicEvent
from db.models.trade_lock import TradeLock
from db.models.trade_lock_phrase import TradeLockPhrase
from mt5.mt5Client import Mt5Client
from services.fcm_service import FCMService, init_firebase
from services.settings_service import WEEKDAY_KR, lock_cfg

KST = ZoneInfo("Asia/Seoul")

# 정기 재확인 규칙(기본: 새벽 00~07시 30분마다, 그 밖에는 2시간봉 마감마다)은 관리 화면 설정
#   lock.checkin_rules / lock.checkin_default
# 재확인 간격, 해제 시간, 집중 구간 요일 등 바꿀 수 있는 값은 관리 화면(/admin) 설정으로 옮김
# → services/settings_service.py 의 DEFAULTS / lock_cfg() 참고

# ── 집중 구간 (아시아장·유로장·미장 등, 관리 화면에서 목록으로 관리) ─────────
# 정한 요일, 또는 고영향 USD 지표가 있는 거래일의 해당 시간에는 전용 문구를 입력해야 거래 가능.
# 시간은 브로커 서버시간 기준이라 서머타임이 자동 반영된다. 예) 아시아장 = 브로커 01:00 ~ 09:59:59
#   서머타임(브로커 UTC+3): KST 07:00 ~ 15:59:59
#   그 외   (브로커 UTC+2): KST 08:00 ~ 16:59:59
# 거래일도 브로커 서버시간 날짜 기준 (목 03:00 KST FOMC → 수요일 거래일).
# 문구: phrase_type "session"은 모든 구간 공통, "session:<구간id>"는 그 구간 전용.
# 구간 목록(아시아장·유로장·미장 등)은 관리 화면 설정 lock.sessions 에서 정한다.

PHRASE_TYPE_CHECKIN = "checkin"
PHRASE_TYPE_SESSION = "session"
PHRASE_TYPE_CONSEC = "consec_loss"   # 연속손절 잠금 전용 (없으면 checkin 문구로 풀림)

REASON_PRIORITY = {
    "restricted_window": 1,
    "session_window": 2,   # 구간 안에서는 정기 재확인 대신 전용 문구 + 구간별 해제 시간
    "consec_loss": 3,
    "manual": 4,
}

REASON_KR = {
    "restricted_window": "정기 재확인",
    "session_window": "집중 구간",
    "consec_loss": "연속손절",
    "manual": "수동 잠금",
}


@dataclass
class SessionWindow:
    """지금 걸려 있는 집중 구간(여러 개가 겹치면 하나로 합침)."""
    start: datetime        # KST
    end: datetime          # KST (미포함)
    causes: list[str]      # 예: ["목요일", "지표: 미국 신규 실업수당청구건수"]
    ids: list[str]         # 설정의 구간 id (예: ["asia"])
    names: list[str]       # 예: ["아시아장"]
    unlock_minutes: int    # 겹치면 가장 짧은 값

    @property
    def key(self) -> str:
        """잠금 해제 기록용. 다른 구간으로 넘어가면 키가 달라져서 다시 잠긴다."""
        return "session_window:" + ",".join(self.ids)

    @property
    def phrase_types(self) -> list[str]:
        """이 구간에서 받는 문구 종류: 모든 구간 공통(session) + 구간 전용(session:<id>)"""
        return [PHRASE_TYPE_SESSION] + [f"{PHRASE_TYPE_SESSION}:{i}" for i in self.ids]

    @property
    def title(self) -> str:
        return "·".join(self.names)


def to_broker_wallclock(dt: datetime) -> datetime:
    """aware datetime → 브로커 서버 벽시계 시각 (naive)."""
    utc_dt = dt.astimezone(timezone.utc)
    return (utc_dt + timedelta(hours=Mt5Client._get_broker_offset(utc_dt))).replace(tzinfo=None)


def broker_wallclock_to_kst(wall: datetime) -> datetime:
    fake_utc = wall.replace(tzinfo=timezone.utc)
    utc_dt = fake_utc - timedelta(hours=Mt5Client._get_broker_offset(fake_utc))
    return utc_dt.astimezone(KST)


async def event_names_on(db: AsyncSession, broker_date: date) -> list[str]:
    result = await db.execute(
        select(EconomicEvent.event_name)
        .where(EconomicEvent.broker_date == broker_date)
        .order_by(EconomicEvent.event_time)
    )
    return list(dict.fromkeys(result.scalars().all()))  # 순서 유지 중복 제거


async def get_session_window(db: AsyncSession, now: datetime) -> SessionWindow | None:
    """지금 걸려 있는 집중 구간(설정 lock.sessions 중 시간·요일/지표일이 맞는 것). 없으면 None."""
    sessions = [x for x in lock_cfg()["sessions"] if x["enabled"]]
    if not sessions:
        return None

    wall = to_broker_wallclock(now)
    minute = wall.hour * 60 + wall.minute
    day = wall.replace(hour=0, minute=0, second=0, microsecond=0)
    events = None  # 필요할 때만 DB 조회

    hits = []
    for x in sessions:
        if not (x["start_min"] <= minute < x["end_min"]):
            continue
        causes = []
        if wall.weekday() in x["weekdays"]:
            causes.append(WEEKDAY_KR[wall.weekday()])
        if x["event_days"]:
            if events is None:
                events = await event_names_on(db, wall.date())
            causes += [f"지표: {name}" for name in events]
        if causes:
            hits.append((x, causes))

    if not hits:
        return None

    causes = list(dict.fromkeys(c for _, cs in hits for c in cs))
    return SessionWindow(
        start=broker_wallclock_to_kst(day + timedelta(minutes=min(x["start_min"] for x, _ in hits))),
        end=broker_wallclock_to_kst(day + timedelta(minutes=max(x["end_min"] for x, _ in hits))),
        causes=causes,
        ids=[x["id"] for x, _ in hits],
        names=[x["name"] for x, _ in hits],
        unlock_minutes=min(x["unlock_minutes"] for x, _ in hits),
    )


def phrase_type_for(reason: str) -> str:
    if reason == "session_window":
        return PHRASE_TYPE_SESSION
    if reason == "consec_loss":
        return PHRASE_TYPE_CONSEC
    return PHRASE_TYPE_CHECKIN


async def accepted_phrase_types(db: AsyncSession, reason: str, session: "SessionWindow | None") -> list[str]:
    """이 잠금을 풀 때 받는 문구 종류.
    연속손절은 전용 문구가 하나라도 있으면 그것만, 없으면 정기 재확인 문구로 풀린다(잠긴 채 못 푸는 상황 방지)."""
    if reason == "session_window" and session is not None:
        return session.phrase_types
    if reason == "consec_loss":
        has = (await db.execute(
            select(TradeLockPhrase.id)
            .where(TradeLockPhrase.phrase_type == PHRASE_TYPE_CONSEC, TradeLockPhrase.is_active == True)
            .limit(1)
        )).first()
        return [PHRASE_TYPE_CONSEC] if has else [PHRASE_TYPE_CHECKIN]
    return [PHRASE_TYPE_CHECKIN]


def _rule_windows(now: datetime, rules: list[dict]) -> list[tuple[datetime, datetime, dict]]:
    """켜진 정기 재확인 규칙들의 now 주변(어제·오늘·내일) 시간대 [(시작, 끝, 규칙)] (KST).
    끝이 시작보다 작으면 자정을 넘기는 구간 (예: 23:00~07:00)."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for r in rules:
        if not r["enabled"]:
            continue
        length = (r["end_min"] - r["start_min"]) % 1440
        for d in (-1, 0, 1):
            start = today + timedelta(days=d, minutes=r["start_min"])
            out.append((start, start + timedelta(minutes=length), r))
    return out


def _block_in(now: datetime, w_start: datetime, w_end: datetime, mode: str, every: int) -> tuple[datetime, int]:
    """[w_start, w_end) 안에서 now가 속한 블록 (시작, 길이 분).
    minutes: 구간 시작부터 every분씩 / bars: MT5 every시간봉 경계(브로커 시간 기준). 구간 경계에서 잘림."""
    if mode == "minutes":
        step = timedelta(minutes=every)
        block_start = w_start + ((now - w_start) // step) * step
        block_end = block_start + step
    else:
        wall = to_broker_wallclock(now)
        bar_wall = wall.replace(hour=(wall.hour // every) * every, minute=0, second=0, microsecond=0)
        block_start = broker_wallclock_to_kst(bar_wall)
        block_end = broker_wallclock_to_kst(bar_wall + timedelta(hours=every))
    block_start = max(block_start, w_start)
    block_end = min(block_end, w_end)
    return block_start, int((block_end - block_start).total_seconds() // 60)


def current_checkin_block(now: datetime) -> tuple[datetime, int]:
    """
    현재 시각이 속한 정기 재확인 블록의 (시작시각 KST, 블록 길이 분) 반환. 블록이 끝나면 다시 잠긴다.
    - 정기 재확인 규칙(lock.checkin_rules) 시간대 안: 그 규칙의 방식으로 나눔 (기본: 새벽 00~07시 30분마다)
    - 그 밖의 시간: lock.checkin_default (기본: MT5 2시간봉 마감마다). 2시간봉일 때:
        서머타임(UTC+3): KST 06·08·10·…시 마감 → 07:00~08:00(1시간), 08~10, 10~12, …, 22~24
        그 외   (UTC+2): KST 07·09·11·…시 마감 → 07~09, 09~11, …, 21~23, 23~24(1시간)
      규칙 시간대 경계와 자정에서 잘리는 블록은 짧아진다.
    """
    now = now.astimezone(KST)
    cfg = lock_cfg()
    windows = _rule_windows(now, cfg["checkin_rules"])

    for w_start, w_end, rule in windows:
        if w_start <= now < w_end:
            return _block_in(now, w_start, w_end, rule["mode"], rule["every"])

    # 규칙에 안 걸리는 시간: 직전 규칙 구간이 끝난 시각 ~ 다음 규칙 구간이 시작하는 시각 (자정에서도 끊음)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    gap_start = max([e for _, e, _ in windows if e <= now] + [midnight])
    gap_end = min([s for s, _, _ in windows if s > now] + [midnight + timedelta(days=1)])
    default = cfg["checkin_default"]
    return _block_in(now, gap_start, gap_end, default["mode"], default["every"])
 
 
async def _get_or_create_lock(db: AsyncSession, account_id: int) -> TradeLock:
    result = await db.execute(select(TradeLock).where(TradeLock.account_id == account_id))

    lock = result.scalar_one_or_none()

    if lock is None:
        lock = TradeLock(account_id=account_id)

        db.add(lock)

        await db.commit()
        await db.refresh(lock)

    return lock
 
 
def determine_current_reason_and_since(
    lock: TradeLock, now: datetime, session: SessionWindow | None = None
) -> tuple[str | None, datetime | None]:
    """(사유, 그 사유가 실제로 시작된 KST 시각) 반환. 우선순위 높은 사유가 이김."""
    candidates = []  # (reason, since_kst)
 
    # 수동 락이 걸려있으면 다른 건 확인할 필요도 없이 최우선으로 확정
    if lock.manual_lock_until and lock.manual_lock_until.replace(tzinfo=KST) > now:
        since = lock.manual_lock_since.replace(tzinfo=KST) if lock.manual_lock_since else now
        return "manual", since
 
    block_start, _ = current_checkin_block(now)
    candidates.append(("restricted_window", block_start))  # 하루 종일 항상 후보로 들어감

    if session is not None:
        candidates.append(("session_window", session.start))
 
    if lock.consec_loss_active:
        since = lock.consec_loss_since.replace(tzinfo=KST) if lock.consec_loss_since else now
        candidates.append(("consec_loss", since))
 
    reason, since = max(candidates, key=lambda item: REASON_PRIORITY[item[0]])

    return reason, since
 
 
def _unlock_minutes_for(reason: str, now: datetime, session: SessionWindow | None = None) -> int:
    if reason == "consec_loss":
        return lock_cfg()["consec_loss_unlock_minutes"]

    if reason == "session_window" and session is not None:
        return session.unlock_minutes
    
    _, minutes = current_checkin_block(now)

    return minutes
 
 
def kst_to_broker_epoch(kst_dt: datetime) -> int:
    """KST 시각을 MT5 서버(브로커) 시간 기준 epoch로 변환."""
    utc_dt = kst_dt.astimezone(timezone.utc)
    offset = Mt5Client._get_broker_offset(utc_dt)
    broker_wallclock = utc_dt + timedelta(hours=offset)

    return int(broker_wallclock.replace(tzinfo=timezone.utc).timestamp())
 
 
async def get_lock_state(db: AsyncSession, account_id: int) -> dict:
    now = datetime.now(KST)
    lock = await _get_or_create_lock(db, account_id)
    session = await get_session_window(db, now)
 
    current_reason, since_kst = determine_current_reason_and_since(lock, now, session)
    locked_since_epoch = kst_to_broker_epoch(since_kst)
    # 해제 기록과 비교할 키: 집중 구간은 어느 구간인지까지 포함 (아시아장에서 풀어도 유로장이 되면 다시 잠김)
    reason_key = session.key if current_reason == "session_window" and session else current_reason

    # 앱이 어떤 문구 목록(checkin/session)을 보여줄지, 왜 잠겼는지 표시할 수 있게 공통으로 붙임
    accepted = await accepted_phrase_types(db, current_reason, session)
    extra = {
        # 앱: phrase_type 으로 /trade-lock/phrases/random?type=… 을 부르면 됨 (checkin | consec_loss | session)
        "phrase_type": PHRASE_TYPE_SESSION if current_reason == "session_window" else accepted[0],
        "phrase_types": accepted,   # 실제로 받는 문구 종류 전체
    }
    # 일일 손실 한도 (services/loss_limit.py) - 오늘 손실률과, 지금 잠금이 그것 때문인지
    from services import loss_limit
    daily = loss_limit.public_info(account_id)
    if daily is not None:
        extra["daily_loss"] = daily
    if current_reason == "manual" and loss_limit.locked_by_limit(account_id, lock.manual_lock_until):
        extra["manual_kind"] = "loss_limit"

    if session is not None:
        extra["session"] = {
            "start": session.start.isoformat(),
            "end": session.end.isoformat(),
            "causes": session.causes,
            "ids": session.ids,
            "names": session.names,
            "unlock_minutes": session.unlock_minutes,
        }
 
    # 수동 락은 confirm_unlock으로 못 풀리므로, unlocked_until 검사 자체를 건너뛰고
    # 항상 locked:true를 반환 (설정한 시간이 지날 때까지 무조건 잠김)
    if current_reason == "manual":
        remaining = int((lock.manual_lock_until.replace(tzinfo=KST) - now).total_seconds())
        return {
            "locked": True, "reason": "manual", "remaining_sec": max(remaining, 0),
            "locked_since_broker_epoch": locked_since_epoch, **extra,
        }
 
    if (
        lock.unlocked_until
        and lock.unlocked_until.replace(tzinfo=KST) > now
        and lock.active_reason == reason_key
    ):
        remaining = int((lock.unlocked_until.replace(tzinfo=KST) - now).total_seconds())

        return {
            "locked": False, "reason": current_reason, "remaining_sec": remaining,
            "locked_since_broker_epoch": locked_since_epoch, **extra,
        }
 
    return {
        "locked": True, "reason": current_reason, "remaining_sec": 0,
        "locked_since_broker_epoch": locked_since_epoch, **extra,
    }
 
class PhraseMismatchError(Exception):
    pass
 
class ManualLockActiveError(Exception):
    """수동 락은 확인 문구로 해제 불가 - 설정한 시간이 지나야만 자동 해제됨."""
    pass
 
class InvalidLockDurationError(Exception):
    pass

async def confirm_unlock(db: AsyncSession, account_id: int, account_number: str, phrase: str) -> dict:
    # 사유에 따라 받는 문구 종류가 달라서(checkin/session) 상태를 먼저 본다
    state = await get_lock_state(db, account_id)

    if not state["locked"]:
        return {"status": "not_needed"}
 
    if state["reason"] == "manual":
        raise ManualLockActiveError()
 
    reason = state["reason"]
    now = datetime.now(KST)
    session = await get_session_window(db, now) if reason == "session_window" else None
    accepted = await accepted_phrase_types(db, reason, session)

    result = await db.execute(
            select(TradeLockPhrase)
            .where(
                TradeLockPhrase.phrase == phrase,
                TradeLockPhrase.is_active == True,
                TradeLockPhrase.phrase_type.in_(accepted),
            )
        )

    if result.scalar_one_or_none() is None:
        raise PhraseMismatchError(accepted[0].split(":")[0])

    if reason == "restricted_window":
        # 입력 시점부터가 아니라 현재 블록 끝(=다음 봉 마감)까지만 해제 → 봉 마감마다 다시 잠김
        block_start, block_minutes = current_checkin_block(now)
        until = block_start + timedelta(minutes=block_minutes)
    else:
        until = now + timedelta(minutes=_unlock_minutes_for(reason, now, session))

    minutes = max(1, round((until - now).total_seconds() / 60))
 
    lock = await _get_or_create_lock(db, account_id)
    lock.unlocked_until = until.replace(tzinfo=None)
    lock.active_reason = session.key if session else reason

    await db.commit()
 
    reason_kr = f"{session.title} 집중 구간" if session else REASON_KR.get(reason, reason)

    init_firebase()

    FCMService().send(
        "거래 잠금 해제됨",
        f"계좌 {account_number}: {reason_kr} 잠금이 {until.strftime('%H:%M')}까지 ({minutes}분) 해제되었습니다.",
        '{"type":"trade_lock_unlocked","account_number":"%s"}' % account_number,
    )
 
    return {
        "status": "unlocked",
        "reason": reason,
        "unlock_minutes": minutes,
        "unlocked_until": lock.unlocked_until.isoformat(),
    }
 
 
async def set_loss_limit_lock(db: AsyncSession, account_id: int, until: datetime) -> None:
    """일일 손실 한도 - until(다음 거래일 06:00)까지 풀 수 없는 잠금. 수동 잠금과 같은 칸을 쓰며,
    이미 더 늦게까지 걸린 수동 잠금이 있으면 그대로 둔다. (알림은 호출한 쪽에서)"""
    now = datetime.now(KST)
    lock = await _get_or_create_lock(db, account_id)
    cur = lock.manual_lock_until.replace(tzinfo=KST) if lock.manual_lock_until else None
    if cur is not None and cur >= until:
        return
    if cur is None or cur <= now:
        lock.manual_lock_since = now.replace(tzinfo=None)
    lock.manual_lock_until = until.astimezone(KST).replace(tzinfo=None)
    await db.commit()


async def set_manual_lock(db: AsyncSession, account_id: int, account_number: str, minutes: int) -> dict:
    max_minutes = lock_cfg()["manual_lock_max_minutes"]
    if minutes <= 0 or minutes > max_minutes:
        raise InvalidLockDurationError(f"1~{max_minutes}분 사이로 설정해야 합니다.")
 
    now = datetime.now(KST)
    lock = await _get_or_create_lock(db, account_id)
    lock.manual_lock_since = now.replace(tzinfo=None)
    lock.manual_lock_until = (now + timedelta(minutes=minutes)).replace(tzinfo=None)
    await db.commit()
 
    init_firebase()
    FCMService().send(
        "거래 잠금 - 수동 잠금 설정됨",
        f"계좌 {account_number}: {minutes}분간 스스로 거래를 잠갔습니다. 확인 문구로도 풀리지 않습니다.",
        '{"type":"trade_lock_manual","account_number":"%s"}' % account_number,
    )
 
    return {
        "status": "locked",
        "minutes": minutes,
        "locked_until": lock.manual_lock_until.isoformat(),
    }
 