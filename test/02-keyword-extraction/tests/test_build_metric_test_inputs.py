# Excel metric 필터·중복 제거·ID 생성 규칙을 검증합니다.
import json

import pytest

from scripts.build_metric_test_inputs import (
    MetricDatasetError,
    build_metric_test_inputs,
    extract_metric_test_inputs,
)


def test_extract_metric_test_inputs_filters_deduplicates_and_limits():
    rows = [
        {"forecast": "Y", "metric": "제외 지표"},
        {"forecast": " y ", "metric": None},
        {"forecast": "N", "metric": "  반도체   수출 "},
        {"forecast": "", "metric": "   "},
        {"forecast": None, "metric": "자동차 수출"},
        {"forecast": "n", "metric": "반도체 수출"},
        {"forecast": "N", "metric": "무역수지"},
    ]

    inputs, stats = extract_metric_test_inputs(rows, limit=2)

    assert inputs == [
        {"claim_id": "C001", "metric": "반도체 수출"},
        {"claim_id": "C002", "metric": "자동차 수출"},
    ]
    assert stats == {
        "total_rows": 7,
        "forecast_excluded_rows": 2,
        "empty_metric_excluded_rows": 1,
        "valid_metric_count": 4,
        "unique_metric_count": 3,
        "selected_metric_count": 2,
    }


def test_extract_metric_test_inputs_preserves_first_unique_order():
    rows = [
        {"forecast": "N", "metric": "지표 B"},
        {"forecast": "N", "metric": "지표 A"},
        {"forecast": "N", "metric": "지표 B"},
    ]

    inputs, _stats = extract_metric_test_inputs(rows, limit=None)

    assert [item["metric"] for item in inputs] == ["지표 B", "지표 A"]


def test_extract_metric_test_inputs_requires_forecast_and_metric_columns():
    with pytest.raises(MetricDatasetError, match="forecast"):
        extract_metric_test_inputs([{"metric": "반도체 수출"}])


def test_build_metric_test_inputs_writes_json(monkeypatch, tmp_path):
    rows = [{"forecast": "N", "metric": "반도체 수출"}]
    monkeypatch.setattr(
        "scripts.build_metric_test_inputs.read_xlsx_sheet_rows",
        lambda _input_path, _sheet_name: rows,
    )
    output_path = tmp_path / "metric_test_inputs.json"

    inputs, stats = build_metric_test_inputs(
        tmp_path / "input.xlsx",
        output_path,
        limit=20,
    )

    assert json.loads(output_path.read_text(encoding="utf-8")) == inputs
    assert stats["selected_metric_count"] == 1
