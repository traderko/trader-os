# init_db.py

from db.models.trade import Trade
from db.models.trade_note import TradeNote
from db.models.trade_lock import TradeLock
from db.models.trade_image import TradeImage
from db.models.account import Account
from db.models.broker import Broker
from db.models.trade import Trade
from db.models.trade_tag import TradeTag
from db.models.trade_tag_map import TradeTagMap
from db.models.economic_event import EconomicEvent
# SQLite(배포판)는 alembic 없이 create_all로 전체 테이블을 만들기 때문에 나머지 모델도 등록
from db.models.trade_lock_phrase import TradeLockPhrase
from db.models.signal_edge_sheet import SignalEdgeSheet
from db.models.account_event import AccountEvent
# db/models/strategy.py 는 "trades" 테이블 이름이 trade.py와 겹쳐서 일부러 넣지 않음 (백테스트용 별도 모델)

from db.base import Base
from db.session import engine

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)