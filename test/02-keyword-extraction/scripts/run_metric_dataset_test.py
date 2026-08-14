# 추출된 metric 입력으로 기존 키워드 생성 전체 파이프라인을 실행합니다.
"""metric 테스트 JSON을 HCX·Embedding 파이프라인에 넣고 결과를 저장한다."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.keyword_generator import generate_kosis_keywords


DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "metric_test_inputs.json"
DEFAULT_OUTPUT_PATH = (
    PROJECT_ROOT / "data" / "metric_pipeline_results_alternative_extraction.json"
)
DEFAULT_LIMIT = 20


class MetricPipelineTestError(RuntimeError):
    """metric 데이터셋 파이프라인 실행 중 발생하는 예외."""


def load_metric_inputs(input_path: Path, limit: Optional[int] = DEFAULT_LIMIT) -> List[Dict[str, str]]:
    """JSON에서 claim_id, metric과 선택적 source_metric을 검증해 읽는다."""

    if limit is not None and limit < 0:
        raise MetricPipelineTestError("limit은 0 이상의 정수여야 합니다.")
    try:
        input_data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MetricPipelineTestError("metric 테스트 입력 JSON을 읽을 수 없습니다.") from error
    if not isinstance(input_data, list):
        raise MetricPipelineTestError("metric 테스트 입력은 JSON 배열이어야 합니다.")

    normalized_inputs: List[Dict[str, str]] = []
    for item in input_data:
        if not isinstance(item, dict):
            raise MetricPipelineTestError("각 metric 테스트 입력은 JSON 객체여야 합니다.")
        claim_id = item.get("claim_id")
        metric = item.get("metric")
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise MetricPipelineTestError("claim_id는 비어 있지 않은 문자열이어야 합니다.")
        if not isinstance(metric, str) or not metric.strip():
            raise MetricPipelineTestError("metric은 비어 있지 않은 문자열이어야 합니다.")
        normalized_item = {
            "claim_id": claim_id.strip(),
            "metric": " ".join(metric.split()),
        }
        source_metric = item.get("source_metric")
        if source_metric is not None:
            if not isinstance(source_metric, str):
                raise MetricPipelineTestError("source_metric은 문자열이어야 합니다.")
            normalized_item["source_metric"] = " ".join(source_metric.split())
        normalized_inputs.append(normalized_item)

    return normalized_inputs if limit is None else normalized_inputs[:limit]


def run_metric_pipeline(
    metric_inputs: List[Dict[str, str]],
    generator: Callable[[Dict[str, Any]], Dict[str, Any]] = generate_kosis_keywords,
) -> List[Dict[str, Any]]:
    """기존 생성기를 호출하고 개별 실패를 기존 schema의 error 결과로 보존한다."""

    results: List[Dict[str, Any]] = []
    for item in metric_inputs:
        try:
            generator_input = {
                "claim_id": item["claim_id"],
                "metric": item["metric"],
                "kosis_eligible": True,
            }
            result = generator(generator_input)
        except Exception as error:
            result = {
                "claim_id": item["claim_id"],
                "metric": item["metric"],
                "original_keyword": item["metric"],
                "seed_keywords": [],
                "original_expanded_keywords": [],
                "seed_expanded_keywords": [],
                "keywords": [],
                "status": "error",
                "error_message": str(error),
            }
        results.append(result)
    return results


def save_pipeline_results(results: List[Dict[str, Any]], output_path: Path) -> None:
    """기존 출력 schema의 결과 목록을 JSON으로 저장한다."""

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="metric 데이터셋으로 전체 파이프라인을 실행합니다.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="결과 JSON 경로. 기본값은 alternative extraction 전용 파일입니다.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    inputs = load_metric_inputs(args.input, limit=args.limit)
    pipeline_results = []
    total = len(inputs)
    for index, item in enumerate(inputs, start=1):
        print(f"[{index}/{total}]")
        print(f"source_metric: {item.get('source_metric', '')}")
        print(f"metric_normalized: {item['metric']}")
        print(f"pipeline input metric: {item['metric']}")
        print("\n처리 중...\n")

        result = run_metric_pipeline([item])[0]
        pipeline_results.append(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if index < total:
            print("=" * 50)

    save_pipeline_results(pipeline_results, args.output)
    print(f"saved: {args.output}")
