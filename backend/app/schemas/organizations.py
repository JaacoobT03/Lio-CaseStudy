import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

MemberRole = Literal["admin", "buyer", "requester"]

# Request fields an organization may optionally mark as required. (The other
# request fields are always required by the app.)
ConfigurableRequiredField = Literal["vat_id", "department"]


class ClassificationRule(BaseModel):
    """A company's booking rule, e.g. "cable ties are always Electrical Engineering"."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # Matched (case-insensitive) in the order line descriptions; list every
    # language the quotes come in, e.g. "cable tie" and "kabelbinder".
    keywords: list[str] = Field(min_length=1)
    commodity_group_id: int
    # Only applies while the lines' net total stays below this amount.
    max_net_total: float | None = None


class OrganizationSettings(BaseModel):
    """Per-organization configuration. Stored as JSONB, validated here.

    This is the reference pattern for tenant config: keep the storage flexible,
    but validate the shape at the edge so it can't drift into a free-for-all.
    """

    model_config = ConfigDict(extra="forbid")

    required_fields: list[ConfigurableRequiredField] = Field(default_factory=list)
    classification_rules: list[ClassificationRule] = Field(default_factory=list)


class OrganizationCreate(BaseModel):
    name: str
    slug: str


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class MyOrganizationOut(OrganizationOut):
    role: MemberRole


class MemberProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    avatar_url: str | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: MemberRole
    created_at: datetime
    # Named to match the Supabase join key the frontend already renders.
    profiles: MemberProfileOut | None = None


class InviteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    role: str
    invited_by: uuid.UUID
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime


class MembersResponse(BaseModel):
    members: list[MemberOut]
    invites: list[InviteOut]


class InviteCreate(BaseModel):
    email: EmailStr
    role: MemberRole = "requester"


class InviteAccept(BaseModel):
    token: str
