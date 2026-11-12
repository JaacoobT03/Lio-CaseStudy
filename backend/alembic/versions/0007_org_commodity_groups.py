"""organization-specific commodity groups

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03

Commodity groups are shared by all organizations. A group with an
``organization_id`` belongs to that organization only (e.g. Acme's
"Office Supplies"); shared groups keep it NULL.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "commodity_groups", sa.Column("organization_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_commodity_groups_organization_id",
        "commodity_groups",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_commodity_groups_organization_id", "commodity_groups", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_commodity_groups_organization_id", table_name="commodity_groups")
    op.drop_constraint(
        "fk_commodity_groups_organization_id", "commodity_groups", type_="foreignkey"
    )
    op.drop_column("commodity_groups", "organization_id")
