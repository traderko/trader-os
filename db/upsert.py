# db/upsert.py
#
# "있으면 갱신, 없으면 추가"(INSERT … ON CONFLICT DO UPDATE)를 PostgreSQL·SQLite 둘 다에서 쓰기 위한 도우미.
# 두 방언 모두 on_conflict_do_update(index_elements=[...], set_={...}) 형태를 지원한다.
# (constraint="이름" 지정은 PostgreSQL 전용이라 쓰지 말 것)

from db.session import IS_SQLITE

if IS_SQLITE:
    from sqlalchemy.dialects.sqlite import insert as upsert_insert  # noqa: F401
else:
    from sqlalchemy.dialects.postgresql import insert as upsert_insert  # noqa: F401
