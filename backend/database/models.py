import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Text, JSON
from backend.database.db import Base


def generate_uuid():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


class Company(Base):
    __tablename__ = "companies"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    email = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="hr")  # hr | admin
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class Subject(Base):
    """
    The person or vendor whose background/compliance is being checked.
    Created once per check request by an HR/admin user.
    """
    __tablename__ = "subjects"

    id = Column(String, primary_key=True, default=generate_uuid)
    company_id = Column(String, ForeignKey("companies.id"), nullable=False)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)

    # mode determines which document types are expected
    mode = Column(String, nullable=False)  # hr | vendor

    # personal / vendor identity fields
    full_name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)

    # HR-mode specific
    date_of_birth = Column(String, nullable=True)
    nationality = Column(String, nullable=True)

    # Vendor-mode specific
    company_name = Column(String, nullable=True)
    registration_number = Column(String, nullable=True)  # GST / PAN / reg doc number

    # consent
    consent_given = Column(Boolean, default=False)
    consent_signed_at = Column(DateTime(timezone=True), nullable=True)

    # overall check status
    status = Column(String, default="pending")  # pending | in_progress | complete | flagged

    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Document(Base):
    """
    A file uploaded for a subject. Path is a storage key resolved by the
    storage backend (local path today, S3 key tomorrow).
    """
    __tablename__ = "documents"

    id = Column(String, primary_key=True, default=generate_uuid)
    subject_id = Column(String, ForeignKey("subjects.id"), nullable=False)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=False)

    doc_type = Column(String, nullable=False)  # resume | degree | offer_letter | contract | pan | gst | reg_doc
    original_filename = Column(String, nullable=False)
    storage_key = Column(String, nullable=False)  # resolved by storage backend
    mime_type = Column(String, nullable=True)
    size_bytes = Column(String, nullable=True)

    # populated after extraction step
    extracted_text = Column(Text, nullable=True)
    extraction_status = Column(String, default="pending")  # pending | done | failed

    uploaded_at = Column(DateTime(timezone=True), default=utcnow)


class Claim(Base):
    """
    A single verifiable claim extracted from a document by Claude.
    e.g. "Worked at Acme Corp from 2018–2021"
    """
    __tablename__ = "claims"

    id = Column(String, primary_key=True, default=generate_uuid)
    subject_id = Column(String, ForeignKey("subjects.id"), nullable=False)
    document_id = Column(String, ForeignKey("documents.id"), nullable=True)

    claim_text = Column(Text, nullable=False)
    category = Column(String, nullable=True)      # employment | education | certification | identity | vendor | other
    source = Column(String, nullable=True)        # institution / employer / issuer
    date_mentioned = Column(String, nullable=True)

    # classifier output
    verifiable = Column(String, nullable=True)           # ai | human | hybrid | no
    classifier_reasoning = Column(Text, nullable=True)   # why this route was chosen

    # overall claim lifecycle status
    verification_status = Column(String, default="pending")
    # pending | ai_in_progress | awaiting_human | human_in_progress | verified | mismatch | not_found | flagged

    # --- AI / Bright Data agent result (ai + hybrid routes) ---
    agent_status = Column(String, nullable=True)         # pending | running | done | failed
    agent_result = Column(JSON, nullable=True)           # raw search hits, sources, URLs found
    ai_verdict = Column(String, nullable=True)           # verified | mismatch | not_found
    ai_verdict_notes = Column(Text, nullable=True)       # AI summary of what it found

    # --- Human review result (human + hybrid routes) ---
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    human_verdict = Column(String, nullable=True)        # verified | mismatch | not_found | flagged
    reviewer_notes = Column(Text, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    # --- Final merged verdict (set after both sides complete for hybrid) ---
    final_verdict = Column(String, nullable=True)        # verified | mismatch | not_found | flagged
    final_verdict_notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow)
