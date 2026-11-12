import uuid

from fastapi import APIRouter, Depends, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.deps import get_organization_id
from app.db.session import get_db
from app.routers.pdf import read_pdf_upload
from app.schemas.extraction import ExtractionResponse
from app.services.extraction import extract_vendor_data

router = APIRouter(prefix="/extraction", tags=["extraction"])


@router.post("", response_model=ExtractionResponse, response_model_by_alias=True)
async def extract(
    file: UploadFile,
    organization_id: uuid.UUID | None = Depends(get_organization_id),  # also requires login
    db: Session = Depends(get_db),
) -> ExtractionResponse:
    """Extract structured vendor data from an uploaded vendor-offer PDF via OpenAI.

    Returns ``{success, data?, error?, missingFields?, warnings?}`` — the same envelope the
    original server action produced (errors are part of the payload, not HTTP
    errors, so the UI can show partial-extraction warnings).
    """
    pdf_bytes = await read_pdf_upload(file)
    # The OpenAI calls block for seconds. Run them in a worker thread so the server
    # keeps answering other requests meanwhile (a plain `def` endpoint did this implicitly).
    return await run_in_threadpool(
        extract_vendor_data, pdf_bytes, file.filename or "quote.pdf", organization_id, db
    )
