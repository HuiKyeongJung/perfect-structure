# 입력 객체의 metric을 원키워드로 보존하고 관련 키워드 후보를 생성합니다.
"""metric 기반 관련 키워드 후보 생성의 기본 로직을 제공한다."""

import json
from pathlib import Path
import logging

from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, Union

from kiwipiepy import Kiwi

try:
    from src.embedding_ranker import rank_keywords
    from src.hcx_keyword_expander import expand_and_filter_keyword
    from src.keyword_dictionary import RELATED_KEYWORD_DICTIONARY
except ModuleNotFoundError:
    from embedding_ranker import rank_keywords
    from hcx_keyword_expander import expand_and_filter_keyword
    from keyword_dictionary import RELATED_KEYWORD_DICTIONARY


LOGGER = logging.getLogger(__name__)


class InvalidMetricInputError(ValueError):
    """유효한 metric을 찾을 수 없을 때 발생하는 예외."""


class OriginalKeywordCandidate(TypedDict):
    """원키워드 후보의 구조."""

    keyword: str
    sources: List[str]


GENERAL_MEASUREMENT_EXPRESSIONS = {
    "수",
    "인원",
    "인구",
    "비율",
    "비중",
    "규모",
    "현황",
}

KIWI_NOUN_TAGS = {"NNG", "NNP", "SL"}
KIWI = Kiwi()


def extract_metric(input_data: Any) -> str:
    """입력 객체에서 metric을 검증·정규화해 반환한다."""

    if not isinstance(input_data, dict):
        raise InvalidMetricInputError("입력은 JSON 객체여야 합니다.")

    if "metric" in input_data:
        metric = input_data["metric"]
    elif "metric:" in input_data:
        metric = input_data["metric:"]
    else:
        raise InvalidMetricInputError("metric을 찾을 수 없습니다.")

    if not isinstance(metric, str):
        raise InvalidMetricInputError("metric은 문자열이어야 합니다.")

    normalized_metric = " ".join(metric.split())
    if not normalized_metric:
        raise InvalidMetricInputError("metric을 찾을 수 없습니다.")

    return normalized_metric


def is_kosis_eligible(input_data: Any) -> bool:
    """kosis_eligible 값이 정확히 True인지 확인한다."""

    return isinstance(input_data, dict) and input_data.get("kosis_eligible") is True


def create_original_candidate(metric: str) -> OriginalKeywordCandidate:
    """정규화된 metric을 원키워드 후보 구조로 만든다."""

    return {"keyword": metric, "sources": ["original"]}


def extract_initial_keywords(metric: str) -> List[str]:
    """metric에서 규칙 기반 1차 관련 키워드 후보를 만든다."""

    normalized_metric = " ".join(metric.split())
    if not normalized_metric:
        return []

    candidates = [normalized_metric]
    tokens = normalized_metric.split()

    if tokens[-1] in GENERAL_MEASUREMENT_EXPRESSIONS:
        _append_keyword_candidate(candidates, " ".join(tokens[:-1]))

    if len(tokens) >= 2:
        _append_keyword_candidate(candidates, " ".join(tokens[:-1]))
        _append_keyword_candidate(candidates, " ".join(tokens[1:]))

    return candidates


def extract_kiwi_nouns(metric: str) -> List[str]:
    """metric에서 Kiwi의 일반명사·고유명사·영문 표현 후보를 추출한다."""

    normalized_metric = " ".join(metric.split())
    if not normalized_metric:
        return []

    nouns: List[str] = []
    for token in KIWI.tokenize(normalized_metric):
        if token.tag in KIWI_NOUN_TAGS and token.form not in nouns:
            nouns.append(token.form)

    return nouns


def extract_surface_keywords(metric: str) -> List[str]:
    """metric에 원문 공백 단위로 있던 단어를 순서대로 추출한다."""

    normalized_metric = " ".join(metric.split())
    if not normalized_metric:
        return []

    surface_keywords: List[str] = []
    for keyword in normalized_metric.split():
        if keyword not in surface_keywords:
            surface_keywords.append(keyword)

    return surface_keywords


def filter_meaningful_seed_candidates(
    metric: str,
    candidates: List[str],
    semantic_validator: Callable[[str, str], bool],
) -> List[str]:
    """의미 검증기를 통과한 후보만 원래 순서대로 반환한다."""

    validated_candidates: List[str] = []
    for candidate in candidates:
        if semantic_validator(metric, candidate) and candidate not in validated_candidates:
            validated_candidates.append(candidate)

    return validated_candidates


def needs_compound_fallback(metric: str, kiwi_nouns: List[str]) -> bool:
    """Kiwi가 metric을 하나의 명사 덩어리로만 반환했는지 확인한다."""

    normalized_metric = "".join(metric.split())
    if not normalized_metric or len(kiwi_nouns) != 1:
        return False

    normalized_kiwi_noun = "".join(kiwi_nouns[0].split())
    return bool(normalized_kiwi_noun) and normalized_metric == normalized_kiwi_noun


def merge_keyword_candidates(*candidate_groups: List[str]) -> List[str]:
    """여러 키워드 후보 그룹을 입력 순서대로 하나의 목록으로 합친다."""

    merged_candidates: List[str] = []
    for candidates in candidate_groups:
        merged_candidates.extend(candidates)

    return merged_candidates


def normalize_and_deduplicate_candidates(candidates: List[Any]) -> List[str]:
    """후보 문자열의 공백을 정리하고 빈 값과 중복을 제거한다."""

    normalized_candidates: List[str] = []
    seen_candidates = set()

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue

        normalized_candidate = " ".join(candidate.split())
        if not normalized_candidate or normalized_candidate in seen_candidates:
            continue

        seen_candidates.add(normalized_candidate)
        normalized_candidates.append(normalized_candidate)

    return normalized_candidates


def expand_seed_keywords_from_dictionary(seed_keywords: List[str]) -> List[str]:
    """seed keyword를 정적 관련어 사전으로 조회해 확장한다."""

    expanded_keywords: List[str] = []
    for seed_keyword in seed_keywords:
        if not isinstance(seed_keyword, str):
            continue

        normalized_seed = " ".join(seed_keyword.split())
        if not normalized_seed:
            continue

        expanded_keywords.extend(
            RELATED_KEYWORD_DICTIONARY.get(normalized_seed, [])
        )

    return normalize_and_deduplicate_candidates(expanded_keywords)


def _expand_keyword_with_hcx(
    original_keyword: str,
    seed_keyword: str,
    expansion_cache: Dict[Tuple[str, str], List[str]],
) -> List[str]:
    """HCX 확장 결과를 함수 실행 범위에서 캐시하고 실패 시 빈 목록을 반환한다."""

    cache_key = (original_keyword, seed_keyword)
    if cache_key not in expansion_cache:
        try:
            expanded_keywords = expand_and_filter_keyword(
                original_keyword,
                seed_keyword,
            )
            expansion_cache[cache_key] = normalize_and_deduplicate_candidates(
                expanded_keywords
            )
        except Exception as error:
            LOGGER.warning(
                "HCX 키워드 확장에 실패해 해당 seed를 건너뜁니다. seed=%s error=%s",
                seed_keyword,
                error,
            )
            expansion_cache[cache_key] = []

    return list(expansion_cache[cache_key])


def generate_kosis_keywords(
    input_data: Any,
    semantic_validator: Optional[Callable[[str, str], bool]] = None,
) -> Dict[str, Union[str, List[str]]]:
    """metric에서 생성한 키워드 후보를 정리해 반환한다."""

    if not isinstance(input_data, dict):
        raise InvalidMetricInputError("입력은 JSON 객체여야 합니다.")

    metric = extract_metric(input_data)
    original_candidate = create_original_candidate(metric)
    result: Dict[str, Union[str, List[str]]] = {
        "claim_id": input_data.get("claim_id", ""),
        "metric": metric,
        "original_keyword": original_candidate["keyword"],
        "seed_keywords": [],
        "original_expanded_keywords": [],
        "seed_expanded_keywords": [],
        "keywords": [],
        "status": "not_eligible",
        "error_message": "",
    }

    if is_kosis_eligible(input_data):
        rule_candidates = extract_initial_keywords(metric)
        surface_seed_keywords = extract_surface_keywords(metric)
        kiwi_raw_keywords = extract_kiwi_nouns(metric)
        validated_kiwi_keywords = []
        if semantic_validator is not None:
            validated_kiwi_keywords = filter_meaningful_seed_candidates(
                metric,
                kiwi_raw_keywords,
                semantic_validator,
            )
        kiwi_seed_keywords = normalize_and_deduplicate_candidates(
            merge_keyword_candidates(surface_seed_keywords, validated_kiwi_keywords)
        )
        dictionary_expanded_keywords = expand_seed_keywords_from_dictionary(
            kiwi_seed_keywords
        )
        expansion_cache: Dict[Tuple[str, str], List[str]] = {}
        original_expanded_keywords = _expand_keyword_with_hcx(
            metric,
            metric,
            expansion_cache,
        )
        seed_expanded_keywords: List[str] = []
        for seed_keyword in kiwi_seed_keywords:
            seed_expanded_keywords.extend(
                _expand_keyword_with_hcx(
                    metric,
                    seed_keyword,
                    expansion_cache,
                )
            )
        seed_expanded_keywords = normalize_and_deduplicate_candidates(
            seed_expanded_keywords
        )

        all_candidates = merge_keyword_candidates(
            [original_candidate["keyword"]],
            rule_candidates,
            kiwi_seed_keywords,
            dictionary_expanded_keywords,
            original_expanded_keywords,
            seed_expanded_keywords,
        )
        result["seed_keywords"] = kiwi_seed_keywords
        result["original_expanded_keywords"] = original_expanded_keywords
        result["seed_expanded_keywords"] = seed_expanded_keywords
        merged_keywords = normalize_and_deduplicate_candidates(all_candidates)
        try:
            ranked_keywords = rank_keywords(
                reference_text=original_candidate["keyword"],
                keywords=merged_keywords,
            )
            if (
                len(ranked_keywords) != len(merged_keywords)
                or set(ranked_keywords) != set(merged_keywords)
            ):
                LOGGER.warning(
                    "Embedding ranking 결과의 후보 구성이 달라 기존 순서를 유지합니다."
                )
                ranked_keywords = merged_keywords
        except Exception as error:
            LOGGER.warning(
                "Embedding ranking에 실패해 기존 키워드 순서를 유지합니다. error=%s",
                error,
            )
            ranked_keywords = merged_keywords

        result["keywords"] = ranked_keywords
        result["status"] = "success"

    return result


def _append_keyword_candidate(candidates: List[str], candidate: str) -> None:
    """비어 있거나 일반 측정 표현만 남은 후보와 중복 후보를 제외한다."""

    if not candidate or candidate in GENERAL_MEASUREMENT_EXPRESSIONS:
        return
    if candidate not in candidates:
        candidates.append(candidate)


if __name__ == "__main__":
    test_inputs = [
        {
            "claim_id": "C001",
            "metric": "청년 취업",
        },
        {
            "claim_id": "C002",
            "metric": "비경제 인구",
        },
        {
            "claim_id": "C003",
            "metric": "하루 평균 수출액",
        },
        {
            "claim_id": "C004",
            "metric": "무역수지 흑자",
        },
        {
            "claim_id": "C005",
            "metric": "국가 채무 증가율",
        },
    ]

    generated_results = []

    for index, test_input in enumerate(test_inputs):
        if index:
            print("=" * 50)

        result = generate_kosis_keywords(
            {**test_input, "kosis_eligible": True},
        )
        generated_results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    output_path = Path(__file__).resolve().parents[1] / "data" / "generated_keywords.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(generated_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
