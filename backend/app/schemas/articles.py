import uuid
from datetime import datetime
from decimal import Decimal

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    article_number: str
    supplier_id: uuid.UUID
    supplier_name: str | None = None
    description: str
    unit_price: Decimal
    currency: str
    unit: str
    quantity: Decimal
    created_at: datetime


class ArticlePage(BaseModel):
    """A page of articles plus the totals the UI needs to render pagination."""

    items: list[ArticleOut]
    total: int
    limit: int
    offset: int


# --- Catalog suggestions (camelCase on the wire, like the extraction) ---------

class SuggestLine(BaseModel):
    """A line of the request being created."""

    model_config = ConfigDict(populate_by_name=True)

    position_description: str = Field(alias="positionDescription")
    amount: float
    unit: str
    unit_price: float = Field(alias="unitPrice")


class SuggestRequest(BaseModel):
    lines: list[SuggestLine]


class CatalogOffer(BaseModel):
    """One supplier's negotiated price for a catalog item."""

    model_config = ConfigDict(populate_by_name=True)

    article_number: str = Field(alias="articleNumber")
    supplier_name: str = Field(alias="supplierName")
    supplier_vat_id: str | None = Field(alias="supplierVatId")
    unit_price: float = Field(alias="unitPrice")
    unit: str


class CatalogSuggestion(BaseModel):
    """A catalog item that matches one request line; offers sorted cheapest first."""

    model_config = ConfigDict(populate_by_name=True)

    line_index: int = Field(alias="lineIndex")  # 0-based position in the request's lines
    article_description: str = Field(alias="articleDescription")
    match: Literal["same", "comparable"]
    reason: str
    offers: list[CatalogOffer]
    # (quoted unit price - best catalog price) x amount; only when the units match and it's cheaper
    saving: float | None = None
