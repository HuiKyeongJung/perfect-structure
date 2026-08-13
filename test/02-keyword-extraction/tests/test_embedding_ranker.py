# Embedding 기반 키워드 순위 계산과 실패 격리 정책을 검증합니다.
from unittest.mock import Mock

import pytest

from src import embedding_ranker


def test_cosine_similarity_of_same_direction_is_one():
    assert embedding_ranker.cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero():
    assert embedding_ranker.cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)


def test_cosine_similarity_of_opposite_vectors_is_minus_one():
    assert embedding_ranker.cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)


def test_cosine_similarity_rejects_dimension_mismatch():
    with pytest.raises(embedding_ranker.CosineSimilarityError, match="차원"):
        embedding_ranker.cosine_similarity([1, 0], [1, 0, 0])


@pytest.mark.parametrize("vec_a, vec_b", [([], [1, 0]), ([1, 0], []), ([0, 0], [1, 0])])
def test_cosine_similarity_rejects_empty_or_zero_vector(vec_a, vec_b):
    with pytest.raises(embedding_ranker.CosineSimilarityError):
        embedding_ranker.cosine_similarity(vec_a, vec_b)


def test_rank_keywords_sorts_by_similarity_descending(monkeypatch):
    vectors = {
        "국가 채무 증가율": [1.0, 0.0],
        "국가채무 증가율": [1.0, 0.0],
        "정부부채 증가율": [0.8, 0.6],
        "GDP 대비 국가채무 비율": [0.2, 0.98],
    }
    monkeypatch.setattr(embedding_ranker, "get_embedding", lambda text: vectors[text])

    result = embedding_ranker.rank_keywords(
        "국가 채무 증가율",
        ["GDP 대비 국가채무 비율", "정부부채 증가율", "국가채무 증가율"],
    )

    assert result == [
        "국가채무 증가율",
        "정부부채 증가율",
        "GDP 대비 국가채무 비율",
    ]


def test_rank_keywords_preserves_input_order_when_similarity_is_equal(monkeypatch):
    monkeypatch.setattr(embedding_ranker, "get_embedding", lambda _text: [1.0, 0.0])

    result = embedding_ranker.rank_keywords("기준", ["첫 번째", "두 번째", "세 번째"])

    assert result == ["첫 번째", "두 번째", "세 번째"]


def test_rank_keywords_deduplicates_and_reuses_embedding_cache(monkeypatch):
    get_embedding = Mock(return_value=[1.0, 0.0])
    monkeypatch.setattr(embedding_ranker, "get_embedding", get_embedding)

    result = embedding_ranker.rank_keywords("기준", ["후보", " 후보 ", "후보"])

    assert result == ["후보"]
    assert get_embedding.call_count == 2
    get_embedding.assert_any_call("기준")
    get_embedding.assert_any_call("후보")


def test_rank_keywords_skips_only_failed_keyword(monkeypatch):
    vectors = {
        "기준": [1.0, 0.0],
        "정상 후보 1": [1.0, 0.0],
        "정상 후보 2": [0.5, 0.5],
    }

    def fake_get_embedding(text):
        if text == "실패 후보":
            raise RuntimeError("embedding failure")
        return vectors[text]

    monkeypatch.setattr(embedding_ranker, "get_embedding", fake_get_embedding)

    result = embedding_ranker.rank_keywords(
        "기준",
        ["정상 후보 2", "실패 후보", "정상 후보 1"],
    )

    assert result == ["정상 후보 1", "정상 후보 2", "실패 후보"]


def test_rank_keywords_returns_empty_list_without_embedding_call(monkeypatch):
    get_embedding = Mock()
    monkeypatch.setattr(embedding_ranker, "get_embedding", get_embedding)

    assert embedding_ranker.rank_keywords("기준", []) == []
    get_embedding.assert_not_called()


def test_rank_keywords_preserves_original_order_when_reference_embedding_fails(monkeypatch):
    monkeypatch.setattr(
        embedding_ranker,
        "get_embedding",
        Mock(side_effect=RuntimeError("embedding failure")),
    )

    assert embedding_ranker.rank_keywords("기준", ["후보 1", "후보 2"]) == [
        "후보 1",
        "후보 2",
    ]


@pytest.mark.parametrize("reference_text", [None, 123, "", "   "])
def test_rank_keywords_rejects_invalid_reference(reference_text):
    with pytest.raises(embedding_ranker.EmbeddingRankerInputError):
        embedding_ranker.rank_keywords(reference_text, ["후보"])


def test_rank_keywords_rejects_non_list_keywords():
    with pytest.raises(embedding_ranker.EmbeddingRankerInputError):
        embedding_ranker.rank_keywords("기준", "후보")


def test_rank_keywords_rejects_non_string_keyword():
    with pytest.raises(embedding_ranker.EmbeddingRankerInputError):
        embedding_ranker.rank_keywords("기준", ["정상 후보", 123])


def test_rank_keywords_skips_blank_keywords(monkeypatch):
    get_embedding = Mock(return_value=[1.0, 0.0])
    monkeypatch.setattr(embedding_ranker, "get_embedding", get_embedding)

    result = embedding_ranker.rank_keywords("기준", ["", "   ", "정상 후보"])

    assert result == ["정상 후보"]
    assert get_embedding.call_count == 2


def test_rank_keywords_public_result_contains_only_strings(monkeypatch):
    monkeypatch.setattr(embedding_ranker, "get_embedding", lambda _text: [1.0, 0.0])

    result = embedding_ranker.rank_keywords("기준", ["후보 1", "후보 2"])

    assert result == ["후보 1", "후보 2"]
    assert all(isinstance(keyword, str) for keyword in result)


def test_rank_keywords_keeps_all_candidates_without_threshold(monkeypatch):
    vectors = {"기준": [1.0, 0.0]}
    keywords = [f"후보 {index}" for index in range(10)]
    for index, keyword in enumerate(keywords):
        vectors[keyword] = [float(10 - index), float(index + 1)]
    monkeypatch.setattr(embedding_ranker, "get_embedding", lambda text: vectors[text])

    result = embedding_ranker.rank_keywords("기준", keywords)

    assert len(result) == 10
    assert set(result) == set(keywords)


def test_rank_keywords_does_not_return_score_objects(monkeypatch):
    monkeypatch.setattr(embedding_ranker, "get_embedding", lambda _text: [1.0, 0.0])

    result = embedding_ranker.rank_keywords("기준", ["후보"])

    assert result == ["후보"]
    assert not any(isinstance(item, dict) for item in result)
