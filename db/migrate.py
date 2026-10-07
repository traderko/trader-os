# db/migrate.py
#
# 서버가 켜질 때 DB 구조를 최신으로 맞춘다 (main.py lifespan 에서 호출). 사용자가 명령을 칠 필요 없음.
#
#   alembic_version 표가 없거나 비어 있음 (처음 설치, 또는 예전 배포판으로 만든 DB)
#       → 모델대로 표를 만들고(create_all - 이미 있는 표는 그대로) 최신 버전(head)으로 표시만 한다
#   있음 → alembic upgrade head (새 버전에서 추가된 마이그레이션만 적용. 이미 최신이면 아무것도 안 함)
#
# 앞으로 모델(db/models)을 바꾸면 alembic/versions 에 마이그레이션도 같이 만들어야 한다:
#   python -m alembic revision --autogenerate -m "설명"   (SQLite에서도 돌도록 env.py 가 batch 모드를 켬)
#
# 앱과 같은 DB(db/session.py 의 DATABASE_URL, 없으면 data/traderos.db)를 쓴다.

import os

from sqlalchemy import create_engine, inspect, text

from db.session import BASE_DIR, DATABASE_URL


def _sync_url() -> str:
    return DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")


def run() -> None:
    ini = os.path.join(BASE_DIR, "alembic.ini")
    if not os.path.isfile(ini):
        print("[db] alembic.ini 가 없어 마이그레이션을 건너뜁니다.")
        return

    from alembic import command
    from alembic.config import Config

    cfg = Config(ini)
    cfg.set_main_option("script_location", os.path.join(BASE_DIR, "alembic"))
    cfg.set_main_option("sqlalchemy.url", _sync_url().replace("%", "%%"))
    cfg.attributes["skip_logging"] = True     # alembic.ini 의 로그 설정이 서버 로그를 덮어쓰지 않게

    engine = create_engine(_sync_url())
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            version = None
            if "alembic_version" in tables:
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()

        if version is None:
            import db.init_db  # noqa: F401  (모든 모델 등록)
            from db.base import Base
            Base.metadata.create_all(engine)
            command.stamp(cfg, "head")
            print("[db] DB를 최신 구조로 만들고 버전을 표시했습니다." if not tables
                  else "[db] 기존 DB에 버전 표시가 없어 지금 구조를 최신 버전으로 표시했습니다.")
        else:
            command.upgrade(cfg, "head")
    finally:
        engine.dispose()
