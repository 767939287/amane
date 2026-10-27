"""metadata plot original

Revision ID: 044efbf8ca90
Revises: 9054ebacea29
Create Date: 2026-10-27 21:02:55.357991
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "044efbf8ca90"
down_revision: str | None = "9054ebacea29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("metadata", sa.Column("plot_original", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column("metadata", "plot_original")
