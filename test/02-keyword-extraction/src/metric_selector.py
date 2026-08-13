# 원문 metric과 정규화 metric 중 키워드 생성에 사용할 하나를 선택합니다.
"""HCX-007의 index 선택을 이용해 입력 후보 중 하나만 반환한다."""

from typing import Any, Callable, Dict, List, Optional

try:
    from src.hcx_client import call_hcx, extract_hcx_content
    from src.hcx_keyword_expander import parse_candidate_indices
except ModuleNotFoundError:
    from hcx_client import call_hcx, extract_hcx_content
    from hcx_keyword_expander import parse_candidate_indices


class InvalidMetricSelectionInputError(ValueError):
    """선택할 수 있는 metric 후보가 없을 때 발생한다."""


def build_metric_selector_messages(
    metric: str,
    metric_normalized: str,
    claim: Optional[str] = None,
) -> List[Dict[str, str]]:
    """HCX-007이 입력된 두 후보 중 index 하나만 선택하도록 지시한다."""

    system_prompt = """당신은 뉴스의 수치 기반 claim을 KOSIS에서 검색할 통계 metric으로 정리하는 전문가입니다.
입력된 두 후보 중 하나만 선택하세요. 새로운 metric을 생성하거나 후보 문구를 수정하지 마세요.
반드시 선택한 후보의 index 하나만 JSON 정수 배열로 반환하세요. 정상 예시는 [0] 또는 [1]입니다.
설명, 이유, Markdown, 후보 문자열은 출력하지 마세요.

판단 우선순위:
1. claim의 원문 의미를 더 정확히 보존하는 후보
2. 대상(entity), 방향성·상태, 비교 의미가 손실되지 않은 후보
3. 측정 가능한 통계지표 또는 KOSIS 검색 표현에 가까운 후보
4. 광범위한 일반 명사보다 대상+지표 구조가 구체적인 후보
5. 수·비율·금액·건수·인구·수출액 등 측정 개념이 명확한 후보

정규화 후보가 원래 의미를 잃거나 다른 의미로 바뀌었다면 원문 metric을 선택하세요.
두 후보가 사실상 동일하면 원문 metric인 index 0을 선택하세요.
과도하게 추론하지 말고 입력된 claim과 두 후보만 근거로 판단하세요."""
    claim_text = _normalize_optional_text(claim) or "제공되지 않음"
    user_prompt = (
        f"claim: {claim_text}\n"
        f"0: {metric}\n"
        f"1: {metric_normalized}\n"
        "더 적합한 하나의 index만 반환하세요."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def select_metric(
    metric: Optional[str],
    metric_normalized: Optional[str],
    claim: Optional[str] = None,
    hcx_caller: Callable[..., Dict[str, Any]] = call_hcx,
) -> str:
    """두 metric 중 하나를 선택하고 실패 시 정규화 metric을 우선 사용한다."""

    original = _normalize_optional_text(metric)
    normalized = _normalize_optional_text(metric_normalized)
    if not original and not normalized:
        raise InvalidMetricSelectionInputError("선택할 metric이 없습니다.")
    if not original:
        return normalized
    if not normalized:
        return original
    if original == normalized:
        return original

    messages = build_metric_selector_messages(original, normalized, claim)
    try:
        response_json = hcx_caller(
            "HCX-007",
            messages,
            thinking_effort="none",
        )
        indices = parse_candidate_indices(
            extract_hcx_content(response_json),
            candidate_count=2,
        )
        if len(indices) != 1:
            return normalized
        return [original, normalized][indices[0]]
    except Exception:
        return normalized


def _normalize_optional_text(value: Optional[str]) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())
