"""
Claim classifier — decides the verification route for each extracted claim.

Routes:
  ai      → fully verifiable via automated web search / Bright Data agent
             e.g. employment at a known public company, GST/PAN number, court records
  human   → requires physical check, phone call, or document inspection
             e.g. degree certificate authenticity, address visit, criminal record
  hybrid  → AI does the initial pass, human confirms the result
             e.g. employment at a small/unknown company, ambiguous dates
  no      → not verifiable by any means (subjective, opinion, boilerplate)

Uses Claude tool-use (forced schema) so the output is always structured.
One API call classifies all claims for a document in a single batch.
"""

from dataclasses import dataclass
from typing import Optional

import anthropic
from sqlalchemy.orm import Session

from backend.config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from backend.database.models import Claim


@dataclass
class ClassifiedClaim:
    claim_id: str
    route: str                        # ai | human | hybrid | no
    reasoning: str


# --- Tool schema ---------------------------------------------------------

_CLASSIFY_TOOL = {
    "name": "classify_claims",
    "description": (
        "For each claim decide how it should be verified: "
        "'ai' = automated web check only, "
        "'human' = physical/manual check required, "
        "'hybrid' = AI first pass then human confirmation, "
        "'no' = cannot be verified at all."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_id": {
                            "type": "string",
                            "description": "The ID of the claim being classified.",
                        },
                        "route": {
                            "type": "string",
                            "enum": ["ai", "human", "hybrid", "no"],
                            "description": "Verification route for this claim.",
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence explaining why this route was chosen.",
                        },
                    },
                    "required": ["claim_id", "route", "reasoning"],
                },
            }
        },
        "required": ["classifications"],
    },
}

_SYSTEM_PROMPT = """\
You are a background verification specialist. For each claim decide the best verification route:

- "ai": The claim references a publicly verifiable fact — employment at a known company,
  a GST/PAN/CIN number, a court record, a university degree from a well-known institution,
  or any fact findable via web search or official public databases.

- "human": The claim requires physical inspection, a direct phone call, or an in-person visit —
  physical degree certificate authenticity check, criminal record from a local court,
  address visit, reference calls.

- "hybrid": The claim is partially verifiable online but the result needs human confirmation —
  employment at an obscure or small company, dates that could not be confirmed precisely,
  or any case where AI verification alone may not be conclusive.

- "no": The claim is subjective, an opinion, or structurally impossible to verify externally.

Be conservative: when in doubt between ai and hybrid, choose hybrid.
When in doubt between human and hybrid, choose hybrid.
"""


# --- Service -------------------------------------------------------------

class ClaimClassifierService:
    """
    Classifies a batch of claims for a subject in a single Claude call
    and persists the route + reasoning back to the DB.

    Usage:
        service = ClaimClassifierService(db)
        results = service.classify_for_subject(subject_id="...")
    """

    def __init__(self, db: Session):
        self.db = db
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    def classify_for_subject(self, subject_id: str) -> list[ClassifiedClaim]:
        """Classify all pending claims for a subject, persist results, return them."""
        claims = (
            self.db.query(Claim)
            .filter(
                Claim.subject_id == subject_id,
                Claim.verifiable.is_(None),          # only unclassified claims
            )
            .all()
        )

        if not claims:
            return []

        classified = self._call_claude(claims)
        self._persist(classified)
        return classified

    def classify_for_document(self, document_id: str) -> list[ClassifiedClaim]:
        """Classify all pending claims for a single document, persist results, return them."""
        claims = (
            self.db.query(Claim)
            .filter(
                Claim.document_id == document_id,
                Claim.verifiable.is_(None),
            )
            .all()
        )

        if not claims:
            return []

        classified = self._call_claude(claims)
        self._persist(classified)
        return classified

    # --- internals -------------------------------------------------------

    def _call_claude(self, claims: list[Claim]) -> list[ClassifiedClaim]:
        claims_payload = "\n".join(
            f"- ID: {c.id} | category: {c.category} | source: {c.source or 'unknown'} "
            f"| date: {c.date_mentioned or 'unknown'} | claim: {c.claim_text}"
            for c in claims
        )

        user_message = (
            f"Classify each of the following {len(claims)} claims.\n\n"
            f"{claims_payload}\n\n"
            "Call classify_claims with a classification for every claim listed."
        )

        message = self.client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            tools=[_CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify_claims"},
            messages=[{"role": "user", "content": user_message}],
        )

        return self._parse_tool_result(message)

    def _parse_tool_result(self, message: anthropic.types.Message) -> list[ClassifiedClaim]:
        for block in message.content:
            if block.type == "tool_use" and block.name == "classify_claims":
                return [
                    ClassifiedClaim(
                        claim_id=item["claim_id"],
                        route=item["route"],
                        reasoning=item["reasoning"],
                    )
                    for item in block.input.get("classifications", [])
                ]
        return []

    def _persist(self, classified: list[ClassifiedClaim]) -> None:
        id_map = {c.claim_id: c for c in classified}
        claims = self.db.query(Claim).filter(Claim.id.in_(id_map.keys())).all()

        for claim in claims:
            result = id_map[claim.id]
            claim.verifiable = result.route
            claim.classifier_reasoning = result.reasoning
            # move status forward so the next stage knows it's ready
            claim.verification_status = "in_progress" if result.route != "no" else "not_found"

        self.db.commit()
