# HCX를 이용해 키워드 후보를 생성하고 필터링합니다.
"""HCX-005 생성과 HCX-007 필터링을 연결하는 키워드 확장 모듈."""

import json
import logging
import re
from typing import Any, List

try:
    from src.hcx_client import HCXClientError, call_hcx, extract_hcx_content
    from src.hcx_prompt import (
        build_hcx005_expansion_messages,
        build_hcx007_filter_messages,
        build_hcx007_retry_messages,
    )
except ModuleNotFoundError:
    from hcx_client import HCXClientError, call_hcx, extract_hcx_content
    from hcx_prompt import (
        build_hcx005_expansion_messages,
        build_hcx007_filter_messages,
        build_hcx007_retry_messages,
    )


LOGGER = logging.getLogger(__name__)


class KeywordArrayParseError(ValueError):
    """HCX 응답이 유효한 키워드 JSON 배열이 아닐 때 발생한다."""


class CandidateIndexParseError(ValueError):
    """HCX-007 응답이 유효한 candidate index 배열이 아닐 때 발생한다."""


def parse_keyword_array(content: str) -> List[str]:
    """HCX content 안의 첫 JSON 배열을 정리된 키워드 목록으로 변환한다."""

    if not isinstance(content, str):
        raise KeywordArrayParseError("HCX 응답이 유효한 JSON 배열이 아닙니다.")

    decoder = json.JSONDecoder()
    parsed_content: Any = None
    for start_index, character in enumerate(content):
        if character != "[":
            continue

        try:
            candidate, _ = decoder.raw_decode(content[start_index:])
        except json.JSONDecodeError:
            continue

        if isinstance(candidate, list):
            parsed_content = candidate
            break

    if parsed_content is None:
        raise KeywordArrayParseError("HCX 응답이 유효한 JSON 배열이 아닙니다.")

    keywords: List[str] = []
    for item in parsed_content:
        if not isinstance(item, str):
            raise KeywordArrayParseError("HCX 키워드 배열의 모든 요소는 문자열이어야 합니다.")

        normalized_item = " ".join(item.split())
        if normalized_item and normalized_item not in keywords:
            keywords.append(normalized_item)

    return keywords


def parse_candidate_indices(content: str, candidate_count: int) -> List[int]:
    """HCX-007 응답에서 유효한 candidate index를 순서대로 추출한다."""

    if not isinstance(content, str):
        raise CandidateIndexParseError("HCX 응답이 유효한 JSON index 배열이 아닙니다.")

    parsed_content: Any = None
    try:
        direct_content = json.loads(content)
        if isinstance(direct_content, list):
            parsed_content = direct_content
    except json.JSONDecodeError:
        pass

    if parsed_content is None:
        array_start = content.find("[")
        array_end = content.rfind("]")
        if array_start != -1 and array_end >= array_start:
            try:
                array_content = json.loads(content[array_start : array_end + 1])
                if isinstance(array_content, list):
                    parsed_content = array_content
            except json.JSONDecodeError:
                pass

    if parsed_content == []:
        return []

    raw_indices: List[Any]
    if isinstance(parsed_content, list):
        raw_indices = parsed_content
    else:
        raw_indices = re.findall(r"(?<![\d.])-?\d+(?![\d.])", content)

    indices: List[int] = []
    for item in raw_indices:
        if isinstance(item, bool):
            continue

        if isinstance(item, int):
            index = item
        elif isinstance(item, str):
            try:
                index = int(item.strip())
            except ValueError:
                continue
        else:
            continue

        if 0 <= index < candidate_count and index not in indices:
            indices.append(index)

    if indices:
        return indices

    raise CandidateIndexParseError("HCX 응답에 유효한 candidate index가 없습니다.")


def generate_keywords_with_hcx005(
    original_keyword: str,
    seed_keyword: str,
    max_keywords: int = 10,
) -> List[str]:
    """HCX-005로 seed와 관련된 키워드 후보를 생성한다."""

    messages = build_hcx005_expansion_messages(
        original_keyword,
        seed_keyword,
        max_keywords,
    )
    try:
        response_json = call_hcx("HCX-005", messages)
        return parse_keyword_array(extract_hcx_content(response_json))
    except (HCXClientError, KeywordArrayParseError) as error:
        LOGGER.warning("HCX-005 키워드 생성에 실패했습니다: %s", error)
        return []


def filter_keywords_with_hcx007(
    original_keyword: str,
    seed_keyword: str,
    candidates: List[str],
) -> List[str]:
    """HCX-007으로 입력 후보 중 관련 키워드만 선택한다."""

    normalized_candidates = _normalize_keyword_list(candidates)
    if not normalized_candidates:
        return []

    messages = build_hcx007_filter_messages(
        original_keyword,
        seed_keyword,
        normalized_candidates,
    )
    try:
        response_json = call_hcx(
            "HCX-007",
            messages,
            thinking_effort="none",
        )
        candidate_indices = parse_candidate_indices(
            extract_hcx_content(response_json),
            len(normalized_candidates),
        )
    except CandidateIndexParseError as first_error:
        LOGGER.warning(
            "HCX-007 응답 형식이 올바르지 않아 한 번 재시도합니다: %s",
            first_error,
        )
        retry_messages = build_hcx007_retry_messages(
            original_keyword,
            seed_keyword,
            normalized_candidates,
        )
        try:
            retry_response_json = call_hcx(
                "HCX-007",
                retry_messages,
                thinking_effort="none",
            )
            candidate_indices = parse_candidate_indices(
                extract_hcx_content(retry_response_json),
                len(normalized_candidates),
            )
        except (HCXClientError, CandidateIndexParseError) as retry_error:
            LOGGER.warning("HCX-007 키워드 필터링 재시도에 실패했습니다: %s", retry_error)
            return normalized_candidates
    except HCXClientError as error:
        LOGGER.warning("HCX-007 키워드 필터링에 실패했습니다: %s", error)
        return normalized_candidates

    return [normalized_candidates[index] for index in candidate_indices]


def expand_and_filter_keyword(
    original_keyword: str,
    seed_keyword: str,
    max_keywords: int = 10,
) -> List[str]:
    """HCX-005 생성 결과를 HCX-007으로 필터링해 반환한다."""

    generated_candidates = generate_keywords_with_hcx005(
        original_keyword,
        seed_keyword,
        max_keywords,
    )
    return filter_keywords_with_hcx007(
        original_keyword,
        seed_keyword,
        generated_candidates,
    )


def _normalize_keyword_list(candidates: List[str]) -> List[str]:
    """키워드 후보의 공백·빈 값·중복을 정리한다."""

    normalized_candidates: List[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue

        normalized_candidate = " ".join(candidate.split())
        if normalized_candidate and normalized_candidate not in normalized_candidates:
            normalized_candidates.append(normalized_candidate)

    return normalized_candidates


if __name__ == "__main__":
    manual_test_cases = [
        ("반도체 수출", "수출"),
        ("자동차 수출", "수출"),
        ("수출액", "수출액"),
        ("하루 평균 수출액", "수출액"),
        ("무역수지 흑자", "무역수지"),
        ("수입액", "수입액"),
        ("추가경정예산", "추가경정예산"),
        ("중앙정부 채무", "채무"),
        ("국가 채무", "채무"),
        ("세수 결손", "세수"),
        ("나라빚 증가 속도", "빚"),
        ("나라 살림 적자", "적자"),
        ("국내총생산(GDP)", "GDP"),
        ("GDP", "GDP"),
        ("취업 청년", "취업"),
        ("취업 청년", "청년"),
        ("비경제 인구", "비경제"),
        ("비경제 인구", "인구"),
    ]

    for original_keyword, seed_keyword in manual_test_cases:
        generated_candidates = generate_keywords_with_hcx005(
            original_keyword,
            seed_keyword,
        )
        filtered_candidates = filter_keywords_with_hcx007(
            original_keyword,
            seed_keyword,
            generated_candidates,
        )

        print("=" * 50)
        print(f"original_keyword: {original_keyword}")
        print(f"seed_keyword: {seed_keyword}")
        print("\n[HCX-005 생성 결과]")
        print(generated_candidates)
        print("\n[HCX-007 필터 결과]")
        print(filtered_candidates)
    print("=" * 50)
