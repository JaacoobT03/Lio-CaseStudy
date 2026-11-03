from pydantic import BaseModel, ConfigDict, Field


class PdfParseResponse(BaseModel):
    text: str


# camelCase on the wire. These classes are also the structured-output schema the
# model must answer in: each description is an instruction it reads next to that
# field. Structured outputs allow no defaults other than None.
class OrderLineData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    position: str | None = Field(default=None, description="Position number as printed, e.g. '2' or '1.1a'.")
    position_description: str = Field(alias="positionDescription")
    amount: float
    unit: str = Field(description="In English, e.g. piece, m², hour, lump sum.")
    unit_price: float = Field(alias="unitPrice", description="Net price per unit, after any discount on this line.")
    total_price: float = Field(alias="totalPrice", description="Net line total; if only a unit price is shown: amount x unitPrice.")
    is_alternative: bool = Field(alias="isAlternative", description="True if offered instead of another position ('Alternativ', '(Alt.)', 'Eventualposition').")
    alternative_to: str | None = Field(default=None, alias="alternativeTo", description="Position number this alternative replaces.")


class ExtraCost(BaseModel):
    label: str = Field(description="As printed, e.g. 'Versandkosten'.")
    amount: float = Field(description="Net amount.")


class ExtractedVendorData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # None = "not found in the document" (e.g. a quote without a VAT ID). That is
    # a normal case, reported as a missing field, not a reason to fail.
    title: str | None = Field(default=None, description="2-5 English words describing what is being bought.")
    vendor_name: str | None = Field(default=None, alias="vendorName", description="The seller who sent the quote, never its recipient (our company).")
    vat_id: str | None = Field(default=None, alias="vatId", description="The seller's VAT ID (USt-IdNr./UID) without spaces.")
    department: str | None = Field(default=None, description="Only if the quote explicitly names a department of the recipient (e.g. 'z. Hd. Einkauf'); never a company name.")
    order_lines: list[OrderLineData] = Field(default_factory=list, alias="orderLines")
    extra_costs: list[ExtraCost] = Field(default_factory=list, alias="extraCosts", description="Shipping, fees or levies that appear only in the summary block, not as a numbered position.")
    net_total: float | None = Field(default=None, alias="netTotal", description="Net total as printed (before VAT), including shipping and fees.")
    vat_amount: float | None = Field(default=None, alias="vatAmount", description="VAT as printed; the sum if it is split.")
    gross_total: float | None = Field(default=None, alias="grossTotal", description="Gross total as printed (including VAT).")
    commodity_group_id: int | None = Field(default=None, alias="commodityGroupId")
    commodity_group_name: str | None = Field(default=None, alias="commodityGroupName")


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool
    data: ExtractedVendorData | None = None
    error: str | None = None
    missing_fields: list[str] | None = Field(default=None, alias="missingFields")
    # e.g. where the text and the PDF reading of a quote disagreed
    warnings: list[str] | None = None
    # Why the PDF pages were scanned (None = the text was enough), shown to the user.
    scanned_because: str | None = Field(default=None, alias="scannedBecause")
    # Set when one of the organization's booking rules chose the commodity group.
    classification_note: str | None = Field(default=None, alias="classificationNote")
