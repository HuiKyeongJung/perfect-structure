# Embedding cosine similarity로 키워드 후보의 검색 순서를 정렬합니다.
"""기준 키워드와 후보 키워드 간의 embedding 유사도 순위를 계산한다."""

import logging
import math
from typing import Dict, List, Tuple

try:
    from src.embedding_client import get_embedding
except ModuleNotFoundError:
    from embedding_client import get_embedding


LOGGER = logging.getLogger(__name__)


class EmbeddingRankerError(RuntimeError):
    """Embedding 키워드 순위 계산 중 발생하는 기본 예외."""


class EmbeddingRankerInputError(EmbeddingRankerError):
    """랭킹 입력값이 올바르지 않을 때 발생한다."""


class CosineSimilarityError(EmbeddingRankerError):
    """코사인 유사도를 계산할 수 없을 때 발생한다."""


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """길이가 같은 두 벡터의 코사인 유사도를 계산한다."""

    if not vec_a or not vec_b:
        raise CosineSimilarityError("코사인 유사도 계산에 빈 벡터를 사용할 수 없습니다.")
    if len(vec_a) != len(vec_b):
        raise CosineSimilarityError("두 벡터의 차원이 일치하지 않습니다.")

    dot_product = sum(value_a * value_b for value_a, value_b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(value * value for value in vec_a))
    norm_b = math.sqrt(sum(value * value for value in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise CosineSimilarityError("영벡터의 코사인 유사도는 계산할 수 없습니다.")

    return float(dot_product / (norm_a * norm_b))


def rank_keywords(reference_text: str, keywords: List[str]) -> List[str]:
    """점수를 노출하지 않고 모든 후보를 기준 텍스트와 가까운 순서로 반환한다."""

    normalized_reference = _normalize_reference_text(reference_text)
    normalized_keywords = _normalize_keywords(keywords)
    if not normalized_keywords:
        return []

    embedding_cache: Dict[str, List[float]] = {}
    try:
        reference_embedding = get_embedding(normalized_reference)
    except Exception as error:
        LOGGER.warning(
            "기준 텍스트 embedding에 실패해 기존 키워드 순서를 유지합니다. error=%s",
            error,
        )
        return normalized_keywords
    embedding_cache[normalized_reference] = reference_embedding

    scored_keywords: List[Tuple[str, float]] = []
    failed_keywords: List[str] = []
    for keyword in normalized_keywords:
        try:
            if keyword not in embedding_cache:
                embedding_cache[keyword] = get_embedding(keyword)
            similarity = cosine_similarity(
                reference_embedding,
                embedding_cache[keyword],
            )
        except Exception as error:
            LOGGER.warning(
                "키워드 embedding 또는 유사도 계산에 실패해 기존 순서로 뒤에 보존합니다. keyword=%s error=%s",
                keyword,
                error,
            )
            failed_keywords.append(keyword)
            continue

        scored_keywords.append((keyword, similarity))

    # Python 정렬은 안정 정렬이므로 동률 후보는 입력 순서를 유지한다.
    scored_keywords.sort(key=lambda item: item[1], reverse=True)
    return [keyword for keyword, _score in scored_keywords] + failed_keywords


def _normalize_reference_text(reference_text: str) -> str:
    """기준 텍스트를 검증하고 앞뒤 공백을 제거한다."""

    if not isinstance(reference_text, str):
        raise EmbeddingRankerInputError("reference_text는 비어 있지 않은 문자열이어야 합니다.")

    normalized_reference = reference_text.strip()
    if not normalized_reference:
        raise EmbeddingRankerInputError("reference_text는 비어 있지 않은 문자열이어야 합니다.")
    return normalized_reference


def _normalize_keywords(keywords: List[str]) -> List[str]:
    """빈 후보를 제외하고 최초 등장한 문자열 후보만 유지한다."""

    if not isinstance(keywords, list):
        raise EmbeddingRankerInputError("keywords는 문자열 리스트여야 합니다.")

    normalized_keywords: List[str] = []
    seen_keywords = set()
    for keyword in keywords:
        if not isinstance(keyword, str):
            raise EmbeddingRankerInputError("keywords의 각 항목은 문자열이어야 합니다.")

        normalized_keyword = keyword.strip()
        if not normalized_keyword or normalized_keyword in seen_keywords:
            continue

        seen_keywords.add(normalized_keyword)
        normalized_keywords.append(normalized_keyword)

    return normalized_keywords


if __name__ == "__main__":
    sample_reference = "국가 채무 증가율"
    sample_keywords = [
        "국가채무 증가율",
        "정부부채 증가율",
        "GDP 대비 국가채무 비율",
        "국채 발행 잔액",
        "청년 고용률",
    ]

    try:
        sample_ranking = rank_keywords(sample_reference, sample_keywords)
        print(f"reference: {sample_reference}\n")
        for rank, keyword in enumerate(sample_ranking, start=1):
            print(f"{rank}. {keyword}")
    except EmbeddingRankerError as error:
        print(f"Embedding ranking 오류: {error}")
