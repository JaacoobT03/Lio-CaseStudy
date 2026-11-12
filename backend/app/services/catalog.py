"""Suggest catalog articles (the organization's negotiated prices) for request lines.

The catalog lists the same item from several suppliers at different prices, so the
model sees each distinct item once and decides which one, if any, fits a line.
Code then picks the cheapest offers and works out the saving.
"""
import logging
import uuid
from collections import defaultdict
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.models import Article
from app.schemas.articles import CatalogOffer, CatalogSuggestion, SuggestLine
from app.services.extraction import model_options

logger = logging.getLogger(__name__)

PROMPT = """Match the lines of a purchase request to the company's catalog of articles with negotiated prices.
- "same": the catalog item is the same product or service.
- "comparable": a different make or specification that serves the same need (e.g. another brand of business laptop).
- If no catalog item is a real fit, return null for the line. A wrong suggestion is worse than none.
- Only match what is bought for its own sake. Return null for lines that only exist because of another
  line: its delivery, shipping, packaging, installation, or a fee such as a copyright levy (Urheberrechtsabgabe).
- Request lines may be in German; the catalog is in English.

Request lines (number | description | unit):
{lines}

Catalog items (number | description | unit):
{items}"""


class LineMatch(BaseModel):
    line: int = Field(description="Number of the request line.")
    item: int | None = Field(description="Number of the matching catalog item, or null if none fits.")
    match: Literal["same", "comparable"] | None
    reason: str = Field(description="One short sentence: why it fits, or why nothing does.")


class CatalogMatches(BaseModel):
    matches: list[LineMatch]


# Units are compared loosely: "pieces" == "piece", "square meters" == "m²".
UNIT_ALIASES = {"pc": "piece", "pcs": "piece", "stk": "piece", "m2": "m²", "qm": "m²",
                "sqm": "m²", "square meter": "m²", "square metre": "m²"}


def _unit(unit: str) -> str:
    unit = unit.strip().lower().removesuffix("s")
    return UNIT_ALIASES.get(unit, unit)


def catalog_items(organization_id: uuid.UUID, db: Session) -> list[tuple[str, list[Article]]]:
    """The organization's catalog as distinct items, each with all its offers."""
    articles = db.scalars(
        select(Article)
        .options(joinedload(Article.supplier))
        .where(Article.organization_id == organization_id)
        .order_by(Article.article_number)
    ).all()
    offers: dict[str, list[Article]] = defaultdict(list)
    for article in articles:
        offers[article.description].append(article)
    return list(offers.items())


def suggest_articles(
    lines: list[SuggestLine], organization_id: uuid.UUID, db: Session
) -> list[CatalogSuggestion]:
    """Catalog items matching the request lines. Optional help: on any problem the
    answer is simply "no suggestions", never an error."""
    items = catalog_items(organization_id, db)
    if not items or not lines or not settings.openai_api_key:
        return []

    prompt = PROMPT.format(
        lines="\n".join(f"{n} | {line.position_description} | {line.unit}" for n, line in enumerate(lines, 1)),
        items="\n".join(f"{n} | {description} | {offers[0].unit}" for n, (description, offers) in enumerate(items, 1)),
    )
    try:
        client = OpenAI(api_key=settings.openai_api_key, timeout=60, max_retries=1)
        response = client.responses.parse(
            model=settings.openai_model,
            input=[{"role": "user", "content": prompt}],
            text_format=CatalogMatches,
            max_output_tokens=4000,
            store=False,
            **model_options(),
        )
        matches = response.output_parsed.matches if response.output_parsed else []
    except Exception:  # noqa: BLE001 — suggestions are optional
        logger.exception("Catalog matching failed")
        return []

    suggestions = []
    for match in matches:
        if match.item is None or match.match is None:
            continue
        if not (1 <= match.line <= len(lines) and 1 <= match.item <= len(items)):
            continue  # the model referred to a line or item that doesn't exist
        line = lines[match.line - 1]
        description, offers = items[match.item - 1]
        offers = sorted(offers, key=lambda article: article.unit_price)
        best = float(offers[0].unit_price)
        saving = None
        if _unit(offers[0].unit) == _unit(line.unit) and line.unit_price > best:
            saving = round((line.unit_price - best) * line.amount, 2)
        suggestions.append(CatalogSuggestion(
            line_index=match.line - 1,
            article_description=description,
            match=match.match,
            reason=match.reason,
            offers=[
                CatalogOffer(
                    article_number=offer.article_number,
                    supplier_name=offer.supplier.name,
                    supplier_vat_id=offer.supplier.vat_id,
                    unit_price=float(offer.unit_price),
                    unit=offer.unit,
                )
                for offer in offers[:3]
            ],
            saving=saving,
        ))
    return suggestions
