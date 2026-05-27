from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database.db import get_db
from backend.database.models import Document, Claim
from backend.services.extraction_pipeline import ExtractionPipeline

router = APIRouter(prefix="/extraction", tags=["extraction"])


@router.post("/{document_id}/run")
def run_extraction(document_id: str, db: Session = Depends(get_db)):
    """
    Trigger the full parse → extract → persist pipeline for a document.
    Returns the list of claims saved to the DB.
    """
    pipeline = ExtractionPipeline(db)
    try:
        claims = pipeline.run(document_id=document_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return {
        "document_id": document_id,
        "claims_extracted": len(claims),
        "claims": [
            {
                "id": c.id,
                "claim_text": c.claim_text,
                "category": c.category,
                "source": c.source,
                "date_mentioned": c.date_mentioned,
                "verification_status": c.verification_status,
            }
            for c in claims
        ],
    }


@router.get("/{document_id}/text")
def get_extracted_text(document_id: str, db: Session = Depends(get_db)):
    """Return the raw extracted text stored for a document."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return {
        "document_id": document_id,
        "extraction_status": doc.extraction_status,
        "extracted_text": doc.extracted_text,
    }


@router.get("/{document_id}/claims")
def get_document_claims(document_id: str, db: Session = Depends(get_db)):
    """Return all claims extracted from a specific document."""
    claims = db.query(Claim).filter(Claim.document_id == document_id).all()
    return {
        "document_id": document_id,
        "claims": [
            {
                "id": c.id,
                "claim_text": c.claim_text,
                "category": c.category,
                "source": c.source,
                "date_mentioned": c.date_mentioned,
                "verification_status": c.verification_status,
                "created_at": c.created_at,
            }
            for c in claims
        ],
    }
