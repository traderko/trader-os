"""add trades.magic (구간매매 등 주문 묶음 번호 - MT5 매직넘버)

Revision ID: b1d7e4a2c901
Revises: 7c4e2a9b1d35
Create Date: 2026-10-17 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1d7e4a2c901'
down_revision: Union[str, Sequence[str], None] = '7c4e2a9b1d35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("trades")}
    if "magic" not in cols:                       # create_all 이 먼저 만들었으면 건너뜀
        with op.batch_alter_table("trades") as b:
            b.add_column(sa.Column("magic", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trades") as b:
        b.drop_column("magic")
