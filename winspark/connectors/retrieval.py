"""Relevance ranking for chat-memory RAG — pick the few past messages actually
relevant to an incoming question, instead of stuffing the whole history into
the prompt.

Two ranking modes, chosen by the caller (see WhatsAppFetchRelayService):

- ``rank_semantic`` — cosine similarity over embedding vectors (best quality;
  used when an OpenAI key can produce embeddings).
- ``rank_lexical`` — a small TF-IDF word-overlap score (no embeddings, no
  dependencies, works offline / on providers without an embeddings endpoint).

Pure functions, stdlib only — no Qt, no network, no third-party packages — so
this is fast and fully unit-testable.
"""

from __future__ import annotations

import math
import re

# A small stop-word list: dropping these keeps lexical matching on the words
# that actually carry meaning ("fee", "deadline") rather than glue words.
_STOPWORDS = frozenset(
    "the a an and or but if then this that these those is are was were be been being "
    "to of in on at for with from by as it its it's i you he she they we me my your our "
    "do does did done have has had will would can could should may might must not no yes "
    "so up out about into over again just how what when where who why which whom than too "
    "here there all any some more most other one two do're you're i'm what's".split()
)

_TOKEN = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    """Lowercase content words (len > 2, non-stopword) for lexical scoring."""
    return [t for t in _TOKEN.findall((text or "").lower()) if len(t) > 2 and t not in _STOPWORDS]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors; 0 for empty/zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def rank_semantic(query_vec: list[float], candidate_vecs: list[list[float]], k: int = 6,
                  floor: float = 0.2) -> list[int]:
    """Indices of the top-k candidate vectors most similar to the query, above a
    similarity floor (keeps genuinely-unrelated messages out), best first."""
    scored = [(i, cosine(query_vec, v)) for i, v in enumerate(candidate_vecs)]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [i for i, score in scored if score >= floor][: max(0, k)]


def lexical_scores(query: str, texts: list[str]) -> list[float]:
    """A TF-IDF-ish overlap score per text: sum of the IDF weights of the query's
    content words that appear in it. Rare shared words (e.g. "deadline") count
    for more than common ones, so matches are meaningful rather than incidental."""
    q_tokens = set(tokenize(query))
    if not q_tokens or not texts:
        return [0.0] * len(texts)
    doc_tokens = [set(tokenize(t)) for t in texts]
    n_docs = len(texts)
    df: dict[str, int] = {}
    for tokens in doc_tokens:
        for tok in tokens:
            df[tok] = df.get(tok, 0) + 1

    def idf(tok: str) -> float:
        return math.log((n_docs + 1) / (df.get(tok, 0) + 1)) + 1.0

    return [sum(idf(tok) for tok in (q_tokens & tokens)) for tokens in doc_tokens]


def rank_lexical(query: str, texts: list[str], k: int = 6) -> list[int]:
    """Indices of the top-k texts sharing the most (IDF-weighted) content words
    with the query — only those with at least one meaningful shared word."""
    scores = lexical_scores(query, texts)
    order = sorted(range(len(texts)), key=lambda i: scores[i], reverse=True)
    return [i for i in order if scores[i] > 0.0][: max(0, k)]
