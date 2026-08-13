# 앞 Task의 Excel 데이터에서 파이프라인 테스트용 metric 입력을 추출합니다.
"""completed_dataset.xlsx에서 유효한 unique metric JSON을 생성한다."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "completed_dataset.xlsx"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "metric_test_inputs.json"
DEFAULT_SHEET_NAME = "수치기반주장_태깅데이터셋"
DEFAULT_LIMIT = 20
REQUIRED_COLUMNS = {"forecast", "metric"}

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)


class MetricDatasetError(RuntimeError):
    """metric 테스트 데이터 추출 중 발생하는 예외."""


def read_xlsx_sheet_rows(input_path: Path, sheet_name: str) -> List[Dict[str, Any]]:
    """외부 패키지 없이 XLSX의 지정 시트를 행 딕셔너리 목록으로 읽는다."""

    path = Path(input_path)
    if not path.is_file():
        raise MetricDatasetError(f"Excel 파일을 찾을 수 없습니다: {path}")

    try:
        with zipfile.ZipFile(path) as workbook_zip:
            shared_strings = _read_shared_strings(workbook_zip)
            worksheet_path = _find_worksheet_path(
                workbook_zip,
                sheet_name,
            )
            row_values = _read_worksheet_values(
                workbook_zip,
                worksheet_path,
                shared_strings,
            )
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError) as error:
        raise MetricDatasetError("Excel 파일 구조를 읽을 수 없습니다.") from error

    if not row_values:
        return []

    headers = [str(value).strip() if value is not None else "" for value in row_values[0]]
    if not headers or all(not header for header in headers):
        raise MetricDatasetError("Excel 헤더 행을 찾을 수 없습니다.")

    rows: List[Dict[str, Any]] = []
    for values in row_values[1:]:
        padded_values = values + [None] * max(0, len(headers) - len(values))
        row = {
            header: padded_values[index]
            for index, header in enumerate(headers)
            if header
        }
        if any(value not in (None, "") for value in row.values()):
            rows.append(row)
    return rows


def extract_metric_test_inputs(
    rows: Sequence[Dict[str, Any]],
    limit: Optional[int] = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """forecast와 metric 조건을 적용해 최초 순서의 unique metric을 만든다."""

    if limit is not None and limit < 0:
        raise MetricDatasetError("limit은 0 이상의 정수여야 합니다.")

    _validate_required_columns(rows)
    forecast_excluded_rows = 0
    empty_metric_excluded_rows = 0
    valid_metrics: List[str] = []

    for row in rows:
        forecast = str(row.get("forecast") or "").strip().upper()
        if forecast == "Y":
            forecast_excluded_rows += 1
            continue

        metric = _normalize_metric(row.get("metric"))
        if not metric:
            empty_metric_excluded_rows += 1
            continue

        valid_metrics.append(metric)

    unique_metrics = list(dict.fromkeys(valid_metrics))
    selected_metrics = unique_metrics if limit is None else unique_metrics[:limit]
    inputs = [
        {"claim_id": f"C{index:03d}", "metric": metric}
        for index, metric in enumerate(selected_metrics, start=1)
    ]
    stats = {
        "total_rows": len(rows),
        "forecast_excluded_rows": forecast_excluded_rows,
        "empty_metric_excluded_rows": empty_metric_excluded_rows,
        "valid_metric_count": len(valid_metrics),
        "unique_metric_count": len(unique_metrics),
        "selected_metric_count": len(selected_metrics),
    }
    return inputs, stats


def build_metric_test_inputs(
    input_path: Path,
    output_path: Path,
    sheet_name: str = DEFAULT_SHEET_NAME,
    limit: Optional[int] = DEFAULT_LIMIT,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Excel을 읽어 metric 테스트 입력 JSON을 저장한다."""

    rows = read_xlsx_sheet_rows(input_path, sheet_name)
    inputs, stats = extract_metric_test_inputs(rows, limit=limit)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(inputs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return inputs, stats


def _validate_required_columns(rows: Sequence[Dict[str, Any]]) -> None:
    """데이터 행에 필수 컬럼이 존재하는지 확인한다."""

    if not rows:
        return
    missing_columns = REQUIRED_COLUMNS - set(rows[0])
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise MetricDatasetError(f"필수 컬럼이 없습니다: {missing}")


def _normalize_metric(metric: Any) -> str:
    """metric을 문자열로 바꾸고 연속 공백을 한 칸으로 정리한다."""

    if metric is None:
        return ""
    return " ".join(str(metric).split())


def _read_shared_strings(workbook_zip: zipfile.ZipFile) -> List[str]:
    """XLSX sharedStrings.xml의 문자열 목록을 읽는다."""

    try:
        root = ET.fromstring(workbook_zip.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(text.text or "" for text in item.findall(f".//{{{MAIN_NS}}}t"))
        for item in root.findall(f"{{{MAIN_NS}}}si")
    ]


def _find_worksheet_path(workbook_zip: zipfile.ZipFile, sheet_name: str) -> str:
    """시트명에 대응하는 worksheet XML 경로를 찾는다."""

    workbook_root = ET.fromstring(workbook_zip.read("xl/workbook.xml"))
    relationship_id = None
    for sheet in workbook_root.findall(f".//{{{MAIN_NS}}}sheet"):
        if sheet.get("name") == sheet_name:
            relationship_id = sheet.get(f"{{{OFFICE_REL_NS}}}id")
            break
    if not relationship_id:
        raise MetricDatasetError(f"Excel 시트를 찾을 수 없습니다: {sheet_name}")

    relationships_root = ET.fromstring(
        workbook_zip.read("xl/_rels/workbook.xml.rels")
    )
    for relationship in relationships_root.findall(f"{{{REL_NS}}}Relationship"):
        if relationship.get("Id") == relationship_id:
            target = relationship.get("Target", "")
            if target.startswith("/"):
                return target.lstrip("/")
            return f"xl/{target}"
    raise MetricDatasetError(f"Excel 시트 경로를 찾을 수 없습니다: {sheet_name}")


def _read_worksheet_values(
    workbook_zip: zipfile.ZipFile,
    worksheet_path: str,
    shared_strings: Sequence[str],
) -> List[List[Any]]:
    """worksheet XML을 행별 값 목록으로 변환한다."""

    root = ET.fromstring(workbook_zip.read(worksheet_path))
    rows: List[List[Any]] = []
    for row in root.findall(f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row"):
        values_by_column: Dict[int, Any] = {}
        for cell in row.findall(f"{{{MAIN_NS}}}c"):
            reference = cell.get("r", "")
            column_index = _column_index(reference)
            values_by_column[column_index] = _read_cell_value(cell, shared_strings)
        if values_by_column:
            max_column = max(values_by_column)
            rows.append(
                [values_by_column.get(index) for index in range(max_column + 1)]
            )
    return rows


def _column_index(cell_reference: str) -> int:
    """A1 셀 참조의 열 문자를 0부터 시작하는 인덱스로 바꾼다."""

    match = re.match(r"([A-Z]+)", cell_reference.upper())
    if not match:
        raise MetricDatasetError(f"잘못된 셀 참조입니다: {cell_reference}")
    index = 0
    for character in match.group(1):
        index = index * 26 + ord(character) - ord("A") + 1
    return index - 1


def _read_cell_value(cell: ET.Element, shared_strings: Sequence[str]) -> Any:
    """셀 타입에 따라 실제 값을 읽는다."""

    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(
            text.text or "" for text in cell.findall(f".//{{{MAIN_NS}}}t")
        )

    value_element = cell.find(f"{{{MAIN_NS}}}v")
    if value_element is None:
        return None
    value = value_element.text or ""
    if cell_type == "s":
        return shared_strings[int(value)]
    if cell_type == "b":
        return value == "1"
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Excel에서 metric 테스트 입력을 추출합니다.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--sheet", default=DEFAULT_SHEET_NAME)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    generated_inputs, summary = build_metric_test_inputs(
        args.input,
        args.output,
        sheet_name=args.sheet,
        limit=args.limit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("metrics:")
    for item in generated_inputs:
        print(f"- {item['claim_id']}: {item['metric']}")
    print(f"saved: {args.output}")
