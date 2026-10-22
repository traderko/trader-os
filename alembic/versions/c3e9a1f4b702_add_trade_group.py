"""add trades.group_id (거래내역에서 직접 지정한 묶음 번호)

Revision ID: c3e9a1f4b702
Revises: b1d7e4a2c901
Create Date: 2026-10-22 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e9a1f4b702'
down_revision: Union[str, Sequence[str], None] = 'b1d7e4a2c901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("trades")}
    if "group_id" not in cols:                    # create_all 이 먼저 만들었으면 건너뜀
        with op.batch_alter_table("trades") as b:
            b.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
    idx = {i["name"] for i in sa.inspect(bind).get_indexes("trades")}
    if "ix_trades_group_id" not in idx:
        op.create_index("ix_trades_group_id", "trades", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_trades_group_id", table_name="trades")
    with op.batch_alter_table("trades") as b:
        b.drop_column("group_id")
