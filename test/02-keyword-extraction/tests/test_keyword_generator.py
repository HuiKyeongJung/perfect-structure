# 입력 객체의 metric을 검증·정규화해 원키워드로 보존하는 기능을 검증합니다.
import pytest

from src.keyword_generator import (
    InvalidMetricInputError,
    create_original_candidate,
    extract_metric,
    generate_kosis_keywords,
    is_kosis_eligible,
)


def test_extracts_a_normal_metric():
    assert extract_metric({"metric": "재배면적"}) == "재배면적"


def test_trims_outer_whitespace_from_metric():
    assert extract_metric({"metric": "  재배면적  "}) == "재배면적"


def test_normalizes_repeated_whitespace_in_metric():
    assert extract_metric({"metric": "재배   면적"}) == "재배 면적"


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
