"""
Upload router — accepts document files for a subject, persists them via
the storage backend, creates a Document row, then fires the background
pipeline (parse → extract → classify → agent verify).

The HTTP response returns immediately with the document ID and a
processing_status of "queued". Callers can poll
GET /extraction/{document_id}/text or /verification/subjects/{id}/claims
to track progress.
"""

import mimetypes
import os

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from backend.database.db import get_db
from backend.database.models import Document, Subject, generate_uuid, utcnow
from backend.services.storage import get_storage
from backend.services.pipeline_task import run_pipeline_for_document

router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
MAX_FILE_SIZE_MB = 20


@router.post("/document")
def upload_document(
    background_tasks: BackgroundTasks,
    subject_id: str = Form(...),
    uploaded_by: str = Form(...),   # user id — replace with JWT token extraction later
    doc_type: str = Form(...),      # resume | degree | offer_letter | contract | pan | gst | reg_doc
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload a document for a subject.
    Returns immediately; processing runs in the background.
    """
    # --- validate subject exists ---
    subject = db.query(Subject).filter(Subject.id == subject_id).first()
    if not subject:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")

    # --- validate file type ---
    mime_type, _ = mimetypes.guess_type(file.filename or "")
    mime_type = mime_type or file.content_type or ""
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type '{mime_type}' not allowed. Accepted: pdf, jpeg, png, webp, gif",
        )

    # --- read and size-check ---
    file_bytes = file.file.read()
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {MAX_FILE_SIZE_MB} MB limit",
        )

    # --- build storage key: {subject_id}/{doc_id}_{original_filename} ---
    document_id = generate_uuid()
    safe_filename = os.path.basename(file.filename or "file")
    storage_key = f"{subject_id}/{document_id}_{safe_filename}"

    # --- save to storage backend ---
    storage = get_storage()
    import io
    storage.save(io.BytesIO(file_bytes), storage_key)

    # --- create Document row ---
    doc = Document(
        id=document_id,
        subject_id=subject_id,
        uploaded_by=uploaded_by,
        doc_type=doc_type,
        original_filename=safe_filename,
        storage_key=storage_key,
        mime_type=mime_type,
        size_bytes=str(len(file_bytes)),
        extraction_status="pending",
        uploaded_at=utcnow(),
    )
    db.add(doc)
    db.commit()

    # --- fire background pipeline ---
    background_tasks.add_task(run_pipeline_for_document, document_id)

    return {
        "document_id": document_id,
        "subject_id": subject_id,
        "doc_type": doc_type,
        "filename": safe_filename,
        "size_bytes": len(file_bytes),
        "processing_status": "queued",
        "message": "Document uploaded. Processing has started in the background.",
    }


@router.get("/document/{document_id}/status")
def get_document_status(document_id: str, db: Session = Depends(get_db)):
    """
    Poll the processing status of an uploaded document.
    Returns extraction status and count of claims found so far.
    """
    from backend.database.models import Claim

    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    claims = db.query(Claim).filter(Claim.document_id == document_id).all()
    route_counts = {"ai": 0, "human": 0, "hybrid": 0, "no": 0, "unclassified": 0}
    for c in claims:
        key = c.verifiable if c.verifiable in route_counts else "unclassified"
        route_counts[key] += 1

    return {
        "document_id": document_id,
        "filename": doc.original_filename,
        "doc_type": doc.doc_type,
        "extraction_status": doc.extraction_status,
        "claims_found": len(claims),
        "routes": route_counts,
    }
