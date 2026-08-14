# 프로젝트 단일 실행 진입점이 기존 selector와 generator를 올바르게 연결하는지 검증합니다.
import json
from pathlib import Path

import run


def _success_result(input_data):
    metric = input_data["metric"]
    return {
        "claim_id": input_data["claim_id"],
        "metric": metric,
        "original_keyword": metric,
        "seed_keywords": [metric],
        "original_expanded_keywords": [f"{metric} 원키워드 확장"],
        "seed_expanded_keywords": [f"{metric} Seed 확장"],
        "keywords": [metric, f"{metric} 검색어"],
        "status": "success",
        "error_message": "",
    }


def _input(claim_id="C001", metric="수출", normalized="수출액"):
    return {
        "claim_id": claim_id,
        "claim": f"{metric} 관련 수치 문장",
        "metric": metric,
        "metric_normalized": normalized,
    }


def test_pipeline_selects_metric_and_calls_generator_once_per_claim():
    selector_calls = []
    generator_calls = []

    def fake_selector(metric, metric_normalized, claim=None):
        selector_calls.append((metric, metric_normalized, claim))
        return metric_normalized

    def fake_generator(input_data):
        generator_calls.append(input_data.copy())
        return _success_result(input_data)

    result = run.process_pipeline_item(
        _input(), selector=fake_selector, generator=fake_generator
    )

    assert selector_calls == [("수출", "수출액", "수출 관련 수치 문장")]
    assert generator_calls == [
        {"claim_id": "C001", "metric": "수출액", "kosis_eligible": True}
    ]
    assert result["selected_metric"] == "수출액"


def test_pipeline_passes_selected_original_metric_to_generator_exactly_once():
    generator_calls = []

    def fake_generator(input_data):
        generator_calls.append(input_data.copy())
        return _success_result(input_data)

    result = run.process_pipeline_item(
        _input(metric="무역수지 흑자", normalized="무역수지"),
        selector=lambda *_args, **_kwargs: "무역수지 흑자",
        generator=fake_generator,
    )

    assert len(generator_calls) == 1
    assert generator_calls[0]["metric"] == "무역수지 흑자"
    assert result["result"]["original_keyword"] == "무역수지 흑자"


def test_run_pipeline_returns_one_result_for_each_of_five_inputs():
    items = [_input(f"C{index:03d}", f"지표 {index}", f"정규 지표 {index}") for index in range(1, 6)]
    generator_calls = []

    def fake_generator(input_data):
        generator_calls.append(input_data.copy())
        return _success_result(input_data)

    results = run.run_pipeline(
        items,
        selector=lambda metric, metric_normalized, claim=None: metric_normalized,
        generator=fake_generator,
        show_progress=False,
    )

    assert len(results) == 5
    assert len(generator_calls) == 5


def test_run_pipeline_continues_after_one_generator_failure():
    items = [_input("C001"), _input("C002"), _input("C003")]
    calls = []

    def sometimes_fails(input_data):
        calls.append(input_data["claim_id"])
        if input_data["claim_id"] == "C002":
            raise RuntimeError("generator failure")
        return _success_result(input_data)

    results = run.run_pipeline(
        items,
        selector=lambda metric, metric_normalized, claim=None: metric_normalized,
        generator=sometimes_fails,
        show_progress=False,
    )

    assert calls == ["C001", "C002", "C003"]
    assert [item["result"]["status"] for item in results] == [
        "success",
        "failure",
        "success",
    ]
    assert "generator failure" in results[1]["result"]["error_message"]


def test_result_preserves_input_selected_metric_and_full_generator_output():
    source = _input()
    result = run.process_pipeline_item(
        source,
        selector=lambda *_args, **_kwargs: "수출액",
        generator=_success_result,
    )

    assert set(("claim_id", "claim", "metric", "metric_normalized", "selected_metric", "result")) <= set(result)
    assert result["claim_id"] == source["claim_id"]
    assert result["claim"] == source["claim"]
    assert result["metric"] == source["metric"]
    assert result["metric_normalized"] == source["metric_normalized"]
    assert result["result"] == _success_result(
        {"claim_id": "C001", "metric": "수출액", "kosis_eligible": True}
    )


def test_selector_failure_is_saved_and_does_not_call_generator():
    generator_calls = []

    def failing_selector(*_args, **_kwargs):
        raise RuntimeError("selector failure")

    def fake_generator(input_data):
        generator_calls.append(input_data)
        return _success_result(input_data)

    result = run.process_pipeline_item(
        _input(), selector=failing_selector, generator=fake_generator
    )

    assert generator_calls == []
    assert result["selected_metric"] == ""
    assert result["result"]["status"] == "failure"
    assert "selector failure" in result["result"]["error_message"]


def test_load_pipeline_inputs_applies_limit_and_preserves_order(tmp_path):
    input_path = tmp_path / "inputs.json"
    items = [_input("C001"), _input("C002"), _input("C003")]
    input_path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    loaded = run.load_pipeline_inputs(input_path, limit=2)

    assert [item["claim_id"] for item in loaded] == ["C001", "C002"]


def test_main_supports_custom_input_output_and_limit(monkeypatch, tmp_path):
    input_path = tmp_path / "custom-input.json"
    output_path = tmp_path / "custom-output.json"
    input_path.write_text(
        json.dumps([_input("C001"), _input("C002")], ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        run,
        "select_metric",
        lambda _metric, normalized, claim=None: normalized,
    )
    monkeypatch.setattr(run, "generate_kosis_keywords", _success_result)

    exit_code = run.main(
        [
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--limit",
            "1",
            "--top-k",
            "1",
        ]
    )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert len(saved) == 1
    assert saved[0]["claim_id"] == "C001"


def test_run_entrypoint_does_not_contain_hardcoded_api_key():
    source = Path(run.__file__).read_text(encoding="utf-8")

    assert "NCP_CLOVASTUDIO_API_KEY=" not in source
    assert "KOSIS_API_KEY=" not in source
