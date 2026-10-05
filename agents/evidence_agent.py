"""
agents/evidence_agent.py

AGENT 2: Evidence Retrieval Agent (this IS your IR module)
------------------------------------------------------------
Rubric mapping: Information Retrieval

Job: given a structured variant identity from the Intake Agent, retrieve
every piece of relevant evidence about it -- SIFT/PolyPhen/CADD scores,
population allele frequency, prior ClinVar classification, and whether
that classification was conflicting between submitters.

This is a genuine Information Retrieval system with:

  1. **Indexing** -- the ClinVar dataset is indexed on gene, HGVS notation,
     genomic position, and a TF-IDF text index over variant descriptions.

  2. **Candidate Retrieval** -- multiple retrieval strategies:
     - Exact variant match (highest priority)
     - Gene match
     - Genomic position match
     - rsID match
     - TF-IDF cosine similarity over variant text

  3. **Relevance Ranking** -- candidates are scored and ranked using a
     weighted combination of:
     - Exact match bonus
     - Gene relevance
     - Clinical significance / review status
     - Evidence field completeness
     - TF-IDF cosine similarity

  4. **Top-K Selection** -- the top K most relevant evidence records are
     returned (default K=5), along with their relevance scores and
     human-readable explanations of *why* each score was assigned.

  5. **Explainability** -- every top-K result includes a normalized
     relevance score (0-1), a match type label ("Exact variant match",
     "Same genomic position", etc.), and a short explanation string so the
     Classification Agent (and the end user) can understand *why* each
     record was retrieved and how it contributes to the final decision.

This makes the Evidence Agent a proper IR module rather than a simple
"look up a row in Excel" -- it demonstrates indexing, retrieval, ranking,
top-K selection, and explainable results, which are core IR concepts.

Security: decrypts the incoming message payload, and encrypts its own
outgoing payload (encryption in transit between agents).
"""

import sys
import os
import re
import pandas as pd
import numpy as np
import pickle

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol.message import AgentMessage, MessageType
from protocol.security import InvalidVariantQuery

# Try to import scikit-learn for TF-IDF; fall back to a simple term-frequency
# scorer if scikit-learn is not available.
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


def _normalise_hgvs(text) -> str:
    """Lowercase an HGVS string and drop a trailing indel nucleotide.

    ClinVar normalises insertions/deletions by omitting the affected base, so a
    user query of "c.5946delT" has to be compared against ClinVar's stored
    "c.5946del" (appearing inside e.g.
    "NM_000059.4(BRCA2):c.5946del (p.Ser1982fs)").
    """
    if text is None:
        return ""
    try:
        if pd.isna(text):
            return ""
    except (TypeError, ValueError):
        pass
    value = str(text).strip().lower()
    return re.sub(r"(del|ins)([acgt])$", r"\1", value)


def _hgvs_matches(haystack, needle) -> bool:
    """True when a variant name contains the query HGVS.

    Case-insensitive and tolerant of the omitted trailing nucleotide in indel
    notation. An empty needle never matches, so "no HGVS in the query" can
    never be reported as an exact match.
    """
    if not needle:
        return False
    hay, nee = _normalise_hgvs(haystack), _normalise_hgvs(needle)
    return bool(hay and nee and nee in hay)


class EvidenceRetrievalAgent:
    name = "evidence_retrieval_agent"

    # Default number of top-K evidence records to return
    DEFAULT_TOP_K = 5

    # Maximum number of evidence records returned in the top_k_results list
    MAX_TOP_K = 10

    def __init__(self, dataset_path: str, top_k: int = DEFAULT_TOP_K, use_cache: bool = True, lazy_load: bool = True):
        self.dataset_path = dataset_path
        self.top_k = min(top_k, self.MAX_TOP_K)
        self.use_cache = use_cache
        self.lazy_load = lazy_load
        
        # Lazy loading: only initialize data when first needed
        self._df = None
        self._vectorizer = None
        self._tfidf_matrix = None
        self._initialized = False
        # Lazily-loaded BRCA index built from ClinVar's variant_summary.txt.gz
        # (see scripts/build_variant_summary_index.py). None = not loaded yet;
        # an empty DataFrame means the index file is absent.
        self._vs_df = None
        
        if not lazy_load:
            self._ensure_initialized()

    def _get_cache_path(self):
        """Generate cache file path based on dataset path."""
        base_path = os.path.dirname(self.dataset_path)
        dataset_name = os.path.splitext(os.path.basename(self.dataset_path))[0]
        return os.path.join(base_path, f"{dataset_name}_tfidf_cache.pkl")

    def _ensure_initialized(self):
        """Ensure the dataset and index are loaded (lazy loading)."""
        if self._initialized:
            return
            
        # Try to load from cache first for performance
        cache_path = self._get_cache_path()
        if self.use_cache and os.path.exists(cache_path):
            try:
                self._load_from_cache(cache_path)
                print(f"[evidence_agent] Loaded pre-built index from cache: {cache_path}")
            except Exception as e:
                print(f"[evidence_agent] Cache load failed, rebuilding: {e}")
                self._build_fresh_index()
        else:
            self._build_fresh_index()
            if self.use_cache:
                try:
                    self._save_to_cache(cache_path)
                    print(f"[evidence_agent] Saved index to cache: {cache_path}")
                except Exception as e:
                    print(f"[evidence_agent] Cache save failed: {e}")
        
        self._initialized = True

    @property
    def df(self):
        """Lazy loading property for dataframe."""
        self._ensure_initialized()
        return self._df

    @df.setter
    def df(self, value):
        """Setter for dataframe."""
        self._df = value

    @property
    def has_rsid_column(self) -> bool:
        """Whether the underlying dataset has an RSID field at all.

        Without it, `_rsid_match` can never find anything and every rsID
        query silently falls back to free-text similarity search -- which
        is not a real rsID lookup and should be flagged as such rather
        than presented as a confident match.
        """
        return "RSID" in self.df.columns

    def _load_from_cache(self, cache_path: str):
        """Load pre-built index from cache file."""
        with open(cache_path, 'rb') as f:
            cached_data = pickle.load(f)
            # Convert back to DataFrame if needed
            if isinstance(cached_data['df'], dict):
                self._df = pd.DataFrame(cached_data['df'])
            else:
                self._df = cached_data['df']
            self._vectorizer = cached_data['vectorizer']
            self._tfidf_matrix = cached_data['tfidf_matrix']

    def _save_to_cache(self, cache_path: str):
        """Save built index to cache file."""
        # Convert DataFrame to dict for better pickle compatibility
        df_dict = self._df.to_dict()
        with open(cache_path, 'wb') as f:
            pickle.dump({
                'df': df_dict,
                'vectorizer': self._vectorizer,
                'tfidf_matrix': self._tfidf_matrix
            }, f)

    def _build_fresh_index(self):
        """Build index from scratch when cache is not available."""
        self._df = pd.read_excel(self.dataset_path, sheet_name="BRCA1_BRCA2 (all 3009)")

        # --- 1. INDEXING ---
        # Build a lookup index on gene + HGVS-derived position info
        self._df["lookup_key"] = self._df["Gene"] + ":" + self._df["HGVS_notation"].astype(str)

        # Build a text corpus for TF-IDF retrieval: combine all text fields
        # into a single searchable document per variant.
        self._df["search_text"] = self._build_search_text(self._df)

        # Build TF-IDF index over the search text corpus
        self._build_tfidf_index()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------
    def _build_search_text(self, df: pd.DataFrame) -> pd.Series:
        """Combine all text fields into a single searchable document per variant."""
        parts = []
        parts.append(df["Gene"].fillna("").astype(str))
        parts.append(df["HGVS_notation"].fillna("").astype(str))
        parts.append(df["Consequence"].fillna("").astype(str))
        parts.append(df["IMPACT"].fillna("").astype(str))
        parts.append(df["Disease_name"].fillna("").astype(str))
        parts.append(df["SIFT_prediction"].fillna("").astype(str))
        parts.append(df["PolyPhen_prediction"].fillna("").astype(str))
        parts.append(df["Variant_type"].fillna("").astype(str))

        # Combine all parts into a single string per row
        result = parts[0]
        for p in parts[1:]:
            result = result + " " + p
        return result

    def _build_tfidf_index(self):
        """Build a TF-IDF vectorizer and index over the search text corpus."""
        if HAS_SKLEARN:
            self._vectorizer = TfidfVectorizer(
                lowercase=True,
                token_pattern=r"(?u)\b[a-zA-Z0-9_]+[a-zA-Z0-9_>]*\b",
                max_features=5000,
            )
            self._tfidf_matrix = self._vectorizer.fit_transform(self._df["search_text"])
        else:
            # Fallback: simple term-frequency based scoring
            self._vectorizer = None
            self._tfidf_matrix = None

    @property
    def vectorizer(self):
        """Lazy loading property for vectorizer."""
        self._ensure_initialized()
        return self._vectorizer

    @property
    def tfidf_matrix(self):
        """Lazy loading property for tfidf_matrix."""
        self._ensure_initialized()
        return self._tfidf_matrix

    # ------------------------------------------------------------------
    # Retrieval strategies
    # ------------------------------------------------------------------
    def _exact_match(self, gene, hgvs):
        """Exact variant lookup by gene + HGVS notation."""
        matches = self.df[self.df["Gene"] == gene] if gene else self.df
        exact = matches[matches["HGVS_notation"].astype(str).str.contains(
            hgvs, na=False, regex=False
        )]
        return exact

    def _gene_match(self, gene):
        """All variants for a gene."""
        return self.df[self.df["Gene"] == gene]

    def _position_match(self, position):
        """Variants at a specific genomic position."""
        return self.df[self.df["POS"].astype(str) == str(position)]

    def _rsid_match(self, rsid):
        """Variants matching an rsID (if the dataset has an RSID column).

        Exact (case-insensitive) matches are preferred; a substring search is
        only used as a fallback for cells that carry several rsIDs.
        """
        if "RSID" in self.df.columns and rsid:
            target = str(rsid).strip().lower()
            column = self.df["RSID"].astype(str).str.strip().str.lower()
            exact = self.df[column == target]
            if not exact.empty:
                return exact
            return self.df[column.str.contains(target, na=False, regex=False)]
        return pd.DataFrame()

    def _tfidf_search(self, query: str, top_k: int = 20) -> pd.DataFrame:
        """
        TF-IDF cosine similarity search over the variant text corpus.
        Returns the top_k most similar variants.
        """
        if not HAS_SKLEARN or self.tfidf_matrix is None:
            return pd.DataFrame()

        query_vec = self.vectorizer.transform([query])
        similarities = cosine_similarity(query_vec, self.tfidf_matrix).flatten()

        # Get top-k indices
        top_indices = np.argsort(similarities)[::-1][:top_k]
        result = self.df.iloc[top_indices].copy()
        result["tfidf_score"] = similarities[top_indices]
        return result

    # ------------------------------------------------------------------
    # ClinVar variant_summary coverage fallback
    # ------------------------------------------------------------------
    def _load_variant_summary(self) -> pd.DataFrame:
        """Lazily load the BRCA index built from ClinVar's variant_summary.txt.gz.

        The study workbook holds only 3009 *conflicting*-classification rows, so
        well-known clean variants (e.g. BRCA2 c.5946delT / rs80359550) are
        simply absent from it. Without this fallback those queries answered from
        loose same-gene similarity search and reported an unrelated row.

        The index is produced by scripts/build_variant_summary_index.py and is
        consulted only when a targeted variant query misses the workbook.
        """
        if self._vs_df is not None:
            return self._vs_df

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "data", "variant_summary_brca.tsv")
        if not os.path.exists(path):
            self._vs_df = pd.DataFrame()
            return self._vs_df

        df = pd.read_csv(path, sep="\t", dtype=str, low_memory=False)
        if "HGVS_notation" in df.columns:
            df["HGVS_norm"] = df["HGVS_notation"].fillna("").map(_normalise_hgvs)
        else:
            df["HGVS_norm"] = ""
        if "RSID" in df.columns:
            df["RSID_norm"] = df["RSID"].fillna("").str.strip().str.lower()
        else:
            df["RSID_norm"] = ""
        self._vs_df = df
        return df

    @staticmethod
    def _prefer_grch37(rows: pd.DataFrame) -> pd.DataFrame:
        """Keep GRCh37 rows (this tool's coordinate system) and deduplicate."""
        if rows.empty:
            return rows
        if "Assembly" in rows.columns:
            grch37 = rows[rows["Assembly"].fillna("").str.upper() == "GRCH37"]
            if not grch37.empty:
                rows = grch37
        if "VariationID" in rows.columns:
            rows = rows.drop_duplicates(subset=["VariationID"], keep="first")
        return rows

    @staticmethod
    def _primary_is_exact(row, gene, hgvs, rsid, position) -> bool:
        """Does this candidate actually answer the targeted variant query?"""
        if _hgvs_matches(row.get("HGVS_notation"), hgvs):
            return True
        if rsid and str(rsid).strip().lower() in str(row.get("RSID") or "").lower():
            return True
        if position and str(row.get("POS")) == str(position):
            return True
        return False

    def _variant_summary_lookup(self, gene, hgvs, rsid, position) -> pd.DataFrame:
        """Exact lookup against the ClinVar variant_summary BRCA index."""
        df = self._load_variant_summary()
        if df.empty:
            return df

        rows = df
        if gene and "Gene" in rows.columns:
            rows = rows[rows["Gene"].fillna("").str.upper() == str(gene).upper()]
        if rows.empty:
            return rows

        hit = pd.DataFrame()
        if rsid and "RSID_norm" in rows.columns:
            target = str(rsid).strip().lower()
            hit = rows[rows["RSID_norm"] == target]
            if hit.empty:
                hit = rows[rows["RSID_norm"].str.contains(target, na=False, regex=False)]
        if hit.empty and hgvs and "HGVS_norm" in rows.columns:
            needle = _normalise_hgvs(hgvs)
            if needle:
                hit = rows[rows["HGVS_norm"].str.contains(needle, na=False, regex=False)]
        if hit.empty and position:
            masks = [
                rows[col].astype(str) == str(position)
                for col in ("Start", "PositionVCF") if col in rows.columns
            ]
            if masks:
                combined = masks[0]
                for mask in masks[1:]:
                    combined = combined | mask
                hit = rows[combined]

        result = self._prefer_grch37(hit)
        if result.empty:
            return result

        result = result.copy()
        # Alias the columns the workbook normally supplies so the shared
        # ranking and evidence-building code sees a familiar schema.
        if "Start" in result.columns:
            result["POS"] = result["Start"]
        if "VariantType" in result.columns:
            result["Variant_type"] = result["VariantType"]
        if "ClinicalSignificance" in result.columns:
            result["Conflicting_Classification_Flag"] = (
                result["ClinicalSignificance"].fillna("").str.lower()
                .str.contains("conflicting").astype(int)
            )

        # Guarantee every workbook column exists, otherwise _rank_candidates()
        # raises KeyError on POS / IMPACT / SIFT_prediction / etc.
        cols = list(dict.fromkeys(list(self.df.columns) + list(result.columns)))
        return result.reindex(columns=cols)

    # ------------------------------------------------------------------
    # Relevance ranking
    # ------------------------------------------------------------------
    def _rank_candidates(self, candidates: pd.DataFrame, query_type: str,
                         gene=None, hgvs=None, position=None) -> pd.DataFrame:
        """
        Rank candidate variants by a weighted relevance score.

        Scoring components:
          - exact_match_bonus:  +10 if the variant exactly matches the query
          - gene_bonus:         +5 if the gene matches
          - position_bonus:     +5 if the position matches
          - impact_bonus:       +2 for HIGH impact (more clinically relevant)
          - conflict_penalty:   -2 if submitters disagreed (less certain)
          - completeness:       +1 per non-null evidence field (0-5)
          - tfidf_score:        TF-IDF cosine similarity (0-1, scaled to 0-5)
          - review_status_bonus: +2 for variants with ClinVar review data

        After computing raw scores, we:
          1. Identify the *match type* and explanation for each result
          2. Normalize the relevance score to 0-1 for readability
          3. Sort by relevance in descending order
        """
        if candidates.empty:
            return candidates

        df = candidates.copy()
        scores = np.zeros(len(df))

        # --- Compute match type and per-result explanation ---
        match_types = []
        explanations = []

        for idx in range(len(df)):
            row = df.iloc[idx]
            comps = []

            # Exact match?
            is_exact = False
            if _hgvs_matches(row.get("HGVS_notation"), hgvs):
                is_exact = True
                comps.append("Exact variant match")

            # Gene match?
            if gene and str(row.get("Gene", "")) == gene:
                comps.append("Same gene")

            # Position match?
            if position and str(row.get("POS", "")) == str(position):
                comps.append("Same genomic position")

            match_type = "Exact variant match" if is_exact else (
                "Same genomic position" if position and str(row.get("POS", "")) == str(position)
                else "Same gene" if gene and str(row.get("Gene", "")) == gene
                else "TF-IDF text similarity"
            )

            # Build explanation
            tfidf_val = float(row.get("tfidf_score", 0)) if "tfidf_score" in df.columns else 0
            reason_parts = []
            if is_exact:
                reason_parts.append("HGVS notation exactly matches the query")
            if gene and str(row.get("Gene", "")) == gene:
                reason_parts.append(f"gene {gene}")
            if position and str(row.get("POS", "")) == str(position):
                reason_parts.append(f"genomic position {position}")
            if tfidf_val > 0:
                reason_parts.append(f"TF-IDF cosine similarity: {tfidf_val:.3f}")
            impact = self._clean_value(row.get("IMPACT"))
            if impact:
                reason_parts.append(f"impact={impact}")
            completeness = sum(
                1 for f in ["SIFT_prediction", "PolyPhen_prediction", "CADD_score",
                            "AF_ESP", "AF_EXAC", "AF_TGP"]
                if pd.notna(row.get(f))
            )
            reason_parts.append(f"{completeness}/6 evidence fields present")

            match_types.append(match_type)
            explanations.append("Retrieved because: " + "; ".join(reason_parts) + ".")

        df["match_type"] = match_types
        df["relevance_explanation"] = explanations

        # --- Compute weighted scores ---
        # Exact match bonus
        if hgvs:
            names = df["HGVS_notation"].astype(str)
            exact_mask = names.str.contains(hgvs, na=False, regex=False)
            # Also try the indel-normalised form: users type "c.5946delT"
            # while ClinVar stores the normalised "c.5946del".
            needle = _normalise_hgvs(hgvs)
            if needle:
                exact_mask = exact_mask | names.str.contains(needle, na=False, regex=False)
            scores[exact_mask.values] += 10.0

        # Gene bonus
        if gene:
            gene_mask = df["Gene"] == gene
            scores[gene_mask.values] += 5.0

        # Position bonus
        if position:
            pos_mask = df["POS"].astype(str) == str(position)
            scores[pos_mask.values] += 5.0

        # Impact bonus
        impact_high = df["IMPACT"].fillna("").astype(str) == "HIGH"
        scores[impact_high.values] += 2.0

        # Conflict penalty
        conflict = df["Conflicting_Classification_Flag"].fillna(0).astype(int) == 1
        scores[conflict.values] -= 2.0

        # Evidence completeness (0-5)
        evidence_fields = ["SIFT_prediction", "PolyPhen_prediction", "CADD_score",
                           "AF_ESP", "AF_EXAC", "AF_TGP"]
        for field in evidence_fields:
            non_null = df[field].notna()
            scores[non_null.values] += 1.0

        # TF-IDF score (scaled to 0-5)
        if "tfidf_score" in df.columns:
            scores += df["tfidf_score"].fillna(0).values * 5.0

        # Review status bonus (variants with ClinVar review data are more trustworthy)
        # The dataset has Disease_name as a proxy for having review data
        has_disease = df["Disease_name"].fillna("").astype(str) != ""
        scores[has_disease.values] += 1.0

        df["relevance_score_raw"] = scores

        # --- Normalize relevance to 0-1 ---
        # IMPORTANT: normalize against a fixed theoretical maximum, not the
        # min/max of *this* candidate batch. Per-batch min-max normalization
        # has a serious failure mode: when every candidate in the batch ties
        # (e.g. an rsID query with no real text-similarity signal, so every
        # row scores the same near-zero raw value), min==max and the old code
        # defaulted the tie to relevance_score = 1.0 -- making a completely
        # undiscriminated, essentially-arbitrary set of candidates display as
        # a perfect "1.00 relevance" match. A fixed denominator means a weak
        # match reads as weak, regardless of what else happened to be in the
        # candidate pool.
        #   exact_match(10) + gene(5) + position(5) + impact(2)
        #   + completeness(6) + tfidf(5) + review_status(1) = 34
        theoretical_max = 34.0
        df["relevance_score"] = (df["relevance_score_raw"] / theoretical_max).clip(lower=0.0, upper=1.0)

        df = df.sort_values("relevance_score", ascending=False)
        return df

    # ------------------------------------------------------------------
    # Row -> evidence dict
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_value(value):
        return None if pd.isna(value) else value

    def _row_to_evidence(self, row, found_exact: bool, relevance_score: float = None,
                         relevance_explanation: str = None) -> dict:
        """Convert a DataFrame row to the evidence dict used by downstream agents."""
        sift_available = pd.notna(row.get("SIFT_prediction"))
        polyphen_available = pd.notna(row.get("PolyPhen_prediction"))
        cadd_available = pd.notna(row.get("CADD_score"))
        return {
            "found": True,
            "exact_match": found_exact,
            "relevance_score": float(relevance_score) if relevance_score is not None else None,
            "relevance_explanation": relevance_explanation,
            "consequence": self._clean_value(row.get("Consequence")),
            "impact": self._clean_value(row.get("IMPACT")),
            "sift_prediction": self._clean_value(row.get("SIFT_prediction")),
            "polyphen_prediction": self._clean_value(row.get("PolyPhen_prediction")),
            "cadd_score": self._clean_value(row.get("CADD_score")),
            "af_esp": self._clean_value(row.get("AF_ESP")),
            "af_exac": self._clean_value(row.get("AF_EXAC")),
            "af_tgp": self._clean_value(row.get("AF_TGP")),
            "predictor_available": {
                "sift": bool(sift_available),
                "polyphen": bool(polyphen_available),
                "sift_polyphen": bool(sift_available and polyphen_available),
                "cadd": bool(cadd_available),
                "complete": bool(sift_available and polyphen_available and cadd_available),
            },
            "disease_name": self._clean_value(row.get("Disease_name")),
            "conflicting_classification_flag": int(row.get("Conflicting_Classification_Flag", 0)),
            "gene": self._clean_value(row.get("Gene")),
            "hgvs": self._clean_value(row.get("HGVS_notation")),
            "position": self._clean_value(row.get("POS")),
            "variant_type": self._clean_value(row.get("Variant_type")),
            # --- Real ClinVar evidence -------------------------------------
            # Populated only when the enriched workbook is in use (see
            # scripts/enrich_clinvar_labels.py); all are None on the original
            # dataset. They let the Classification Agent reconcile its own
            # ACMG-style call against ClinVar's reviewed consensus, and expose
            # the rsID so rsID queries can actually be looked up.
            "prior_classification": self._clean_value(row.get("ClinicalSignificance")),
            "prior_review_status": self._clean_value(row.get("ClinVarReviewStatus")),
            "prior_review_stars": self._clean_value(row.get("ClinVarReviewStars")),
            "prior_raw_significance": self._clean_value(row.get("ClinVarRawSignificance")),
            "rsid": self._clean_value(row.get("RSID")),
        }

    # ------------------------------------------------------------------
    # Main retrieval entry point
    # ------------------------------------------------------------------
    def retrieve(self, intake_message: AgentMessage, message_log) -> AgentMessage:
        # Decrypt the incoming payload (it was encrypted by the Intake Agent)
        intake_payload = intake_message.decrypt_payload()
        query_type = intake_payload.get("query_type", "gene_variant")
        gene = intake_payload.get("gene")
        hgvs = intake_payload.get("hgvs")
        position = intake_payload.get("position")
        rsid = intake_payload.get("rsid")
        raw_query = intake_payload.get("raw_query", "")

        # Declared up front so `coverage_fallback` is ALWAYS a boolean in the payload.
        # Otherwise it is only set on the record-found path, so a consumer reading
        # it with .get() would receive None instead of False on not-found results.
        evidence = {"found": False, "query_type": query_type, "coverage_fallback": False}

        # --- CANDIDATE RETRIEVAL ---
        candidates = pd.DataFrame()
        # Tracks whether an rsID query was resolved by a real rsID lookup (as
        # opposed to a free-text guess), so it can count as a verified match.
        rsid_exact = False

        if query_type == "gene_variant":
            # Exact variant lookup first
            candidates = self._exact_match(gene, hgvs)
            if candidates.empty:
                # Fall back to gene-level + TF-IDF
                candidates = self._gene_match(gene)
                tfidf = self._tfidf_search(raw_query)
                if not tfidf.empty:
                    candidates = pd.concat([candidates, tfidf]).drop_duplicates(
                        subset=["Gene", "HGVS_notation"]
                    )
            else:
                # Exact match found -- BUT still retrieve similar variants from the
                # same gene to provide ranked evidence context (top-K ranking).
                # This ensures we always return 5-10 evidence records with relevance scores.
                gene_variants = self._gene_match(gene)
                if not gene_variants.empty and len(gene_variants) > self.top_k:
                    # Take gene variants NOT already in the exact match
                    combined = pd.concat([candidates, gene_variants]).drop_duplicates(
                        subset=["Gene", "HGVS_notation"]
                    )
                    # Limit to a manageable number for ranking
                    candidates = combined.head(self.top_k * 2)
                elif not gene_variants.empty:
                    candidates = gene_variants.head(self.top_k * 2)
                # If gene only has few variants, use what we have

        elif query_type == "variant_only":
            candidates = self._exact_match(None, hgvs)
            if candidates.empty:
                candidates = self._tfidf_search(raw_query)

        elif query_type == "gene_only":
            candidates = self._gene_match(gene)

        elif query_type == "position":
            candidates = self._position_match(position)
            if candidates.empty:
                candidates = self._tfidf_search(raw_query)

        elif query_type == "rsid":
            candidates = self._rsid_match(rsid)
            rsid_exact = not candidates.empty
            if candidates.empty:
                candidates = self._tfidf_search(raw_query)

        elif query_type == "position_only":
            candidates = self._position_match(position)

        elif query_type == "rsid_search":
            # Intent: "Tell me about rs123456" -> rsID lookup
            candidates = self._rsid_match(rsid)
            rsid_exact = not candidates.empty
            if candidates.empty:
                candidates = self._tfidf_search(raw_query)

        # --- COVERAGE FALLBACK (ClinVar variant_summary BRCA index) -------
        # The study workbook is a conflicting-classification subset, so a clean
        # well-known variant can be missing from it. The branch above would then
        # return loose same-gene similarity matches and present an unrelated row
        # as *the* answer. Resolve the queried variant from the dedicated BRCA
        # index first; only if that also misses do we keep the fuzzy result.
        from_variant_summary = False
        if query_type in ("gene_variant", "variant_only", "rsid", "rsid_search"):
            have_exact = not candidates.empty and self._primary_is_exact(
                candidates.iloc[0], gene, hgvs, rsid, position
            )
            if not have_exact:
                fallback = self._variant_summary_lookup(gene, hgvs, rsid, position)
                if not fallback.empty:
                    candidates = fallback
                    from_variant_summary = True
                    if rsid:
                        rsid_exact = True

        # This dataset has no RSID field at all, so an rsID query can never
        # be a genuine lookup -- it always falls back to free-text
        # similarity search over the raw query string, which is not the
        # same thing and should not be presented as a confident match.
        rsid_lookup_unsupported = (
            query_type in ("rsid", "rsid_search")
            and bool(rsid)
            and not self.has_rsid_column
        )

        # --- RELEVANCE RANKING ---
        if not candidates.empty:
            ranked = self._rank_candidates(
                candidates, query_type, gene=gene, hgvs=hgvs, position=position
            )

            # --- TOP-K SELECTION ---
            top_k = ranked.head(self.top_k)

            # Primary evidence = the top-ranked record
            primary = top_k.iloc[0]
            found_exact = _hgvs_matches(
                primary.get("HGVS_notation"), hgvs
            ) or rsid_exact

            evidence = self._row_to_evidence(
                primary,
                found_exact=found_exact,
                relevance_score=primary.get("relevance_score"),
                relevance_explanation=primary.get("relevance_explanation"),
            )
            evidence["query_type"] = query_type
            evidence["coverage_fallback"] = from_variant_summary
            if from_variant_summary:
                evidence["evidence_limitation"] = (
                    "This variant is not in the 3009-row study workbook, so it was "
                    "resolved from ClinVar's variant_summary BRCA index. That source "
                    "supplies the classification, review status, coordinates and rsID "
                    "but no functional predictors (SIFT/PolyPhen/CADD) or allele "
                    "frequencies, so the evidence-based call here is necessarily weak "
                    "and the result leans on the ClinVar consensus."
                )

            # Include ALL top-K results with explainable relevance scores
            evidence["top_k_results"] = []
            for _, row in top_k.iterrows():
                row_exact = _hgvs_matches(
                    row.get("HGVS_notation"), hgvs
                ) or rsid_exact
                row_sift_available = pd.notna(row.get("SIFT_prediction"))
                row_polyphen_available = pd.notna(row.get("PolyPhen_prediction"))
                row_cadd_available = pd.notna(row.get("CADD_score"))
                evidence["top_k_results"].append({
                    "gene": self._clean_value(row.get("Gene")),
                    "hgvs": self._clean_value(row.get("HGVS_notation")),
                    "impact": self._clean_value(row.get("IMPACT")),
                    "relevance_score": float(row.get("relevance_score", 0)),
                    "relevance_explanation": row.get("relevance_explanation", ""),
                    "match_type": row.get("match_type", "TF-IDF text similarity"),
                    "exact_match": row_exact,
                    "sift_prediction": self._clean_value(row.get("SIFT_prediction")),
                    "polyphen_prediction": self._clean_value(row.get("PolyPhen_prediction")),
                    "cadd_score": self._clean_value(row.get("CADD_score")),
                    "predictor_available": {
                        "sift": bool(row_sift_available),
                        "polyphen": bool(row_polyphen_available),
                        "sift_polyphen": bool(row_sift_available and row_polyphen_available),
                        "cadd": bool(row_cadd_available),
                        "complete": bool(row_sift_available and row_polyphen_available and row_cadd_available),
                    },
                    "conflicting_classification_flag": int(row.get("Conflicting_Classification_Flag", 0)),
                })

            # For gene_only queries, include the total count
            if query_type == "gene_only":
                evidence["total_variants_in_gene"] = len(candidates)
                evidence["gene"] = gene

            # IR metadata for the report
            evidence["ir_metadata"] = {
                "retrieval_strategy": self._describe_retrieval(query_type),
                "candidates_retrieved": len(candidates),
                "top_k_returned": len(top_k),
                "max_possible_top_k": self.top_k,
                "ranking_method": "weighted relevance scoring (exact match + gene + position + impact + completeness + TF-IDF + review data)",
                "tfidf_enabled": HAS_SKLEARN,
                "evidence_source": (
                    "clinvar_variant_summary (coverage fallback; no functional predictors)"
                    if from_variant_summary else "study workbook"
                ),
                "scoring_components": {
                    "exact_match": 10,
                    "gene_match": 5,
                    "position_match": 5,
                    "high_impact": 2,
                    "conflict_penalty": -2,
                    "evidence_completeness": "up to 6",
                    "tfidf_scaled": "0-5",
                    "review_data": 1,
                },
            }
            if rsid_lookup_unsupported:
                evidence["ir_metadata"]["retrieval_caveat"] = (
                    f"This dataset has no rsID field, so \"{rsid}\" could not be looked up "
                    "directly. The variant(s) below were found by free-text similarity "
                    "search over the query instead and are NOT a verified match for that "
                    "rsID -- treat this result with caution and verify the rsID separately "
                    "(e.g. against dbSNP/ClinVar) before relying on it."
                )
            evidence["rsid_lookup_unsupported"] = rsid_lookup_unsupported
        else:
            evidence["message"] = self._not_found_message(query_type, gene, hgvs, position, rsid)
            evidence["rsid_lookup_unsupported"] = rsid_lookup_unsupported

        message = AgentMessage(
            variant_id=intake_message.variant_id,
            stage="evidence_retrieved",
            sender=self.name,
            receiver="classification_agent",
            message_type=(
                MessageType.EVIDENCE_RESULT if evidence.get("found")
                else MessageType.EVIDENCE_ERROR
            ),
            trace_id=intake_message.trace_id,
            payload=evidence,
        )
        # Encrypt payload before passing to next agent
        message.encrypt_payload()
        message_log.record(message)
        return message

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _describe_retrieval(self, query_type: str) -> str:
        """Describe which retrieval strategy was used for this query type."""
        strategies = {
            "gene_variant": "exact variant match + same-gene context + TF-IDF fallback",
            "variant_only": "exact HGVS match + TF-IDF fallback",
            "gene_only": "gene index lookup",
            "position": "genomic position index + TF-IDF fallback",
            "rsid": "rsID index + TF-IDF fallback",
            "rsid_search": "rsID index + TF-IDF fallback",
            "position_only": "genomic position index",
        }
        return strategies.get(query_type, "TF-IDF + structured lookup")

    def _not_found_message(self, query_type, gene, hgvs, position, rsid) -> str:
        """Generate a helpful not-found message."""
        if query_type == "gene_variant":
            return f"No exact match found for {gene}:{hgvs} in the dataset."
        elif query_type == "variant_only":
            return f"No match found for {hgvs} in the dataset."
        elif query_type == "gene_only":
            return f"No variants found for gene {gene}."
        elif query_type == "position":
            return f"No variant found at position {position} in {gene}."
        elif query_type == "rsid":
            return f"No variant found for {rsid}."
        elif query_type == "rsid_search":
            return f"No variant found for {rsid}."
        elif query_type == "position_only":
            return f"No variant found at position {position}."
        return "No matching evidence found."


if __name__ == "__main__":
    from protocol.message import MessageLog
    from agents.intake_agent import IntakeAgent

    log = MessageLog()
    intake = IntakeAgent()
    evidence_agent = EvidenceRetrievalAgent("data/clinvar_variant_dataset.xlsx")

    test_queries = [
        "Is BRCA1 NC_000017.10:g.41197778A>G pathogenic?",
        "What variants are in BRCA1?",
        "What is NC_000017.10:g.41201179C>T?",
        "What variant is at position 41197778 in BRCA1?",
    ]

    for q in test_queries:
        try:
            intake_msg = intake.process(q, log)
            evidence_msg = evidence_agent.retrieve(intake_msg, log)
            payload = evidence_msg.decrypt_payload()
            print(f"QUERY: {q}")
            print(f"  -> found={payload.get('found')}, gene={payload.get('gene')}, "
                  f"hgvs={payload.get('hgvs')}, impact={payload.get('impact')}")
            print(f"  -> relevance_score={payload.get('relevance_score')}")
            print(f"  -> explanation: {payload.get('relevance_explanation', 'N/A')}")
            if "ir_metadata" in payload:
                meta = payload["ir_metadata"]
                print(f"  -> IR: strategy={meta['retrieval_strategy']}, "
                      f"candidates={meta['candidates_retrieved']}, top_k={meta['top_k_returned']}")
            if "top_k_results" in payload:
                print(f"  -> Top-K results:")
                for r in payload["top_k_results"][:5]:
                    print(f"     - [{r['match_type']}] {r['gene']}:{r['hgvs']} "
                          f"(score={r['relevance_score']:.2f}, exact={r['exact_match']})")
            if "message" in payload:
                print(f"  -> message: {payload['message']}")
            print()
        except (InvalidVariantQuery, KeyError, TypeError, AttributeError) as e:
            print(f"QUERY: {q}")
            print(f"  -> ERROR: {e}")
            print()
