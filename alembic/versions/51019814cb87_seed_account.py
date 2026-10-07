"""seed account

Revision ID: 51019814cb87
Revises: 3b155915cc70
Create Date: 2026-04-08 01:16:56.270396

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '51019814cb87'
down_revision: Union[str, Sequence[str], None] = '3b155915cc70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    op.execute(
        """
        INSERT INTO brokers (name, server)
        VALUES ('Hantec Markets', 'HantecMarketsMU-MT5');

        INSERT INTO brokers (name, server)
        VALUES ('Infinox', 'InfinoxLimited-MT5Live');
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    pass
