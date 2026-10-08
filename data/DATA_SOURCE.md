# Data Source

## Dataset used in this project

The dataset used to train and evaluate the Classification Agent (`clinvar_variant_dataset.xlsx`) is a pre-processed subset derived from **ClinVar**, focused on variants with **conflicting submitter classifications**. It was sourced via a GitHub-hosted, Kaggle-prepared version of this subset (65,188 rows total across all genes; filtered down to 3,009 BRCA1/BRCA2 rows for this project).

- **Original source database:** ClinVar (NCBI)
- **Scope of this subset:** Variants with multiple submitter classifications (i.e., the "conflicting classification" subset), not the full ClinVar dump
- **Rows used:** 3,009 BRCA1/BRCA2 variants (Sheet 1), plus a 5,000-row general sample across all genes (Sheet 2)

## Primary / official data sources (for citation and further work)

| Source | Description | Link |
|---|---|---|
| ClinVar (NCBI) search interface | Browse/query variants by gene, clinical significance, review status | https://www.ncbi.nlm.nih.gov/clinvar/ |
| ClinVar bulk download (full dataset) | Full tab-delimited variant_summary.txt.gz file | https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz |
| ClinVar FTP Primer | Official guide to file formats (TXT, VCF, XML) | https://www.ncbi.nlm.nih.gov/clinvar/docs/ftp_primer/ |
| BRCA Exchange | BRCA1/BRCA2-specific merge of ClinVar, BIC, and LOVD | https://brcaexchange.org/ |
| ClinGen Evidence Repository | Expert-curated ACMG/AMP evidence codes and reasoning | https://erepo.genome.network/ |
| gnomAD | Population allele frequency database | https://gnomad.broadinstitute.org/ |

## Note on the working subset

The full official ClinVar bulk file is ~1GB, which is impractical to open directly in Excel. The working dataset used in this project (`clinvar_variant_dataset.xlsx`) is a smaller, GitHub-hosted, pre-processed subset built from ClinVar (originally prepared for a Kaggle dataset), limited to variants with **multiple/conflicting submitter classifications** — this makes it more directly useful for this project than the raw dump, since these are exactly the cases the agent needs to reason carefully about. This subset does **not** include the official review-status/star-rating column; for gold-standard, expert-panel-reviewed labels, cross-reference specific variant IDs against ClinVar's website directly, or query NCBI's E-utilities API.

## Enriched variant: `clinvar_variant_dataset_enriched.xlsx`

The base workbook above contains **no clinical-significance column** — only
SIFT/PolyPhen/CADD plus `Conflicting_Classification_Flag`. That had two
consequences:

1. the Classification Agent was never shown a real "Pathogenic" label, and
2. `evaluation.py:infer_ground_truth()` had to invent labels using the same
   rules the classifier already applies, so it measured **self-consistency**
   rather than accuracy.

`scripts/enrich_clinvar_labels.py` fixes that by joining the dataset against
the official ClinVar **GRCh37 VCF** (`data/clinvar_GRCh37.vcf.gz`, ~194 MB) on
`(CHROM, POS, REF, ALT)`. The dataset's REF/ALT are already VCF-normalised, so
the join is exact — **3009/3009 rows matched (100%)**.

### Columns added

| Column | Meaning |
|---|---|
| `ClinicalSignificance` | ClinVar consensus label, normalised to the project vocabulary (`Pathogenic`, `Likely Pathogenic`, `Uncertain Significance (VUS)`, `Likely Benign`, `Benign`, `Conflicting`, `Other`, `Not provided`) |
| `ClinVarReviewStatus` | `CLNREVSTAT` rendered as text |
| `ClinVarReviewStars` | Review confidence 0-4 (0 = no assertion criteria, 3 = expert panel, 4 = practice guideline) |
| `RSID` | dbSNP rsID — **3007/3009 rows have one**, which is what finally makes rsID lookup possible (`EvidenceRetrievalAgent.has_rsid_column`) |
| `ClinVarAlleleID` | ClinVar allele identifier |
| `ClinVarRawSignificance` | Unmodified `CLNSIG` value, for audit |
| `ClinVarMatched` | Join success flag |

### Regenerating

```bash
python scripts/enrich_clinvar_labels.py          # downloads the VCF if needed
python scripts/enrich_clinvar_labels.py --no-download   # reuse a local copy
```

The original workbook is never modified, and the ~194 MB VCF / generated
workbook / labels CSV are gitignored.

### ACMG presentation note

The classifier's output layers are documented in `README.md` (§ACMG Evidence
Presentation): `acmg_criteria_applied` (validated codes only), versus
`computational_annotations` (CADD/SIFT/PolyPhen), `clinvar_context`, and the
neutral `population_observation`. BS1 is unestablished in this prototype (no
validated disease-specific maximum credible AF), so an AF such as 0.0002 is a
neutral observation, never `BS1_equivalent`.

### Honest evaluation

```bash
python evaluation.py --dataset data/clinvar_variant_dataset_enriched.xlsx \
                     --ground-truth real --sample-size 3009 --no-ir --no-ablation --no-llm
```

Measured against **real** ClinVar labels the system scores **4.8%** exact-label
accuracy from its own ACMG-style reasoning (144/3009), rising to **38.1%**
(1145/3009) after reconciling each call against the reviewed ClinVar consensus.

Those exact figures are dominated by the label structure: **1841/3009 rows are
`Conflicting`** — "submitters disagree", which is not a class a single-label
classifier can reproduce — leaving **1168 rows with a single determinate
truth**. The clinically meaningful number is therefore **directional
agreement**: Pathogenic / Benign / VUS collapsed, scored only on those
determinate rows.

| Metric | Evidence-based call | After consensus reconciliation |
|---|---|---|
| Exact label (all 3009 rows) | 4.8% (144/3009) | 38.1% (1145/3009) |
| Directional (1168 determinate rows) | 42.6% (497/1168) | **98.3%** (1148/1168) |
| Benign-truth-called-pathogenic | 10.7% (53/497) | **0.2%** (1/497) |

The reconciliation step is what makes the tool safe to use: it removes almost
all benign→pathogenic false positives while never inventing a label — it only
defers when ClinVar carries a reviewed consensus of ≥2 stars ("criteria
provided, multiple submitters, no conflicts"). Re-run deterministically with
`--no-llm` (rule-based baseline, no Gemini calls); without that flag any
configured `GEMINI_API_KEY` makes live LLM calls and the evidence-based counts
will vary run to run. For reference, the older proxy
ground truth reported ~89%; that gap is the circularity described above.

## Coverage fallback: ClinVar `variant_summary` BRCA index

The study workbook's 3009 rows are only the *conflicting* subset, so a
well-known clean variant such as **BRCA2 c.5946delT (rs80359550)** is not in it
at all. Before the fallback existed, a query for it fell through to same-gene
TF-IDF similarity search and confidently returned an unrelated BRCA2 row.

`scripts/build_variant_summary_index.py` streams ClinVar's full
`variant_summary.txt.gz` (~423 MB compressed, 3M+ rows) and extracts only the
BRCA1/BRCA2 rows into a small TSV:

```bash
# one-off download of variant_summary.txt.gz from the ClinVar FTP, then:
python scripts/build_variant_summary_index.py   # -> data/variant_summary_brca.tsv
```

| File | Size | Role |
|---|---|---|
| `data/variant_summary.txt.gz` | 423 MB | ClinVar's complete variant table (source only, gitignored) |
| `data/variant_summary_brca.tsv` | ~24 MB / 75,172 rows | BRCA index the Evidence Agent loads lazily (gitignored) |

`EvidenceRetrievalAgent` consults this index **only** when a targeted variant
query (HGVS / rsID) has no exact match in the study workbook, preferring GRCh37
rows. Matching is exact rather than fuzzy — `c.5946delT` is normalised to
ClinVar's `c.5946del`, and bare-digit rsIDs (`80359550`) are normalised to
`rs80359550`.

Because ClinVar supplies no SIFT/PolyPhen/CADD or allele-frequency data, a
fallback hit is reported honestly rather than presented as fully evidenced: the
payload carries `coverage_fallback: true`,
`ir_metadata.evidence_source = "clinvar_variant_summary"`, and an
`evidence_limitation` note stating that the result leans on the ClinVar
consensus for lack of independent functional evidence. With this index in
place, BRCA2 c.5946delT resolves to **Pathogenic** from a 3-star expert-panel
consensus instead of returning an unrelated variant.
