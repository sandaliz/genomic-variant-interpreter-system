"""
scripts/enrich_clinvar_labels.py

Attach real ClinVar labels to the project's BRCA1/BRCA2 dataset.

Why this exists
---------------
The working dataset (`data/clinvar_variant_dataset.xlsx`) contains NO
clinical significance column at all -- only SIFT/PolyPhen/CADD and a
boolean `Conflicting_Classification_Flag`. That means:

  * the Classification Agent has never been shown a real "Pathogenic"
    label, and cannot be evaluated against one;
  * `evaluation.py:infer_ground_truth()` had to invent labels using the
    *same* rules as the classifier, so its "accuracy" measures
    self-consistency, not correctness.

This script joins the dataset against the official ClinVar GRCh37 VCF and
appends the consensus label, review status and dbSNP rsID per variant.

It is deliberately additive: the original workbook is never overwritten,
all existing columns are preserved, and a new sheet-compatible workbook is
written to `--output`.

Matching
--------
The dataset's REF/ALT columns are already VCF-normalised (e.g. a deletion
is stored as REF="GTCT", ALT="G"), which is exactly how ClinVar's GRCh37
VCF represents alleles. So the join key is:

    (CHROM, POS, REF, ALT)

Coordinates in the dataset use GRCh37 accessions (NC_000017.10 /
NC_000013.10), so the GRCh37 VCF is the correct reference set.

Usage
-----
    python scripts/enrich_clinvar_labels.py
    python scripts/enrich_clinvar_labels.py --vcf /path/to/clinvar.vcf.gz
    python scripts/enrich_clinvar_labels.py --no-download   # require local VCF
"""

import argparse
import gzip
import os
import sys
import urllib.request

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATASET = os.path.join(PROJECT_ROOT, "data", "clinvar_variant_dataset.xlsx")
DEFAULT_SHEET = "BRCA1_BRCA2 (all 3009)"
DEFAULT_VCF = os.path.join(PROJECT_ROOT, "data", "clinvar_GRCh37.vcf.gz")
DEFAULT_OUTPUT = os.path.join(
    PROJECT_ROOT, "data", "clinvar_variant_dataset_enriched.xlsx"
)
DEFAULT_LABELS_CSV = os.path.join(PROJECT_ROOT, "data", "clinvar_labels.csv")

VCF_URL = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh37/clinvar.vcf.gz"

# Only chromosomes present in this project's dataset.
WANTED_CHROMS = {"13", "17"}

# ---------------------------------------------------------------------------
# ClinVar value normalisation
# ---------------------------------------------------------------------------
# Raw CLNSIG strings use underscores for spaces. We map them to the same
# label vocabulary the Classification Agent already uses, so the enriched
# column can be compared against predictions directly.
CLNSIG_MAP = {
    "pathogenic": "Pathogenic",
    "likely_pathogenic": "Likely Pathogenic",
    "pathogenic/likely_pathogenic": "Pathogenic",
    "pathogenic,_low_penetrance": "Pathogenic",
    "likely_pathogenic,_low_penetrance": "Likely Pathogenic",
    "uncertain_significance": "Uncertain Significance (VUS)",
    "likely_benign": "Likely Benign",
    "benign": "Benign",
    "benign/likely_benign": "Benign",
    "conflicting_classifications_of_pathogenicity": "Conflicting",
    "conflicting_interpretations_of_pathogenicity": "Conflicting",
    "conflicting_classifications_of_pathogenicity,_risk_allele": "Conflicting",
    "drug_response": "Other",
    "risk_factor": "Other",
    "association": "Other",
    "protective": "Other",
    "affects": "Other",
    "other": "Other",
    "confers_sensitivity": "Other",
    "not_provided": "Not provided",
    "no_classification_for_the_single_variant": "Not provided",
    "no_classification_provided": "Not provided",
    "no_assertion_criteria_provided": "Not provided",
    "no_assertion_provided": "Not provided",
    "": "Not provided",
}

# Strongest assertion wins if a key somehow maps to several records.
CLNSIG_PRECEDENCE = [
    "Pathogenic",
    "Likely Pathogenic",
    "Conflicting",
    "Uncertain Significance (VUS)",
    "Likely Benign",
    "Benign",
    "Other",
    "Not provided",
]

# CLNREVSTAT -> ClinVar review star rating (0-4).
REVSTAT_STARS = {
    "practice_guideline": 4,
    "reviewed_by_expert_panel": 3,
    "criteria_provided,_multiple_submitters,_no_conflicts": 2,
    "criteria_provided,_single_submitter": 1,
    "criteria_provided,_conflicting_classifications": 1,
    "criteria_provided,_conflicting_interpretations": 1,
    "no_assertion_criteria_provided": 0,
    "no_assertion_provided": 0,
    "no_classification_provided": 0,
}


def _norm_significance(raw: str) -> str:
    """Map a raw ClinVar CLNSIG value to the project's label vocabulary."""
    if not raw:
        return "Not provided"
    key = raw.strip().lower()
    if key in CLNSIG_MAP:
        return CLNSIG_MAP[key]
    # Handle compound / suffixed values we did not enumerate explicitly.
    if "conflicting" in key:
        return "Conflicting"
    if key.startswith("pathogenic") or "likely_pathogenic" in key:
        return "Pathogenic"
    if key.startswith("benign") or "likely_benign" in key:
        return "Benign"
    if "uncertain" in key:
        return "Uncertain Significance (VUS)"
    return "Other"


def _parse_info(info_field: str) -> dict:
    """Parse a VCF INFO column into a dict (first '=' only)."""
    out = {}
    for chunk in info_field.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            out[k] = v
        elif chunk:
            out[chunk] = True
    return out


def _humanise_revstat(raw: str) -> str:
    """Turn CLNREVSTAT into a readable review-status string."""
    if not raw:
        return "not provided"
    stars = REVSTAT_STARS.get(raw.lower())
    text = raw.replace("_", " ").replace(",", ",").strip()
    return f"{text} ({stars} star{'s' if stars != 1 else ''})" if stars is not None else text


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def download_vcf(url: str, dest: str, jobs: int = 4, chunk_mb: int = 4,
                 attempts: int = 5) -> None:
    """Download `url` to `dest` using parallel HTTP range requests.

    The ClinVar GRCh37 VCF is ~190 MB and the NCBI FTP endpoint throttles a
    single connection to roughly 150 KB/s, which makes a plain download take
    ~20 minutes. The endpoint advertises `Accept-Ranges: bytes`, so we fetch
    the file as several independent ranges concurrently and concatenate them.
    Byte-range concatenation reproduces the original bgzipped file exactly.

    NCBI returns HTTP 503 when too many ranges are requested at once, so each
    chunk is retried with exponential backoff and a 206 status check protects
    against a server that silently ignores the Range header.
    """
    import concurrent.futures as futures
    import tempfile
    import time

    # The initial HEAD is retried too: NCBI occasionally resets connections
    # when several range requests have just been issued in quick succession.
    total = accepts_ranges = None
    last_error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=120) as resp:
                total = int(resp.headers["Content-Length"])
                accepts_ranges = resp.headers.get("Accept-Ranges", "") == "bytes"
            break
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            time.sleep(2 ** attempt)
    if total is None:
        raise RuntimeError(f"could not reach {url} after {attempts} attempts: {last_error}")

    if not accepts_ranges:
        print(f"  server does not support ranges; downloading serially to {dest}")
        urllib.request.urlretrieve(url, dest)
        return

    chunk_size = chunk_mb * 1024 * 1024
    ranges = [(start, min(start + chunk_size - 1, total - 1))
              for start in range(0, total, chunk_size)]
    print(f"  {total / 1e6:.1f} MB in {len(ranges)} chunks using {jobs} connections")

    tmpdir = tempfile.mkdtemp(prefix="clinvar_dl_")

    def fetch(index_range):
        index, (start, end) = index_range
        part = os.path.join(tmpdir, f"part_{index:05d}")
        last_error = None
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(
                    url, headers={"Range": f"bytes={start}-{end}"}
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    if resp.status != 206:
                        raise RuntimeError(
                            f"expected 206 Partial Content, got {resp.status}"
                        )
                    with open(part, "wb") as out:
                        while True:
                            block = resp.read(1 << 16)
                            if not block:
                                break
                            out.write(block)
                if os.path.getsize(part) != (end - start + 1):
                    raise RuntimeError(
                        f"short read: {os.path.getsize(part)} of {end - start + 1} bytes"
                    )
                return part
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"chunk {index} failed after {attempts} attempts: {last_error}")

    try:
        with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            parts = list(pool.map(fetch, enumerate(ranges)))
        with open(dest, "wb") as out:
            for part in parts:
                with open(part, "rb") as handle:
                    while True:
                        block = handle.read(1 << 20)
                        if not block:
                            break
                        out.write(block)
    finally:
        for name in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, name))
        os.rmdir(tmpdir)
    print(f"  downloaded {os.path.getsize(dest) / 1e6:.1f} MB -> {dest}")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def build_keys(df: pd.DataFrame) -> set:
    """Build the (chrom, pos, ref, alt) key set from the dataset."""
    keys = set()
    for row in df.itertuples(index=False):
        chrom = str(getattr(row, "CHROM")).strip()
        pos = str(getattr(row, "POS")).strip()
        ref = str(getattr(row, "REF")).strip().upper()
        alt = str(getattr(row, "ALT")).strip().upper()
        keys.add((chrom, pos, ref, alt))
    return keys


def extract_labels(vcf_path: str, wanted_keys: set) -> dict:
    """Stream the ClinVar VCF and collect labels for the requested keys.

    The VCF is chromosome-ordered, so once we have passed chr17 and see a
    chromosome we do not need, we can stop reading (the file is bgzipped and
    decompresses sequentially, so this saves real time).
    """
    labels = {}
    seen_chr17 = False

    with gzip.open(vcf_path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("##"):
                continue
            if line.startswith("#"):
                continue

            fields = line.rstrip("\n").split("\t")
            if len(fields) < 8:
                continue

            chrom = fields[0].replace("chr", "")
            if chrom not in WANTED_CHROMS:
                if seen_chr17:
                    break
                continue
            if chrom == "17":
                seen_chr17 = True

            pos, ref, alt, info = fields[1], fields[3], fields[4], fields[7]
            alts = alt.split(",")
            info_d = _parse_info(info)

            raw_sig = info_d.get("CLNSIG", "")
            raw_rev = info_d.get("CLNREVSTAT", "")
            rsid = info_d.get("RS")
            alleleid = info_d.get("ALLELEID")

            sig_parts = raw_sig.split("|")
            rev_parts = raw_rev.split("|")

            for index, single_alt in enumerate(alts):
                key = (chrom, pos, ref.upper(), single_alt.upper())
                if key not in wanted_keys:
                    continue
                sig = sig_parts[index] if len(sig_parts) == len(alts) else raw_sig
                rev = rev_parts[index] if len(rev_parts) == len(alts) else raw_rev
                labels[key] = {
                    "ClinicalSignificance": _norm_significance(sig),
                    "ReviewStatus": _humanise_revstat(rev),
                    "ReviewStars": REVSTAT_STARS.get(rev.lower()),
                    "RSID": f"rs{rsid}" if rsid and rsid.isdigit() else None,
                    "AlleleID": alleleid,
                    "ClinVarRawSignificance": sig,
                }
    return labels


def merge_labels(df: pd.DataFrame, labels: dict) -> pd.DataFrame:
    """Return a copy of `df` with the ClinVar label columns appended."""
    out = df.copy()
    cols = {
        "ClinicalSignificance": [],
        "ClinVarReviewStatus": [],
        "ClinVarReviewStars": [],
        "RSID": [],
        "ClinVarAlleleID": [],
        "ClinVarRawSignificance": [],
        "ClinVarMatched": [],
    }
    for row in df.itertuples(index=False):
        key = (
            str(getattr(row, "CHROM")).strip(),
            str(getattr(row, "POS")).strip(),
            str(getattr(row, "REF")).strip().upper(),
            str(getattr(row, "ALT")).strip().upper(),
        )
        hit = labels.get(key)
        cols["ClinicalSignificance"].append(hit["ClinicalSignificance"] if hit else None)
        cols["ClinVarReviewStatus"].append(hit["ReviewStatus"] if hit else None)
        cols["ClinVarReviewStars"].append(hit["ReviewStars"] if hit else None)
        cols["RSID"].append(hit["RSID"] if hit else None)
        cols["ClinVarAlleleID"].append(hit["AlleleID"] if hit else None)
        cols["ClinVarRawSignificance"].append(hit["ClinVarRawSignificance"] if hit else None)
        cols["ClinVarMatched"].append(hit is not None)
    for name, values in cols.items():
        out[name] = values
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--sheet", default=DEFAULT_SHEET)
    parser.add_argument("--vcf", default=DEFAULT_VCF, help="ClinVar GRCh37 VCF (.vcf.gz)")
    parser.add_argument("--download", dest="download", action="store_true", default=True,
                        help="download the VCF if it is not present (default)")
    parser.add_argument("--no-download", dest="download", action="store_false",
                        help="fail instead of downloading when the VCF is missing")
    parser.add_argument("--jobs", type=int, default=4, help="parallel download connections")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="enriched workbook (original is never modified)")
    parser.add_argument("--labels-csv", default=DEFAULT_LABELS_CSV,
                        help="sidecar CSV of the joined labels")
    args = parser.parse_args()

    # --- 1. VCF availability ---
    if not os.path.exists(args.vcf):
        if not args.download:
            print(f"ERROR: {args.vcf} not found and --no-download was given.")
            return 2
        print(f"Downloading ClinVar GRCh37 VCF -> {args.vcf}")
        download_vcf(VCF_URL, args.vcf, jobs=args.jobs)
    else:
        print(f"Using existing VCF: {args.vcf} "
              f"({os.path.getsize(args.vcf) / 1e6:.1f} MB)")

    # --- 2. Load the dataset (all sheets, to preserve them on write) ---
    print(f"Loading {args.dataset}")
    sheets = pd.read_excel(args.dataset, sheet_name=None)
    if args.sheet not in sheets:
        print(f"ERROR: sheet '{args.sheet}' not found. Available: {list(sheets)}")
        return 2
    df = sheets[args.sheet]
    print(f"  {len(df)} rows, {len(df.columns)} columns in '{args.sheet}'")

    # --- 3. Extract labels from the VCF ---
    keys = build_keys(df)
    print(f"Matching {len(keys)} unique (chrom,pos,ref,alt) keys against the VCF...")
    labels = extract_labels(args.vcf, keys)

    # --- 4. Join ---
    enriched = merge_labels(df, labels)
    sheets[args.sheet] = enriched

    matched = int(enriched["ClinVarMatched"].sum())
    print(f"\nMatched {matched}/{len(enriched)} rows "
          f"({100 * matched / max(len(enriched), 1):.1f}%)")
    print("\nClinicalSignificance distribution (matched rows):")
    counts = enriched["ClinicalSignificance"].value_counts(dropna=False)
    for label, count in counts.items():
        print(f"  {str(label):32} {count}")
    print("\nClinVar review stars (matched rows):")
    for stars, count in enriched["ClinVarReviewStars"].value_counts(dropna=False).items():
        print(f"  {str(stars):32} {count}")

    # --- 5. Write outputs ---
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with pd.ExcelWriter(args.output) as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
    print(f"\nWrote enriched workbook -> {args.output}")

    sidecar = enriched[[
        "CHROM", "POS", "REF", "ALT", "Gene", "HGVS_notation",
        "ClinicalSignificance", "ClinVarReviewStatus", "ClinVarReviewStars",
        "RSID", "ClinVarAlleleID", "ClinVarRawSignificance", "ClinVarMatched",
    ]]
    sidecar.to_csv(args.labels_csv, index=False)
    print(f"Wrote labels sidecar     -> {args.labels_csv}")

    unmatched = enriched[~enriched["ClinVarMatched"]]
    if len(unmatched):
        print(f"\n{len(unmatched)} rows had no exact ClinVar allele match "
              f"(likely REF/ALT representation differences); they keep their "
              f"original columns and get ClinicalSignificance = None.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

