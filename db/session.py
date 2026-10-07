# db/session.py
#
# DB는 SQLite 파일 하나 (data/traderos.db) - 따로 설치할 것 없음.
#   예전 PostgreSQL 데이터는 tools/pg_to_sqlite.py 로 한 번 옮기면 된다.
#   (.env 에 DATABASE_URL 을 넣으면 그 주소를 쓰지만, 이제는 지원하지 않는 방식)

import os

from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SQLITE_PATH = os.path.join(DATA_DIR, "traderos.db")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    os.makedirs(DATA_DIR, exist_ok=True)
    DATABASE_URL = "sqlite+aiosqlite:///" + SQLITE_PATH.replace("\\", "/")
    print(f"[db] {SQLITE_PATH}")

IS_SQLITE = DATABASE_URL.startswith("sqlite")
DB_ECHO = os.getenv("DB_ECHO", "0") == "1"   # SQL 로그가 필요할 때만 .env에 DB_ECHO=1

engine = create_async_engine(
    DATABASE_URL,
    echo=DB_ECHO,
    # SQLite: 메인 서버와 계좌별 워커가 같은 파일을 동시에 쓰므로 잠겨 있으면 최대 30초 기다림
    connect_args={"timeout": 30} if IS_SQLITE else {},
)

if IS_SQLITE:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")      # 읽기와 쓰기가 서로 막지 않게 (여러 프로세스 동시 접근)
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
