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
from datetime import date, datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
from pydantic import BaseModel, Field
from typing import Optional

from main import VariantInterpretationSystem
from protocol.security import authenticate, AuthenticationError


# ------------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------------
app = FastAPI(
    title="Genomic Variant Interpretation API",
    description="Multi-agent AI system for BRCA1/BRCA2 variant classification",
    version="1.0.0",
)

# CORS for the web frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared system instance (agents are stateless, so this is safe)
_system = VariantInterpretationSystem(use_llm=True)

# Demo API key used when the frontend doesn't provide one
DEMO_API_KEY = "demo-clinician-key"
REVIEW_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "review_signoffs.json")

def _load_review_signoffs() -> dict:
    """Load reviewer sign-off metadata from the local academic prototype store."""
    try:
        if os.path.exists(REVIEW_STORE_PATH):
            with open(REVIEW_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}

def _save_review_signoffs(data: dict) -> None:
    os.makedirs(os.path.dirname(REVIEW_STORE_PATH), exist_ok=True)
    temp_path = REVIEW_STORE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(temp_path, REVIEW_STORE_PATH)

_REVIEW_SIGNOFFS = _load_review_signoffs()


# ------------------------------------------------------------------
# Request/Response models
# ------------------------------------------------------------------
class QueryRequest(BaseModel):
    query: str = Field(..., description="Free-text query, e.g. 'Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?'")
    api_key: Optional[str] = Field(None, description="Optional API key (defaults to demo key)")


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


# ------------------------------------------------------------------
# API key dependency
# ------------------------------------------------------------------
def verify_api_key(x_api_key: str = Header(None)):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    try:
        user = authenticate(x_api_key)
        return user
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.get("/")
async def root():
    """Serve the web frontend."""
    frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "index.html")
    if os.path.exists(frontend_path):
        return FileResponse(frontend_path)
    return {"message": "Genomic Variant Interpretation API. See /docs for API documentation."}


@app.get("/health")
@app.get("/healthz")  # alias: Render's default health-check path
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "genomic-variant-interpretation"}


@app.post("/api/classify", response_model=QueryResponse)
async def classify(request: QueryRequest):
    """
    Classify a genetic variant through the full 4-agent pipeline.

    Example:
    {
        "query": "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?"
    }

    Returns the full pipeline result including extracted entities,
    top-K evidence with relevance scores, classification, confidence,
    audit trail, and the final report.
    """
    # Use the provided API key, or default to the demo key
    api_key = request.api_key or DEMO_API_KEY

    result = _system.run(request.query, api_key)

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

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
        review_signoff=_REVIEW_SIGNOFFS.get(result["variant_id"]),
        is_gene_overview=result.get("is_gene_overview", False),
        total_variants_in_gene=result.get("total_variants_in_gene"),
        rsid_lookup_unsupported=result.get("rsid_lookup_unsupported", False),
        retrieval_caveat=result.get("retrieval_caveat"),
        consensus=result.get("consensus"),
    )


@app.post("/api/review/{variant_id}")
async def save_review_signoff(variant_id: str, request: ReviewSignoffRequest):
    """Persist human-review metadata for a generated variant report.

    This is intentionally a small local JSON store for the academic prototype;
    it is not a production clinical records system.
    """
    try:
        parsed_date = date.fromisoformat(request.review_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="review_date must use YYYY-MM-DD format") from exc
    if parsed_date > date.today():
        raise HTTPException(status_code=422, detail="review_date cannot be in the future")

    signoff = {
        "reviewer_name": request.reviewer_name.strip(),
        "review_date": request.review_date,
        "signature": request.signature.strip() if request.signature else None,
        "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    _REVIEW_SIGNOFFS[variant_id] = signoff
    try:
        _save_review_signoffs(_REVIEW_SIGNOFFS)
    except OSError as exc:
        # Container hosts may have an ephemeral or read-only filesystem;
        # keep the sign-off for this process rather than failing the request.
        print(f"[review] persist skipped: {exc}")
    return {"variant_id": variant_id, "review_signoff": signoff}


@app.get("/api/review/{variant_id}")
async def get_review_signoff(variant_id: str):
    """Return stored human-review metadata for a variant, if present."""
    return {"variant_id": variant_id, "review_signoff": _REVIEW_SIGNOFFS.get(variant_id)}


@app.post("/api/report/pdf")
async def create_pdf_report(request: PDFReportRequest):
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
    stored_signoff = _REVIEW_SIGNOFFS.get(request.variant_id, {})
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
    ]
    doc.build(story)
    buffer.seek(0)
    return StreamingResponse(
        buffer, media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="variant_interpretation_report.pdf"'}
    )


@app.get("/api/pipeline/{variant_id}")
async def get_pipeline(variant_id: str, user: str = Depends(verify_api_key)):
    """
    Get the full pipeline result and audit trail for a previously queried variant.
    Requires X-API-Key header.
    """
    messages = _system.log.history_for(variant_id)
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
async def get_trail(variant_id: str, user: str = Depends(verify_api_key)):
    """Get the full agent-to-agent message audit trail for a variant."""
    messages = _system.log.history_for(variant_id)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
