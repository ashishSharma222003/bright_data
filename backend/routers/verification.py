from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.database.db import get_db
from backend.database.models import Claim
from backend.services.claim_classifier import ClaimClassifierService
from backend.services.bright_data_agent import BrightDataAgentService

router = APIRouter(prefix="/verification", tags=["verification"])


@router.post("/subjects/{subject_id}/classify")
def classify_subject_claims(subject_id: str, db: Session = Depends(get_db)):
    """
    Classify all unclassified claims for a subject in one Claude call.
    Sets verifiable route (ai / human / hybrid / no) and reasoning on each claim.
    """
    service = ClaimClassifierService(db)
    results = service.classify_for_subject(subject_id)

    if not results:
        return {"message": "No unclassified claims found", "classified": 0}

    return {
        "subject_id": subject_id,
        "classified": len(results),
        "breakdown": _breakdown(results),
        "claims": [{"claim_id": r.claim_id, "route": r.route, "reasoning": r.reasoning} for r in results],
    }


@router.post("/documents/{document_id}/classify")
def classify_document_claims(document_id: str, db: Session = Depends(get_db)):
    """Classify all unclassified claims for a single document."""
    service = ClaimClassifierService(db)
    results = service.classify_for_document(document_id)

    if not results:
        return {"message": "No unclassified claims found", "classified": 0}

    return {
        "document_id": document_id,
        "classified": len(results),
        "breakdown": _breakdown(results),
        "claims": [{"claim_id": r.claim_id, "route": r.route, "reasoning": r.reasoning} for r in results],
    }


@router.get("/subjects/{subject_id}/claims")
def get_subject_claims_by_route(subject_id: str, route: str | None = None, db: Session = Depends(get_db)):
    """
    Return all claims for a subject, optionally filtered by route.
    ?route=ai | human | hybrid | no
    """
    query = db.query(Claim).filter(Claim.subject_id == subject_id)
    if route:
        query = query.filter(Claim.verifiable == route)
    claims = query.all()

    return {
        "subject_id": subject_id,
        "route_filter": route,
        "total": len(claims),
        "claims": [
            {
                "id": c.id,
                "claim_text": c.claim_text,
                "category": c.category,
                "source": c.source,
                "route": c.verifiable,
                "reasoning": c.classifier_reasoning,
                "verification_status": c.verification_status,
            }
            for c in claims
        ],
    }


@router.post("/claims/{claim_id}/agent-verify")
def agent_verify_claim(claim_id: str, db: Session = Depends(get_db)):
    """
    Run the Bright Data agentic loop for a single ai/hybrid claim.
    For hybrid claims, sets status to awaiting_human after AI completes.
    """
    service = BrightDataAgentService(db)
    try:
        result = service.verify_claim(claim_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return {
        "claim_id": claim_id,
        "ai_verdict": result.ai_verdict,
        "ai_verdict_notes": result.ai_verdict_notes,
        "tools_used": result.tool_calls_made,
        "sources_found": len(result.sources),
    }


@router.post("/subjects/{subject_id}/agent-verify-all")
def agent_verify_all(subject_id: str, db: Session = Depends(get_db)):
    """
    Run the Bright Data agent on all ai and hybrid claims for a subject
    that have not yet been agent-verified.
    """
    claims = (
        db.query(Claim)
        .filter(
            Claim.subject_id == subject_id,
            Claim.verifiable.in_(["ai", "hybrid"]),
            Claim.agent_status.is_(None),
        )
        .all()
    )

    if not claims:
        return {"message": "No eligible claims found", "processed": 0}

    service = BrightDataAgentService(db)
    results = []
    errors = []

    for claim in claims:
        try:
            result = service.verify_claim(claim.id)
            results.append({
                "claim_id": claim.id,
                "route": claim.verifiable,
                "ai_verdict": result.ai_verdict,
                "tools_used": result.tool_calls_made,
            })
        except Exception as exc:
            errors.append({"claim_id": claim.id, "error": str(exc)})

    return {
        "subject_id": subject_id,
        "processed": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


def _breakdown(results) -> dict:
    counts = {"ai": 0, "human": 0, "hybrid": 0, "no": 0}
    for r in results:
        counts[r.route] = counts.get(r.route, 0) + 1
    return counts
