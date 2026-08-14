# metric 입력 실행 스크립트가 production 생성기를 재사용하고 결과를 저장하는지 검증합니다.
import json

from scripts.run_metric_dataset_test import (
    DEFAULT_OUTPUT_PATH,
    load_metric_inputs,
    run_metric_pipeline,
    save_pipeline_results,
)


def test_default_output_is_separated_for_alternative_metric_extraction():
    assert DEFAULT_OUTPUT_PATH.name == "metric_pipeline_results_alternative_extraction.json"


def test_load_metric_inputs_applies_limit_and_normalizes_metric(tmp_path):
    input_path = tmp_path / "inputs.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "claim_id": "C001",
                    "source_metric": "  반도체   수출 ",
                    "metric": "  반도체   수출액 ",
                },
                {"claim_id": "C002", "metric": "자동차 수출"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert load_metric_inputs(input_path, limit=1) == [
        {
            "claim_id": "C001",
            "source_metric": "반도체 수출",
            "metric": "반도체 수출액",
        }
    ]


def test_run_metric_pipeline_does_not_pass_source_metric_to_generator():
    calls = []

    def fake_generator(input_data):
        calls.append(input_data)
        return {
            "claim_id": input_data["claim_id"],
            "metric": input_data["metric"],
            "original_keyword": input_data["metric"],
            "seed_keywords": [],
            "original_expanded_keywords": [],
            "seed_expanded_keywords": [],
            "keywords": [input_data["metric"]],
            "status": "success",
            "error_message": "",
        }

    results = run_metric_pipeline(
        [
            {
                "claim_id": "C001",
                "source_metric": "중국 수출",
                "metric": "대중 수출액",
            }
        ],
        generator=fake_generator,
    )

    assert calls == [
        {"claim_id": "C001", "metric": "대중 수출액", "kosis_eligible": True}
    ]
    assert results[0]["original_keyword"] == "대중 수출액"


def test_run_metric_pipeline_enables_full_pipeline_without_copying_logic():
    calls = []

    def fake_generator(input_data):
        calls.append(input_data)
        return {
            "claim_id": input_data["claim_id"],
            "metric": input_data["metric"],
            "original_keyword": input_data["metric"],
            "seed_keywords": [],
            "original_expanded_keywords": [],
            "seed_expanded_keywords": [],
            "keywords": [input_data["metric"]],
            "status": "success",
            "error_message": "",
        }

    results = run_metric_pipeline(
        [{"claim_id": "C001", "metric": "반도체 수출"}],
        generator=fake_generator,
    )

    assert calls == [
        {"claim_id": "C001", "metric": "반도체 수출", "kosis_eligible": True}
    ]
    assert results[0]["status"] == "success"
    assert "similarity" not in results[0]


def test_run_metric_pipeline_keeps_processing_after_one_error():
    def fake_generator(input_data):
        if input_data["claim_id"] == "C001":
            raise RuntimeError("API failure")
        return {
            "claim_id": input_data["claim_id"],
            "metric": input_data["metric"],
            "original_keyword": input_data["metric"],
            "seed_keywords": [],
            "original_expanded_keywords": [],
            "seed_expanded_keywords": [],
            "keywords": [input_data["metric"]],
            "status": "success",
            "error_message": "",
        }

    results = run_metric_pipeline(
        [
            {"claim_id": "C001", "metric": "실패 지표"},
            {"claim_id": "C002", "metric": "정상 지표"},
        ],
        generator=fake_generator,
    )

    assert [result["status"] for result in results] == ["error", "success"]


def test_save_pipeline_results_writes_json(tmp_path):
    output_path = tmp_path / "results.json"
    results = [{"claim_id": "C001", "keywords": ["반도체 수출"]}]

    save_pipeline_results(results, output_path)

    assert json.loads(output_path.read_text(encoding="utf-8")) == results
