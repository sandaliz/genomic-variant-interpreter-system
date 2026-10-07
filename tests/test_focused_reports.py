import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.report_agent import ReportAgent


def test_population_question_gets_a_focused_answer():
    evidence = {
        "found": True,
        "intent": "population_frequency",
        "gene": "BRCA1",
        "hgvs": "NC_000017.10:g.41276111C>A",
        "af_esp": 0.0001,
        "af_exac": 0.0002,
        "af_tgp": None,
        "af_gnomad": None,
    }
    report = ReportAgent(use_llm=False).generate_report_template(
        evidence,
        {"classification": "VUS", "confidence": "low", "confidence_numeric": 20},
        "BRCA1:NC_000017.10:g.41276111C>A",
    )
    assert report.startswith("Population frequency:")
    assert "Maximum observed allele frequency: 0.0002" in report
    assert "rare in the available population data" in report
    assert "COMPLETE VARIANT INTERPRETATION" in report
    assert "Classification: VUS" in report
