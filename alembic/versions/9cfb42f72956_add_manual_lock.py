"""add manual_lock

Revision ID: 9cfb42f72956
Revises: 28a5a2b74619
Create Date: 2026-08-04 15:13:15.911413

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9cfb42f72956'
down_revision: Union[str, Sequence[str], None] = '28a5a2b74619'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column("trade_locks", sa.Column("manual_lock_since", sa.DateTime(), nullable=True))
    op.add_column("trade_locks", sa.Column("manual_lock_until", sa.DateTime(), nullable=True))
 
 
def downgrade():
    op.drop_column("trade_locks", "manual_lock_until")
    op.drop_column("trade_locks", "manual_lock_since")
 