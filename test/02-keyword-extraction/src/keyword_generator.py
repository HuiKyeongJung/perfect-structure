# 입력 객체의 metric을 원키워드로 보존하는 기능을 제공합니다.
"""KOSIS 검색 키워드 생성의 metric 입력 단계를 제공한다."""

from typing import Any, Dict, List, TypedDict, Union


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
    """metric에서 규칙 기반 1차 KOSIS 검색 키워드 후보를 만든다."""

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


def generate_kosis_keywords(input_data: Any) -> Dict[str, Union[str, List[str]]]:
    """metric에서 생성한 키워드 후보를 정리해 반환한다."""

    if not isinstance(input_data, dict):
        raise InvalidMetricInputError("입력은 JSON 객체여야 합니다.")

    metric = extract_metric(input_data)
    original_candidate = create_original_candidate(metric)
    result: Dict[str, Union[str, List[str]]] = {
        "claim_id": input_data.get("claim_id", ""),
        "metric": metric,
        "original_keyword": original_candidate["keyword"],
        "keywords": [],
        "status": "not_eligible",
        "error_message": "",
    }

    if is_kosis_eligible(input_data):
        rule_candidates = extract_initial_keywords(metric)
        all_candidates = merge_keyword_candidates(rule_candidates)
        result["keywords"] = normalize_and_deduplicate_candidates(all_candidates)
        result["status"] = "success"

    return result


def _append_keyword_candidate(candidates: List[str], candidate: str) -> None:
    """비어 있거나 일반 측정 표현만 남은 후보와 중복 후보를 제외한다."""

    if not candidate or candidate in GENERAL_MEASUREMENT_EXPRESSIONS:
        return
    if candidate not in candidates:
        candidates.append(candidate)

if __name__ == "__main__":
    sample_input = {
        "claim_id": "C002",
        "metric": "청년 취업자 수",
        "kosis_eligible": True,
    }

    result = generate_kosis_keywords(sample_input)

    print("\n=== KOSIS 키워드 생성 결과 ===")
    print(f"claim_id        : {result['claim_id']}")
    print(f"metric          : {result['metric']}")
    print(f"original_keyword: {result['original_keyword']}")
    print(f"status          : {result['status']}")
    print("keywords:")

    for i, keyword in enumerate(result["keywords"], start=1):
        print(f"  {i}. {keyword}")