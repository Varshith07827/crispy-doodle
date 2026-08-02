"""Tests for the RAG retrieval primitives (pure, no network)."""

import math

from winspark.connectors import retrieval as r


def test_tokenize_drops_stopwords_and_short_words():
    toks = r.tokenize("What is the exam FEE deadline?")
    assert "fee" in toks and "deadline" in toks and "exam" in toks
    assert "is" not in toks and "the" not in toks  # stopwords gone


def test_cosine_basic():
    assert r.cosine([1, 0], [1, 0]) == 1.0
    assert r.cosine([1, 0], [0, 1]) == 0.0
    assert abs(r.cosine([1, 1], [1, 0]) - (1 / math.sqrt(2))) < 1e-9
    assert r.cosine([], [1]) == 0.0  # empty/mismatched -> 0


def test_lexical_ranks_the_relevant_message_and_excludes_chitchat():
    texts = [
        "ok see you later",
        "the exam fee is 45000, due January 15",
        "haha that's funny",
        "the meeting is moved to 3pm tomorrow",
    ]
    idx = r.rank_lexical("how much is the exam fee?", texts, k=3)
    # The fee line ranks first; unrelated chit-chat ("ok see you", "haha") is
    # excluded because it shares no meaningful words.
    assert idx[0] == 1
    assert 0 not in idx and 2 not in idx


def test_lexical_returns_empty_when_nothing_shares_words():
    assert r.rank_lexical("quantum entanglement", ["dinner at 8", "see you"], k=3) == []


def test_semantic_ranks_by_cosine_and_respects_floor():
    query = [1.0, 0.0, 0.0]
    cands = [[0.95, 0.05, 0.0], [0.0, 1.0, 0.0], [0.7, 0.1, 0.1]]
    picked = r.rank_semantic(query, cands, k=3, floor=0.2)
    assert picked[0] == 0            # most similar first
    assert 1 not in picked           # orthogonal vector below the floor
    # A high floor drops everything but the near-identical vector.
    assert r.rank_semantic(query, cands, k=3, floor=0.99) == [0] or \
           r.rank_semantic(query, cands, k=3, floor=0.99) == []
