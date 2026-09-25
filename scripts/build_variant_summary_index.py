#!/usr/bin/env python3
"""
build_variant_summary_index.py

Build a compact BRCA1/BRCA2 index from ClinVar's variant_summary.txt.gz.

Why this exists
---------------
The study workbook (data/clinvar_variant_dataset.xlsx) contains only 3009 rows
that were selected because they carry *conflicting* classifications. Well-known
clean variants -- e.g. BRCA2 c.5946delT / rs80359550 -- are therefore absent
from it, and the pipeline used to answer "not found" for them.

ClinVar's variant_summary.txt.gz covers every curated variation, but at ~423 MB
compressed (3M+ rows) it is far too large to load at runtime. This script
streams it once and extracts only the BRCA1/BRCA2 rows into a small TSV that the
evidence agent loads lazily as a *fallback* when a query misses the study
workbook.

The fallback supplies ClinVar's classification, review status, coordinates and
rsID. It does NOT supply functional predictors (SIFT/PolyPhen/CADD/frequencies),
so evidence-based calls for fallback rows stay weak -- the ClinVar consensus
reconciliation in the classification agent is what makes them usable.

Usage:
    python scripts/build_variant_summary_index.py

Output:
    data/variant_summary_brca.tsv
"""

import gzip
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "variant_summary.txt.gz")
DST = os.path.join(ROOT, "data", "variant_summary_brca.tsv")

# Restricting to BRCA keeps the index a few MB and loads in well under a second.
# Extend this set (and re-run) if the tool's scope grows beyond BRCA.
GENES = {"BRCA1", "BRCA2"}

# Columns copied into the index. Field names are normalised so downstream code
# does not have to care about ClinVar's header spelling.
KEEP = [
    ("Name", "HGVS_notation"),
    ("GeneSymbol", "Gene"),
    ("ClinicalSignificance", "ClinicalSignificance"),
    ("ReviewStatus", "ClinVarReviewStatus"),
    ("Type", "VariantType"),
    ("Assembly", "Assembly"),
    ("ChromosomeAccession", "ChromosomeAccession"),
    ("Chromosome", "Chromosome"),
    ("Start", "Start"),
    ("Stop", "Stop"),
    ("ReferenceAllele", "ReferenceAllele"),
    ("AlternateAllele", "AlternateAllele"),
    ("PositionVCF", "PositionVCF"),
    ("ReferenceAlleleVCF", "ReferenceAlleleVCF"),
    ("AlternateAlleleVCF", "AlternateAlleleVCF"),
    ("RS# (dbSNP)", "RSID"),
    # Read twice: once as text for display, once to derive the numeric star
    # rating the classification agent needs for consensus deferral.
    ("ReviewStatus", "ClinVarReviewStars"),
    ("PhenotypeList", "Disease_name"),
    ("NumberSubmitters", "NumberSubmitters"),
    ("LastEvaluated", "LastEvaluated"),
    ("AlleleID", "ClinVarAlleleID"),
    ("VariationID", "VariationID"),
]

OUT_FIELDS = [dst_name for _, dst_name in KEEP]


def stars_for(review_status: str) -> int:
    """Map a ClinVar review status onto its star rating."""
    text = (review_status or "").lower()
    if "practice guideline" in text:
        return 4
    if "reviewed by expert panel" in text:
        return 3
    if "criteria provided, multiple submitters" in text:
        return 2
    if "criteria provided, single submitter" in text:
        return 1
    if "no assertion" in text or "no interpretation" in text:
        return 0
    return 0


def normalise_rsid(raw: str) -> str:
    """ClinVar stores bare digits ('80359550'); return 'rs80359550'."""
    raw = (raw or "").strip()
    if not raw or raw.lower() in {"na", "n/a", ".", "-"}:
        return ""
    if raw.startswith("rs"):
        return raw
    return f"rs{raw}" if raw.isdigit() else ""


def main() -> int:
    if not os.path.exists(SRC):
        print(f"ERROR: {SRC} not found. Download it first:\n"
              f"  https://ftp.ncbi.nlm.nih.gov/pub/clinvar/xml/ "
              f"ClinVarVCF-GRCh37/ variant_summary.txt.gz",
              file=sys.stderr)
        return 1

    header_idx = None
    rows = 0
    with gzip.open(SRC, "rt", errors="replace") as fh, \
            open(DST, "w", encoding="utf-8") as out:
        out.write("\t".join(OUT_FIELDS) + "\n")

        for line in fh:
            if line.startswith("#"):
                header_idx = {name: i for i, name in
                              enumerate(line.lstrip("#").rstrip("\n").split("\t"))}
                missing = [src for src, _ in KEEP if src not in header_idx]
                if missing:
                    print(f"ERROR: source file missing columns {missing}",
                          file=sys.stderr)
                    return 1
                continue
            if header_idx is None:
                continue

            parts = line.rstrip("\n").split("\t")
            gene = parts[header_idx["GeneSymbol"]]
            if gene not in GENES:
                continue

            values = []
            for src, dst_name in KEEP:
                value = parts[header_idx[src]] if header_idx[src] < len(parts) else ""
                if dst_name == "ClinVarReviewStars":
                    value = stars_for(value)
                elif dst_name == "RSID":
                    value = normalise_rsid(value)
                values.append(value)
            out.write("\t".join(str(value) for value in values) + "\n")
            rows += 1

    size = os.path.getsize(DST)
    print(f"Wrote {rows:,} BRCA rows -> {DST} ({size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
