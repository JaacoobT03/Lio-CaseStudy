"""Commodity groups an organization can see, and its own booking rules.

Booking rules are company quirks such as Acme's "cable ties are always booked
under Electrical Engineering". They are suggestions: a matching rule pre-selects
its group, and the user can still choose another one.
"""
import uuid

from sqlalchemy import or_

from app.models import CommodityGroup
from app.schemas.organizations import ClassificationRule


def visible_to(organization_id: uuid.UUID | None):
    """Filter for the groups an organization may use: the shared ones plus its own."""
    return or_(
        CommodityGroup.organization_id.is_(None),
        CommodityGroup.organization_id == organization_id,
    )


def match_rule(
    rules: list[ClassificationRule], lines: list[tuple[str, float]]
) -> ClassificationRule | None:
    """The first rule whose keywords appear in the lines that make up most of the
    net value (so €20 of cable ties in a €10,000 machine order don't count), and
    whose amount limit, if it has one, the lines stay below.

    `lines` are (description, net line total) pairs of the lines being bought.
    """
    net_total = sum(total for _, total in lines)
    for rule in rules:
        if rule.max_net_total is not None and net_total >= rule.max_net_total:
            continue
        keywords = [keyword.lower() for keyword in rule.keywords]
        matching = sum(
            total for text, total in lines if any(k in text.lower() for k in keywords)
        )
        if net_total and matching > net_total / 2:
            return rule
    return None
