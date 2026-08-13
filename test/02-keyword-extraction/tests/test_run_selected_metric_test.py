# 선택된 metric 하나만 기존 generator에 전달되는지 검증합니다.
import json

from scripts.run_selected_metric_test import (
    run_selected_metric,
    save_selected_metric_results,
)


def _generator_result(input_data):
    metric = input_data["metric"]
    return {
        "claim_id": input_data["claim_id"],
        "metric": metric,
        "original_keyword": metric,
        "seed_keywords": [metric],
        "original_expanded_keywords": [f"{metric} 확장"],
        "seed_expanded_keywords": [f"{metric} seed 확장"],
        "keywords": [metric, f"{metric} 검색"],
        "status": "success",
        "error_message": "",
    }


def test_selected_metric_path_calls_generator_once_with_normalized_metric():
    selector_calls = []
    generator_calls = []

    def fake_selector(metric, metric_normalized, claim=None):
        selector_calls.append((metric, metric_normalized, claim))
        return metric_normalized

    def fake_generator(input_data):
        generator_calls.append(input_data.copy())
        return _generator_result(input_data)

    result = run_selected_metric(
        {
            "claim_id": "C001",
            "claim": "수출 관련 claim",
            "metric": "수출",
            "metric_normalized": "수출액",
        },
        selector=fake_selector,
        generator=fake_generator,
    )

    assert selector_calls == [("수출", "수출액", "수출 관련 claim")]
    assert generator_calls == [
        {"claim_id": "C001", "metric": "수출액", "kosis_eligible": True}
    ]
    assert result["claim_id"] == "C001"
    assert result["claim"] == "수출 관련 claim"
    assert result["metric"] == "수출"
    assert result["metric_normalized"] == "수출액"
    assert result["selected_metric"] == "수출액"
    assert result["result"]["original_keyword"] == "수출액"


def test_selected_metric_path_can_select_original_and_calls_generator_once():
    generator_calls = []

    def fake_generator(input_data):
        generator_calls.append(input_data.copy())
        return _generator_result(input_data)

    result = run_selected_metric(
        {
            "claim_id": "C002",
            "claim": "무역수지 흑자가 중요한 claim",
            "metric": "무역수지 흑자",
            "metric_normalized": "무역수지",
        },
        selector=lambda *_args, **_kwargs: "무역수지 흑자",
        generator=fake_generator,
    )

    assert len(generator_calls) == 1
    assert generator_calls[0]["metric"] == "무역수지 흑자"
    assert result["result"]["metric"] == "무역수지 흑자"


def test_selected_metric_result_wrapper_and_save_preserve_full_generator_result(tmp_path):
    result = run_selected_metric(
        {
            "claim_id": "C001",
            "claim": "수출이 감소했다.",
            "metric": "수출",
            "metric_normalized": "수출액",
        },
        selector=lambda *_args, **_kwargs: "수출액",
        generator=_generator_result,
    )
    output_path = tmp_path / "selected.json"

    save_selected_metric_results([result], output_path)
    saved = json.loads(output_path.read_text(encoding="utf-8"))

    assert saved[0]["claim_id"] == "C001"
    assert saved[0]["claim"] == "수출이 감소했다."
    assert saved[0]["metric"] == "수출"
    assert saved[0]["metric_normalized"] == "수출액"
    assert saved[0]["selected_metric"] == "수출액"
    assert saved[0]["result"]["keywords"] == ["수출액", "수출액 검색"]


def test_selector_failure_keeps_all_input_fields_in_error_result():
    def failing_selector(*_args, **_kwargs):
        raise RuntimeError("selector failure")

    result = run_selected_metric(
        {
            "claim_id": "C003",
            "claim": "미국 수출이 감소했다.",
            "metric": "미국 수출",
            "metric_normalized": "대미 수출액",
        },
        selector=failing_selector,
        generator=_generator_result,
    )

    assert result["claim_id"] == "C003"
    assert result["claim"] == "미국 수출이 감소했다."
    assert result["metric"] == "미국 수출"
    assert result["metric_normalized"] == "대미 수출액"
    assert result["selected_metric"] == "대미 수출액"
    assert result["result"]["status"] == "error"
    assert "selector failure" in result["result"]["error_message"]


def test_generator_failure_keeps_input_and_selected_metric_in_error_result():
    def failing_generator(_input_data):
        raise RuntimeError("generator failure")

    result = run_selected_metric(
        {
            "claim_id": "C004",
            "claim": "중국 수출이 감소했다.",
            "metric": "중국 수출",
            "metric_normalized": "대중 수출액",
        },
        selector=lambda *_args, **_kwargs: "대중 수출액",
        generator=failing_generator,
    )

    assert result["claim"] == "중국 수출이 감소했다."
    assert result["metric"] == "중국 수출"
    assert result["metric_normalized"] == "대중 수출액"
    assert result["selected_metric"] == "대중 수출액"
    assert result["result"]["status"] == "error"
    assert "generator failure" in result["result"]["error_message"]
