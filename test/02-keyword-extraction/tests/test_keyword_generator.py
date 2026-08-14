# 입력 객체의 metric을 검증·정규화해 원키워드로 보존하는 기능을 검증합니다.
import pytest

from src import keyword_generator as keyword_generator_module
from src.keyword_generator import (
    InvalidMetricInputError,
    create_original_candidate,
    extract_initial_keywords,
    extract_kiwi_nouns,
    extract_metric,
    extract_surface_keywords,
    expand_seed_keywords_from_dictionary,
    generate_kosis_keywords,
    is_kosis_eligible,
    filter_meaningful_seed_candidates,
    merge_keyword_candidates,
    needs_compound_fallback,
    normalize_and_deduplicate_candidates,
)


@pytest.fixture(autouse=True)
def mock_hcx_expansion_by_default(monkeypatch):
    """단위 테스트에서 실제 HCX와 Embedding API 호출을 차단한다."""

    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        lambda _original_keyword, _seed_keyword: [],
        raising=False,
    )
    monkeypatch.setattr(
        keyword_generator_module,
        "rank_keywords",
        lambda reference_text, keywords: list(keywords),
        raising=False,
    )


def test_extracts_a_normal_metric():
    assert extract_metric({"metric": "재배면적"}) == "재배면적"


def test_trims_outer_whitespace_from_metric():
    assert extract_metric({"metric": "  재배면적  "}) == "재배면적"


def test_normalizes_repeated_whitespace_in_metric():
    assert extract_metric({"metric": "재배   면적"}) == "재배 면적"


def test_normalizes_four_or_more_consecutive_spaces_in_metric():
    assert extract_metric({"metric": "청년    취업자      수"}) == "청년 취업자 수"


@pytest.mark.parametrize(
    ("metric", "expected_nouns"),
    [
        ("청년 취업자 수", ["청년", "취업자", "수"]),
        ("주택 매매가격", ["주택", "매매", "가격"]),
        ("재배면적", ["재배", "면적"]),
        ("소비자물가지수", ["소비자", "물가", "지수"]),
        ("청년    취업자      수", ["청년", "취업자", "수"]),
        ("", []),
    ],
)
def test_extract_kiwi_nouns_returns_normalized_nouns_in_original_order(
    metric,
    expected_nouns,
):
    assert extract_kiwi_nouns(metric) == expected_nouns


def test_extract_kiwi_nouns_keeps_meaningful_english_abbreviations():
    assert extract_kiwi_nouns("국내총생산(GDP)") == ["국내", "총생산", "GDP"]


def test_extract_surface_keywords_keeps_original_space_delimited_words():
    assert extract_surface_keywords("나랏빚 증가 폭") == ["나랏빚", "증가", "폭"]


def test_extract_kiwi_nouns_keeps_raw_nouns_for_compound_words():
    assert extract_kiwi_nouns("나랏빚 증가 폭") == ["나랏", "빚", "증가", "폭"]


def test_filter_meaningful_seed_candidates_uses_the_given_validator():
    def fake_validator(metric, candidate):
        return metric == "나랏빚 증가 폭" and candidate != "나랏"

    assert filter_meaningful_seed_candidates(
        "나랏빚 증가 폭",
        ["나랏", "빚", "증가", "폭"],
        fake_validator,
    ) == ["빚", "증가", "폭"]


@pytest.mark.parametrize(
    ("seed_keywords", "expected_keywords"),
    [
        (["GDP"], ["국내총생산"]),
        (["국내", "GDP", "총생산"], ["국내총생산"]),
    ],
)
def test_expand_seed_keywords_from_dictionary_returns_related_keywords(
    seed_keywords,
    expected_keywords,
):
    assert expand_seed_keywords_from_dictionary(seed_keywords) == expected_keywords


def test_expand_seed_keywords_from_dictionary_returns_empty_for_unknown_seeds():
    assert expand_seed_keywords_from_dictionary(["반도체", "설비", "투자"]) == []


def test_expand_seed_keywords_from_dictionary_removes_duplicates():
    assert expand_seed_keywords_from_dictionary(["GDP", "GDP"]) == ["국내총생산"]


def test_generate_kosis_keywords_adds_dictionary_expansion_from_gdp_seed():
    result = generate_kosis_keywords(
        {"metric": "국내총생산(GDP)", "kosis_eligible": True},
        semantic_validator=lambda _metric, _candidate: True,
    )

    assert result["seed_expanded_keywords"] == []
    assert "국내총생산" in result["keywords"]


def test_generate_kosis_keywords_keeps_unknown_metric_without_dictionary_expansion():
    result = generate_kosis_keywords(
        {"metric": "반도체 설비 투자", "kosis_eligible": True}
    )

    assert result["status"] == "success"
    assert result["seed_expanded_keywords"] == []
    assert result["seed_keywords"] == ["반도체", "설비", "투자"]


@pytest.mark.parametrize(
    ("metric", "kiwi_nouns", "expected"),
    [
        ("소비자물가지수", ["소비자", "물가", "지수"], False),
        ("재배면적", ["재배", "면적"], False),
        ("주택 매매가격", ["주택", "매매", "가격"], False),
        ("추가경정예산", ["추가경정예산"], True),
        ("추가 경정 예산", ["추가경정예산"], True),
        ("", ["추가경정예산"], False),
        ("추가경정예산", [], False),
    ],
)
def test_needs_compound_fallback_compares_metric_and_kiwi_nouns(
    metric,
    kiwi_nouns,
    expected,
):
    assert needs_compound_fallback(metric, kiwi_nouns) is expected


def test_accepts_the_legacy_metric_key_when_metric_is_missing():
    assert extract_metric({"metric:": "재배면적"}) == "재배면적"


def test_prefers_metric_over_the_legacy_metric_key():
    assert extract_metric({"metric": "재배면적", "metric:": "다른 값"}) == "재배면적"


@pytest.mark.parametrize(
    "input_data",
    [
        {},
        {"metric": ""},
        {"metric": "   "},
        {"metric": 100},
        {"metric": None},
        "재배면적",
        None,
    ],
)
def test_rejects_missing_or_invalid_metric(input_data):
    with pytest.raises(InvalidMetricInputError):
        extract_metric(input_data)


def test_does_not_infer_metric_from_claim_text():
    with pytest.raises(InvalidMetricInputError):
        extract_metric({"claim": "재배면적이 감소했다."})


@pytest.mark.parametrize(
    ("input_data", "expected"),
    [
        ({"kosis_eligible": True}, True),
        ({"kosis_eligible": False}, False),
        ({}, False),
        ({"kosis_eligible": None}, False),
        ({"kosis_eligible": "true"}, False),
        ({"kosis_eligible": 1}, False),
        ("not a dict", False),
    ],
)
def test_checks_kosis_eligibility_strictly(input_data, expected):
    assert is_kosis_eligible(input_data) is expected


def test_creates_an_original_keyword_candidate():
    assert create_original_candidate("재배면적") == {
        "keyword": "재배면적",
        "sources": ["original"],
    }


def test_generates_a_metric_only_keyword_for_an_eligible_input():
    result = generate_kosis_keywords(
        {
            "claim_id": "Ae4300e50-C001",
            "claim": "재배면적이 10만4943㏊로 감소했다.",
            "metric": "재배면적",
            "value": "10만4943",
            "unit": "㏊",
            "period": "2025",
            "kosis_eligible": True,
        }
    )

    assert result == {
        "claim_id": "Ae4300e50-C001",
        "metric": "재배면적",
        "original_keyword": "재배면적",
        "seed_keywords": ["재배면적"],
        "original_expanded_keywords": [],
        "seed_expanded_keywords": [],
        "keywords": ["재배면적"],
        "status": "success",
        "error_message": "",
    }


def test_returns_not_eligible_but_preserves_a_normalized_metric():
    result = generate_kosis_keywords(
        {
            "claim_id": "Ae4300e50-C004",
            "metric:": "  재배   면적  ",
            "period": "2022",
            "kosis_eligible": False,
        }
    )

    assert result == {
        "claim_id": "Ae4300e50-C004",
        "metric": "재배 면적",
        "original_keyword": "재배 면적",
        "seed_keywords": [],
        "original_expanded_keywords": [],
        "seed_expanded_keywords": [],
        "keywords": [],
        "status": "not_eligible",
        "error_message": "",
    }


def test_period_and_value_do_not_change_the_extracted_metric():
    first = extract_metric(
        {"metric": "재배면적", "value": "10만4943", "period": "2025"}
    )
    second = extract_metric(
        {"metric": "재배면적", "value": "1.0", "period": None}
    )

    assert first == second == "재배면적"


def test_extract_initial_keywords_keeps_the_original_metric_first():
    keywords = extract_initial_keywords("  항공사   정비사 수  ")

    assert keywords[0] == "항공사 정비사 수"


def test_extract_initial_keywords_generates_expected_candidates_in_order():
    assert extract_initial_keywords("항공사 정비사 수") == [
        "항공사 정비사 수",
        "항공사 정비사",
        "정비사 수",
    ]


def test_extract_initial_keywords_removes_a_general_measurement_expression():
    assert extract_initial_keywords("청년 인구") == ["청년 인구", "청년"]


def test_extract_initial_keywords_keeps_a_single_token_metric_only():
    assert extract_initial_keywords("실업률") == ["실업률"]


def test_extract_initial_keywords_removes_duplicates_and_empty_candidates():
    keywords = extract_initial_keywords("고용 현황")

    assert keywords == ["고용 현황", "고용"]
    assert "" not in keywords
    assert len(keywords) == len(set(keywords))


@pytest.mark.parametrize(
    "kosis_eligible",
    [False, None, "true", 1],
)
def test_generate_kosis_keywords_returns_no_candidates_when_not_eligible(
    kosis_eligible,
):
    result = generate_kosis_keywords(
        {
            "claim_id": "C002",
            "metric": "항공사 정비사 수",
            "kosis_eligible": kosis_eligible,
        }
    )

    assert result["keywords"] == []
    assert result["status"] == "not_eligible"


def test_generate_kosis_keywords_returns_no_candidates_when_eligibility_is_missing():
    result = generate_kosis_keywords(
        {
            "claim_id": "C003",
            "metric": "항공사 정비사 수",
        }
    )

    assert result["keywords"] == []
    assert result["status"] == "not_eligible"


def test_generate_kosis_keywords_returns_initial_candidates_when_eligible():
    result = generate_kosis_keywords(
        {
            "claim_id": "C001",
            "metric": "항공사 정비사 수",
            "kosis_eligible": True,
        }
    )

    assert result["keywords"] == [
        "항공사 정비사 수",
        "항공사 정비사",
        "정비사 수",
        "항공사",
        "정비사",
        "수",
    ]
    assert result["status"] == "success"


def test_generate_kosis_keywords_does_not_expose_embedding_scores():
    result = generate_kosis_keywords(
        {"claim_id": "C001", "metric": "국가 채무 증가율", "kosis_eligible": True}
    )

    assert "similarity" not in result
    assert "embedding_score" not in result
    assert "ranked_keywords" not in result


def test_generate_kosis_keywords_returns_final_seed_keywords_only():
    result = generate_kosis_keywords(
        {
            "claim_id": "C005",
            "metric": "소비자물가지수",
            "kosis_eligible": True,
        },
        semantic_validator=lambda _metric, _candidate: True,
    )

    assert result["original_keyword"] == "소비자물가지수"
    assert result["seed_keywords"] == ["소비자물가지수", "소비자", "물가", "지수"]
    assert result["keywords"] == ["소비자물가지수", "소비자", "물가", "지수"]
    for field in (
        "original_seed_keywords",
        "surface_seed_keywords",
        "kiwi_raw_keywords",
        "kiwi_seed_keywords",
    ):
        assert field not in result


@pytest.mark.parametrize(
    ("metric", "expected_seed_keywords"),
    [
        ("재배면적", ["재배면적", "재배", "면적"]),
        ("주택 매매가격", ["주택", "매매가격", "매매", "가격"]),
        ("추가경정예산", ["추가경정예산"]),
    ],
)
def test_generate_kosis_keywords_merges_validated_kiwi_keywords_into_final_seeds(
    metric,
    expected_seed_keywords,
):
    result = generate_kosis_keywords(
        {"metric": metric, "kosis_eligible": True},
        semantic_validator=lambda _metric, _candidate: True,
    )

    assert result["seed_keywords"] == expected_seed_keywords


def test_generate_kosis_keywords_keeps_expanded_keyword_lists_empty():
    result = generate_kosis_keywords(
        {"metric": "소비자물가지수", "kosis_eligible": True}
    )

    assert result["original_expanded_keywords"] == []
    assert result["seed_expanded_keywords"] == []


def test_generate_kosis_keywords_includes_validated_english_abbreviation_in_final_seeds():
    result = generate_kosis_keywords(
        {
            "claim_id": "C006",
            "metric": "국내총생산(GDP)",
            "kosis_eligible": True,
        },
        semantic_validator=lambda _metric, _candidate: True,
    )

    assert result["seed_keywords"] == ["국내총생산(GDP)", "국내", "총생산", "GDP"]


def test_merges_keyword_candidate_groups_in_given_order():
    merged = merge_keyword_candidates(
        ["항공사 정비사 수", "항공사 정비사"],
        ["정비사", "항공 정비사"],
        ["항공사 정비사"],
    )

    assert merged == [
        "항공사 정비사 수",
        "항공사 정비사",
        "정비사",
        "항공 정비사",
        "항공사 정비사",
    ]


def test_normalizes_and_deduplicates_keyword_candidates():
    candidates = normalize_and_deduplicate_candidates(
        ["항공사 정비사", "  항공사   정비사 ", "", "정비사", "정비사", 123, "   "]
    )

    assert candidates == ["항공사 정비사", "정비사"]


def test_generate_kosis_keywords_keeps_original_metric_first_after_pipeline():
    result = generate_kosis_keywords(
        {
            "claim_id": "C004",
            "metric": "  항공사   정비사 수  ",
            "kosis_eligible": True,
        }
    )

    assert result["keywords"] == [
        "항공사 정비사 수",
        "항공사 정비사",
        "정비사 수",
        "항공사",
        "정비사",
        "수",
    ]


def test_generate_kosis_keywords_uses_surface_seeds_without_a_validator():
    result = generate_kosis_keywords(
        {
            "metric": "나랏빚 증가 폭",
            "kosis_eligible": True,
        }
    )

    assert result["seed_keywords"] == ["나랏빚", "증가", "폭"]


def test_generate_kosis_keywords_adds_only_validated_kiwi_raw_keywords():
    def fake_validator(metric, candidate):
        return metric == "나랏빚 증가 폭" and candidate != "나랏"

    result = generate_kosis_keywords(
        {"metric": "나랏빚 증가 폭", "kosis_eligible": True},
        semantic_validator=fake_validator,
    )

    assert result["seed_keywords"] == ["나랏빚", "증가", "폭", "빚"]
    assert {"나랏빚", "증가", "폭", "빚"}.issubset(result["keywords"])
    assert "나랏" not in result["keywords"]


@pytest.mark.parametrize(
    ("metric", "expected_kiwi_seed_keywords"),
    [
        ("소비자물가지수", ["소비자물가지수", "소비자", "물가", "지수"]),
        ("재배면적", ["재배면적", "재배", "면적"]),
        ("국내총생산(GDP)", ["국내총생산(GDP)", "국내", "총생산", "GDP"]),
        ("추가경정예산", ["추가경정예산"]),
    ],
)
def test_generate_kosis_keywords_merges_surface_and_validated_kiwi_keywords(
    metric,
    expected_kiwi_seed_keywords,
):
    result = generate_kosis_keywords(
        {"metric": metric, "kosis_eligible": True},
        semantic_validator=lambda _metric, _candidate: True,
    )

    assert result["seed_keywords"] == expected_kiwi_seed_keywords


def test_generate_kosis_keywords_integrates_original_and_seed_hcx_expansions(
    monkeypatch,
):
    expansions = {
        "비경제 인구": ["비경제활동인구"],
        "비경제": ["비경제활동인구", "비경제활동인구 추이"],
        "인구": ["총인구", "생산가능인구"],
    }
    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        lambda _original_keyword, seed_keyword: expansions[seed_keyword],
    )

    result = generate_kosis_keywords(
        {"metric": "비경제 인구", "kosis_eligible": True}
    )

    assert result["original_expanded_keywords"] == ["비경제활동인구"]
    assert result["seed_expanded_keywords"] == [
        "비경제활동인구",
        "비경제활동인구 추이",
        "총인구",
        "생산가능인구",
    ]
    assert result["keywords"] == [
        "비경제 인구",
        "비경제",
        "인구",
        "비경제활동인구",
        "비경제활동인구 추이",
        "총인구",
        "생산가능인구",
    ]


def test_generate_kosis_keywords_deduplicates_hcx_expansions(monkeypatch):
    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        lambda _original_keyword, _seed_keyword: ["비경제활동인구"],
    )

    result = generate_kosis_keywords(
        {"metric": "비경제 인구", "kosis_eligible": True}
    )

    assert result["keywords"].count("비경제활동인구") == 1


def test_generate_kosis_keywords_continues_when_original_hcx_expansion_fails(
    monkeypatch,
):
    def fake_expander(_original_keyword, seed_keyword):
        if seed_keyword == "비경제 인구":
            raise RuntimeError("original expansion failed")
        return [f"{seed_keyword} 확장"]

    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        fake_expander,
    )

    result = generate_kosis_keywords(
        {"metric": "비경제 인구", "kosis_eligible": True}
    )

    assert result["original_expanded_keywords"] == []
    assert result["seed_expanded_keywords"] == ["비경제 확장", "인구 확장"]
    assert result["status"] == "success"


def test_generate_kosis_keywords_skips_only_the_failed_seed_expansion(monkeypatch):
    def fake_expander(_original_keyword, seed_keyword):
        if seed_keyword == "비경제":
            raise RuntimeError("seed expansion failed")
        if seed_keyword == "인구":
            return ["총인구"]
        return ["비경제활동인구"]

    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        fake_expander,
    )

    result = generate_kosis_keywords(
        {"metric": "비경제 인구", "kosis_eligible": True}
    )

    assert result["original_expanded_keywords"] == ["비경제활동인구"]
    assert result["seed_expanded_keywords"] == ["총인구"]
    assert result["status"] == "success"


def test_generate_kosis_keywords_keeps_local_candidates_when_all_hcx_calls_fail(
    monkeypatch,
):
    def fail_hcx(*_args, **_kwargs):
        raise RuntimeError("HCX unavailable")

    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        fail_hcx,
    )

    result = generate_kosis_keywords(
        {"metric": "비경제 인구", "kosis_eligible": True}
    )

    assert result["original_expanded_keywords"] == []
    assert result["seed_expanded_keywords"] == []
    assert result["keywords"] == ["비경제 인구", "비경제", "인구"]
    assert result["status"] == "success"


def test_generate_kosis_keywords_reuses_hcx_result_when_original_equals_seed(
    monkeypatch,
):
    calls = []

    def fake_expander(original_keyword, seed_keyword):
        calls.append((original_keyword, seed_keyword))
        return ["국내총생산"]

    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        fake_expander,
    )

    result = generate_kosis_keywords(
        {"metric": "GDP", "kosis_eligible": True}
    )

    assert calls == [("GDP", "GDP")]
    assert result["original_expanded_keywords"] == ["국내총생산"]
    assert result["seed_expanded_keywords"] == ["국내총생산"]
    assert result["keywords"].count("국내총생산") == 1


def test_generate_kosis_keywords_ranks_only_final_merged_keywords(monkeypatch):
    expansions = {
        "비경제 인구": ["비경제활동인구"],
        "비경제": ["비경제활동인구 추이"],
        "인구": ["총인구"],
    }
    ranking_calls = []

    monkeypatch.setattr(
        keyword_generator_module,
        "expand_and_filter_keyword",
        lambda _original_keyword, seed_keyword: expansions[seed_keyword],
    )

    def fake_rank_keywords(reference_text, keywords):
        ranking_calls.append((reference_text, list(keywords)))
        return list(reversed(keywords))

    monkeypatch.setattr(keyword_generator_module, "rank_keywords", fake_rank_keywords)

    result = generate_kosis_keywords(
        {"claim_id": "C001", "metric": "비경제 인구", "kosis_eligible": True}
    )

    reference_text, merged_keywords = ranking_calls[0]
    assert reference_text == "비경제 인구"
    assert result["keywords"] == list(reversed(merged_keywords))
    assert len(result["keywords"]) == len(merged_keywords)
    assert all(isinstance(keyword, str) for keyword in result["keywords"])
    assert result["original_keyword"] in result["keywords"]
    assert result["seed_keywords"] == ["비경제", "인구"]
    assert result["original_expanded_keywords"] == ["비경제활동인구"]
    assert result["seed_expanded_keywords"] == ["비경제활동인구 추이", "총인구"]
    assert "similarity" not in result
    assert "embedding_score" not in result
    assert "ranked_keywords" not in result


def test_generate_kosis_keywords_falls_back_when_embedding_ranking_fails(
    monkeypatch,
):
    monkeypatch.setattr(
        keyword_generator_module,
        "rank_keywords",
        lambda reference_text, keywords: (_ for _ in ()).throw(
            RuntimeError("embedding unavailable")
        ),
    )

    result = generate_kosis_keywords(
        {"metric": "비경제 인구", "kosis_eligible": True}
    )

    assert result["keywords"] == ["비경제 인구", "비경제", "인구"]
    assert result["status"] == "success"


@pytest.mark.parametrize(
    "invalid_ranked_keywords",
    [
        ["비경제 인구", "비경제"],
        ["비경제 인구", "비경제", "인구", "새 후보"],
    ],
)
def test_generate_kosis_keywords_falls_back_if_ranking_loses_or_adds_candidates(
    monkeypatch,
    invalid_ranked_keywords,
):
    monkeypatch.setattr(
        keyword_generator_module,
        "rank_keywords",
        lambda reference_text, keywords: invalid_ranked_keywords,
    )

    result = generate_kosis_keywords(
        {"metric": "비경제 인구", "kosis_eligible": True}
    )

    assert result["keywords"] == ["비경제 인구", "비경제", "인구"]
    assert result["original_keyword"] in result["keywords"]
