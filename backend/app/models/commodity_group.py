import uuid

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class CommodityGroup(Base, CreatedAtMixin):
    """Reference data: the shared commodity group taxonomy (~2,000 groups), plus
    groups an organization has for itself (e.g. Acme's "Office Supplies")."""

    __tablename__ = "commodity_groups"

    # IDs are fixed reference values, not auto-generated. Shared groups come from
    # the seed data; organization-specific groups use IDs from 100001 up.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # None = shared by all organizations; set = visible to that organization only.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
