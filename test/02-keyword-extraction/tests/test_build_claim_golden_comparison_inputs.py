# claim_golden의 원문·정규화 metric 비교 pair 생성 규칙을 검증합니다.
from scripts.build_claim_golden_comparison_inputs import extract_comparison_inputs


def test_extract_comparison_inputs_filters_and_deduplicates_by_source_metric():
    rows = [
        {
            "forecast": " Y ",
            "kosis_eligible": True,
            "metric": "예측 원문",
            "metric_normalized": "예측 정규화",
        },
        {
            "forecast": "N",
            "kosis_eligible": False,
            "metric": "비대상 원문",
            "metric_normalized": "비대상 정규화",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": " 중국   수출 ",
            "metric_normalized": " 대중   수출액 ",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "중국 수출",
            "metric_normalized": "대중 수출액 증가율",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "수출액",
            "metric_normalized": "대중 수출액",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "",
            "metric_normalized": "정규화만 있음",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문만 있음",
            "metric_normalized": "  ",
        },
    ]

    inputs, stats = extract_comparison_inputs(rows, limit=20)

    assert inputs == [
        {
            "claim_id": "C001",
            "metric": "중국 수출",
            "metric_normalized": "대중 수출액",
        },
        {"claim_id": "C002", "metric": "수출액", "metric_normalized": "대중 수출액"},
    ]
    assert stats == {
        "total_rows": 7,
        "forecast_excluded_rows": 1,
        "ineligible_excluded_rows": 1,
        "empty_metric_excluded_rows": 1,
        "empty_metric_normalized_excluded_rows": 1,
        "valid_pair_count": 3,
        "unique_metric_count": 2,
        "selected_metric_count": 2,
    }


def test_extract_comparison_inputs_applies_limit_after_source_metric_deduplication():
    rows = [
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문 A",
            "metric_normalized": "정규화 A의 다른 값",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문 A",
            "metric_normalized": "정규화 A",
        },
        {
            "forecast": "N",
            "kosis_eligible": True,
            "metric": "원문 B",
            "metric_normalized": "정규화 B",
        },
    ]

    inputs, stats = extract_comparison_inputs(rows, limit=1)

    assert inputs == [
        {
            "claim_id": "C001",
            "metric": "원문 A",
            "metric_normalized": "정규화 A의 다른 값",
        }
    ]
    assert stats["unique_metric_count"] == 2
    assert stats["selected_metric_count"] == 1
