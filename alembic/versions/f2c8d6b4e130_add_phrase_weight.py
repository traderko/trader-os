"""add trade_lock_phrases.weight (확인 문구 중요도 ★1~5 - services/phrase_pick.py)

Revision ID: f2c8d6b4e130
Revises: e7b3c5d1a920
Create Date: 2026-11-25 19:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2c8d6b4e130'
down_revision: Union[str, Sequence[str], None] = 'e7b3c5d1a920'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trade_lock_phrases")}
    if "weight" not in cols:
        with op.batch_alter_table("trade_lock_phrases") as b:
            b.add_column(sa.Column("weight", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trade_lock_phrases") as b:
        b.drop_column("weight")
