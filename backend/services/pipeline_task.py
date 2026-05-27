"""
Background pipeline task — runs the full processing chain for a document
after it is uploaded.

Stages (run sequentially, each stage failure is logged and does not crash the server):
  1. Parse  : document_parser  → raw text saved onto Document row
  2. Extract: claim_extractor  → Claim rows created
  3. Classify: claim_classifier → route (ai | human | hybrid | no) set on each Claim
  4. Agent  : bright_data_agent → runs on all ai + hybrid claims

Human-routed claims are left at verification_status="in_progress" for the
human review queue to pick up — the agent does not touch them.

Isolation:
  Each stage gets its own DB session so a failure mid-pipeline does not
  roll back already-committed work from earlier stages.

Swapping to Celery/ARQ later:
  Move the body of `run_pipeline_for_document` into a Celery task.
  The FastAPI upload endpoint calls `.delay(document_id)` instead of
  `background_tasks.add_task(run_pipeline_for_document, document_id)`.
  No other code changes needed.
"""

import logging
import traceback

from backend.database.db import SessionLocal
from backend.database.models import Document, Subject
from backend.services.extraction_pipeline import ExtractionPipeline
from backend.services.claim_classifier import ClaimClassifierService
from backend.services.bright_data_agent import BrightDataAgentService

logger = logging.getLogger(__name__)


def run_pipeline_for_document(document_id: str) -> None:
    """
    Entry point called by FastAPI BackgroundTasks.
    Each stage opens and closes its own DB session.
    """
    logger.info("[pipeline] starting for document %s", document_id)

    subject_id = _stage_extract(document_id)
    if not subject_id:
        return  # extraction failed, already logged

    _stage_classify(subject_id, document_id)
    _stage_agent(subject_id)

    logger.info("[pipeline] complete for document %s", document_id)


# ---------------------------------------------------------------------------
# Stage 1 + 2 : parse file → extract claims
# ---------------------------------------------------------------------------

def _stage_extract(document_id: str) -> str | None:
    db = SessionLocal()
    try:
        pipeline = ExtractionPipeline(db)
        claims = pipeline.run(document_id)

        doc = db.query(Document).filter(Document.id == document_id).first()
        subject_id = doc.subject_id if doc else None

        logger.info(
            "[pipeline] extract done — document=%s claims=%d",
            document_id, len(claims),
        )
        return subject_id

    except Exception:
        logger.error(
            "[pipeline] extract failed — document=%s\n%s",
            document_id, traceback.format_exc(),
        )
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stage 3 : classify claims
# ---------------------------------------------------------------------------

def _stage_classify(subject_id: str, document_id: str) -> None:
    db = SessionLocal()
    try:
        service = ClaimClassifierService(db)
        results = service.classify_for_document(document_id)
        logger.info(
            "[pipeline] classify done — document=%s classified=%d",
            document_id, len(results),
        )
    except Exception:
        logger.error(
            "[pipeline] classify failed — document=%s\n%s",
            document_id, traceback.format_exc(),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stage 4 : Bright Data agent (ai + hybrid claims only)
# ---------------------------------------------------------------------------

def _stage_agent(subject_id: str) -> None:
    db = SessionLocal()
    try:
        service = BrightDataAgentService(db)

        from backend.database.models import Claim
        claims = (
            db.query(Claim)
            .filter(
                Claim.subject_id == subject_id,
                Claim.verifiable.in_(["ai", "hybrid"]),
                Claim.agent_status.is_(None),
            )
            .all()
        )

        logger.info(
            "[pipeline] agent starting — subject=%s eligible_claims=%d",
            subject_id, len(claims),
        )

        for claim in claims:
            try:
                service.verify_claim(claim.id)
                logger.info("[pipeline] agent done — claim=%s verdict=%s", claim.id, claim.ai_verdict)
            except Exception:
                logger.error(
                    "[pipeline] agent failed — claim=%s\n%s",
                    claim.id, traceback.format_exc(),
                )

    except Exception:
        logger.error(
            "[pipeline] agent stage failed — subject=%s\n%s",
            subject_id, traceback.format_exc(),
        )
    finally:
        db.close()
