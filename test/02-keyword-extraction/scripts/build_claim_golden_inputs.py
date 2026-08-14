# claim_golden.xlsx에서 정규화된 metric 테스트 입력을 추출합니다.
"""원문 metric을 보존하고 metric_normalized를 실제 입력으로 생성한다."""

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
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "claim_golden_metric_inputs.json"
DEFAULT_SHEET_NAME = "claims"
DEFAULT_LIMIT = 20
REQUIRED_COLUMNS = {"forecast", "kosis_eligible", "metric", "metric_normalized"}


def extract_claim_golden_inputs(
    rows: Sequence[Dict[str, Any]],
    limit: Optional[int] = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """KOSIS 대상인 비예측 metric_normalized를 최초 순서대로 중복 제거한다."""

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
    empty_metric_normalized_excluded = 0
    valid_metric_pairs: List[Tuple[str, str]] = []

    for row in rows:
        forecast = _normalize(row.get("forecast")).upper()
        if forecast == "Y":
            forecast_excluded += 1
            continue

        eligible = _normalize(row.get("kosis_eligible")).upper()
        if eligible not in {"TRUE", "1", "Y"}:
            ineligible_excluded += 1
            continue

        normalized_metric = _normalize(row.get("metric_normalized"))
        if not normalized_metric:
            empty_metric_normalized_excluded += 1
            continue
        source_metric = _normalize(row.get("metric"))
        valid_metric_pairs.append((source_metric, normalized_metric))

    unique_metric_pairs: List[Tuple[str, str]] = []
    seen_normalized_metrics = set()
    for source_metric, normalized_metric in valid_metric_pairs:
        if normalized_metric in seen_normalized_metrics:
            continue
        seen_normalized_metrics.add(normalized_metric)
        unique_metric_pairs.append((source_metric, normalized_metric))
    selected_pairs = (
        unique_metric_pairs if limit is None else unique_metric_pairs[:limit]
    )
    inputs = [
        {
            "claim_id": f"C{index:03d}",
            "source_metric": source_metric,
            "metric": normalized_metric,
        }
        for index, (source_metric, normalized_metric) in enumerate(
            selected_pairs, start=1
        )
    ]
    stats = {
        "total_rows": len(rows),
        "forecast_excluded_rows": forecast_excluded,
        "ineligible_excluded_rows": ineligible_excluded,
        "empty_metric_normalized_excluded_rows": empty_metric_normalized_excluded,
        "valid_metric_count": len(valid_metric_pairs),
        "unique_metric_count": len(unique_metric_pairs),
        "selected_metric_count": len(selected_pairs),
    }
    return inputs, stats


def build_claim_golden_inputs(
    input_path: Path = DEFAULT_INPUT_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    sheet_name: str = DEFAULT_SHEET_NAME,
    limit: Optional[int] = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """claim_golden Excel을 읽어 테스트 입력 JSON을 저장한다."""

    rows = read_xlsx_sheet_rows(input_path, sheet_name)
    inputs, stats = extract_claim_golden_inputs(rows, limit=limit)
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
        description="claim_golden에서 metric_normalized 입력을 추출합니다."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--sheet", default=DEFAULT_SHEET_NAME)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    generated_inputs, summary = build_claim_golden_inputs(
        args.input,
        args.output,
        sheet_name=args.sheet,
        limit=args.limit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("metrics:")
    for item in generated_inputs:
        print(
            f"- {item['claim_id']}: "
            f"{item['source_metric']} -> {item['metric']}"
        )
    print(f"saved: {args.output}")
