# claim_golden의 metric_normalized 입력 생성 규칙을 검증합니다.
from scripts.build_claim_golden_inputs import extract_claim_golden_inputs


def test_extract_claim_golden_inputs_filters_and_deduplicates():
    rows = [
        {
            "forecast": " Y ",
            "kosis_eligible": True,
            "metric": "예측 지표",
            "metric_normalized": "예측 정규화 지표",
        },
        {
            "forecast": "N",
            "kosis_eligible": False,
            "metric": "비대상 지표",
            "metric_normalized": "비대상 정규화 지표",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "  반도체   수출 ",
            "metric_normalized": "  반도체   수출액 ",
        },
        {
            "forecast": "",
            "kosis_eligible": "true",
            "metric": "반도체 수출",
            "metric_normalized": "반도체 수출액",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "자동차 수출",
            "metric_normalized": "자동차 수출액",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문 지표",
            "metric_normalized": "   ",
        },
    ]

    inputs, stats = extract_claim_golden_inputs(rows, limit=20)

    assert inputs == [
        {
            "claim_id": "C001",
            "source_metric": "반도체 수출",
            "metric": "반도체 수출액",
        },
        {
            "claim_id": "C002",
            "source_metric": "자동차 수출",
            "metric": "자동차 수출액",
        },
    ]
    assert stats == {
        "total_rows": 6,
        "forecast_excluded_rows": 1,
        "ineligible_excluded_rows": 1,
        "empty_metric_normalized_excluded_rows": 1,
        "valid_metric_count": 3,
        "unique_metric_count": 2,
        "selected_metric_count": 2,
    }


def test_extract_claim_golden_inputs_applies_limit_after_deduplication():
    rows = [
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문 A",
            "metric_normalized": "정규화 지표",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문 B",
            "metric_normalized": "정규화 지표",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문 C",
            "metric_normalized": "다른 지표",
        },
    ]

    inputs, stats = extract_claim_golden_inputs(rows, limit=1)

    assert inputs == [
        {
            "claim_id": "C001",
            "source_metric": "원문 A",
            "metric": "정규화 지표",
        }
    ]
    assert stats["unique_metric_count"] == 2
    assert stats["selected_metric_count"] == 1
