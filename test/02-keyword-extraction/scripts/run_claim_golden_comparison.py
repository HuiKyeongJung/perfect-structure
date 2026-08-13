# 원문 metric과 정규화 metric을 독립 실행해 결과를 비교합니다.
"""동일 claim의 두 metric을 production generator에 각각 통과시킨다."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.keyword_generator import generate_kosis_keywords


DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "claim_golden_comparison_inputs.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "claim_golden_comparison_results.json"
DEFAULT_LIMIT = 1
DEFAULT_TOP_K = 10


class ClaimGoldenComparisonError(RuntimeError):
    """비교 입력 또는 실행 준비가 올바르지 않을 때 발생하는 예외."""


def load_comparison_inputs(
    input_path: Path,
    limit: Optional[int] = DEFAULT_LIMIT,
) -> List[Dict[str, str]]:
    """비교 입력 JSON을 검증·정규화하고 요청한 개수만 반환한다."""

    if limit is not None and limit < 0:
        raise ClaimGoldenComparisonError("limit은 0 이상의 정수여야 합니다.")
    try:
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ClaimGoldenComparisonError("비교 입력 JSON을 읽을 수 없습니다.") from error
    if not isinstance(data, list):
        raise ClaimGoldenComparisonError("비교 입력은 JSON 배열이어야 합니다.")

    inputs: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ClaimGoldenComparisonError("각 비교 입력은 JSON 객체여야 합니다.")
        claim_id = _required_text(item.get("claim_id"), "claim_id")
        metric = _required_text(item.get("metric"), "metric")
        metric_normalized = _required_text(
            item.get("metric_normalized"), "metric_normalized"
        )
        inputs.append(
            {
                "claim_id": claim_id,
                "metric": metric,
                "metric_normalized": metric_normalized,
            }
        )
    return inputs if limit is None else inputs[:limit]


def run_comparison_pair(
    item: Dict[str, str],
    generator: Callable[[Dict[str, Any]], Dict[str, Any]] = generate_kosis_keywords,
) -> Dict[str, Any]:
    """원문과 정규화 metric을 서로 영향을 주지 않도록 각각 실행한다."""

    claim_id = item["claim_id"]
    metric = item["metric"]
    metric_normalized = item["metric_normalized"]
    original_result = _run_one_metric(claim_id, metric, generator)
    normalized_result = _run_one_metric(claim_id, metric_normalized, generator)
    return {
        "claim_id": claim_id,
        "source": {
            "metric": metric,
            "metric_normalized": metric_normalized,
        },
        "original_metric_result": original_result,
        "normalized_metric_result": normalized_result,
    }


def save_comparison_results(results: List[Dict[str, Any]], output_path: Path) -> None:
    """두 파이프라인의 전체 상세 결과를 자르지 않고 JSON으로 저장한다."""

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_comparison_result(
    result: Dict[str, Any],
    output_path: Path,
) -> List[Dict[str, Any]]:
    """기존 실행 결과를 유지한 채 새 비교 결과를 배열 뒤에 추가한다."""

    output = Path(output_path)
    if output.exists():
        try:
            saved_results = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ClaimGoldenComparisonError(
                "기존 비교 결과 JSON을 읽을 수 없습니다."
            ) from error
        if not isinstance(saved_results, list):
            raise ClaimGoldenComparisonError("기존 비교 결과는 JSON 배열이어야 합니다.")
    else:
        saved_results = []

    result_pair = _comparison_pair_key(result)
    replaced = False
    if result_pair is not None:
        for index, saved_result in enumerate(saved_results):
            if _comparison_pair_key(saved_result) == result_pair:
                saved_results[index] = result
                replaced = True
                break
    if not replaced:
        saved_results.append(result)
    save_comparison_results(saved_results, output)
    return saved_results


def _comparison_pair_key(result: Any) -> Optional[tuple]:
    """비교 결과의 원문·정규화 metric pair를 중복 식별 키로 반환한다."""

    if not isinstance(result, dict):
        return None
    source = result.get("source")
    if not isinstance(source, dict):
        return None
    metric = source.get("metric")
    metric_normalized = source.get("metric_normalized")
    if not isinstance(metric, str) or not isinstance(metric_normalized, str):
        return None
    return (" ".join(metric.split()), " ".join(metric_normalized.split()))


def print_comparison_summary(
    result: Dict[str, Any],
    index: int,
    total: int,
    top_k: int = DEFAULT_TOP_K,
) -> None:
    """전체 저장 결과는 유지하면서 터미널에는 각 목록의 일부만 출력한다."""

    if top_k < 0:
        raise ClaimGoldenComparisonError("top-k는 0 이상의 정수여야 합니다.")
    line = "=" * 60
    subline = "-" * 60
    source = result["source"]
    print(line)
    print(f"[{index}/{total}] {result['claim_id']}")
    print(line)
    print("\n[INPUT]")
    print(f"원문 metric      : {source['metric']}")
    print(f"정규화 metric    : {source['metric_normalized']}")
    _print_result_block(
        "[A] ORIGINAL METRIC",
        result["original_metric_result"],
        top_k,
        subline,
    )
    _print_result_block(
        "[B] NORMALIZED METRIC",
        result["normalized_metric_result"],
        top_k,
        subline,
    )
    print(f"\n{subline}")
    print("[SUMMARY]")
    print(subline)
    print(f"original status   : {result['original_metric_result']['status']}")
    print(f"normalized status : {result['normalized_metric_result']['status']}")


def _run_one_metric(
    claim_id: str,
    metric: str,
    generator: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    try:
        return generator(
            {
                "claim_id": claim_id,
                "metric": metric,
                "kosis_eligible": True,
            }
        )
    except Exception as error:
        return {
            "claim_id": claim_id,
            "metric": metric,
            "original_keyword": metric,
            "seed_keywords": [],
            "original_expanded_keywords": [],
            "seed_expanded_keywords": [],
            "keywords": [],
            "status": "error",
            "error_message": str(error),
        }


def _print_result_block(
    title: str,
    result: Dict[str, Any],
    top_k: int,
    subline: str,
) -> None:
    print(f"\n{subline}")
    print(title)
    print(subline)
    print("\n원키워드:")
    print(result.get("original_keyword", ""))
    _print_list("Seed", result.get("seed_keywords", []), top_k)
    _print_list(
        "원키워드 확장",
        result.get("original_expanded_keywords", []),
        top_k,
    )
    _print_list(
        "Seed 확장",
        result.get("seed_expanded_keywords", []),
        top_k,
    )
    _print_list("최종 keywords TOP", result.get("keywords", []), top_k, numbered=True)


def _print_list(title: str, values: Any, top_k: int, numbered: bool = False) -> None:
    print(f"\n{title} {top_k if numbered else ''}:".rstrip())
    display_values = values[:top_k] if isinstance(values, list) else []
    for index, value in enumerate(display_values, start=1):
        prefix = f"{index}." if numbered else "-"
        print(f"{prefix} {value}")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClaimGoldenComparisonError(
            f"{field_name}은 비어 있지 않은 문자열이어야 합니다."
        )
    return " ".join(value.split())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="claim_golden의 원문·정규화 metric 결과를 비교합니다."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    comparison_inputs = load_comparison_inputs(args.input, limit=args.limit)
    total = len(comparison_inputs)

    for index, item in enumerate(comparison_inputs, start=1):
        print(f"[{index}/{total}] {item['claim_id']} 시작")
        print("  → ORIGINAL metric 처리 중...")
        original_result = _run_one_metric(
            item["claim_id"], item["metric"], generate_kosis_keywords
        )
        print("  → ORIGINAL 완료")
        print("  → NORMALIZED metric 처리 중...")
        normalized_result = _run_one_metric(
            item["claim_id"], item["metric_normalized"], generate_kosis_keywords
        )
        print("  → NORMALIZED 완료")

        comparison_result = {
            "claim_id": item["claim_id"],
            "source": {
                "metric": item["metric"],
                "metric_normalized": item["metric_normalized"],
            },
            "original_metric_result": original_result,
            "normalized_metric_result": normalized_result,
        }
        append_comparison_result(comparison_result, args.output)
        print(f"[{index}/{total}] 저장 완료\n")
        print_comparison_summary(comparison_result, index, total, top_k=args.top_k)

    print(f"\n전체 상세 결과: {args.output}")
