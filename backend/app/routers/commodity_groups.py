import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_organization_id
from app.db.session import get_db
from app.models import CommodityGroup
from app.schemas.requests import CommodityGroupOut
from app.services.classification import visible_to

router = APIRouter(prefix="/commodity-groups", tags=["commodity-groups"])


@router.get("", response_model=list[CommodityGroupOut])
def list_commodity_groups(
    organization_id: uuid.UUID | None = Depends(get_organization_id),
    db: Session = Depends(get_db),
) -> list[CommodityGroupOut]:
    """The shared groups plus the caller's organization's own groups."""
    groups = db.scalars(
        select(CommodityGroup).where(visible_to(organization_id)).order_by(CommodityGroup.id)
    )
    return [CommodityGroupOut.model_validate(g) for g in groups]
