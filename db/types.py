# db/types.py
#
# 시간대가 있는 날짜시간 컬럼.
#   PostgreSQL은 timestamptz로 시간대를 보존하지만, SQLite는 시간대를 버리고 저장해서
#   읽을 때 "시간대 없는" 값이 나온다 → 비교·변환 코드가 깨질 수 있음.
#   그래서 저장할 때 UTC로 바꾸고, 읽을 때 UTC 시간대를 붙여서 두 DB에서 똑같이 동작하게 한다.
#   PostgreSQL에서는 기존 DateTime(timezone=True)와 동작이 같다.

from datetime import timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class TZDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def __init__(self, timezone: bool = True, **kw):  # 기존 DateTime(timezone=True) 호출 모양 유지
        super().__init__(**kw)

    def process_bind_param(self, value, dialect):
        if value is not None and dialect.name == "sqlite" and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        # PostgreSQL 결과는 건드리지 않음 (기존 동작 그대로)
        if value is not None and dialect.name == "sqlite" and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
