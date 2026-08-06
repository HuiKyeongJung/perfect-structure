# 입력 객체의 metric을 원키워드로 보존하는 기능을 제공합니다.
"""KOSIS 검색 키워드 생성의 metric 입력 단계를 제공한다."""

from typing import Any, Dict, List, Union


class InvalidMetricInputError(ValueError):
    """유효한 metric을 찾을 수 없을 때 발생하는 예외."""


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


def create_original_candidate(metric: str) -> Dict[str, List[str]]:
    """정규화된 metric을 원키워드 후보 구조로 만든다."""

    return {"keyword": metric, "sources": ["original"]}


def generate_kosis_keywords(input_data: Any) -> Dict[str, Union[str, List[str]]]:
    """metric 하나만 보존한 최소 KOSIS 검색 키워드 결과를 반환한다."""

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
        result["keywords"] = [original_candidate["keyword"]]
        result["status"] = "success"

    return result
