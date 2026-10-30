# services/market_time.py
#
# 장 시작 시각 중 '유로장(런던) 시작'은 런던 시간 08:00 으로 계산한다.
#   브로커 서버 시간(뉴욕 + 7시간)은 미국 서머타임만 따라가서, 미국·영국 서머타임 날짜가 다른 기간
#   (3월 둘째 일요일 ~ 마지막 일요일, 10월 마지막 일요일 ~ 11월 첫 일요일)에는 브로커 10:00 이 런던 07:00 이 됨.
#   → 그 기간에는 런던 08:00 = 브로커 11:00 (한국 17:00). 트레이딩뷰 시가박스와 같은 기준.
#   아시아장 시작(브로커 01:00)·미장 개장(뉴욕 09:30)은 미국 기준이라 브로커 시간 그대로 맞음.

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
EURO_OPEN_BROKER_DEFAULT = 600          # 브로커 10:00 (평소 = 런던 08:00)


def euro_open_on(d: date) -> datetime:
    """그 날짜(런던 날짜)의 런던 08:00 (aware)"""
    return datetime(d.year, d.month, d.day, 8, 0, tzinfo=LONDON)


def euro_open_broker_minute(broker_day: date) -> int:
    """브로커 날짜의 유로장 시작이 브로커 시계로 몇 분째인지 (보통 600, 서머타임 어긋나는 주는 660)"""
    t = euro_open_on(broker_day)
    b = t.astimezone(NY).replace(tzinfo=None) + timedelta(hours=7)
    return (b.date() - broker_day).days * 1440 + b.hour * 60 + b.minute


def euro_open_before(now: datetime) -> datetime:
    """now 이전 가장 최근 유로장 시작 (런던 평일)"""
    d = now.astimezone(LONDON).date()
    for i in range(9):
        day = d - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        t = euro_open_on(day)
        if t <= now:
            return t
    raise ValueError("유로장 시작을 찾지 못했습니다.")


def euro_open_after(now: datetime) -> datetime:
    """now 이후 가장 가까운 유로장 시작 (런던 평일)"""
    d = now.astimezone(LONDON).date()
    for i in range(9):
        day = d + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        t = euro_open_on(day)
        if t > now:
            return t
    return now + timedelta(days=7)
