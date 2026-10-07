"""add session lock (economic_events, trade_lock_phrases.phrase_type)

Revision ID: 7c4e2a9b1d35
Revises: 06efe86660bf
Create Date: 2026-10-03 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c4e2a9b1d35'
down_revision: Union[str, Sequence[str], None] = '06efe86660bf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # init_db의 create_all이 서버 기동 시 테이블을 먼저 만들었을 수도 있어서 있으면 건너뜀
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("economic_events"):
        op.create_table(
            "economic_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("event_name", sa.String(), nullable=False),
            sa.Column("currency", sa.String(), nullable=False),
            sa.Column("impact", sa.Integer(), nullable=False),
            sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("broker_date", sa.Date(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_name", "event_time", name="uq_economic_event_name_time"),
        )
        op.create_index("ix_economic_events_broker_date", "economic_events", ["broker_date"])

    # 기존 문구는 전부 정기 재확인용(checkin)으로 채워짐
    op.add_column(
        "trade_lock_phrases",
        sa.Column("phrase_type", sa.String(), server_default="checkin", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("trade_lock_phrases", "phrase_type")
    op.drop_index("ix_economic_events_broker_date", table_name="economic_events")
    op.drop_table("economic_events")
