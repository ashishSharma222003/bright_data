"""
Extraction pipeline — orchestrates the full parse → extract → persist flow
for a single document.

Called after a file is uploaded. Persists both the raw extracted text onto
the Document row and each verifiable claim as a Claim row so all data is
available for future reference, re-processing, and audit.
"""

from sqlalchemy.orm import Session

from backend.database.models import Document, Claim, generate_uuid, utcnow
from backend.services.document_parser import DocumentParserService
from backend.services.claim_extractor import ClaimExtractorService
from backend.services.storage import get_storage


class ExtractionPipeline:
    """
    Usage:
        pipeline = ExtractionPipeline(db)
        pipeline.run(document_id="...")
    """

    def __init__(self, db: Session):
        self.db = db
        self.parser = DocumentParserService()
        self.extractor = ClaimExtractorService()
        self.storage = get_storage()

    def run(self, document_id: str) -> list[Claim]:
        """
        1. Load Document from DB
        2. Parse file → raw text  (OCR / vision / native PDF)
        3. Extract claims → list[ExtractedClaim]
        4. Persist raw text back onto Document row
        5. Persist each claim as a Claim row
        Returns the saved Claim rows.
        """
        doc = self._get_document(document_id)

        # --- Step 1: parse file to raw text ---
        try:
            file_path = self.storage.get_path(doc.storage_key)
            raw_text = self.parser.parse(file_path=file_path, doc_type=doc.doc_type)
        except Exception as exc:
            doc.extraction_status = "failed"
            self.db.commit()
            raise RuntimeError(f"Parsing failed for document {document_id}: {exc}") from exc

        # --- Step 2: extract structured claims ---
        try:
            result = self.extractor.extract(raw_text=raw_text, doc_type=doc.doc_type)
        except Exception as exc:
            doc.extraction_status = "failed"
            self.db.commit()
            raise RuntimeError(f"Claim extraction failed for document {document_id}: {exc}") from exc

        # --- Step 3: persist raw text onto Document ---
        doc.extracted_text = result.raw_text
        doc.extraction_status = "done"

        # --- Step 4: persist each claim ---
        saved_claims = []
        for extracted in result.claims:
            claim = Claim(
                id=generate_uuid(),
                subject_id=doc.subject_id,
                document_id=doc.id,
                claim_text=extracted.claim_text,
                category=extracted.category,
                source=extracted.source,
                date_mentioned=extracted.date_mentioned,
                verification_status="pending",
                created_at=utcnow(),
            )
            self.db.add(claim)
            saved_claims.append(claim)

        self.db.commit()
        return saved_claims

    def _get_document(self, document_id: str) -> Document:
        doc = self.db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            raise ValueError(f"Document {document_id} not found")
        return doc
