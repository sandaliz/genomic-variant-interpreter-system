"""
retrieval/bm25.py

BM25 (Okapi) scoring, implemented here rather than pulled from a package.

Why hand-rolled. The alternative -- ``rank_bm25`` -- is a single maintained
file that would work fine, but this project's story is "clone it and run it",
and BM25 is about forty lines of arithmetic with a formula that is published.
Writing it out means the report can point at a line number for every claim it
makes about the ranking, which is the same reason the retrieval index is built
locally instead of being called from a service.

The score for document d and query Q is the standard Okapi formulation:

    score(d, Q) = Σ_{t ∈ Q} IDF(t) · (f(t,d) · (k1 + 1))
                              / (f(t,d) + k1 · (1 - b + b · |d| / avgdl))

where f(t,d) is the raw term frequency of t in d and |d| is the document
length in tokens. k1 controls how quickly repeated terms saturate -- at k1=0 a
term counts once no matter how often it appears -- and b controls how hard a
long document is penalised for being long. b=0 disables the length penalty
entirely, which for this corpus would hand a systematic advantage to whichever
variant row happens to carry the most annotations, so it matters that it is
switched on.

The IDF is the Robertson-Sparck Jones form with the ``1 +`` of the Lucene
"BM25+" variant:

    IDF(t) = ln(1 + (N - n(t) + 0.5) / (n(t) + 0.5))

which stays positive for a term that appears in every document. The
unsmoothed form goes negative there, so a query made entirely of corpus-wide
terms would score *lower* for a document that contains them, which is not what
the model is supposed to mean.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, List, Sequence

# Robertson & Zaragoza's TREC-3 values, and the ones every published BM25
# baseline quotes. Left untuned on purpose: a reader comparing these numbers
# with any other BM25 result should not have to wonder whether the constants
# were fitted to flatter this corpus.
DEFAULT_K1 = 1.2
DEFAULT_B = 0.75

# Lower-cased runs of ASCII letters and digits. Everything else is a separator:
# the "." in HGVS notation is dropped rather than kept, and so are the "_" and
# ">" that TF-IDF's own token pattern *does* keep -- so this is deliberately not
# the same tokenisation the vectorizer applies. "NC_000017.10" is one token to
# TF-IDF and three here, and "c.5946delT" becomes ["c", "5946delt"], not
# ["c5946delt"]. What the two models share is the input *field* (``search_text``),
# which is what makes a rank difference between them a difference between the
# models rather than between their corpora.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text) -> List[str]:
    """Lower-cased alphanumeric tokens. ``None`` tokenises to nothing at all."""
    if text is None:
        return []
    return _TOKEN_RE.findall(str(text).lower())


class BM25Index:
    """
    A BM25 index over a list of documents.

    Built from the same per-variant text field the TF-IDF index uses, so the
    two models are scored on identical input and any difference in ranking is
    a difference between the models rather than between their corpora.
    """

    def __init__(self, documents: Iterable, k1: float = DEFAULT_K1,
                 b: float = DEFAULT_B):
        self.k1 = float(k1)
        self.b = float(b)
        self._tokenised: List[List[str]] = [tokenize(doc) for doc in documents]
        self._counts: List[Counter] = [Counter(tokens) for tokens in self._tokenised]
        self._lengths: List[int] = [len(tokens) for tokens in self._tokenised]
        self._n = len(self._tokenised)
        self._avgdl = (sum(self._lengths) / self._n) if self._n else 0.0

        # Document frequency per term, computed once. Term frequency is read
        # per document from the Counters above.
        self._document_frequency: Counter = Counter()
        for counts in self._counts:
            self._document_frequency.update(counts.keys())

    def __len__(self) -> int:
        return self._n

    @property
    def average_document_length(self) -> float:
        return self._avgdl

    def idf(self, term: str) -> float:
        """The Robertson-Sparck Jones IDF of a single token."""
        if not self._n:
            return 0.0
        seen_in = self._document_frequency.get(term, 0)
        if not seen_in:
            return 0.0
        return math.log(1.0 + (self._n - seen_in + 0.5) / (seen_in + 0.5))

    def score_of(self, doc_index: int, terms: Sequence[str]) -> float:
        """BM25 of one document against an already-tokenised query."""
        if not self._n or doc_index < 0 or doc_index >= self._n:
            return 0.0
        counts = self._counts[doc_index]
        length = self._lengths[doc_index]
        # Guard the degenerate corpus where every document is empty: avgdl is 0
        # and the length ratio is undefined. Treat it as a fully length-penalised
        # document rather than dividing by zero.
        normaliser = 1.0 - self.b
        if self._avgdl:
            normaliser += self.b * (length / self._avgdl)
        total = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            weight = self.idf(term)
            if not weight:
                continue
            total += weight * (frequency * (self.k1 + 1.0)) / (
                frequency + self.k1 * normaliser
            )
        return total

    def score(self, query: str) -> List[float]:
        """
        BM25 of every document against a query string.

        Returns one score per document, in document order. A term absent from
        the whole corpus contributes nothing: its IDF is 0, so it cannot
        distinguish documents and is correctly ignored rather than treated as
        evidence of absence.
        """
        terms = tokenize(query)
        if not terms:
            return [0.0] * self._n
        return [self.score_of(i, terms) for i in range(self._n)]

    def explain(self, query: str, doc_index: int, limit: int = 3) -> List[tuple]:
        """
        The terms that contributed most to one document's score.

        Used for the per-result relevance explanation, so a BM25 rank can be
        read as "these query terms, these weights" instead of a bare number.
        """
        terms = tokenize(query)
        contributions = []
        for term in dict.fromkeys(terms):
            counts = self._counts[doc_index] if 0 <= doc_index < self._n else {}
            frequency = counts.get(term, 0) if counts else 0
            if not frequency:
                continue
            length = self._lengths[doc_index]
            normaliser = 1.0 - self.b
            if self._avgdl:
                normaliser += self.b * (length / self._avgdl)
            weight = self.idf(term)
            value = weight * (frequency * (self.k1 + 1.0)) / (
                frequency + self.k1 * normaliser
            )
            contributions.append((term, value))
        contributions.sort(key=lambda pair: pair[1], reverse=True)
        return contributions[:limit]
