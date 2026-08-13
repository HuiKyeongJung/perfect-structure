# 선택된 metric 하나만 production 키워드 파이프라인으로 실행합니다.
"""원문·정규화 metric 선택 결과와 전체 키워드 결과를 저장·요약한다."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.keyword_generator import generate_kosis_keywords
from src.metric_selector import select_metric


DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "claim_golden_comparison_inputs.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "selected_metric_pipeline_results.json"
DEFAULT_LIMIT = 1
DEFAULT_TOP_K = 10


class SelectedMetricTestError(RuntimeError):
    """선택 metric 테스트 입력 또는 실행이 올바르지 않을 때 발생한다."""


def load_selected_metric_inputs(
    input_path: Path,
    limit: Optional[int] = DEFAULT_LIMIT,
) -> List[Dict[str, str]]:
    """원문·정규화 metric 입력과 선택적 claim 문맥을 읽는다."""

    if limit is not None and limit < 0:
        raise SelectedMetricTestError("limit은 0 이상의 정수여야 합니다.")
    try:
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SelectedMetricTestError("선택 metric 입력 JSON을 읽을 수 없습니다.") from error
    if not isinstance(data, list):
        raise SelectedMetricTestError("선택 metric 입력은 JSON 배열이어야 합니다.")

    inputs: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise SelectedMetricTestError("각 선택 metric 입력은 JSON 객체여야 합니다.")
        normalized_item = {
            "claim_id": _required_text(item.get("claim_id"), "claim_id"),
            "metric": _optional_text(item.get("metric")),
            "metric_normalized": _optional_text(item.get("metric_normalized")),
        }
        claim = _optional_text(item.get("claim"))
        if claim:
            normalized_item["claim"] = claim
        if not normalized_item["metric"] and not normalized_item["metric_normalized"]:
            raise SelectedMetricTestError("선택할 metric이 없습니다.")
        inputs.append(normalized_item)
    return inputs if limit is None else inputs[:limit]


def run_selected_metric(
    item: Dict[str, str],
    selector: Callable[..., str] = select_metric,
    generator: Callable[[Dict[str, Any]], Dict[str, Any]] = generate_kosis_keywords,
) -> Dict[str, Any]:
    """입력과 선택값을 보존하고 선택된 metric만 generator에 전달한다."""

    claim_id = item["claim_id"]
    metric = item.get("metric", "")
    metric_normalized = item.get("metric_normalized", "")
    claim = item.get("claim", "")

    # 입력 네 필드는 generator 반환값과 섞지 않고 최상위에 그대로 보존한다.
    wrapper: Dict[str, Any] = {
        "claim_id": claim_id,
        "claim": claim,
        "metric": metric,
        "metric_normalized": metric_normalized,
    }

    try:
        selected_metric = selector(metric, metric_normalized, claim=claim)
    except Exception as error:
        # 예상 밖 selector 실패에서도 확인 가능한 후보 하나와 입력 정보는 남긴다.
        selected_metric = metric_normalized or metric
        wrapper["selected_metric"] = selected_metric
        wrapper["result"] = _error_result(claim_id, selected_metric, error)
        return wrapper

    wrapper["selected_metric"] = selected_metric
    try:
        # Production generator의 기존 schema는 result 객체 안에 그대로 보존한다.
        wrapper["result"] = generator(
            {
                "claim_id": claim_id,
                "metric": selected_metric,
                "kosis_eligible": True,
            }
        )
    except Exception as error:
        wrapper["result"] = _error_result(claim_id, selected_metric, error)
    return wrapper


def _error_result(
    claim_id: str,
    selected_metric: str,
    error: Exception,
) -> Dict[str, Any]:
    """실패해도 keyword_generator와 같은 필드 구조로 오류를 기록한다."""

    return {
        "claim_id": claim_id,
        "metric": selected_metric,
        "original_keyword": selected_metric,
        "seed_keywords": [],
        "original_expanded_keywords": [],
        "seed_expanded_keywords": [],
        "keywords": [],
        "status": "error",
        "error_message": str(error),
    }


def save_selected_metric_results(
    results: List[Dict[str, Any]],
    output_path: Path,
) -> None:
    """선택 정보와 generator 상세 결과를 JSON으로 저장한다."""

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def print_selected_metric_summary(
    wrapper: Dict[str, Any],
    index: int,
    total: int,
    top_k: int = DEFAULT_TOP_K,
) -> None:
    """선택 정보와 선택된 metric의 키워드 결과만 요약 출력한다."""

    if top_k < 0:
        raise SelectedMetricTestError("top-k는 0 이상의 정수여야 합니다.")
    line = "=" * 60
    subline = "-" * 60
    result = wrapper["result"]
    print(line)
    print(f"[{index}/{total}] {wrapper['claim_id']}")
    print(line)
    if wrapper.get("claim"):
        print(f"\nclaim:\n{wrapper['claim']}")
    print(f"\n원문 metric   : {wrapper['metric']}")
    print(f"정규화 metric : {wrapper['metric_normalized']}")
    print(f"선택 metric   : {wrapper['selected_metric']}")
    print(f"\n{subline}\n[SELECTED METRIC PIPELINE]\n{subline}")
    print(f"\n원키워드:\n{result.get('original_keyword', '')}")
    _print_list("Seed", result.get("seed_keywords", []), top_k)
    _print_list("원키워드 확장", result.get("original_expanded_keywords", []), top_k)
    _print_list("Seed 확장", result.get("seed_expanded_keywords", []), top_k)
    _print_list("최종 keywords TOP", result.get("keywords", []), top_k, numbered=True)
    print(f"\nstatus: {result.get('status', '')}")


def _print_list(title: str, values: Any, top_k: int, numbered: bool = False) -> None:
    print(f"\n{title} {top_k if numbered else ''}:".rstrip())
    display_values = values[:top_k] if isinstance(values, list) else []
    for index, value in enumerate(display_values, start=1):
        prefix = f"{index}." if numbered else "-"
        print(f"{prefix} {value}")


def _optional_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _required_text(value: Any, field_name: str) -> str:
    normalized = _optional_text(value)
    if not normalized:
        raise SelectedMetricTestError(
            f"{field_name}은 비어 있지 않은 문자열이어야 합니다."
        )
    return normalized


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="더 적합한 metric 하나를 선택해 전체 키워드 파이프라인을 실행합니다."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    inputs = load_selected_metric_inputs(args.input, limit=args.limit)
    results: List[Dict[str, Any]] = []
    total = len(inputs)
    for index, item in enumerate(inputs, start=1):
        print(f"[{index}/{total}] {item['claim_id']} metric 선택 및 처리 중...")
        wrapper = run_selected_metric(item)
        results.append(wrapper)
        save_selected_metric_results(results, args.output)
        print_selected_metric_summary(wrapper, index, total, top_k=args.top_k)
    print(f"\n전체 상세 결과: {args.output}")
