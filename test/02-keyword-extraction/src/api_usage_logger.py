# 실제 CLOVA Studio API 호출의 토큰·호출 횟수·예상 비용을 Markdown으로 기록합니다.
"""HCX 및 Embedding v2 성공 호출 사용량을 docs 로그에 누적한다."""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
USAGE_LOG_PATH = PROJECT_ROOT / "docs" / "API_USAGE_LOG.md"

# 부가세 별도, 원/1,000토큰. 공식적으로 금액을 확인한 항목만 계산한다.
VERIFIED_PRICES_PER_1K: Dict[str, Dict[str, float]] = {
    "HCX-007": {"input": 1.25, "output": 5.0},
    "Embedding v2": {"input": 0.2, "output": 0.0},
}

_RUN_ID = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
_RUN_EVENTS: List[Dict[str, Any]] = []


def _as_non_negative_int(value: Any) -> Optional[int]:
    """bool을 제외한 0 이상의 정수를 반환한다."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def extract_hcx_usage(response_json: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Chat Completions v3 응답에서 입력·출력·전체 토큰 수를 읽는다."""

    try:
        usage = response_json["result"]["usage"]
    except (KeyError, TypeError):
        return None, None, None

    if not isinstance(usage, dict):
        return None, None, None

    input_tokens = _as_non_negative_int(usage.get("promptTokens"))
    output_tokens = _as_non_negative_int(usage.get("completionTokens"))
    total_tokens = _as_non_negative_int(usage.get("totalTokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def extract_embedding_tokens(response_json: Dict[str, Any]) -> Optional[int]:
    """Embedding v2 응답에서 과금 기준인 입력 토큰 수를 읽는다."""

    try:
        return _as_non_negative_int(response_json["result"]["inputTokens"])
    except (KeyError, TypeError):
        return None


def calculate_estimated_cost(
    service: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
) -> Optional[float]:
    """공식 단가가 확인된 서비스의 예상 비용을 원 단위로 계산한다."""

    prices = VERIFIED_PRICES_PER_1K.get(service)
    if prices is None or input_tokens is None:
        return None
    if prices["output"] and output_tokens is None:
        return None

    return (
        input_tokens * prices["input"]
        + (output_tokens or 0) * prices["output"]
    ) / 1000


def _current_run_id() -> str:
    """현재 Python 프로세스의 로그 실행 식별자를 반환한다."""

    return _RUN_ID


def _format_number(value: Optional[int]) -> str:
    return "확인 불가" if value is None else f"{value:,}"


def _format_cost(value: Optional[float]) -> str:
    return "미산정" if value is None else f"{value:.6f}원"


def _normalize_latency(value: Any) -> Optional[float]:
    """0 이상의 호출 응답 시간을 밀리초 단위 실수로 정리한다."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


def _format_latency(value: Optional[float]) -> str:
    return "확인 불가" if value is None else f"{value:.2f}ms"


def _base_document() -> str:
    """공식 기준과 로그 해석 방법이 포함된 문서 머리말을 만든다."""

    return """# CLOVA Studio API 사용량 로그

실제 수동 테스트에서 성공한 HCX-005·HCX-007·Embedding v2 호출을 기록합니다. API 키, 프롬프트, 응답 본문과 임베딩 벡터는 기록하지 않습니다.

응답 시간은 API 요청 전송 직전부터 JSON 응답을 읽은 직후까지의 경과 시간이며, 네트워크 지연을 포함합니다. 각 호출의 밀리초(ms)와 실행 전체의 평균 응답 시간을 기록합니다.

새 테스트 명령에서 첫 API 호출이 발생하면 이전 실행 로그를 덮어쓰며, 같은 명령 안에서 발생하는 호출은 현재 실행 로그에 계속 합산합니다.

## 과금 기준

| 서비스 | 기준 | 적용 단가(VAT 별도) | 로그 계산 |
|---|---:|---:|---|
| HCX-005 | 입력·출력 각 1,000토큰 | 공식 가격표의 금액이 현재 `-`로 표시됨 | 토큰·호출만 기록, 비용 미산정 |
| HCX-007 | 입력 1,000토큰 / 출력 1,000토큰 | 1.25원 / 5원 | 예상 비용 계산 |
| Embedding v2 | 입력 1,000토큰 | 0.2원 | 예상 비용 계산 |

- HCX 비용: `(입력 토큰 × 입력 단가 + 출력 토큰 × 출력 단가) ÷ 1,000`
- Embedding v2 비용: `입력 토큰 × 0.2원 ÷ 1,000`
- 표시 금액은 VAT 별도 예상치이며 실제 청구액은 NAVER Cloud 콘솔을 기준으로 확인해야 합니다.
- Embedding v2는 요청당 최대 8,192토큰, 벡터 차원은 1,024입니다.

## 호출 한도 참고

| 구분 | HCX-005 | HCX-007 | Embedding v2 |
|---|---:|---:|---:|
| 웹·테스트 API | 60 QPM / 60,000 TPM | 60 QPM / 60,000 TPM | 60 QPM / 40,000 TPM |
| 서비스 앱 | 300 QPM / 180,000 TPM | 180 QPM / 300,000 TPM | 540 QPM / 960,000 TPM |

QPM은 분당 호출 수, TPM은 분당 처리 토큰 수입니다. HCX의 TPM은 입력 토큰과 요청한 최대 출력 토큰을 기준으로 계산되므로, 아래 로그의 실제 응답 토큰 합계와 다를 수 있습니다.

## 공식 근거

- [CLOVA Studio 요금](https://www.ncloud.com/product/aiService/clovaStudio#pricing)
- [CLOVA Studio 전체 요금표](https://www.ncloud.com/charge/price/ko)
- [HCX-007 출시 및 요금 안내](https://www.ncloud-forums.com/topic/537/)
- [CLOVA Studio 이용량 제어 정책](https://guide.ncloud-docs.com/docs/clovastudio-ratelimiting)
- [Embedding v2 사양](https://guide.ncloud-docs.com/docs/ko/clovastudio-explorer03)

## 실행 로그

아직 기록된 실제 API 호출이 없습니다.
"""


def _render_run_block() -> str:
    """현재 실행에서 수집한 호출 내역과 합계를 Markdown으로 만든다."""

    rows = []
    for index, event in enumerate(_RUN_EVENTS, start=1):
        rows.append(
            "| {index} | {service} | {input_tokens} | {output_tokens} | "
            "{total_tokens} | {latency} | {cost} |".format(index=index, **event)
        )

    known_cost = sum(
        event["cost_value"]
        for event in _RUN_EVENTS
        if event["cost_value"] is not None
    )
    has_unknown_cost = any(
        event["cost_value"] is None for event in _RUN_EVENTS
    )
    known_latencies = [
        event["latency_value"]
        for event in _RUN_EVENTS
        if event["latency_value"] is not None
    ]
    average_latency = (
        sum(known_latencies) / len(known_latencies)
        if known_latencies
        else None
    )
    total_input = sum(
        event["input_value"] or 0 for event in _RUN_EVENTS
    )
    total_output = sum(
        event["output_value"] or 0 for event in _RUN_EVENTS
    )
    total_tokens = sum(
        event["total_value"] or 0 for event in _RUN_EVENTS
    )
    cost_summary = f"{known_cost:.6f}원"
    if has_unknown_cost:
        cost_summary += " + 단가 미확인 호출 미산정"

    return "\n".join(
        [
            f"<!-- API-USAGE-RUN:{_current_run_id()}:START -->",
            f"### 실행 {_current_run_id()}",
            "",
            "| # | 서비스 | 입력 토큰 | 출력 토큰 | 전체 토큰 | 응답 시간 | 예상 비용 |",
            "|---:|---|---:|---:|---:|---:|---:|",
            *rows,
            "",
            "| 실행 합계 | 값 |",
            "|---|---:|",
            f"| 호출 합계 | {len(_RUN_EVENTS)}회 |",
            f"| 입력 토큰 합계 | {total_input:,} |",
            f"| 출력 토큰 합계 | {total_output:,} |",
            f"| 전체 토큰 합계 | {total_tokens:,} |",
            f"| 평균 응답 시간 | {_format_latency(average_latency)} |",
            f"| 예상 비용 합계 | {cost_summary} |",
            f"<!-- API-USAGE-RUN:{_current_run_id()}:END -->",
        ]
    )


def _write_current_run() -> None:
    """이전 실행 로그를 현재 프로세스에서 수집한 호출 내역으로 교체한다."""

    USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if USAGE_LOG_PATH.exists():
        content = USAGE_LOG_PATH.read_text(encoding="utf-8")
    else:
        content = _base_document()

    log_heading = "## 실행 로그"
    if log_heading in content:
        document_header = content.split(log_heading, 1)[0].rstrip()
    else:
        document_header = _base_document().split(log_heading, 1)[0].rstrip()

    content = (
        f"{document_header}\n\n"
        f"{log_heading}\n\n"
        f"{_render_run_block()}\n"
    )

    USAGE_LOG_PATH.write_text(content, encoding="utf-8")


def record_api_usage(
    service: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int],
    *,
    latency_ms: Optional[float] = None,
    force: bool = False,
) -> None:
    """성공한 API 호출 한 건을 현재 실행 로그에 누적한다."""

    import os

    if not force and os.getenv("PYTEST_CURRENT_TEST"):
        return

    cost = calculate_estimated_cost(service, input_tokens, output_tokens)
    normalized_latency = _normalize_latency(latency_ms)
    _RUN_EVENTS.append(
        {
            "service": service,
            "input_tokens": _format_number(input_tokens),
            "output_tokens": _format_number(output_tokens),
            "total_tokens": _format_number(total_tokens),
            "latency": _format_latency(normalized_latency),
            "cost": _format_cost(cost),
            "input_value": input_tokens,
            "output_value": output_tokens,
            "total_value": total_tokens,
            "latency_value": normalized_latency,
            "cost_value": cost,
        }
    )
    _write_current_run()
