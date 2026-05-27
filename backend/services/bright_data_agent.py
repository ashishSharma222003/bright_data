"""
Bright Data agentic verification pipeline.

Handles claims routed as 'ai' or 'hybrid'.

Agentic loop:
  Claude decides which Bright Data tool to call based on the claim.
  It keeps calling tools until it has enough evidence to reach a verdict.
  The loop ends when Claude returns a final_verdict tool call.

Bright Data tools available to the agent:
  - serp_search        : Google/Bing search results for a query
  - web_unlocker       : Fetch a specific URL that may be behind bot protection
  - scrape_linkedin    : Scrape a LinkedIn company or profile page
  - scrape_mca         : Scrape MCA (Ministry of Corporate Affairs) for company info
  - scrape_court       : Scrape court record databases
  - scrape_job_boards  : Search job boards for employer/role verification

Each tool returns raw data. Claude synthesises across multiple tool calls
before producing a final verdict.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic
import httpx
from sqlalchemy.orm import Session

from backend.config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from backend.database.models import Claim, utcnow

# ---------------------------------------------------------------------------
# Bright Data MCP config
# Replace with your actual Bright Data API credentials
# ---------------------------------------------------------------------------
BRIGHT_DATA_API_KEY = ""   # loaded from env in production
BRIGHT_DATA_BASE_URL = "https://api.brightdata.com"

MAX_AGENT_ITERATIONS = 10  # hard cap on tool call rounds


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class AgentVerificationResult:
    claim_id: str
    ai_verdict: str                          # verified | mismatch | not_found
    ai_verdict_notes: str
    sources: list[dict] = field(default_factory=list)   # URLs / evidence collected
    tool_calls_made: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bright Data tool definitions (passed to Claude as tools)
# ---------------------------------------------------------------------------

_BRIGHT_DATA_TOOLS = [
    {
        "name": "serp_search",
        "description": (
            "Search Google/Bing for public information about a claim. "
            "Use for: employer existence, news about a company, general fact-checking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "num_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_unlocker",
        "description": (
            "Fetch a specific URL that may be behind bot protection or rate limits. "
            "Use when you have a direct URL to verify (company website, LinkedIn, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "scrape_linkedin",
        "description": "Scrape a LinkedIn company or profile page for employment verification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "Company name or person name to search on LinkedIn."},
            },
            "required": ["search_query"],
        },
    },
    {
        "name": "scrape_mca",
        "description": (
            "Look up a company on MCA (Ministry of Corporate Affairs) India. "
            "Use for verifying CIN, GST, PAN, or company registration."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name_or_number": {"type": "string", "description": "Company name, CIN, GST, or PAN number."},
            },
            "required": ["company_name_or_number"],
        },
    },
    {
        "name": "scrape_court",
        "description": "Search court record databases for criminal or civil case information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_name": {"type": "string"},
                "jurisdiction": {"type": "string", "description": "State or country jurisdiction.", "default": "India"},
            },
            "required": ["person_name"],
        },
    },
    {
        "name": "scrape_job_boards",
        "description": "Search job boards (Naukri, LinkedIn Jobs, Indeed) to verify a person's listed role/employer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "employer": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": ["employer"],
        },
    },
    {
        "name": "final_verdict",
        "description": (
            "Call this when you have collected enough evidence to reach a conclusion. "
            "Do NOT call this until you have done at least one search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["verified", "mismatch", "not_found"],
                    "description": (
                        "verified = claim is confirmed by evidence found, "
                        "mismatch = evidence contradicts the claim, "
                        "not_found = no evidence either way."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "2-3 sentence summary of what was found and why this verdict was chosen.",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs or source names that informed the verdict.",
                },
            },
            "required": ["verdict", "summary"],
        },
    },
]

_SYSTEM_PROMPT = """\
You are an automated background verification agent with access to Bright Data web tools.

Your job: verify a single claim about a person or vendor using the tools available.

Strategy:
1. Choose the most relevant tool(s) for the claim type.
2. For employment claims: try serp_search then scrape_linkedin.
3. For company/vendor claims: try scrape_mca then serp_search.
4. For court/criminal claims: use scrape_court.
5. For education claims: serp_search the institution + person name.
6. Use web_unlocker if you find a specific URL worth fetching.
7. Once you have enough evidence (or exhausted reasonable searches), call final_verdict.

Be thorough but efficient. Do not call final_verdict after only one failed search — try at least 2 tools.
"""


# ---------------------------------------------------------------------------
# Bright Data API call executor
# ---------------------------------------------------------------------------

class BrightDataExecutor:
    """Executes actual Bright Data API calls. Swap internals for real endpoints."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def serp_search(self, query: str, num_results: int = 5) -> dict:
        # Real call: Bright Data SERP API
        # Stub returns structure so agent can parse it
        return {"results": [], "query": query, "note": "stub — wire to Bright Data SERP API"}

    def web_unlocker(self, url: str) -> dict:
        return {"url": url, "content": "", "note": "stub — wire to Bright Data Web Unlocker"}

    def scrape_linkedin(self, search_query: str) -> dict:
        return {"search_query": search_query, "profiles": [], "note": "stub — wire to Bright Data LinkedIn scraper"}

    def scrape_mca(self, company_name_or_number: str) -> dict:
        return {"query": company_name_or_number, "records": [], "note": "stub — wire to Bright Data MCA scraper"}

    def scrape_court(self, person_name: str, jurisdiction: str = "India") -> dict:
        return {"person_name": person_name, "cases": [], "note": "stub — wire to Bright Data court scraper"}

    def scrape_job_boards(self, employer: str, role: str = "") -> dict:
        return {"employer": employer, "role": role, "listings": [], "note": "stub — wire to Bright Data job board scraper"}

    def execute(self, tool_name: str, tool_input: dict) -> dict:
        dispatch = {
            "serp_search": lambda: self.serp_search(**tool_input),
            "web_unlocker": lambda: self.web_unlocker(**tool_input),
            "scrape_linkedin": lambda: self.scrape_linkedin(**tool_input),
            "scrape_mca": lambda: self.scrape_mca(**tool_input),
            "scrape_court": lambda: self.scrape_court(**tool_input),
            "scrape_job_boards": lambda: self.scrape_job_boards(**tool_input),
        }
        fn = dispatch.get(tool_name)
        if not fn:
            return {"error": f"Unknown tool: {tool_name}"}
        return fn()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class BrightDataAgentService:
    """
    Agentic verification loop for a single claim.

    Claude autonomously decides which Bright Data tools to call,
    collects evidence, and produces a final verdict.

    Usage:
        service = BrightDataAgentService(db)
        result = service.verify_claim(claim_id="...")
    """

    def __init__(self, db: Session):
        self.db = db
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.executor = BrightDataExecutor(api_key=BRIGHT_DATA_API_KEY)

    def verify_claim(self, claim_id: str) -> AgentVerificationResult:
        claim = self.db.query(Claim).filter(Claim.id == claim_id).first()
        if not claim:
            raise ValueError(f"Claim {claim_id} not found")
        if claim.verifiable not in ("ai", "hybrid"):
            raise ValueError(f"Claim {claim_id} is routed '{claim.verifiable}' — agent only handles ai/hybrid")

        # Mark as running
        claim.agent_status = "running"
        claim.verification_status = "ai_in_progress"
        self.db.commit()

        try:
            result = self._run_agent_loop(claim)
            self._persist_result(claim, result)
            return result
        except Exception as exc:
            claim.agent_status = "failed"
            self.db.commit()
            raise RuntimeError(f"Agent failed for claim {claim_id}: {exc}") from exc

    # --- agentic loop ----------------------------------------------------

    def _run_agent_loop(self, claim: Claim) -> AgentVerificationResult:
        messages = [
            {
                "role": "user",
                "content": (
                    f"Verify this claim:\n\n"
                    f"Claim: {claim.claim_text}\n"
                    f"Category: {claim.category}\n"
                    f"Source/Employer/Institution: {claim.source or 'unknown'}\n"
                    f"Date mentioned: {claim.date_mentioned or 'unknown'}\n\n"
                    "Use the available tools to find evidence, then call final_verdict."
                ),
            }
        ]

        tool_calls_made = []
        collected_sources = []

        for _ in range(MAX_AGENT_ITERATIONS):
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=2048,
                system=_SYSTEM_PROMPT,
                tools=_BRIGHT_DATA_TOOLS,
                messages=messages,
            )

            # Append assistant turn
            messages.append({"role": "assistant", "content": response.content})

            # Check stop reason
            if response.stop_reason == "end_turn":
                # Claude decided to stop without calling final_verdict — treat as not_found
                return AgentVerificationResult(
                    claim_id=claim.id,
                    ai_verdict="not_found",
                    ai_verdict_notes="Agent stopped without reaching a verdict.",
                    sources=collected_sources,
                    tool_calls_made=tool_calls_made,
                )

            # Process tool calls
            tool_results = []
            verdict_block = None

            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_calls_made.append(block.name)

                if block.name == "final_verdict":
                    verdict_block = block
                    break

                # Execute Bright Data tool
                raw_result = self.executor.execute(block.name, block.input)

                # Collect any URLs found
                if "results" in raw_result:
                    for r in raw_result["results"]:
                        if isinstance(r, dict) and r.get("url"):
                            collected_sources.append({"url": r["url"], "tool": block.name})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(raw_result),
                })

            # If Claude called final_verdict, we're done
            if verdict_block:
                sources_from_verdict = [
                    {"url": s, "tool": "final_verdict"}
                    for s in verdict_block.input.get("sources", [])
                ]
                return AgentVerificationResult(
                    claim_id=claim.id,
                    ai_verdict=verdict_block.input["verdict"],
                    ai_verdict_notes=verdict_block.input["summary"],
                    sources=collected_sources + sources_from_verdict,
                    tool_calls_made=tool_calls_made,
                )

            # Feed tool results back for next iteration
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        # Exceeded max iterations
        return AgentVerificationResult(
            claim_id=claim.id,
            ai_verdict="not_found",
            ai_verdict_notes="Agent reached maximum iterations without a conclusive verdict.",
            sources=collected_sources,
            tool_calls_made=tool_calls_made,
        )

    # --- persistence -----------------------------------------------------

    def _persist_result(self, claim: Claim, result: AgentVerificationResult) -> None:
        claim.agent_status = "done"
        claim.agent_result = {
            "tool_calls": result.tool_calls_made,
            "sources": result.sources,
        }
        claim.ai_verdict = result.ai_verdict
        claim.ai_verdict_notes = result.ai_verdict_notes

        if claim.verifiable == "ai":
            # AI-only: agent verdict is the final verdict
            claim.final_verdict = result.ai_verdict
            claim.final_verdict_notes = result.ai_verdict_notes
            claim.verification_status = result.ai_verdict  # verified | mismatch | not_found
        else:
            # hybrid: AI done, now needs human review
            claim.verification_status = "awaiting_human"

        self.db.commit()
