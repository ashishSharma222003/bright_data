"""
Claim extractor — takes raw text from document_parser and asks Claude
to pull out every verifiable claim as structured data.

Uses Claude's tool-use (structured output) feature so the response is
always schema-valid — no JSON parsing gymnastics needed.

A "claim" is any statement that can be independently verified:
  - Employment: company, role, dates
  - Education: institution, degree, graduation year
  - Certification: issuer, cert name, date
  - Identity: name, DOB, nationality
  - Vendor: registration number, GST/PAN, contract value, party names
"""

from dataclasses import dataclass, field
from typing import Optional

import anthropic

from backend.config import ANTHROPIC_API_KEY, CLAUDE_MODEL


@dataclass
class ExtractedClaim:
    claim_text: str
    source: Optional[str] = None        # institution / employer / issuer
    date_mentioned: Optional[str] = None
    category: Optional[str] = None      # employment | education | certification | identity | vendor | other


@dataclass
class ExtractionResult:
    claims: list[ExtractedClaim] = field(default_factory=list)
    raw_text: str = ""                  # original text passed in, stored for audit


# Tool schema — Claude is forced to return data matching this shape
_EXTRACT_TOOL = {
    "name": "extract_claims",
    "description": (
        "Extract every verifiable claim from the document text. "
        "Only include claims that can be checked against an external source."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim_text": {
                            "type": "string",
                            "description": "The exact verifiable statement.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Institution, employer, or issuer named in the claim.",
                        },
                        "date_mentioned": {
                            "type": "string",
                            "description": "Any date or date range referenced in the claim.",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["employment", "education", "certification", "identity", "vendor", "other"],
                            "description": "Type of claim.",
                        },
                    },
                    "required": ["claim_text", "category"],
                },
            }
        },
        "required": ["claims"],
    },
}

_SYSTEM_PROMPT = (
    "You are a compliance analyst reviewing documents for background verification. "
    "Extract only claims that can be independently verified against an external source. "
    "Do not include opinions, formatting artifacts, or boilerplate text."
)


class ClaimExtractorService:
    """
    Sends extracted document text to Claude using tool-use (structured output)
    and returns typed ExtractedClaim objects.

    Usage:
        service = ClaimExtractorService()
        result = service.extract(raw_text="...", doc_type="resume")
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    def extract(self, raw_text: str, doc_type: str) -> ExtractionResult:
        user_message = (
            f"Document type: {doc_type}\n\n"
            f"Document text:\n---\n{raw_text[:12000]}\n---\n\n"
            "Call extract_claims with every verifiable claim you find."
        )

        message = self.client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            tools=[_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_claims"},  # force tool use
            messages=[{"role": "user", "content": user_message}],
        )

        claims = self._parse_tool_result(message)
        return ExtractionResult(claims=claims, raw_text=raw_text)

    def _parse_tool_result(self, message: anthropic.types.Message) -> list[ExtractedClaim]:
        for block in message.content:
            if block.type == "tool_use" and block.name == "extract_claims":
                raw_claims = block.input.get("claims", [])
                return [
                    ExtractedClaim(
                        claim_text=item["claim_text"],
                        source=item.get("source"),
                        date_mentioned=item.get("date_mentioned"),
                        category=item.get("category"),
                    )
                    for item in raw_claims
                ]
        return []
