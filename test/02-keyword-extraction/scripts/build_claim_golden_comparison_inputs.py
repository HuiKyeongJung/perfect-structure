# claim_golden.xlsx에서 원문·정규화 metric 비교 pair를 생성합니다.
"""비교 가능한 metric pair를 원문 metric 기준으로 중복 제거해 저장한다."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_metric_test_inputs import MetricDatasetError, read_xlsx_sheet_rows


DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "claim_golden.xlsx"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "claim_golden_comparison_inputs.json"
DEFAULT_SHEET_NAME = "claims"
DEFAULT_LIMIT = 20
REQUIRED_COLUMNS = {
    "forecast",
    "kosis_eligible",
    "metric",
    "metric_normalized",
}


def extract_comparison_inputs(
    rows: Sequence[Dict[str, Any]],
    limit: Optional[int] = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """원문 metric별 최초 (metric, metric_normalized) pair를 추출한다."""

    if limit is not None and limit < 0:
        raise MetricDatasetError("limit은 0 이상의 정수여야 합니다.")
    if rows:
        missing = REQUIRED_COLUMNS - set(rows[0])
        if missing:
            raise MetricDatasetError(
                f"필수 컬럼이 없습니다: {', '.join(sorted(missing))}"
            )

    forecast_excluded = 0
    ineligible_excluded = 0
    empty_metric_excluded = 0
    empty_metric_normalized_excluded = 0
    valid_pairs: List[Tuple[str, str, str]] = []

    for row in rows:
        if _normalize(row.get("forecast")).upper() == "Y":
            forecast_excluded += 1
            continue

        eligible = _normalize(row.get("kosis_eligible")).upper()
        if eligible not in {"TRUE", "1", "Y"}:
            ineligible_excluded += 1
            continue

        metric = _normalize(row.get("metric"))
        if not metric:
            empty_metric_excluded += 1
            continue

        metric_normalized = _normalize(row.get("metric_normalized"))
        if not metric_normalized:
            empty_metric_normalized_excluded += 1
            continue

        claim = _normalize(row.get("claim"))
        valid_pairs.append((metric, metric_normalized, claim))

    unique_pairs: List[Tuple[str, str, str]] = []
    seen_metrics = set()
    for pair in valid_pairs:
        metric = pair[0]
        if metric in seen_metrics:
            continue
        seen_metrics.add(metric)
        unique_pairs.append(pair)

    selected_pairs = unique_pairs if limit is None else unique_pairs[:limit]
    inputs = []
    for index, (metric, metric_normalized, claim) in enumerate(
        selected_pairs, start=1
    ):
        item = {
            "claim_id": f"C{index:03d}",
            "metric": metric,
            "metric_normalized": metric_normalized,
        }
        if claim:
            item["claim"] = claim
        inputs.append(item)
    stats = {
        "total_rows": len(rows),
        "forecast_excluded_rows": forecast_excluded,
        "ineligible_excluded_rows": ineligible_excluded,
        "empty_metric_excluded_rows": empty_metric_excluded,
        "empty_metric_normalized_excluded_rows": empty_metric_normalized_excluded,
        "valid_pair_count": len(valid_pairs),
        "unique_metric_count": len(unique_pairs),
        "selected_metric_count": len(selected_pairs),
    }
    return inputs, stats


def build_comparison_inputs(
    input_path: Path = DEFAULT_INPUT_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    sheet_name: str = DEFAULT_SHEET_NAME,
    limit: Optional[int] = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Excel에서 비교 pair를 읽어 전용 입력 JSON으로 저장한다."""

    rows = read_xlsx_sheet_rows(input_path, sheet_name)
    inputs, stats = extract_comparison_inputs(rows, limit=limit)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inputs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return inputs, stats


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="claim_golden에서 원문·정규화 metric 비교 pair를 추출합니다."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--sheet", default=DEFAULT_SHEET_NAME)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    generated_inputs, summary = build_comparison_inputs(
        input_path=args.input,
        output_path=args.output,
        sheet_name=args.sheet,
        limit=args.limit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("pairs:")
    for item in generated_inputs:
        print(
            f"- {item['claim_id']}: "
            f"{item['metric']} -> {item['metric_normalized']}"
        )
    print(f"saved: {args.output}")
