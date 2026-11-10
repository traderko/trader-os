"""add trades.sl, trades.tp, trades.close_reason (청산 직전 SL/TP 와 청산 이유)

Revision ID: d4f1c2b8e310
Revises: c3e9a1f4b702
Create Date: 2026-11-10 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4f1c2b8e310'
down_revision: Union[str, Sequence[str], None] = 'c3e9a1f4b702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}
    add = [c for c in (sa.Column("sl", sa.Float(), nullable=True), sa.Column("tp", sa.Float(), nullable=True),
                       sa.Column("close_reason", sa.String(), nullable=True)) if c.name not in cols]
    if add:                                       # create_all 이 먼저 만들었으면 건너뜀
        with op.batch_alter_table("trades") as b:
            for c in add:
                b.add_column(c)


def downgrade() -> None:
    with op.batch_alter_table("trades") as b:
        b.drop_column("close_reason")
        b.drop_column("tp")
        b.drop_column("sl")
