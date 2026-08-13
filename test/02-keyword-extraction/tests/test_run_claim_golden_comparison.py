# 원문·정규화 metric이 독립 파이프라인으로 실행되는지 검증합니다.
import json

from scripts.run_claim_golden_comparison import (
    append_comparison_result,
    print_comparison_summary,
    run_comparison_pair,
    save_comparison_results,
)


def _success_result(input_data, suffix=""):
    metric = input_data["metric"]
    return {
        "claim_id": input_data["claim_id"],
        "metric": metric,
        "original_keyword": metric,
        "seed_keywords": [f"{metric} seed"],
        "original_expanded_keywords": [f"{metric} original {i}" for i in range(12)],
        "seed_expanded_keywords": [f"{metric} seed {i}" for i in range(12)],
        "keywords": [f"{metric} keyword {i}{suffix}" for i in range(12)],
        "status": "success",
        "error_message": "",
    }


def test_run_comparison_pair_calls_generator_twice_with_independent_metrics():
    calls = []

    def fake_generator(input_data):
        calls.append(input_data.copy())
        return _success_result(input_data)

    result = run_comparison_pair(
        {
            "claim_id": "C001",
            "metric": "중국 수출",
            "metric_normalized": "대중 수출액",
        },
        generator=fake_generator,
    )

    assert calls == [
        {"claim_id": "C001", "metric": "중국 수출", "kosis_eligible": True},
        {"claim_id": "C001", "metric": "대중 수출액", "kosis_eligible": True},
    ]
    assert result["source"] == {
        "metric": "중국 수출",
        "metric_normalized": "대중 수출액",
    }
    assert result["original_metric_result"]["original_keyword"] == "중국 수출"
    assert result["normalized_metric_result"]["original_keyword"] == "대중 수출액"
    for field in (
        "original_keyword",
        "seed_keywords",
        "original_expanded_keywords",
        "seed_expanded_keywords",
        "keywords",
        "status",
        "error_message",
    ):
        assert field in result["original_metric_result"]
        assert field in result["normalized_metric_result"]


def test_print_comparison_summary_limits_terminal_lists_only(capsys):
    result = {
        "claim_id": "C001",
        "source": {"metric": "중국 수출", "metric_normalized": "대중 수출액"},
        "original_metric_result": _success_result(
            {"claim_id": "C001", "metric": "중국 수출"}
        ),
        "normalized_metric_result": _success_result(
            {"claim_id": "C001", "metric": "대중 수출액"}
        ),
    }

    print_comparison_summary(result, index=1, total=1, top_k=3)
    output = capsys.readouterr().out

    assert "[A] ORIGINAL METRIC" in output
    assert "[B] NORMALIZED METRIC" in output
    assert "중국 수출 keyword 2" in output
    assert "중국 수출 keyword 3" not in output
    assert len(result["original_metric_result"]["keywords"]) == 12


def test_save_comparison_results_keeps_full_keyword_lists(tmp_path):
    result = {
        "claim_id": "C001",
        "source": {"metric": "중국 수출", "metric_normalized": "대중 수출액"},
        "original_metric_result": _success_result(
            {"claim_id": "C001", "metric": "중국 수출"}
        ),
        "normalized_metric_result": _success_result(
            {"claim_id": "C001", "metric": "대중 수출액"}
        ),
    }
    output_path = tmp_path / "comparison.json"

    save_comparison_results([result], output_path)
    saved = json.loads(output_path.read_text(encoding="utf-8"))

    assert len(saved[0]["original_metric_result"]["keywords"]) == 12
    assert len(saved[0]["normalized_metric_result"]["keywords"]) == 12


def test_append_comparison_result_keeps_previous_runs(tmp_path):
    output_path = tmp_path / "comparison.json"
    first = {
        "claim_id": "C001",
        "source": {"metric": "수출", "metric_normalized": "수출액"},
        "original_metric_result": {},
        "normalized_metric_result": {},
    }
    second = {
        "claim_id": "C002",
        "source": {"metric": "중국 수출", "metric_normalized": "대중 수출액"},
        "original_metric_result": {},
        "normalized_metric_result": {},
    }

    append_comparison_result(first, output_path)
    append_comparison_result(second, output_path)

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved == [first, second]


def test_append_comparison_result_replaces_duplicate_pair_with_latest_result(tmp_path):
    output_path = tmp_path / "comparison.json"
    first = {
        "claim_id": "C001",
        "source": {"metric": "수출", "metric_normalized": "수출액"},
        "original_metric_result": {"keywords": ["이전 결과"]},
        "normalized_metric_result": {},
    }
    latest = {
        "claim_id": "C001",
        "source": {"metric": "수출", "metric_normalized": "수출액"},
        "original_metric_result": {"keywords": ["최신 결과"]},
        "normalized_metric_result": {},
    }

    append_comparison_result(first, output_path)
    append_comparison_result(latest, output_path)

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["original_metric_result"]["keywords"] == ["최신 결과"]


def test_run_comparison_pair_preserves_other_result_when_one_pipeline_fails():
    def fake_generator(input_data):
        if input_data["metric"] == "중국 수출":
            raise RuntimeError("original failed")
        return _success_result(input_data)

    result = run_comparison_pair(
        {
            "claim_id": "C001",
            "metric": "중국 수출",
            "metric_normalized": "대중 수출액",
        },
        generator=fake_generator,
    )

    assert result["original_metric_result"]["status"] == "error"
    assert result["normalized_metric_result"]["status"] == "success"
    assert result["normalized_metric_result"]["keywords"]
