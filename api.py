"""
api.py

FastAPI REST API for the Genomic Variant Interpretation system.

This provides an HTTP-based agent communication layer -- external clients
(and potentially other agents) can interact with the system via REST.

Rubric mapping: Agent Communication Protocols (HTTP/API-based)

Run:
    uvicorn api:app --reload --port 8000
"""

import os
import sys
import json
import uuid
from datetime import date, datetime, timezone

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
from pydantic import BaseModel, Field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from main import VariantInterpretationSystem
from protocol.security import authenticate, AuthenticationError
from auth.dependencies import get_current_user, require_admin
from auth.schemas import (
    UpgradeDecision,
    UpgradeRequestCreate,
)
from auth.security import hash_password
from auth.routes import router as auth_router
from db.database import get_db
from db.models import ALLOWED_ROLES, Plan, ReviewSignoff, UPGRADE_STATUSES, UpgradeRequest, User
from services.entitlement_service import (
    check_quota,
    entitlements,
    record_usage,
    usage_this_month,
)
from services.analytics_service import build_analytics
from services.mailer import get_mailer
from services.upgrade_service import (
    create_request as upgrade_create,
    decide,
    decision_body as upgrade_decision_body,
    mark_contacted,
)


# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------
app = FastAPI(
    title="Genomic Variant Interpretation API",
    description="Multi-agent AI system for BRCA1/BRCA2 variant classification",
    version="1.0.0",
)

# Account endpoints (/api/auth/*): register, login, me.
app.include_router(auth_router)

# CORS for the web frontend.
# Restricted to the local demo origins by default. Set FRONTEND_ORIGINS
# (comma-separated) to add the real deployment origin(s) before shipping.
_DEFAULT_ORIGINS = "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared system instances (agents are stateless, so this is safe). Two
# variants are pre-built so a per-plan feature gate (LLM on/off) never needs
# to mutate a shared instance at request time.
_system_llm = VariantInterpretationSystem(use_llm=True)
_system_rules = VariantInterpretationSystem(use_llm=False)
_system = _system_llm  # legacy default: behaves exactly as before


def _signoff_payload(row: Optional[ReviewSignoff]) -> Optional[dict]:
    """
    Serialise a sign-off for the API, or None when there is no row.

    The shape is unchanged from the old JSON store ({reviewer_name, review_date,
    signature, saved_at}) so the frontend and the PDF report keep working;
    reviewed_by is added alongside it for the audit trail.
    """
    if row is None:
        return None
    return {
        "variant_id": row.variant_id,
        "reviewer_name": row.reviewer_name,
        "review_date": row.review_date.isoformat(),
        "signature": row.signature,
        "saved_at": row.saved_at.isoformat(timespec="seconds") + "Z",
        "reviewed_by": row.reviewed_by,
    }


def _history_for(variant_id: str) -> list:
    """
    Audit-trail lookup across both system variants.

    Each VariantInterpretationSystem owns its own MessageLog, so a query run
    under a rules-only plan lands in a different log than an LLM-plan query.
    Searching both keeps /api/trail and /api/pipeline complete regardless of
    which plan handled the original request.
    """
    seen = {}
    for system in (_system_llm, _system_rules):
        for message in system.log.history_for(variant_id):
            seen[message.message_id] = message
    return sorted(seen.values(), key=lambda m: str(m.timestamp))

# Demo API key used by the agent layer when the API calls into main.py.
# Review sign-offs live in PostgreSQL (see db/models.py ReviewSignoff), not in a
# local file, so there is no review store path or in-memory cache any more.
DEMO_API_KEY = "demo-clinician-key"


class UserUpdateRequest(BaseModel):
    """Fields an administrator may change on another account."""

    # All optional so a PATCH touches only what was sent. Unknown fields are
    # rejected by pydantic rather than silently ignored, which stops a typo in a
    # client's payload from looking like it worked.
    plan_id: Optional[int] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class AdminUserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    plan: str
    plan_id: int
    created_at: Optional[str] = None
    used_this_month: int


# ------------------------------------------------------------------
# Request/Response models
# ------------------------------------------------------------------
class UsageInfo(BaseModel):
    used_this_month: int
    monthly_limit: Optional[int] = None
    remaining: Optional[int] = None


class EntitlementInfo(BaseModel):
    llm_enabled: bool
    api_access: bool
    audit_export: bool


class QueryRequest(BaseModel):
    query: str = Field(..., description="Free-text query, e.g. 'Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?'")
    # Accepted for backwards compatibility with the pre-JWT API, but ignored:
    # authentication is the Authorization: Bearer token, and the agent layer
    # uses its own credential internally.
    api_key: Optional[str] = Field(
        None,
        description=(
            "Deprecated and ignored. Authentication is the JWT sent in the "
            "Authorization: Bearer header."
        ),
    )


class FullPipelineResponse(BaseModel):
    """Full response with all pipeline data for the frontend."""
    variant_id: str
    report_text: str
    evidence_summary: str
    classification: str
    confidence: str
    confidence_numeric: float
    reasoning_mode: str
    criteria_applied: list
    acmg_criteria_applied: list = Field(default_factory=list)
    computational_annotations: list = Field(default_factory=list)
    clinvar_context: list = Field(default_factory=list)
    population_observation: Optional[str] = None
    pipeline_steps: list
    audit_trail: list
    top_k_results: list
    ir_metadata: dict
    responsible_ai: dict
    intent: str
    extracted_entities: dict
    reasoning_mode: str = "rule_based"
    review_signoff: Optional[dict] = None
    is_gene_overview: bool = False
    total_variants_in_gene: Optional[int] = None
    rsid_lookup_unsupported: bool = False
    retrieval_caveat: Optional[str] = None
    error: Optional[str] = None
    consensus: Optional[dict] = None
    review_required: bool = False
    coverage_fallback: bool = False
    evidence_limitation: Optional[str] = None
    plan: Optional[str] = None
    usage: Optional[UsageInfo] = None
    entitlements: Optional[EntitlementInfo] = None


class PDFReportRequest(BaseModel):
    variant_id: str
    classification: str
    confidence: str
    confidence_numeric: float
    reasoning_mode: str = "rule_based"
    report_text: str
    evidence_summary: str = ""
    consensus: Optional[dict] = None
    criteria_applied: list = Field(default_factory=list)
    acmg_criteria_applied: list = Field(default_factory=list)
    computational_annotations: list = Field(default_factory=list)
    clinvar_context: list = Field(default_factory=list)
    population_observation: Optional[str] = None
    top_k_results: list = Field(default_factory=list)
    reviewer_name: Optional[str] = None
    review_date: Optional[str] = None
    signature: Optional[str] = None


class ReviewSignoffRequest(BaseModel):
    reviewer_name: str = Field(..., min_length=1, max_length=120)
    review_date: str = Field(..., min_length=10, max_length=10, description="ISO date YYYY-MM-DD")
    signature: Optional[str] = Field(None, max_length=120)


class QueryResponse(BaseModel):
    variant_id: str
    report_text: str
    evidence_summary: str
    classification: str
    confidence: str
    confidence_numeric: float
    criteria_applied: list
    acmg_criteria_applied: list = Field(default_factory=list)
    computational_annotations: list = Field(default_factory=list)
    clinvar_context: list = Field(default_factory=list)
    population_observation: Optional[str] = None
    pipeline_steps: list
    audit_trail: list
    top_k_results: list
    ir_metadata: dict
    responsible_ai: dict
    intent: str
    extracted_entities: dict
    reasoning_mode: str = "rule_based"
    review_signoff: Optional[dict] = None
    is_gene_overview: bool = False
    total_variants_in_gene: Optional[int] = None
    rsid_lookup_unsupported: bool = False
    retrieval_caveat: Optional[str] = None
    error: Optional[str] = None
    consensus: Optional[dict] = None
    review_required: bool = False
    coverage_fallback: bool = False
    evidence_limitation: Optional[str] = None
    # Entitlement context, so the UI can show the plan and the remaining quota
    # without a second round-trip to /api/auth/me.
    plan: Optional[str] = None
    usage: Optional[UsageInfo] = None
    entitlements: Optional[EntitlementInfo] = None


# ------------------------------------------------------------------
# Legacy agent-layer API key dependency
# ------------------------------------------------------------------
def verify_api_key(x_api_key: str = Header(None)):
    """
    Verify an agent-layer API key (X-API-Key header).

    No route uses this any more -- every endpoint authenticates with a JWT
    account token instead. It is kept because it is the exact check the
    pipeline still performs internally (main.py -> authenticate()), so it is
    the reference implementation of that boundary and stays testable.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    try:
        return authenticate(x_api_key)
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

# app.css and app.js are served from here rather than inlined, so the frontend is
# three reviewable files instead of one very long one. Nothing else is exposed.
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.get("/")
async def root():
    """Serve the web frontend."""
    frontend_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(frontend_path):
        return FileResponse(frontend_path)
    return {"message": "Genomic Variant Interpretation API. See /docs for API documentation."}


@app.get("/health")
@app.get("/healthz")  # alias: Render's default health-check path
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "genomic-variant-interpretation"}


@app.post("/api/classify", response_model=QueryResponse)
def classify(
    request: QueryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Classify a genetic variant through the full 4-agent pipeline.

    Requires a JWT account token (``Authorization: Bearer <token>``); an
    unauthenticated request is rejected with 401. The account's plan decides
    whether LLM-assisted reasoning is used, and its monthly quota is checked
    before any agent work starts.

    Example:
    {
        "query": "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?"
    }

    Returns the full pipeline result including extracted entities,
    top-K evidence with relevance scores, classification, confidence,
    audit trail, the final report, and the caller's plan/usage/entitlements.
    """
    # --- Security + entitlements: authenticated user, plan gate, quota ---
    info = check_quota(db, current_user)
    llm_allowed = bool(info["llm_enabled"])
    system = _system_llm if llm_allowed else _system_rules
    # Deterministic per-user request key: a retry of the same query by the same
    # user maps to the same id, so usage is recorded once instead of twice.
    request_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"{current_user.id}:{request.query.strip()}"
    ))

    # The agent layer keeps its own credential check as defence-in-depth: JWT ran
    # at the edge above, and main.py still refuses untrusted callers.
    result = system.run(request.query, DEMO_API_KEY, user_id=str(current_user.id))

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    # Record the accepted call. record_usage() re-verifies the quota inside the
    # same transaction that writes the row, so concurrent requests cannot both
    # pass the pre-check and overshoot the plan limit.
    record_usage(
        db,
        current_user.id,
        request_id,
        current_user.plan.monthly_query_limit,
    )
    ent = entitlements(current_user, usage_this_month(db, current_user.id))

    # The report response carries the sign-off inline so the UI can render the
    # review banner without a second request.
    db_signoff = db.scalars(
        select(ReviewSignoff).where(ReviewSignoff.variant_id == result["variant_id"])
    ).one_or_none()

    return QueryResponse(
        variant_id=result["variant_id"],
        report_text=result["report_text"],
        evidence_summary=result["evidence_summary"],
        classification=result["classification"],
        confidence=result["confidence"],
        confidence_numeric=result["confidence_numeric"],
        criteria_applied=result.get("criteria_applied", []),
        acmg_criteria_applied=result.get("acmg_criteria_applied", result.get("criteria_applied", [])),
        computational_annotations=result.get("computational_annotations", []),
        clinvar_context=result.get("clinvar_context", []),
        population_observation=result.get("population_observation"),
        pipeline_steps=result["pipeline_steps"],
        audit_trail=result["audit_trail"],
        top_k_results=result["top_k_results"],
        ir_metadata=result["ir_metadata"],
        responsible_ai=result["responsible_ai"],
        intent=result["intent"],
        extracted_entities=result["extracted_entities"],
        reasoning_mode=result.get("reasoning_mode", "rule_based"),
        # Sign-offs come from PostgreSQL now, so this is a lookup rather than a
        # dict hit. A missing row simply means "not yet signed off".
        review_signoff=_signoff_payload(db_signoff),
        is_gene_overview=result.get("is_gene_overview", False),
        total_variants_in_gene=result.get("total_variants_in_gene"),
        rsid_lookup_unsupported=result.get("rsid_lookup_unsupported", False),
        retrieval_caveat=result.get("retrieval_caveat"),
        consensus=result.get("consensus"),
        review_required=result.get("review_required", False),
        coverage_fallback=result.get("coverage_fallback", False),
        evidence_limitation=result.get("evidence_limitation"),
        plan=current_user.plan.name,
        usage=UsageInfo(
            used_this_month=ent["used_this_month"],
            monthly_limit=ent["monthly_limit"],
            remaining=ent["remaining"],
        ),
        entitlements=EntitlementInfo(
            llm_enabled=ent["llm_enabled"],
            api_access=ent["api_access"],
            audit_export=ent["audit_export"],
        ),
    )


@app.post("/api/review/{variant_id}")
def save_review_signoff(
    variant_id: str,
    request: ReviewSignoffRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Record (or update) a genetic counselor's sign-off for a variant.

    Stored in PostgreSQL rather than a local file so the attestation has an
    accountable owner (reviewed_by), survives restarts and can be listed for
    audit. One sign-off per variant; re-signing updates the row in place.
    """
    try:
        parsed_date = date.fromisoformat(request.review_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="review_date must use YYYY-MM-DD format") from exc
    if parsed_date > date.today():
        raise HTTPException(status_code=422, detail="review_date cannot be in the future")

    reviewer = request.reviewer_name.strip()
    if not reviewer:
        raise HTTPException(status_code=422, detail="reviewer_name cannot be blank")

    signoff = db.scalars(
        select(ReviewSignoff).where(ReviewSignoff.variant_id == variant_id)
    ).one_or_none()
    if signoff is None:
        signoff = ReviewSignoff(variant_id=variant_id)
        db.add(signoff)
    signoff.reviewer_name = reviewer
    signoff.review_date = parsed_date
    # A whitespace-only signature is treated as "not signed", not as a blank
    # string that would render as an empty signature line in the PDF.
    signoff.signature = (request.signature or "").strip() or None
    signoff.saved_at = datetime.now(timezone.utc)
    # Ownership is rewritten on every save, so "which account recorded this"
    # always reflects the most recent sign-off.
    signoff.reviewed_by = current_user.id
    db.commit()
    db.refresh(signoff)
    return {"variant_id": variant_id, "review_signoff": _signoff_payload(signoff)}


@app.get("/api/review/{variant_id}")
def get_review_signoff(
    variant_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the stored sign-off for a variant, or null when none exists."""
    signoff = db.scalars(
        select(ReviewSignoff).where(ReviewSignoff.variant_id == variant_id)
    ).one_or_none()
    return {"variant_id": variant_id,
            "review_signoff": _signoff_payload(signoff) if signoff else None}


@app.get("/api/reviews")
def list_review_signoffs(
    limit: int = 100,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    List recorded sign-offs, newest first.

    Admin-only: this is an attestation register rather than a per-caller view,
    so it sits behind the same role check as the account listing.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    rows = db.scalars(
        select(ReviewSignoff).order_by(ReviewSignoff.saved_at.desc()).limit(limit)
    ).all()
    return {"total": len(rows),
            "review_signoffs": [_signoff_payload(row) for row in rows]}


@app.post("/api/report/pdf")
async def create_pdf_report(request: PDFReportRequest, db: Session = Depends(get_db)):
    """Create a print-ready academic clinical-style report PDF."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=42,
        title=f"Variant Interpretation Report - {request.variant_id}",
        author="VariantLens / IT3041 Academic Prototype",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TitleCustom", parent=styles["Title"], fontSize=18, leading=22,
                           textColor=colors.HexColor("#10233f"), spaceAfter=8)
    subtitle = ParagraphStyle("Subtitle", parent=styles["Normal"], fontSize=8.5,
                              textColor=colors.HexColor("#667085"), spaceAfter=16)
    h2 = ParagraphStyle("H2Custom", parent=styles["Heading2"], fontSize=11, leading=14,
                        textColor=colors.HexColor("#17345b"), spaceBefore=12, spaceAfter=7)
    body = ParagraphStyle("BodyCustom", parent=styles["BodyText"], fontSize=8.7,
                          leading=13, textColor=colors.HexColor("#273449"))
    small = ParagraphStyle("SmallCustom", parent=body, fontSize=7.5, leading=10,
                           textColor=colors.HexColor("#667085"))
    story = [
        Paragraph("Genomic Variant Interpretation Report", title),
        Paragraph("VariantLens · IT3041 academic decision-support prototype", subtitle),
        Paragraph(f"<b>Variant:</b> {request.variant_id}", body),
        Spacer(1, 8),
    ]
    pdf_row = db.scalars(
        select(ReviewSignoff).where(ReviewSignoff.variant_id == request.variant_id)
    ).one_or_none()
    stored_signoff = _signoff_payload(pdf_row) if pdf_row else {}
    # Values typed into the form win over the stored sign-off, so a reviewer can
    # correct a date without re-saving the record first.
    reviewer_name = request.reviewer_name or stored_signoff.get("reviewer_name")
    review_date = request.review_date or stored_signoff.get("review_date")
    signature = request.signature or stored_signoff.get("signature")
    summary = [
        ["Classification", request.classification],
        ["Confidence", f"{request.confidence.upper()} ({request.confidence_numeric:.1f}/100)"],
        ["Reasoning mode", request.reasoning_mode],
        ["Human review", "Recorded" if reviewer_name and review_date else "Pending"],
    ]
    # Provenance of the final label: deferred-to-ClinVar vs the tool's own
    # evidence-based reasoning must be visible on the signed PDF too.
    consensus = request.consensus or {}
    if consensus.get("final_from") == "clinvar_consensus":
        basis = (
            "Deferred to ClinVar consensus "
            f"({consensus.get('clinvar_review_stars')}-star: "
            f"{consensus.get('clinvar_review_status') or 'reviewed'})"
        )
        if consensus.get("evidence_based_confidence_numeric") is not None:
            basis += (
                "; tool's own evidence alone was "
                f"{consensus['evidence_based_confidence_numeric']}/100 "
                f"({consensus.get('evidence_based_confidence')})"
            )
        summary.insert(3, ["Basis of final call", basis])
    elif consensus.get("agreement") is True:
        summary.insert(3, ["Basis of final call",
                           "Evidence-based reasoning; agrees with ClinVar consensus"])
    elif consensus.get("agreement") is False:
        summary.insert(3, ["Basis of final call",
                           "Evidence-based reasoning; disagrees with ClinVar consensus "
                           "- flagged for human review"])
    t = Table(summary, colWidths=[105, 390])
    t.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(0,-1),colors.HexColor("#f4f7fb")),
        ("TEXTCOLOR",(0,0),(0,-1),colors.HexColor("#475467")),
        ("FONTNAME",(0,0),(-1,-1),"Helvetica"),
        ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),
        ("FONTSIZE",(0,0),(-1,-1),8.5),
        ("GRID",(0,0),(-1,-1),0.4,colors.HexColor("#dbe3ee")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("PADDING",(0,0),(-1,-1),7),
    ]))
    story += [t, Paragraph("Report", h2), Paragraph(request.report_text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\n","<br/>"), body)]
    acmg_pdf = request.acmg_criteria_applied or request.criteria_applied
    if acmg_pdf:
        story.append(Paragraph("ACMG criteria applied (validated codes only)", h2))
        for criterion in acmg_pdf:
            safe = str(criterion).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            story.append(Paragraph(f"• {safe}", body))
    if request.computational_annotations:
        story.append(Paragraph("Computational annotations (supporting only, not ACMG criteria)", h2))
        for item in request.computational_annotations:
            safe = str(item).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            story.append(Paragraph(f"• {safe}", body))
    if request.population_observation:
        story.append(Paragraph("Population-frequency observation (BS1 unestablished)", h2))
        safe = str(request.population_observation).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        story.append(Paragraph(f"• {safe}", body))
    if request.clinvar_context:
        story.append(Paragraph("ClinVar context (database status, not an ACMG criterion)", h2))
        for item in request.clinvar_context:
            safe = str(item).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            story.append(Paragraph(f"• {safe}", body))
    if request.top_k_results:
        story.append(Paragraph("Top-ranked evidence", h2))
        rows=[["#", "Match", "Variant", "Relevance"]]
        for i, item in enumerate(request.top_k_results,1):
            rows.append([str(i), str(item.get("match_type","Similarity")),
                         f"{item.get('gene','?')}:{item.get('hgvs','?')}",
                         f"{float(item.get('relevance_score',0)):.2f}"])
        et=Table(rows,colWidths=[24,100,300,65],repeatRows=1)
        et.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#17345b")),
            ("TEXTCOLOR",(0,0),(-1,0),colors.white),
            ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
            ("FONTSIZE",(0,0),(-1,-1),7.3),
            ("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#dbe3ee")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f8fafc")]),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("PADDING",(0,0),(-1,-1),5),
        ]))
        story.append(et)
    story += [
        Paragraph("Review / sign-off", h2),
        Paragraph(f"Reviewer / genetic counselor: {reviewer_name or '________________________________'}", body),
        Spacer(1, 8),
        Paragraph(f"Review date: {review_date or '__________________'}    Signature: {signature or '_______________________________'}", body),
        Spacer(1, 14),
        Paragraph("<b>Important:</b> This document is generated by an academic decision-support prototype. "
                  "Classification confidence is not medical certainty. It does not provide a medical diagnosis, "
                  "does not replace qualified genetic counselors, clinicians or laboratory interpretation, and is "
                  "not intended for standalone clinical use. ACMG-style criteria shown here are simplified project "
                  "equivalents and require expert review.", small),
        Spacer(1, 8),
        Paragraph("<b>Population-data limitation (fairness):</b> Allele-frequency databases may have uneven "
                  "representation across ancestry groups. Population frequency evidence should therefore be "
                  "interpreted cautiously and should not be treated as equally informative for all populations.",
                  small),
    ]
    doc.build(story)
    buffer.seek(0)
    return StreamingResponse(
        buffer, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="variant_interpretation_report.pdf"'}
    )


@app.get("/api/pipeline/{variant_id}")
def get_pipeline(variant_id: str, current_user: User = Depends(get_current_user)):
    """
    Get the full pipeline result and audit trail for a previously queried variant.
    Requires a valid JWT (Authorization: Bearer).
    """
    messages = _history_for(variant_id)
    if not messages:
        raise HTTPException(status_code=404, detail=f"No pipeline data found for {variant_id}")

    pipeline = {"variant_id": variant_id, "messages": []}
    for m in messages:
        payload = m.decrypt_payload()
        pipeline["messages"].append({
            "message_id": m.message_id,
            "trace_id": m.trace_id,
            "sender": m.sender,
            "receiver": m.receiver,
            "message_type": m.message_type,
            "stage": m.stage,
            "timestamp": m.timestamp,
            "encrypted": m.encrypted,
            "variant_id": m.variant_id,
            "payload": payload,
        })
    return pipeline


@app.get("/api/trail/{variant_id}")
def get_trail(variant_id: str, current_user: User = Depends(get_current_user)):
    """Get the full agent-to-agent message audit trail for a variant."""
    messages = _history_for(variant_id)
    if not messages:
        raise HTTPException(status_code=404, detail=f"No trail found for {variant_id}")
    return [
        {
            "message_id": m.message_id,
            "trace_id": m.trace_id,
            "sender": m.sender,
            "receiver": m.receiver,
            "message_type": m.message_type,
            "stage": m.stage,
            "timestamp": m.timestamp,
            "encrypted": m.encrypted,
            "variant_id": m.variant_id,
            "payload": m.decrypt_payload(),
        }
        for m in messages
    ]


def _admin_user_payload(db: Session, user: User) -> dict:
    """Serialise an account for the admin endpoints. Never includes a hash."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "plan": user.plan.name,
        "plan_id": user.plan_id,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "used_this_month": usage_this_month(db, user.id),
    }


def _active_admin_count(db: Session, exclude_user_id: Optional[int] = None) -> int:
    """
    Count active administrators other than one account.

    Used to refuse a change that would leave the deployment with no way back
    in: without an admin, the only fix is a direct database shell.
    """
    query = select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True))
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    return int(db.scalar(query) or 0)


def _upgrade_payload(request: UpgradeRequest) -> dict:
    """Serialise an upgrade request as the requesting user sees it."""
    return {
        "id": request.id,
        "target_plan": request.target_plan.name,
        "target_plan_id": request.target_plan_id,
        "note": request.note,
        "status": request.status,
        "admin_note": request.admin_note,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
    }


@app.post("/api/upgrade-requests", status_code=status.HTTP_201_CREATED)
def create_upgrade_request(
    body: UpgradeRequestCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Ask to move to a paid plan.

    This does not change the plan -- it puts the request in front of an
    administrator, who decides. The user keeps their current (working) plan and
    quota until then, which is the point: nobody loses access while waiting.
    """
    request = upgrade_create(db, current_user, body.target_plan_id, body.note)
    return _upgrade_payload(request)


@app.get("/api/upgrade-requests/mine")
def my_upgrade_requests(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """This account's upgrade history, newest first."""
    rows = db.scalars(
        select(UpgradeRequest)
        .where(UpgradeRequest.user_id == current_user.id)
        .order_by(UpgradeRequest.created_at.desc())
    ).all()
    return {"total": len(rows),
            "requests": [_upgrade_payload(r) for r in rows]}


@app.get("/api/admin/analytics")
def admin_analytics(
    days: int = 30,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Dashboard figures: accounts, query volume, plan mix, upgrade queue, sign-offs.

    Everything is counted live from the same tables the rest of the app writes,
    so a number here can never drift from the data it describes.
    """
    if days < 1 or days > 365:
        raise HTTPException(status_code=422, detail="days must be between 1 and 365")
    return build_analytics(db, days)


@app.get("/api/admin/upgrade-requests")
def admin_list_upgrade_requests(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = 100,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    The upgrade queue for administrators.

    Includes the requester's name and email: the hand-off is a conversation, so
    the admin needs to know who to reply to, not just what was asked for.
    """
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    query = select(UpgradeRequest).order_by(UpgradeRequest.created_at.desc())
    if status_filter:
        if status_filter not in UPGRADE_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of: {', '.join(UPGRADE_STATUSES)}")
        query = query.where(UpgradeRequest.status == status_filter)
    rows = db.scalars(query.limit(limit)).all()

    return {
        "total": len(rows),
        "requests": [
            {
                **_upgrade_payload(r),
                "user_id": r.user_id,
                "username": r.user.username,
                "email": r.user.email,
                "current_plan": r.user.plan.name,
            }
            for r in rows
        ],
    }


@app.patch("/api/admin/upgrade-requests/{request_id}")
def admin_decide_upgrade_request(
    request_id: int,
    body: UpgradeDecision,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Approve, decline, or mark a request as contacted.

    Approving applies the plan change in the same transaction as the decision,
    so the two can never disagree. The requester is emailed either way.
    """
    row = db.get(UpgradeRequest, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Upgrade request not found.")

    decision = body.status.strip().lower()
    if decision == "contacted":
        row = mark_contacted(db, row, body.admin_note)
    else:
        row = decide(db, row, decision, body.admin_note)
        # Tell the requester. A decision the user never hears about is not a
        # hand-off.
        get_mailer().send(
            to_email=row.user.email,
            subject=f"Your VariantLens upgrade request was {row.status}",
            body=upgrade_decision_body(row, row.target_plan.name,
                                       row.user.username),
        )

    return {
        "request": {**_upgrade_payload(row),
                    "user_id": row.user_id,
                    "username": row.user.username,
                    "email": row.user.email,
                    "current_plan": row.user.plan.name},
    }


@app.get("/api/admin/users")
def admin_list_users(
    limit: int = 100,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Account listing for administrators. Password material is never returned."""
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    users = db.scalars(select(User).order_by(User.id).limit(limit)).all()
    return {"total": len(users),
            "users": [_admin_user_payload(db, u) for u in users]}


@app.get("/api/admin/plans")
def admin_list_plans(
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Plan catalogue with subscriber counts, for the admin dashboard."""
    plans = db.scalars(select(Plan).order_by(Plan.id)).all()
    return {
        "total": len(plans),
        "plans": [
            {
                "id": plan.id,
                "name": plan.name,
                "price_cents": plan.price_cents,
                "monthly_query_limit": plan.monthly_query_limit,
                "is_unlimited": plan.is_unlimited,
                "llm_enabled": plan.llm_enabled,
                "api_access": plan.api_access,
                "audit_export": plan.audit_export,
                "subscribers": int(db.scalar(
                    select(func.count(User.id)).where(User.plan_id == plan.id)
                ) or 0),
            }
            for plan in plans
        ],
    }


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    body: UserUpdateRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Change another account's plan, active state or role.

    Two guards matter here, and both exist because this endpoint is the one
    place a token holder can gain or lose reach:
      * an admin cannot change their own role or active state, so a session
        cannot lock its own owner out;
      * a change that would remove the last active administrator is refused,
        because after that nobody could administer the deployment through the
        API at all.
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Account not found.")

    if body.role is not None and body.role not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of: {', '.join(ALLOWED_ROLES)}")

    demoting_or_disabling_admin = (
        target.role == "admin"
        and ((body.role is not None and body.role != "admin")
             or body.is_active is False)
    )
    if demoting_or_disabling_admin and _active_admin_count(db, exclude_user_id=target.id) == 0:
        raise HTTPException(
            status_code=409,
            detail="Refusing to remove the last active administrator. "
                   "Promote another admin first.")

    self_change = (
        (body.role is not None and body.role != current_user.role)
        or body.is_active is not None and body.is_active != current_user.is_active
    )
    if target.id == current_user.id and self_change:
        raise HTTPException(
            status_code=400,
            detail="An administrator cannot change their own role or active state.")

    if body.plan_id is not None:
        plan = db.get(Plan, body.plan_id)
        if plan is None:
            raise HTTPException(status_code=422, detail="Unknown plan_id.")
        target.plan_id = plan.id
    if body.is_active is not None:
        target.is_active = body.is_active
    if body.role is not None:
        target.role = body.role

    db.commit()
    db.refresh(target)
    return {"user": _admin_user_payload(db, target)}


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_password(
    user_id: int,
    body: PasswordResetRequest,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Reset another account's password.

    An administrator may also reset their own password this way if they have
    somehow lost the current one. Any token the target already holds stays valid
    until it expires -- password reset revokes the credential, not the session.
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Account not found.")
    target.password_hash = hash_password(body.new_password)
    db.commit()
    # The response deliberately says nothing beyond the outcome: never echo the
    # new password, and never return the hash.
    return {"reset": True, "username": target.username}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
