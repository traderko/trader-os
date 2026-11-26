"""add trades.balance_before (청산 직전 잔고 - 거래 손익 % 기준)

Revision ID: a5c7e9d1b3f2
Revises: f2c8d6b4e130
Create Date: 2026-11-26 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a5c7e9d1b3f2'
down_revision: Union[str, Sequence[str], None] = 'f2c8d6b4e130'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}
    if "balance_before" not in cols:              # create_all 이 먼저 만들었으면 건너뜀
        with op.batch_alter_table("trades") as b:
            b.add_column(sa.Column("balance_before", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trades") as b:
        b.drop_column("balance_before")
