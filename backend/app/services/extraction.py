import base64
import logging
import re
import time
import unicodedata
import uuid

from openai import OpenAI
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CommodityGroup, Organization
from app.schemas.extraction import ExtractedVendorData, ExtractionResponse
from app.schemas.organizations import OrganizationSettings
from app.services.classification import match_rule, visible_to
from app.services.pdf import extract_text

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You extract procurement data from German vendor quotes and classify them into "
    "commodity groups. Report only what the document says."
)

# If any of these is still missing after reading the PDF's text, we give the
# model the PDF itself (text + page images), e.g. for a letterhead that is an image.
KEY_FIELDS = {"Vendor Name", "VAT ID", "Order Lines", "Net Total"}

# Legal forms and filler words, ignored when comparing company names.
NAME_NOISE = {"gmbh", "mbh", "ag", "kg", "se", "co", "ltd", "inc", "und", "and"}


def _build_prompt(commodity_groups_list: str, pdf_text: str | None = None) -> str:
    """The extraction prompt. With pdf_text the document is pasted in as text;
    without it, the prompt refers to the PDF attached to the same message.
    Field-specific rules live in the schema's descriptions (schemas/extraction.py)."""
    document = "The quote is the attached PDF." if pdf_text is None else f"The quote's text:\n{pdf_text}"
    return f"""Extract a vendor's quote (German: "Angebot") so that a purchase request can be created.
The quote was sent BY the vendor TO our company, the customer.

Rules:
- Report only what the document says. If a value is not in it, return null. Never guess.
- Money: plain numbers in EUR ("1.438,00" = 1438.00), always NET (before VAT), discounts applied.
- Order lines: every numbered position that has a price, in document order. Not: headings
  without a price, page carry-overs ("Übertrag"), adverts or recommendations.
- Alternatives stay in the list, marked with isAlternative; they are not part of the total.
- Extra costs: shipping, fees or levies that appear only in the summary block.
- Units in English; keep descriptions in the document's language.

{document}

Commodity group: pick the SINGLE most appropriate group from the list below, considering
the PRIMARY nature and purpose of the items:
- Branded office decor/furniture -> Office Equipment (15)
- Traditional promotional items (brochures, giveaways, banners) -> Promotional Materials (43)
- Software/IT products -> Software (31) or IT Services (30)
- If genuinely uncertain, set commodityGroupId to null.

Available Commodity Groups (ID | Category | Name):
{commodity_groups_list}"""


def _pdf_message(pdf_bytes: bytes, filename: str, commodity_groups_list: str) -> list[dict]:
    """A user message carrying the PDF itself (the Responses API gives the model its
    text and an image of each page) plus the prompt."""
    encoded = base64.b64encode(pdf_bytes).decode()
    return [
        {"type": "input_file", "filename": filename, "file_data": f"data:application/pdf;base64,{encoded}"},
        {"type": "input_text", "text": _build_prompt(commodity_groups_list)},
    ]


def _model_options() -> dict:
    """Reasoning models (e.g. Luna) take an effort level instead of a temperature;
    leave OPENAI_REASONING_EFFORT empty for older models such as gpt-4o-mini."""
    if settings.openai_reasoning_effort:
        return {"reasoning": {"effort": settings.openai_reasoning_effort}}
    return {"temperature": 0.3}


def _ask_model(client: OpenAI, user_content: str | list[dict], label: str) -> ExtractedVendorData:
    """One extraction call. Raises if the reply is cut off, empty or isn't valid data."""
    started = time.monotonic()
    response = client.responses.parse(
        model=settings.openai_model,
        instructions=SYSTEM_PROMPT,
        input=[{"role": "user", "content": user_content}],
        max_output_tokens=8000,  # includes the model's reasoning, not just the answer
        store=False,  # don't keep customer quotes on OpenAI's side
        text_format=ExtractedVendorData,  # structured outputs: the reply must match this schema
        **_model_options(),
    )
    if response.usage:
        logger.info(
            "%s pass: %s input + %s output tokens, %.1fs", label,
            response.usage.input_tokens, response.usage.output_tokens, time.monotonic() - started,
        )
    if response.status == "incomplete":
        raise ValueError(f"The model stopped early: {response.incomplete_details}")
    if response.output_parsed is None:  # e.g. the model refused
        raise ValueError("The model returned no data")
    return response.output_parsed


def _missing_fields(data: ExtractedVendorData, valid_group_ids: set[int]) -> list[str]:
    """Fields the user has to fill in by hand (same rules as the original).
    An unknown commodity group id is dropped and counts as missing."""
    if data.commodity_group_id is not None and data.commodity_group_id not in valid_group_ids:
        logger.warning("Invalid commodity group ID returned: %s", data.commodity_group_id)
        data.commodity_group_id = None
        data.commodity_group_name = None

    missing_fields: list[str] = []
    if not (data.title or "").strip():
        missing_fields.append("Title/Short Description")
    if not (data.vendor_name or "").strip():
        missing_fields.append("Vendor Name")
    if not (data.vat_id or "").strip():
        missing_fields.append("VAT ID")
    if not data.order_lines:
        missing_fields.append("Order Lines")
    if not data.net_total:
        missing_fields.append("Net Total")
    if not data.commodity_group_id or not data.commodity_group_name:
        missing_fields.append("Commodity Group")
    return missing_fields


def _fill_gaps(primary: ExtractedVendorData, backup: ExtractedVendorData) -> None:
    """Copy a value from `backup` into `primary` only where `primary` has none."""
    for field in ExtractedVendorData.model_fields:
        if not getattr(primary, field) and getattr(backup, field):
            setattr(primary, field, getattr(backup, field))


def _name_words(name: str) -> set[str]:
    """'Hoffmann Nürnberg GmbH' -> {'hoffmann', 'nurnberg'}: lower case, accents
    removed, legal form dropped."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", plain)) - NAME_NOISE


def _disagreements(from_text: ExtractedVendorData, from_pdf: ExtractedVendorData) -> list[str]:
    """Warnings for fields that both passes found but read differently."""
    def comparable(value) -> str:  # ignore case and spaces: "DE 123" == "de123"
        return "".join(str(value).split()).lower()

    def warning(label, text_value, pdf_value) -> str:
        return (f"{label}: the text says '{text_value}', the PDF says '{pdf_value}'. "
                "We used the PDF reading, please check.")

    warnings = []
    # Names count as the same company if they share a word, so "styleGREEN / FlowerArt
    # GmbH" vs "FlowerArt GmbH (styleGREEN)" is fine; only unrelated names are flagged.
    text_vendor, pdf_vendor = from_text.vendor_name, from_pdf.vendor_name
    if text_vendor and pdf_vendor and not _name_words(text_vendor) & _name_words(pdf_vendor):
        warnings.append(warning("Vendor", text_vendor, pdf_vendor))
    for label, text_value, pdf_value in [
        ("VAT ID", from_text.vat_id, from_pdf.vat_id),
        ("Net total", from_text.net_total, from_pdf.net_total),
    ]:
        if text_value and pdf_value and comparable(text_value) != comparable(pdf_value):
            warnings.append(warning(label, text_value, pdf_value))
    return warnings


def _cents(amount: float) -> int:
    return round(amount * 100)


def _check_numbers(data: ExtractedVendorData) -> list[str]:
    """Warnings where the extracted numbers don't add up. A quote states its numbers
    more than once (line totals, net, VAT, gross), so code can check the model's reading."""
    warnings = []
    bought = [line for line in data.order_lines if not line.is_alternative]
    for number, line in enumerate(bought, start=1):
        expected = line.amount * line.unit_price
        # Allow a cent, or half a cent per unit: vendors calculate with unrounded unit prices.
        if abs(_cents(expected) - _cents(line.total_price)) > max(1, line.amount / 2):
            warnings.append(
                f"Pos. {line.position or number}: {line.amount:g} × {line.unit_price:.2f} = {expected:.2f}, "
                f"but the quote says {line.total_price:.2f}. Please check quantity, price or discount."
            )
    lines_and_extras = sum(line.total_price for line in bought) + sum(cost.amount for cost in data.extra_costs)
    if data.net_total is not None and _cents(lines_and_extras) != _cents(data.net_total):
        warnings.append(
            f"The lines and extra costs add up to €{lines_and_extras:,.2f}, "
            f"but the quote's net total is €{data.net_total:,.2f}."
        )
    if None not in (data.net_total, data.vat_amount, data.gross_total) and (
        _cents(data.net_total + data.vat_amount) != _cents(data.gross_total)
    ):
        warnings.append(
            f"Net €{data.net_total:,.2f} + VAT €{data.vat_amount:,.2f} = "
            f"€{data.net_total + data.vat_amount:,.2f}, but the quote's gross total is €{data.gross_total:,.2f}."
        )
    return warnings


def _apply_booking_rule(data: ExtractedVendorData, rules, groups_by_id: dict) -> str | None:
    """Pre-select the group of the company's booking rule that matches, if any, and
    return a note for the user (who can still pick another group)."""
    bought = [(line.position_description, line.total_price) for line in data.order_lines if not line.is_alternative]
    rule = match_rule(rules, bought)
    if rule is None or rule.commodity_group_id not in groups_by_id:
        return None
    group = groups_by_id[rule.commodity_group_id]
    data.commodity_group_id, data.commodity_group_name = group.id, group.name
    return f"booking rule '{rule.name}' ({group.category} › {group.name})"


def extract_vendor_data(
    pdf_bytes: bytes, filename: str, organization_id: uuid.UUID | None, db: Session
) -> ExtractionResponse:
    """Extract vendor data from a quote PDF and classify its commodity group.

    Pass 1 gives the model the PDF's text (cheap). Only if a key field is still
    missing does pass 2 give it the PDF itself (text + page images). When both
    passes ran, the PDF reading wins, pass 1 fills its gaps, and fields the two
    read differently are reported as warnings. Finally the organization's booking
    rules may pre-select a different commodity group.
    """
    if not pdf_bytes:
        return ExtractionResponse(success=False, error="The uploaded file is empty")

    if not settings.openai_api_key:
        return ExtractionResponse(
            success=False,
            error="OPENAI_API_KEY is not configured on the server",
        )

    # The shared groups plus the organization's own. Generated variants such as
    # "Toner Cartridges — Pallet" can't be told apart from a quote, so the model only
    # gets the base groups (~380 instead of ~2,000); users can still pick any group.
    commodity_groups = [
        g for g in db.scalars(select(CommodityGroup).where(visible_to(organization_id)).order_by(CommodityGroup.id))
        if " — " not in g.name
    ]
    if not commodity_groups:
        return ExtractionResponse(
            success=False,
            error="Failed to load commodity groups for classification",
        )
    commodity_groups_list = "\n".join(
        f"{g.id} | {g.category} | {g.name}" for g in commodity_groups
    )
    groups_by_id = {g.id: g for g in commodity_groups}
    organization = db.get(Organization, organization_id) if organization_id else None
    rules = OrganizationSettings(**(organization.settings or {})).classification_rules if organization else []
    # Fail after a minute instead of the library's default of 10 minutes per attempt.
    client = OpenAI(api_key=settings.openai_api_key, timeout=60, max_retries=1)

    def respond(data: ExtractedVendorData, warnings: list[str], scanned_because: str | None) -> ExtractionResponse:
        """The successful answer: booking rule, missing fields and number checks."""
        classification_note = _apply_booking_rule(data, rules, groups_by_id)
        missing_fields = _missing_fields(data, set(groups_by_id))
        return ExtractionResponse(
            success=True,
            data=data,
            missing_fields=missing_fields or None,
            warnings=[*warnings, *_check_numbers(data)] or None,
            scanned_because=scanned_because,
            classification_note=classification_note,
        )

    # Pass 1: the PDF's text layer. Skipped if there is none (e.g. a scanned PDF).
    from_text = None
    try:
        pdf_text = extract_text(pdf_bytes)
        if pdf_text.strip():
            from_text = _ask_model(client, _build_prompt(commodity_groups_list, pdf_text), "Text")
    except Exception:  # noqa: BLE001 — pass 2 still gets its chance
        logger.exception("Extraction from the PDF text failed")

    scanned_because = "the PDF has no readable text"
    if from_text is not None:
        missing_fields = _missing_fields(from_text, set(groups_by_id))
        missing_key_fields = KEY_FIELDS.intersection(missing_fields)
        if not missing_key_fields:
            return respond(from_text, [], None)
        scanned_because = f"{', '.join(sorted(missing_key_fields))} not found in the text"
        logger.info("Missing after the text pass: %s -> reading the PDF itself", ", ".join(missing_fields))

    # Pass 2: the PDF itself.
    try:
        from_pdf = _ask_model(
            client, _pdf_message(pdf_bytes, filename, commodity_groups_list), "PDF"
        )
    except Exception as exc:  # noqa: BLE001 — mirror the original catch-all
        logger.exception("Extraction from the PDF itself failed")
        if from_text is None:
            return ExtractionResponse(success=False, error=str(exc))
        # Keep what the text pass found rather than losing everything.
        return respond(from_text, [], None)

    warnings: list[str] = []
    if from_text is not None:
        warnings = _disagreements(from_text, from_pdf)
        _fill_gaps(from_pdf, from_text)
    return respond(from_pdf, warnings, scanned_because)
