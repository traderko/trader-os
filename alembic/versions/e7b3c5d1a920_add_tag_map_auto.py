"""add trade_tag_map.auto (자동으로 추정한 근거 - services/tag_guess.py)

Revision ID: e7b3c5d1a920
Revises: d4f1c2b8e310
Create Date: 2026-11-23 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b3c5d1a920'
down_revision: Union[str, Sequence[str], None] = 'd4f1c2b8e310'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trade_tag_map")}
    if "auto" not in cols:                        # create_all 이 먼저 만들었으면 건너뜀
        with op.batch_alter_table("trade_tag_map") as b:
            b.add_column(sa.Column("auto", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trade_tag_map") as b:
        b.drop_column("auto")
