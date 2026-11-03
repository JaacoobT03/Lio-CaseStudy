"""End-to-end smoke test exercising every endpoint over an in-memory SQLite DB.

Run with: python -m pytest tests/ -q   (or plainly: python tests/test_smoke.py)
"""
import io
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import Article, CommodityGroup, RequestActivity, Supplier
from app.seed import seed

# ---------------------------------------------------------------------------
# Test database (in-memory SQLite) + commodity group seed
# ---------------------------------------------------------------------------
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


# SQLite ignores foreign keys (and their ON DELETE CASCADE) unless enabled —
# Postgres enforces them natively.
from sqlalchemy import event  # noqa: E402


@event.listens_for(engine, "connect")
def _enable_sqlite_fks(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

Base.metadata.create_all(engine)

# Seed reference data through the same code path production uses, so the test DB
# mirrors it (large commodity taxonomy + 120 suppliers with empty descriptions).
with TestingSession() as _s:
    seed(_s)


def _override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db
client = TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_full_user_journey():
    # --- health ---
    assert client.get("/health").json() == {"status": "ok"}

    # --- unauthenticated access is rejected ---
    assert client.get("/requests").status_code == 401
    assert client.get("/auth/me").status_code == 401
    assert client.get("/commodity-groups").status_code == 401

    # --- signup ---
    r = client.post(
        "/auth/signup",
        json={"email": "Owner@Example.com", "password": "secret123", "full_name": "Olive Owner"},
    )
    assert r.status_code == 201, r.text
    owner_token = r.json()["access_token"]
    assert r.json()["user"]["email"] == "owner@example.com"  # lower-cased

    # duplicate signup -> 409
    r = client.post(
        "/auth/signup", json={"email": "owner@example.com", "password": "secret123"}
    )
    assert r.status_code == 409

    # short password -> 422
    r = client.post("/auth/signup", json={"email": "x@example.com", "password": "abc"})
    assert r.status_code == 422

    # --- signin ---
    assert (
        client.post(
            "/auth/signin", json={"email": "owner@example.com", "password": "wrong"}
        ).status_code
        == 401
    )
    r = client.post(
        "/auth/signin", json={"email": "owner@example.com", "password": "secret123"}
    )
    assert r.status_code == 200
    owner_token = r.json()["access_token"]

    # --- me ---
    r = client.get("/auth/me", headers=_auth(owner_token))
    assert r.status_code == 200
    assert r.json()["profile"]["full_name"] == "Olive Owner"

    # --- no organization yet: org-scoped endpoints -> 403 ---
    assert client.get("/requests", headers=_auth(owner_token)).status_code == 403
    assert client.get("/organizations/me", headers=_auth(owner_token)).status_code == 403

    # --- create organization ---
    r = client.post(
        "/organizations",
        json={"name": "Acme Corp", "slug": "acme-corp"},
        headers=_auth(owner_token),
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "admin"

    # duplicate slug -> 409
    r = client.post(
        "/organizations",
        json={"name": "Acme 2", "slug": "acme-corp"},
        headers=_auth(owner_token),
    )
    assert r.status_code == 409

    # --- organizations/me ---
    r = client.get("/organizations/me", headers=_auth(owner_token))
    assert r.status_code == 200
    assert r.json()["name"] == "Acme Corp"
    assert r.json()["role"] == "admin"

    # --- commodity groups ---
    r = client.get("/commodity-groups", headers=_auth(owner_token))
    assert r.status_code == 200
    group_ids = [g["id"] for g in r.json()]
    assert 15 in group_ids and 31 in group_ids and 43 in group_ids
    assert len(group_ids) >= 300

    # --- create request (camelCase payload, as the frontend sends) ---
    payload = {
        "requestorName": "Olive Owner",
        "titleShortDescription": "Software Licenses",
        "vendorName": "Adobe Systems",
        "vatId": "DE123456789",
        "commodityGroupId": 31,
        "totalCost": 1500.0,
        "department": "IT",
        "orderLines": [
            {
                "positionDescription": "Creative Cloud",
                "unitPrice": 150.0,
                "amount": 10,
                "unit": "licenses",
                "totalPrice": 1500.0,
            }
        ],
    }
    r = client.post("/requests", json=payload, headers=_auth(owner_token))
    assert r.status_code == 201, r.text
    request_id = r.json()["id"]
    assert r.json()["status"] == "open"
    assert r.json()["requestor_name"] == "Olive Owner"
    assert r.json()["commodity_groups"]["name"] == "Software"
    assert len(r.json()["order_lines"]) == 1
    assert r.json()["order_lines"][0]["position_description"] == "Creative Cloud"

    # unknown commodity group -> 422
    bad = dict(payload, commodityGroupId=999999)
    assert client.post("/requests", json=bad, headers=_auth(owner_token)).status_code == 422

    # --- list requests ---
    r = client.get("/requests", headers=_auth(owner_token))
    assert r.status_code == 200
    assert len(r.json()) == 1

    # --- status update logs activity ---
    r = client.patch(
        f"/requests/{request_id}/status",
        json={"status": "in_progress"},
        headers=_auth(owner_token),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    def _activity_actions() -> list[str]:
        with TestingSession() as s:
            rows = s.scalars(
                select(RequestActivity)
                .where(RequestActivity.request_id == uuid.UUID(request_id))
                .order_by(RequestActivity.created_at)
            ).all()
            return [a.action for a in rows]

    # created + status_changed so far
    assert _activity_actions() == ["created", "status_changed"]

    # same-status update does NOT add activity
    client.patch(
        f"/requests/{request_id}/status",
        json={"status": "in_progress"},
        headers=_auth(owner_token),
    )
    assert _activity_actions() == ["created", "status_changed"]

    # invalid status -> 422
    r = client.patch(
        f"/requests/{request_id}/status",
        json={"status": "bogus"},
        headers=_auth(owner_token),
    )
    assert r.status_code == 422

    # --- full update replaces order lines ---
    update = {
        "requestorName": "Olive Owner",
        "title": "Software Licenses (updated)",
        "vendorName": "Adobe Systems",
        "vatId": "DE123456789",
        "commodityGroupId": 15,
        "totalCost": 500.0,
        "department": "IT",
        "orderLines": [
            {
                "positionDescription": "Desk",
                "unitPrice": 250.0,
                "amount": 2,
                "unit": "pieces",
                "totalPrice": 500.0,
            },
            {
                "positionDescription": "Chair",
                "unitPrice": 0.0,
                "amount": 1.28,
                "unit": "meters",
                "totalPrice": 0.0,
            },
        ],
    }
    r = client.put(f"/requests/{request_id}", json=update, headers=_auth(owner_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Software Licenses (updated)"
    assert body["commodity_groups"]["id"] == 15
    assert [l["position_description"] for l in body["order_lines"]] == ["Desk", "Chair"]
    assert body["order_lines"][1]["amount"] == 1.28  # decimal amounts supported
    assert [l["line_order"] for l in body["order_lines"]] == [1, 2]

    # --- invites ---
    r = client.post(
        "/organizations/me/invites",
        json={"email": "member@example.com"},
        headers=_auth(owner_token),
    )
    assert r.status_code == 201, r.text
    invite_token = None
    # token isn't exposed via the API response; fetch from DB like the email link would carry it
    from app.models import OrganizationInvite

    with TestingSession() as s:
        invite = s.scalar(
            select(OrganizationInvite).where(OrganizationInvite.email == "member@example.com")
        )
        assert invite is not None and invite.token and invite.expires_at is not None
        invite_token = invite.token

    # duplicate invite -> 409
    r = client.post(
        "/organizations/me/invites",
        json={"email": "member@example.com"},
        headers=_auth(owner_token),
    )
    assert r.status_code == 409

    # members endpoint shows 1 member + 1 pending invite
    r = client.get("/organizations/me/members", headers=_auth(owner_token))
    assert r.status_code == 200
    assert len(r.json()["members"]) == 1
    assert r.json()["members"][0]["profiles"]["full_name"] == "Olive Owner"
    assert len(r.json()["invites"]) == 1

    # --- second user accepts invite ---
    r = client.post(
        "/auth/signup",
        json={"email": "member@example.com", "password": "secret123", "full_name": "Mia Member"},
    )
    member_token = r.json()["access_token"]

    # wrong-email user cannot accept
    r = client.post(
        "/auth/signup", json={"email": "other@example.com", "password": "secret123"}
    )
    other_token = r.json()["access_token"]
    r = client.post(
        "/organizations/invites/accept",
        json={"token": invite_token},
        headers=_auth(other_token),
    )
    assert r.status_code == 403

    r = client.post(
        "/organizations/invites/accept",
        json={"token": invite_token},
        headers=_auth(member_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "requester"

    # accepted invite cannot be reused
    r = client.post(
        "/organizations/invites/accept",
        json={"token": invite_token},
        headers=_auth(member_token),
    )
    assert r.status_code == 404

    # member sees the org's requests
    r = client.get("/requests", headers=_auth(member_token))
    assert r.status_code == 200
    assert len(r.json()) == 1

    # members endpoint now shows 2 members, 0 pending invites
    r = client.get("/organizations/me/members", headers=_auth(owner_token))
    assert len(r.json()["members"]) == 2
    assert len(r.json()["invites"]) == 0

    # plain member cannot invite (admin required)
    r = client.post(
        "/organizations/me/invites",
        json={"email": "third@example.com"},
        headers=_auth(member_token),
    )
    assert r.status_code == 403

    # --- cancel invite ---
    r = client.post(
        "/organizations/me/invites",
        json={"email": "third@example.com"},
        headers=_auth(owner_token),
    )
    invite_id = r.json()["id"]
    r = client.delete(
        f"/organizations/me/invites/{invite_id}", headers=_auth(owner_token)
    )
    assert r.status_code == 204

    # --- isolation: user in a different org sees nothing ---
    r = client.post(
        "/organizations",
        json={"name": "Other Org", "slug": "other-org"},
        headers=_auth(other_token),
    )
    assert r.status_code == 201
    r = client.get("/requests", headers=_auth(other_token))
    assert r.json() == []
    # ...and cannot touch the first org's request
    assert (
        client.delete(f"/requests/{request_id}", headers=_auth(other_token)).status_code
        == 404
    )

    # --- delete request ---
    r = client.delete(f"/requests/{request_id}", headers=_auth(owner_token))
    assert r.status_code == 204
    assert client.get("/requests", headers=_auth(owner_token)).json() == []
    # order lines + activity cascaded
    with TestingSession() as s:
        assert (
            s.scalars(
                select(RequestActivity).where(
                    RequestActivity.request_id == uuid.UUID(request_id)
                )
            ).all()
            == []
        )


def test_pdf_parse():
    # Build a tiny real PDF in memory, then round-trip it through the endpoint.
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    r = client.post("/auth/signup", json={"email": "pdf@example.com", "password": "secret123"})
    token = r.json()["access_token"]

    r = client.post(
        "/pdf/parse",
        files={"file": ("offer.pdf", buf.getvalue(), "application/pdf")},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert "text" in r.json()  # blank page -> empty text, but parses fine

    # non-PDF rejected
    r = client.post(
        "/pdf/parse",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=_auth(token),
    )
    assert r.status_code == 400


def test_activity_log_and_document():
    from pypdf import PdfWriter

    admin = client.post(
        "/auth/signin", json={"email": "admin@acme.com", "password": "demo1234"}
    ).json()["access_token"]

    payload = {
        "requestorName": "Alex Admin", "titleShortDescription": "Chairs",
        "vendorName": "FurnitureCo", "vatId": "DE1", "commodityGroupId": 15,
        "totalCost": 500.0, "department": "Ops",
        "orderLines": [
            {"positionDescription": "Chair", "unitPrice": 250, "amount": 2, "unit": "pc", "totalPrice": 500}
        ],
    }
    rid = client.post("/requests", json=payload, headers=_auth(admin)).json()["id"]

    # edit two fields -> one 'updated' activity naming those fields
    updated = dict(payload)
    updated["title"] = "Office Chairs"
    updated["totalCost"] = 600.0
    updated["orderLines"] = [
        {"positionDescription": "Chair", "unitPrice": 300, "amount": 2, "unit": "pc", "totalPrice": 600}
    ]
    # RequestUpdate uses "title" (not titleShortDescription)
    updated = {
        "requestorName": "Alex Admin", "title": "Office Chairs", "vendorName": "FurnitureCo",
        "vatId": "DE1", "commodityGroupId": 15, "totalCost": 600.0, "department": "Ops",
        "orderLines": updated["orderLines"],
    }
    r = client.put(f"/requests/{rid}", json=updated, headers=_auth(admin))
    assert r.status_code == 200, r.text

    # approve, then a status change
    client.patch(f"/requests/{rid}/approval", json={"decision": "approved"}, headers=_auth(admin))
    client.patch(f"/requests/{rid}/status", json={"status": "closed"}, headers=_auth(admin))

    # attach a PDF
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    r = client.put(
        f"/requests/{rid}/document",
        files={"file": ("offer.pdf", buf.getvalue(), "application/pdf")},
        headers=_auth(admin),
    )
    assert r.status_code == 204, r.text

    # activity feed (newest first) captures every event
    r = client.get(f"/requests/{rid}/activity", headers=_auth(admin))
    assert r.status_code == 200
    actions = [a["action"] for a in r.json()]
    assert set(actions) == {
        "created", "updated", "approved", "status_changed", "document_attached"
    }
    updated_entry = next(a for a in r.json() if a["action"] == "updated")
    assert set(updated_entry["detail"]["changed_fields"]) == {
        "title", "total_cost", "order_lines"
    }

    # the stored PDF is served back
    r = client.get(f"/requests/{rid}/document", headers=_auth(admin))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"

    client.delete(f"/requests/{rid}", headers=_auth(admin))


def _tiny_pdf() -> bytes:
    """A blank one-page PDF, enough to pass the upload checks."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _upload_for_extraction(token: str) -> dict:
    r = client.post(
        "/extraction",
        files={"file": ("quote.pdf", _tiny_pdf(), "application/pdf")},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_extraction_without_api_key():
    r = client.post("/auth/signup", json={"email": "ex@example.com", "password": "secret123"})
    token = r.json()["access_token"]

    body = _upload_for_extraction(token)
    # Without OPENAI_API_KEY configured the endpoint reports a clean failure
    # envelope rather than an HTTP error.
    assert body["success"] is False
    assert "OPENAI_API_KEY" in body["error"]

    # non-PDF rejected
    r = client.post(
        "/extraction",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=_auth(token),
    )
    assert r.status_code == 400


class FakeOpenAI:
    """Stands in for openai.OpenAI in tests: answers each call with the next
    reply from `replies` and remembers every request, so no real API call is made."""

    replies: list[dict] = []
    requests: list[dict] = []

    def __init__(self, api_key=None, **options):
        self.responses = SimpleNamespace(parse=self._parse)

    def _parse(self, **request):
        FakeOpenAI.requests.append(request)
        # Like the real structured-outputs call: the reply is checked against the schema.
        parsed = request["text_format"].model_validate(FakeOpenAI.replies.pop(0))
        return SimpleNamespace(
            output_parsed=parsed, status="completed", incomplete_details=None, usage=None
        )


def _use_fake_openai(monkeypatch, *replies: dict, pdf_text: str):
    """Route extraction through FakeOpenAI, one reply per model call.
    `pdf_text` is what pypdf "finds" in the uploaded PDF ("" = no text layer)."""
    from app.core.config import settings
    from app.services import extraction

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(extraction, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(extraction, "extract_text", lambda pdf_bytes: pdf_text)
    FakeOpenAI.replies = list(replies)
    FakeOpenAI.requests = []
    r = client.post("/auth/signup", json={"email": f"{uuid.uuid4().hex}@example.com", "password": "secret123"})
    return r.json()["access_token"]


def _sent_pdf(request: dict) -> bool:
    """Did this model call carry the PDF file itself?"""
    content = request["input"][0]["content"]
    return isinstance(content, list) and content[0]["type"] == "input_file"


# A complete answer, as the model gives it for a quote it can read fully.
COMPLETE_REPLY = {
    "title": "Moss wall",
    "vendorName": "FlowerArt GmbH",
    "vatId": "DE271073640",
    "department": None,
    "orderLines": [{"position": "1", "positionDescription": "Moss module", "amount": 1.28, "unit": "m²",
                    "unitPrice": 559, "totalPrice": 715.52, "isAlternative": False, "alternativeTo": None}],
    "extraCosts": [{"label": "Versandkosten", "amount": 113.85}],
    "netTotal": 829.37,
    "vatAmount": 157.58,
    "grossTotal": 986.95,
    "commodityGroupId": 15,
    "commodityGroupName": "Office Equipment",
}


def test_extraction_reads_text_first(monkeypatch):
    """If the text gives us every key field, the PDF is never sent: one cheap call."""
    token = _use_fake_openai(monkeypatch, COMPLETE_REPLY, pdf_text="Angebot OF2312380 ...")

    body = _upload_for_extraction(token)

    assert body["success"] is True, body
    assert len(FakeOpenAI.requests) == 1
    assert not _sent_pdf(FakeOpenAI.requests[0])
    assert FakeOpenAI.requests[0]["store"] is False  # customer quotes aren't kept at OpenAI
    assert FakeOpenAI.requests[0]["text_format"].__name__ == "ExtractedVendorData"  # structured outputs
    assert body["scannedBecause"] is None


def test_extraction_falls_back_to_pdf(monkeypatch):
    """styleGREEN: the text lacks the VAT ID (it's in an image) and names the customer
    as vendor. The PDF pass is used, and the vendor disagreement is flagged."""
    from_text = {**COMPLETE_REPLY, "vendorName": "Lio Technologies GmbH", "vatId": None}
    token = _use_fake_openai(monkeypatch, from_text, COMPLETE_REPLY, pdf_text="Angebot OF2312380 ...")

    body = _upload_for_extraction(token)

    assert body["success"] is True, body
    assert len(FakeOpenAI.requests) == 2
    assert not _sent_pdf(FakeOpenAI.requests[0]) and _sent_pdf(FakeOpenAI.requests[1])
    assert body["data"]["vendorName"] == "FlowerArt GmbH"
    assert body["data"]["vatId"] == "DE271073640"
    assert any("Vendor" in warning for warning in body["warnings"])
    assert body["scannedBecause"] == "VAT ID not found in the text"


def test_same_vendor_spelled_differently_is_not_flagged():
    """Real cases from the eval: the same company written two ways is not a
    disagreement; two unrelated names are."""
    from app.schemas.extraction import ExtractedVendorData as Data
    from app.services.extraction import _disagreements

    assert _disagreements(Data(vendorName="styleGREEN / FlowerArt GmbH"),
                          Data(vendorName="FlowerArt GmbH (styleGREEN)")) == []
    assert _disagreements(Data(vendorName="Hoffmann Nurnberg GmbH"),
                          Data(vendorName="Hoffmann Nürnberg GmbH")) == []
    assert len(_disagreements(Data(vendorName="Lio Technologies GmbH"),
                              Data(vendorName="FlowerArt GmbH"))) == 1


def test_number_checks_flag_what_does_not_add_up():
    """A quote states its numbers more than once, so code can check the model's reading.
    The numbers are from real eval runs."""
    from app.schemas.extraction import ExtractedVendorData as Data
    from app.services.extraction import _check_numbers

    def line(amount, unit_price, total):
        return {"positionDescription": "x", "amount": amount, "unit": "piece",
                "unitPrice": unit_price, "totalPrice": total, "isAlternative": False}

    # styleGREEN read correctly: lines + shipping = net, net + VAT = gross.
    # (4.8 x 25.13 = 120.624 is printed as 120.62: rounding is fine.)
    style_green = Data(orderLines=[line(1.28, 559, 715.52), line(4.8, 25.13, 120.62), line(1, 350, 350)],
                       extraCosts=[{"label": "Versandkosten", "amount": 113.85}],
                       netTotal=1299.99, vatAmount=247.00, grossTotal=1546.99)
    assert _check_numbers(style_green) == []
    # The run that read the net total as 1,300.00: it matches neither the lines + extra
    # costs (1,299.99) nor gross - VAT (1,546.99 - 247.00), so both checks fire.
    assert len(_check_numbers(style_green.model_copy(update={"net_total": 1300.00}))) == 2
    # The Apple run that read the net without the levy: neither sum matches.
    apple = Data(orderLines=[line(1, 1467.61, 1467.61)],
                 extraCosts=[{"label": "Urheberrechtsabgabe", "amount": 10.55}],
                 netTotal=1467.61, vatAmount=280.85, grossTotal=1759.01)
    assert len(_check_numbers(apple)) == 2
    # A line that doesn't multiply out (list price instead of the discounted one).
    assert _check_numbers(Data(orderLines=[line(1, 894.08, 715.26)]))[0].startswith("Pos. 1:")


def test_booking_rules_match_lines_by_value():
    """Acme's booking rules (as seeded): keywords in either language, judged by the
    lines that make up most of the net value, and the "small" limit."""
    from app.schemas.organizations import ClassificationRule
    from app.seed import ACME_BOOKING_RULES
    from app.services.classification import match_rule

    rules = [ClassificationRule(**rule) for rule in ACME_BOOKING_RULES]
    name = lambda lines: (match_rule(rules, lines) or SimpleNamespace(name=None)).name  # noqa: E731

    assert name([("Kabelbinder 200 mm schwarz, 1000 Stk.", 49.0)]) == "Cable ties"
    assert name([("Cable ties UV-resistant, pack of 100", 12.5)]) == "Cable ties"
    assert name([("Toner cartridge black HP 305A", 89.0)]) == "Printer toner"
    assert name([("Figma software subscription, 12 months", 540.0)]) == "Small software subscriptions"
    assert name([("Enterprise software subscription", 5000.0)]) is None  # not "small"
    # €20 of cable ties inside a €10,000 machine order don't make it a cable-tie order.
    assert name([("Kabelbinder", 20.0), ("CNC machine", 10000.0)]) is None
    assert name([("Moosbild mit Logo", 1438.0)]) is None


def test_acme_office_supplies_group_is_acme_only():
    """Acme's own "Office Supplies" group: Acme sees it and can use it; BMW can do neither."""
    acme, bmw = _signin("requester@acme.com"), _signin("admin@bmw.com")

    def group_ids(token):
        return {g["id"] for g in client.get("/commodity-groups", headers=_auth(token)).json()}

    assert 100001 in group_ids(acme)
    assert 100001 not in group_ids(bmw)

    request = {"requestorName": "R", "titleShortDescription": "Figma", "vendorName": "Figma Inc.",
               "commodityGroupId": 100001, "totalCost": 540.0,
               "orderLines": [{"positionDescription": "Figma subscription", "unitPrice": 540.0,
                               "amount": 1, "unit": "year", "totalPrice": 540.0}]}
    assert client.post("/requests", json=request, headers=_auth(acme)).status_code == 201
    assert client.post("/requests", json=request, headers=_auth(bmw)).status_code == 422


def test_saving_one_setting_keeps_the_others():
    """Saving "required fields" used to replace the whole settings object, which
    would have wiped Acme's booking rules."""
    admin = _signin("admin@acme.com")
    r = client.patch("/organizations/me/settings", json={"required_fields": ["vat_id"]}, headers=_auth(admin))
    assert r.status_code == 200, r.text
    settings = client.get("/organizations/me/settings", headers=_auth(admin)).json()
    assert settings["required_fields"] == ["vat_id"]
    assert len(settings["classification_rules"]) == 3
    # restore
    client.patch("/organizations/me/settings", json={"required_fields": []}, headers=_auth(admin))


def test_extraction_applies_the_booking_rule(monkeypatch):
    """The AI files cable ties under Consumables; Acme's rule pre-selects Electrical
    Engineering and says so."""
    cable_ties = {**COMPLETE_REPLY, "commodityGroupId": 49, "commodityGroupName": "Consumables",
                  "orderLines": [{**COMPLETE_REPLY["orderLines"][0],
                                  "positionDescription": "Kabelbinder 200 mm, 1000 Stk."}]}
    _use_fake_openai(monkeypatch, cable_ties, pdf_text="Angebot Kabelbinder ...")

    body = _upload_for_extraction(_signin("requester@acme.com"))

    assert body["data"]["commodityGroupId"] == 11
    assert body["classificationNote"] == "booking rule 'Cable ties' (Facility Management › Electrical Engineering)"


def test_catalog_suggestions(monkeypatch):
    """The MacBook gets Acme's cheapest ultralight laptop; the grinder gets nothing;
    no saving is claimed when the units differ; BMW (no catalog) gets nothing."""
    from app.core.config import settings
    from app.db.session import get_db as _get_db
    from app.models import Organization
    from app.services import catalog

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(catalog, "OpenAI", FakeOpenAI)
    db = next(app.dependency_overrides[_get_db]())
    acme_id = db.scalar(select(Organization.id).where(Organization.slug == "acme"))
    items = [description for description, _ in catalog.catalog_items(acme_id, db)]
    laptop = items.index('Business laptop 13" ultralight / 16 GB / 1 TB') + 1
    moss_wall = items.index("Custom moss wall, preserved reindeer moss, made to measure") + 1

    FakeOpenAI.requests = []
    FakeOpenAI.replies = [{"matches": [
        {"line": 1, "item": laptop, "match": "comparable", "reason": "Another brand of 13-inch laptop."},
        {"line": 2, "item": None, "match": None, "reason": "No grinding machines in the catalog."},
        {"line": 3, "item": moss_wall, "match": "same", "reason": "A preserved moss wall."},
    ]}]
    lines = [
        {"positionDescription": '13" MacBook Air M2, 16 GB, 512 GB', "amount": 1, "unit": "piece", "unitPrice": 1467.61},
        {"positionDescription": "Schleifmaschine HN-GS 450", "amount": 1, "unit": "piece", "unitPrice": 15000},
        {"positionDescription": "Moosbild 160x80 cm", "amount": 1, "unit": "piece", "unitPrice": 1438},
    ]
    r = client.post("/articles/suggest", json={"lines": lines}, headers=_auth(_signin("requester@acme.com")))
    assert r.status_code == 200, r.text
    by_line = {s["lineIndex"]: s for s in r.json()}

    assert set(by_line) == {0, 2}  # nothing for the grinder
    offers = by_line[0]["offers"]
    assert offers[0] == {"articleNumber": "DEL-00905", "supplierName": "Delacroix Systems GmbH",
                         "supplierVatId": offers[0]["supplierVatId"], "unitPrice": 1438.0, "unit": "piece"}
    assert [o["unitPrice"] for o in offers] == sorted(o["unitPrice"] for o in offers)  # cheapest first
    assert by_line[0]["saving"] == 29.61
    assert by_line[2]["saving"] is None  # €/m² vs. a picture priced per piece: no fake saving

    # BMW has no catalog: no suggestions, and no model call at all.
    FakeOpenAI.requests = []
    r = client.post("/articles/suggest", json={"lines": lines}, headers=_auth(_signin("admin@bmw.com")))
    assert r.json() == [] and FakeOpenAI.requests == []


def test_extraction_survives_missing_vat_id(monkeypatch):
    """A quote without a VAT ID (Hoffmann) used to fail the whole extraction. Now both
    passes run, neither finds one, and it's reported as missing."""
    no_vat = {**COMPLETE_REPLY, "vendorName": "Hoffmann Nurnberg GmbH", "vatId": None}
    token = _use_fake_openai(monkeypatch, no_vat, no_vat, pdf_text="Angebot ANG-HN-2026-041 ...")

    body = _upload_for_extraction(token)

    assert body["success"] is True, body
    assert len(FakeOpenAI.requests) == 2
    assert body["data"]["vatId"] is None
    assert "VAT ID" in body["missingFields"]


def test_extraction_scanned_pdf_goes_straight_to_pdf(monkeypatch):
    """No text layer (a scan): skip the text pass and send the PDF right away."""
    token = _use_fake_openai(monkeypatch, COMPLETE_REPLY, pdf_text="")

    body = _upload_for_extraction(token)

    assert body["success"] is True, body
    assert len(FakeOpenAI.requests) == 1
    assert _sent_pdf(FakeOpenAI.requests[0])


def test_commodity_groups_expanded():
    r = client.post("/auth/signup", json={"email": "cg@example.com", "password": "secret123"})
    token = r.json()["access_token"]

    r = client.get("/commodity-groups", headers=_auth(token))
    assert r.status_code == 200
    groups = r.json()
    # Large taxonomy.
    assert len(groups) >= 1000, f"expected a large taxonomy, got {len(groups)}"
    # ids contiguous from 1; first one matches the original taxonomy
    assert groups[0] == {"id": 1, "category": "General Services", "name": "Accommodation Rentals"}
    assert len({g["category"] for g in groups}) >= 20

    # Every leaf name belongs to exactly one category.
    by_name: dict[str, set[str]] = {}
    for g in groups:
        by_name.setdefault(g["name"], set()).add(g["category"])
    ambiguous = {n: c for n, c in by_name.items() if len(c) > 1}
    assert not ambiguous, f"names spanning multiple categories: {ambiguous}"


def _signin(email: str, password: str = "demo1234") -> str:
    r = client.post("/auth/signin", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_suppliers_are_org_scoped_master_data():
    token = _signin("admin@acme.com")

    # auth required
    assert client.get("/suppliers").status_code == 401

    r = client.get("/suppliers/count", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["count"] >= 100

    r = client.get("/suppliers?limit=500", headers=_auth(token))
    assert r.status_code == 200
    suppliers = r.json()
    assert len(suppliers) >= 100
    # every seeded supplier has an empty description, ready for enrichment
    assert all((s["description"] or "") == "" for s in suppliers)
    # sorted by name, carries catalog metadata
    assert suppliers == sorted(suppliers, key=lambda s: s["name"])
    assert all(s["name"] and s["category"] for s in suppliers)

    # search filter works
    r = client.get("/suppliers?search=logistik", headers=_auth(token))
    assert r.status_code == 200
    assert len(r.json()) >= 1
    assert all("logistik" in s["name"].lower() for s in r.json())

    # master data is private to Acme: another tenant sees none of it
    other = _signin("admin@walmart.com")
    assert client.get("/suppliers/count", headers=_auth(other)).json()["count"] == 0
    assert client.get("/suppliers?limit=500", headers=_auth(other)).json() == []


def test_articles_catalog_paginated_and_searchable():
    token = _signin("admin@acme.com")

    # auth required
    assert client.get("/articles").status_code == 401

    r = client.get("/articles/count", headers=_auth(token))
    assert r.status_code == 200
    total = r.json()["count"]
    assert total >= 1000

    # first page: bounded size, correct envelope, ordered by article number
    r = client.get("/articles?limit=25&offset=0", headers=_auth(token))
    assert r.status_code == 200
    page = r.json()
    assert page["total"] == total
    assert page["limit"] == 25 and page["offset"] == 0
    assert len(page["items"]) == 25
    numbers = [a["article_number"] for a in page["items"]]
    assert numbers == sorted(numbers)
    # every article carries its supplier and a price
    assert all(a["supplier_id"] and a["supplier_name"] for a in page["items"])
    assert all(a["description"] and a["unit"] for a in page["items"])

    # second page returns different rows
    r2 = client.get("/articles?limit=25&offset=25", headers=_auth(token))
    assert r2.status_code == 200
    assert {a["id"] for a in r2.json()["items"]}.isdisjoint({a["id"] for a in page["items"]})

    # search matches the seeded office-decoration items
    r = client.get("/articles?search=moss", headers=_auth(token))
    assert r.status_code == 200
    found = r.json()
    assert found["total"] >= 1
    assert any("moss wall" in a["description"].lower() for a in found["items"])

    # catalog is private to Acme: another tenant's catalog is empty
    other = _signin("admin@walmart.com")
    assert client.get("/articles/count", headers=_auth(other)).json()["count"] == 0
    assert client.get("/articles", headers=_auth(other)).json()["total"] == 0


def test_seed_is_idempotent():
    # The DB was already seeded at import; seeding again must add nothing and
    # must not change the row counts (safe to run on every startup).
    with TestingSession() as db:
        before_cg = db.scalar(select(func.count()).select_from(CommodityGroup))
        before_sup = db.scalar(select(func.count()).select_from(Supplier))
        before_art = db.scalar(select(func.count()).select_from(Article))
        counts = seed(db)
        after_cg = db.scalar(select(func.count()).select_from(CommodityGroup))
        after_sup = db.scalar(select(func.count()).select_from(Supplier))
        after_art = db.scalar(select(func.count()).select_from(Article))

    assert all(v == 0 for v in counts.values()), counts
    assert before_cg == after_cg
    assert before_sup == after_sup
    assert before_art == after_art


def test_demo_user_can_sign_in():
    # The seeded demo account works out of the box (documented in the README).
    r = client.post(
        "/auth/signin", json={"email": "admin@acme.com", "password": "demo1234"}
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    # ...and already belongs to an organization, so the app is usable immediately.
    r = client.get("/organizations/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["slug"] == "acme"
    assert r.json()["role"] == "admin"

    assert client.get("/requests", headers=_auth(token)).status_code == 200


def test_approval_flow_with_roles():
    # Uses the seeded demo org: requester creates, buyer approves.
    def signin(email: str) -> str:
        r = client.post("/auth/signin", json={"email": email, "password": "demo1234"})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    requester = signin("requester@acme.com")
    buyer = signin("buyer@acme.com")
    admin = signin("admin@acme.com")

    payload = {
        "requestorName": "Rene Requester",
        "titleShortDescription": "Laptops",
        "vendorName": "TechSupply",
        "vatId": "DE999",
        "commodityGroupId": 31,
        "totalCost": 4200.0,
        "department": "Engineering",
        "orderLines": [
            {"positionDescription": "Laptop", "unitPrice": 1400, "amount": 3, "unit": "pc", "totalPrice": 4200}
        ],
    }
    r = client.post("/requests", json=payload, headers=_auth(requester))
    assert r.status_code == 201, r.text
    request_id = r.json()["id"]
    # every new request starts pending
    assert r.json()["approval_status"] == "pending"
    assert r.json()["approved_by"] is None

    # requester cannot approve (not a buyer/admin)
    r = client.patch(
        f"/requests/{request_id}/approval",
        json={"decision": "approved"},
        headers=_auth(requester),
    )
    assert r.status_code == 403

    # buyer approves
    r = client.patch(
        f"/requests/{request_id}/approval",
        json={"decision": "approved"},
        headers=_auth(buyer),
    )
    assert r.status_code == 200, r.text
    assert r.json()["approval_status"] == "approved"
    assert r.json()["approved_by"] is not None
    assert r.json()["approved_at"] is not None

    # already decided -> 409 (even for an admin)
    r = client.patch(
        f"/requests/{request_id}/approval",
        json={"decision": "rejected"},
        headers=_auth(admin),
    )
    assert r.status_code == 409

    # admins can also decide; rejection works
    r = client.post("/requests", json=payload, headers=_auth(requester))
    second_id = r.json()["id"]
    r = client.patch(
        f"/requests/{second_id}/approval",
        json={"decision": "rejected"},
        headers=_auth(admin),
    )
    assert r.status_code == 200
    assert r.json()["approval_status"] == "rejected"

    # buyers cannot touch org settings (admin-only)
    assert (
        client.patch(
            "/organizations/me/settings",
            json={"required_fields": []},
            headers=_auth(buyer),
        ).status_code
        == 403
    )

    # cleanup: remove the demo-org requests this test created
    for rid in (request_id, second_id):
        client.delete(f"/requests/{rid}", headers=_auth(admin))


def test_org_settings_required_fields():
    # Fresh org (settings default to empty).
    r = client.post(
        "/auth/signup", json={"email": "settings@example.com", "password": "secret123"}
    )
    token = r.json()["access_token"]
    client.post(
        "/organizations",
        json={"name": "Settings Co", "slug": "settings-co"},
        headers=_auth(token),
    )

    # Default settings: nothing extra required.
    r = client.get("/organizations/me/settings", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"required_fields": [], "classification_rules": []}

    base_payload = {
        "requestorName": "S", "titleShortDescription": "T", "vendorName": "V",
        "vatId": "", "commodityGroupId": 31, "totalCost": 10.0, "department": "",
        "orderLines": [
            {"positionDescription": "x", "unitPrice": 10, "amount": 1, "unit": "pc", "totalPrice": 10}
        ],
    }
    # Without a requirement, a request with blank vat_id/department is accepted.
    assert client.post("/requests", json=base_payload, headers=_auth(token)).status_code == 201

    # Configure vat_id + department as required.
    r = client.patch(
        "/organizations/me/settings",
        json={"required_fields": ["vat_id", "department"]},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert set(r.json()["required_fields"]) == {"vat_id", "department"}

    # Now the same blank-field request is rejected...
    r = client.post("/requests", json=base_payload, headers=_auth(token))
    assert r.status_code == 422
    assert "vat_id" in r.json()["detail"] and "department" in r.json()["detail"]

    # ...and providing them succeeds.
    ok = dict(base_payload, vatId="DE123", department="IT")
    assert client.post("/requests", json=ok, headers=_auth(token)).status_code == 201

    # Unknown setting keys are rejected (validated at the edge).
    assert (
        client.patch(
            "/organizations/me/settings",
            json={"bogus": True},
            headers=_auth(token),
        ).status_code
        == 422
    )


def test_org_settings_admin_only():
    # A plain member cannot change settings.
    owner = client.post(
        "/auth/signup", json={"email": "so-owner@example.com", "password": "secret123"}
    ).json()["access_token"]
    client.post(
        "/organizations",
        json={"name": "SO Co", "slug": "so-co"},
        headers=_auth(owner),
    )
    invite = client.post(
        "/organizations/me/invites",
        json={"email": "so-member@example.com"},
        headers=_auth(owner),
    )
    from app.models import OrganizationInvite

    with TestingSession() as db:
        token_val = db.scalar(
            select(OrganizationInvite.token).where(
                OrganizationInvite.email == "so-member@example.com"
            )
        )
    member = client.post(
        "/auth/signup", json={"email": "so-member@example.com", "password": "secret123"}
    ).json()["access_token"]
    client.post(
        "/organizations/invites/accept", json={"token": token_val}, headers=_auth(member)
    )

    # member can read settings but not change them
    assert client.get("/organizations/me/settings", headers=_auth(member)).status_code == 200
    r = client.patch(
        "/organizations/me/settings",
        json={"required_fields": ["vat_id"]},
        headers=_auth(member),
    )
    assert r.status_code == 403


if __name__ == "__main__":
    test_full_user_journey()
    test_pdf_parse()
    test_extraction_without_api_key()
    test_activity_log_and_document()
    test_commodity_groups_expanded()
    test_suppliers_seeded_with_empty_descriptions()
    test_seed_is_idempotent()
    test_demo_user_can_sign_in()
    test_approval_flow_with_roles()
    test_org_settings_required_fields()
    test_org_settings_admin_only()
    print("ALL SMOKE TESTS PASSED")
