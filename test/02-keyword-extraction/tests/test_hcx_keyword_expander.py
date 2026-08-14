# HCX 키워드 확장과 필터링 흐름을 실제 API 없이 검증합니다.
import pytest

from src import hcx_keyword_expander as expander
from src.hcx_client import HCXClientError
from src.hcx_prompt import (
    build_hcx005_expansion_messages,
    build_hcx007_filter_messages,
    build_hcx007_retry_messages,
)


def test_parse_keyword_array_returns_a_string_list():
    assert expander.parse_keyword_array('["재배면적", "경지면적"]') == [
        "재배면적",
        "경지면적",
    ]


def test_parse_keyword_array_removes_duplicates():
    assert expander.parse_keyword_array('["재배면적", "경지면적", "경지면적"]') == [
        "재배면적",
        "경지면적",
    ]


def test_parse_keyword_array_removes_blank_strings():
    assert expander.parse_keyword_array('["재배면적", " ", "경지면적", ""]') == [
        "재배면적",
        "경지면적",
    ]


def test_parse_keyword_array_rejects_invalid_json():
    with pytest.raises(ValueError):
        expander.parse_keyword_array("재배면적, 경지면적")


def test_parse_keyword_array_extracts_array_from_markdown_code_fence():
    content = """추천 후보입니다.
```json
["반도체 수출액", "반도체 수출 실적"]
```
위 후보만 사용하세요."""

    assert expander.parse_keyword_array(content) == [
        "반도체 수출액",
        "반도체 수출 실적",
    ]


def test_parse_keyword_array_extracts_array_with_explanatory_text_around_it():
    content = '다음 후보를 유지합니다: ["반도체 수출액"] 감사합니다.'

    assert expander.parse_keyword_array(content) == ["반도체 수출액"]


def test_parse_candidate_indices_returns_integer_indices():
    assert expander.parse_candidate_indices("[0, 2]", 3) == [0, 2]


def test_parse_candidate_indices_recovers_array_from_markdown_and_explanation():
    content = """선택 결과입니다.
```json
[0, 2]
```
"""

    assert expander.parse_candidate_indices(content, 3) == [0, 2]


def test_parse_candidate_indices_accepts_numeric_strings():
    assert expander.parse_candidate_indices('["0", "2"]', 3) == [0, 2]


def test_parse_candidate_indices_accepts_comma_separated_numbers():
    assert expander.parse_candidate_indices("0, 2", 3) == [0, 2]


def test_parse_candidate_indices_extracts_numbers_from_explanation():
    content = "선택된 index는 0, 2입니다."

    assert expander.parse_candidate_indices(content, 3) == [0, 2]


def test_parse_candidate_indices_accepts_newline_separated_numbers():
    assert expander.parse_candidate_indices("0\n2", 3) == [0, 2]


def test_parse_candidate_indices_removes_duplicates_and_out_of_range_values():
    assert expander.parse_candidate_indices("[0, 2, 0, 7, -1]", 3) == [0, 2]


def test_parse_candidate_indices_accepts_explicit_empty_array():
    assert expander.parse_candidate_indices("[]", 3) == []


def test_parse_candidate_indices_rejects_explanation_without_numbers():
    with pytest.raises(ValueError):
        expander.parse_candidate_indices("적절한 후보가 없습니다.", 3)


def test_hcx005_prompt_allows_broader_statistical_candidates_for_recall():
    messages = build_hcx005_expansion_messages("반도체 수출", "수출")

    assert "seed_keyword를 단독으로 일반 확장해도 됩니다." in messages[0]["content"]
    assert "검색 후보의 recall을 높이는 것" in messages[0]["content"]
    assert "공식 통계 지표명 또는 통계표 제목형 표현을 반드시 포함하세요." in messages[0]["content"]
    assert "원본 metric과 반대 방향의 표현은 생성하지 마세요." in messages[0]["content"]


def test_hcx005_prompt_prioritizes_specific_statistical_terms_over_broad_words():
    messages = build_hcx005_expansion_messages("고용", "고용")
    prompt = messages[0]["content"]

    assert "넓은 단어 하나보다 구체적인 복합 통계 표현을 우선하세요." in prompt
    assert "고용 → 고용률, 취업자 수, 산업별 취업자 수" in prompt
    assert "분류축+지표" in prompt
    assert "시간축+지표" in prompt
    assert "원본 metric에 대상·주제어가 있으면 이를 유지한 대상+지표 후보도 반드시 생성하세요." in prompt


def test_hcx005_prompt_contains_official_term_examples_and_survey_name_guard():
    messages = build_hcx005_expansion_messages("물가", "물가")
    prompt = messages[0]["content"]

    assert "물가 → 소비자물가지수" in prompt
    assert "취업자 → 취업자 수" in prompt
    assert "출산율 → 합계출산율" in prompt
    assert "노인 → 고령인구" in prompt
    assert "확실하지 않은 구체적인 조사명을 임의로 만들지 마세요." in prompt


def test_hcx005_prompt_expands_each_seed_independently_from_original_keyword():
    messages = build_hcx005_expansion_messages("비경제 인구", "인구")
    prompt = messages[0]["content"]

    assert "현재 호출의 증폭 대상은 seed_keyword 하나입니다." in prompt
    assert "seed_keyword가 original_keyword와 다르면 seed_keyword를 독립적으로 증폭하세요." in prompt
    assert "original_keyword의 대상·문맥을 모든 후보에 강제로 포함하지 마세요." in prompt
    assert "seed_keyword의 원래 문자열도 모든 후보에 강제로 포함할 필요가 없습니다." in prompt
    assert "뉴스 표현을 KOSIS 통계 용어 공간으로 변환하세요." in prompt


def test_hcx005_original_and_seed_expansion_use_separate_calls():
    original_messages = build_hcx005_expansion_messages("비경제 인구", "비경제 인구")
    seed_messages = build_hcx005_expansion_messages("비경제 인구", "인구")

    assert "확장할 seed: 비경제 인구" in original_messages[1]["content"]
    assert "확장할 seed: 인구" in seed_messages[1]["content"]
    assert original_messages[1]["content"] != seed_messages[1]["content"]


def test_hcx007_prompt_keeps_broader_statistical_search_candidates():
    messages = build_hcx007_filter_messages(
        "반도체 수출",
        "수출",
        ["상품 수출", "반도체 수출액"],
    )

    assert "후보 문자열을 다시 출력하거나 새 후보를 만들지 마세요." in messages[0]["content"]
    assert "원본보다 넓은 의미라도 통계 검색어로 활용 가능하면 유지하세요." in messages[0]["content"]
    assert "명백한 비통계 노이즈를 제거하는 역할만 합니다." in messages[0]["content"]
    assert "원본 metric과 반대 방향의 후보는 제거하세요." in messages[0]["content"]
    assert "유지할 후보의 index만 JSON 정수 배열로 반환하세요." in messages[0]["content"]
    assert "0: 상품 수출" in messages[1]["content"]
    assert "1: 반도체 수출액" in messages[1]["content"]
    assert "유지할 후보가 없으면 []를 반환하세요." in messages[0]["content"]


def test_hcx007_prompt_uses_recall_first_keep_policy():
    messages = build_hcx007_filter_messages(
        "반도체 수출",
        "수출",
        ["수출액", "반도체 수출액", "수출입 동향"],
    )
    prompt = messages[0]["content"]

    assert "애매하면 KEEP하세요." in prompt
    assert "명백한 비통계 노이즈만 REMOVE하세요." in prompt
    assert "최종 검색 결과와 관련도 판단은 downstream KOSIS 통합검색 단계가 수행합니다." in prompt
    assert "original_keyword의 대상이 없어도 seed_keyword와 관련된 통계 표현이면 KEEP할 수 있습니다." in prompt
    assert "후보를 1~2개로 과도하게 줄이지 마세요." in prompt
    assert "시간축이 달라지거나 생략돼도 통계 검색 가치가 있으면 KEEP하세요." in prompt
    assert "국회 심의·심사, 제출 시기, 승인 절차" in prompt
    assert "세입결손처럼 같은 통계 분야의 관련 지표" in prompt


def test_hcx007_prompt_requires_removing_clear_opposite_directions():
    messages = build_hcx007_filter_messages(
        "무역수지 흑자",
        "무역수지",
        ["무역적자 규모", "무역수지", "상품수지", "서비스수지"],
    )
    prompt = messages[0]["content"]

    assert "명백한 반대 방향 후보는 반드시 REMOVE하세요." in prompt
    assert "흑자/적자" in prompt
    assert "증가/감소" in prompt
    assert "상승/하락" in prompt
    assert "확대/축소" in prompt
    assert "방향이 없는 상위 지표는 KEEP할 수 있습니다." in prompt


def test_hcx007_filter_keeps_related_axis_candidates_and_removes_opposite_candidate(
    monkeypatch,
):
    candidates = [
        "무역적자 규모",
        "월별 수출액",
        "연간 수출액",
        "분기별 수출액",
        "산업별 수출액",
        "품목별 수출액",
        "지역별 수출액",
        "국가별 수출액",
    ]
    captured_messages = []

    def fake_call_hcx(_model_name, messages, **_kwargs):
        captured_messages.extend(messages)
        return "[1, 2, 3, 4, 5, 6, 7]"

    monkeypatch.setattr(expander, "call_hcx", fake_call_hcx)
    monkeypatch.setattr(expander, "extract_hcx_content", lambda response: response)

    result = expander.filter_keywords_with_hcx007(
        "하루 평균 수출액",
        "수출액",
        candidates,
    )

    assert result == candidates[1:]
    assert "시간축·분류축이 달라도 seed와 관련된 통계 검색어는 적극적으로 KEEP하세요." in captured_messages[0]["content"]


def test_hcx007_prompt_prioritizes_avoiding_false_negatives():
    messages = build_hcx007_filter_messages(
        "하루 평균 수출액",
        "수출액",
        ["월별 수출액", "연간 수출액", "산업별 수출액"],
    )
    prompt = messages[0]["content"]

    assert "후보를 최소화하는 것이 목표가 아닙니다." in prompt
    assert "좋은 후보를 제거하는 false negative가 불필요한 후보를 남기는 false positive보다 더 위험합니다." in prompt
    assert "KOSIS 검색에 조금이라도 유용할 수 있으면 KEEP하세요." in prompt
    assert "original_keyword와 가장 정확히 일치하는 후보만 고르지 마세요." in prompt
    assert "후보 10개 중 7~10개를 KEEP하는 것도 정상적인 결과입니다." in prompt


def test_hcx007_prompt_marks_time_and_classification_axis_candidates_as_keepable():
    candidates = [
        "월별 수출액",
        "연간 수출액",
        "일평균 수출액",
        "연도별 수출액",
        "분기별 수출액",
        "산업별 수출액",
        "지역별 수출액",
        "국가별 수출액",
        "품목별 수출액",
    ]
    messages = build_hcx007_filter_messages("하루 평균 수출액", "수출액", candidates)
    prompt = messages[0]["content"]

    assert "월별·연간·일평균·연도별·분기별 + 지표는 KEEP 가능한 시간축 후보입니다." in prompt
    assert "산업별·지역별·국가별·품목별·연령별·성별 + 지표는 KEEP 가능한 분류축 후보입니다." in prompt
    for index, candidate in enumerate(candidates):
        assert f"{index}: {candidate}" in messages[1]["content"]


def test_hcx007_prompt_keeps_seed_terms_without_original_subject_and_removes_noise():
    messages = build_hcx007_filter_messages(
        "반도체 수출",
        "수출",
        ["수출 금액", "수출량", "월별 수출 추이", "감소 원인 분석", "국회 심의 과정"],
    )
    prompt = messages[0]["content"]

    assert "원본 subject 문자열이 없어도 seed 기반의 정상적인 통계 표현이면 KEEP하세요." in prompt
    assert "감소 원인 분석" in prompt
    assert "국회 심의 과정" in prompt
    assert "명백한 비통계 노이즈로 REMOVE하세요." in prompt


def test_hcx007_prompt_receives_diverse_semiconductor_export_candidates_by_index():
    candidates = [
        "수출액",
        "무역수지",
        "상품수출",
        "반도체 수출액",
        "산업별 수출",
        "지역별 수출",
        "월간 수출",
        "연간 수출",
        "일평균 수출",
        "수출입 동향",
    ]
    messages = build_hcx007_filter_messages("반도체 수출", "수출", candidates)

    for index, candidate in enumerate(candidates):
        assert f"{index}: {candidate}" in messages[1]["content"]


def test_hcx007_retry_prompt_requires_a_json_array_only():
    messages = build_hcx007_retry_messages(
        "반도체 수출",
        "수출",
        ["수출액", "품목별 수출"],
    )

    assert "반드시 JSON 정수 배열 하나만 반환하세요." in messages[0]["content"]
    assert "Markdown을 사용하지 마세요." in messages[0]["content"]
    assert "입력 candidates에 실제 존재하는 index만 반환하세요." in messages[0]["content"]


def test_filter_keywords_with_hcx007_skips_api_call_for_empty_candidates(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("HCX API should not be called")

    monkeypatch.setattr(expander, "call_hcx", fail_if_called)

    assert expander.filter_keywords_with_hcx007("재배면적", "재배면적", []) == []


def test_filter_keywords_with_hcx007_removes_out_of_range_indices(monkeypatch):
    monkeypatch.setattr(expander, "call_hcx", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        expander,
        "extract_hcx_content",
        lambda _response: "[0, 7]",
    )

    assert expander.filter_keywords_with_hcx007(
        "재배면적",
        "재배면적",
        ["경지면적", "작물별 재배면적"],
    ) == ["경지면적"]


def test_filter_keywords_with_hcx007_does_not_retry_after_a_valid_first_response(
    monkeypatch,
):
    called_requests = []

    def fake_call_hcx(model_name, _messages, **kwargs):
        called_requests.append((model_name, kwargs))
        return "[0]"

    monkeypatch.setattr(expander, "call_hcx", fake_call_hcx)
    monkeypatch.setattr(expander, "extract_hcx_content", lambda response: response)

    assert expander.filter_keywords_with_hcx007(
        "재배면적",
        "재배면적",
        ["경지면적", "작물별 재배면적"],
    ) == ["경지면적"]
    assert called_requests == [("HCX-007", {"thinking_effort": "none"})]


def test_filter_keywords_with_hcx007_retries_once_after_parse_failure(monkeypatch):
    called_models = []
    responses = iter(["선택 결과는 다음과 같습니다.", "[0]"])

    def fake_call_hcx(model_name, _messages, **_kwargs):
        called_models.append(model_name)
        return next(responses)

    monkeypatch.setattr(expander, "call_hcx", fake_call_hcx)
    monkeypatch.setattr(expander, "extract_hcx_content", lambda response: response)

    assert expander.filter_keywords_with_hcx007(
        "재배면적",
        "재배면적",
        ["경지면적", "작물별 재배면적"],
    ) == ["경지면적"]
    assert called_models == ["HCX-007", "HCX-007"]


def test_filter_keywords_with_hcx007_uses_candidates_as_fallback_after_two_parse_failures(
    monkeypatch,
):
    called_models = []
    responses = iter(["첫 번째 형식 오류", "두 번째 형식 오류"])

    def fake_call_hcx(model_name, _messages, **_kwargs):
        called_models.append(model_name)
        return next(responses)

    monkeypatch.setattr(expander, "call_hcx", fake_call_hcx)
    monkeypatch.setattr(expander, "extract_hcx_content", lambda response: response)

    candidates = ["경지면적", "작물별 재배면적"]
    assert expander.filter_keywords_with_hcx007(
        "재배면적",
        "재배면적",
        candidates,
    ) == candidates
    assert called_models == ["HCX-007", "HCX-007"]


def test_filter_keywords_with_hcx007_removes_out_of_range_index_after_retry(
    monkeypatch,
):
    responses = iter(["형식 오류", "[0, 7]"])

    monkeypatch.setattr(expander, "call_hcx", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(expander, "extract_hcx_content", lambda response: response)

    assert expander.filter_keywords_with_hcx007(
        "재배면적",
        "재배면적",
        ["경지면적", "작물별 재배면적"],
    ) == ["경지면적"]


def test_expand_and_filter_keyword_runs_hcx005_then_hcx007(monkeypatch):
    called_models = []

    def fake_call_hcx(model_name, _messages, **_kwargs):
        called_models.append(model_name)
        return {"model_name": model_name}

    def fake_extract_content(response):
        if response["model_name"] == "HCX-005":
            return '["경지면적", "작물별 재배면적"]'
        return "[0]"

    monkeypatch.setattr(expander, "call_hcx", fake_call_hcx)
    monkeypatch.setattr(expander, "extract_hcx_content", fake_extract_content)

    assert expander.expand_and_filter_keyword("재배면적", "재배면적") == ["경지면적"]
    assert called_models == ["HCX-005", "HCX-007"]


def test_generate_keywords_with_hcx005_returns_empty_on_failure(monkeypatch):
    def raise_client_error(*_args, **_kwargs):
        raise HCXClientError("request failed")

    monkeypatch.setattr(expander, "call_hcx", raise_client_error)

    assert expander.generate_keywords_with_hcx005("재배면적", "재배면적") == []


def test_filter_keywords_with_hcx007_keeps_generated_candidates_on_failure(monkeypatch):
    def raise_client_error(*_args, **_kwargs):
        raise HCXClientError("request failed")

    monkeypatch.setattr(expander, "call_hcx", raise_client_error)

    assert expander.filter_keywords_with_hcx007(
        "재배면적",
        "재배면적",
        ["경지면적", "작물별 재배면적"],
    ) == ["경지면적", "작물별 재배면적"]
