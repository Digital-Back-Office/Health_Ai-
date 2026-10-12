"""Identity checks use surname + date of birth + phone; postcode becomes optional

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("patients", "postcode", existing_type=sa.String(length=8), nullable=True)
    op.drop_index("ix_patients_identity", table_name="patients")
    op.create_index("ix_patients_identity", "patients", ["date_of_birth", "phone"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_patients_identity", table_name="patients")
    op.create_index("ix_patients_identity", "patients", ["date_of_birth", "postcode"], unique=False)
    op.execute("UPDATE patients SET postcode = '' WHERE postcode IS NULL")
    op.alter_column("patients", "postcode", existing_type=sa.String(length=8), nullable=False)
