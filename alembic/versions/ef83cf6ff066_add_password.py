"""add password

Revision ID: ef83cf6ff066
Revises: 51019814cb87
Create Date: 2026-04-08 01:35:33.592402

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ef83cf6ff066'
down_revision: Union[str, Sequence[str], None] = '51019814cb87'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
