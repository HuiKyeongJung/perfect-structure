# CLOVA Studio Chat Completions v3 API 공통 클라이언트를 제공합니다.
"""HCX-005와 HCX-007 호출을 위한 공통 클라이언트 모듈."""

import os
import sys
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

import api_usage_logger
# try:
#     from src import api_usage_logger
# except ModuleNotFoundError:
#     import api_usage_logger


API_BASE_URL = "https://clovastudio.stream.ntruss.com/v3/chat-completions"
SUPPORTED_MODELS = {"HCX-005", "HCX-007"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HCXClientError(RuntimeError):
    """HCX 클라이언트 처리 중 발생하는 기본 예외."""


class HCXConfigurationError(HCXClientError):
    """HCX API 키 또는 모델 설정이 올바르지 않을 때 발생한다."""


class HCXRequestError(HCXClientError):
    """HCX API 네트워크 또는 HTTP 요청이 실패했을 때 발생한다."""


class HCXResponseParseError(HCXClientError):
    """HCX API 응답 형식이 예상과 다를 때 발생한다."""


def _load_api_key() -> str:
    """프로젝트 루트의 .env에서 CLOVA Studio API 키를 읽는다."""

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("NCP_CLOVASTUDIO_API_KEY", "").strip()
    if not api_key:
        raise HCXConfigurationError(
            "NCP_CLOVASTUDIO_API_KEY가 설정되어 있지 않습니다."
        )

    return api_key


def _build_request_body(
    model_name: str,
    messages: List[Dict[str, Any]],
    thinking_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """모델별 Chat Completions v3 요청 body를 만든다."""

    if model_name == "HCX-005":
        return {
            "messages": messages,
            "topP": 0.8,
            "topK": 0,
            "maxTokens": 300,
            "temperature": 0.5,
            "repetitionPenalty": 1.1,
        }

    return {
        "messages": messages,
        "thinking": {"effort": thinking_effort or "low"},
        "topP": 0.8,
        "topK": 0,
        "maxCompletionTokens": 1000,
        "temperature": 0.3,
        "repetitionPenalty": 1.1,
    }


def call_hcx(
    model_name: str,
    messages: List[Dict[str, Any]],
    timeout: int = 30,
    thinking_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """지정한 HCX 모델에 Chat Completions v3 요청을 보내고 JSON을 반환한다."""

    if model_name not in SUPPORTED_MODELS:
        raise HCXConfigurationError(
            "지원하지 않는 model_name입니다. HCX-005 또는 HCX-007을 사용하세요."
        )

    api_key = _load_api_key()
    url = f"{API_BASE_URL}/{model_name}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = _build_request_body(model_name, messages, thinking_effort)

    request_started_at = perf_counter()
    try:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as error:
        raise HCXRequestError("HCX API 네트워크 오류가 발생했습니다.") from error

    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise HCXRequestError(
            f"HCX API HTTP 오류가 발생했습니다. status={response.status_code}"
        ) from error

    try:
        response_json = response.json()
    except ValueError as error:
        raise HCXResponseParseError("HCX API 응답을 JSON으로 읽을 수 없습니다.") from error

    latency_ms = round((perf_counter() - request_started_at) * 1000, 3)
    input_tokens, output_tokens, total_tokens = api_usage_logger.extract_hcx_usage(
        response_json
    )
    api_usage_logger.record_api_usage(
        service=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
    )
    return response_json


def extract_hcx_content(response_json: Dict[str, Any]) -> str:
    """정상 HCX 응답에서 모델의 최종 텍스트 content를 반환한다."""

    try:
        content = response_json["result"]["message"]["content"]
    except (KeyError, TypeError) as error:
        raise HCXResponseParseError(
            "HCX API 응답에 result.message.content가 없습니다."
        ) from error

    if not isinstance(content, str):
        raise HCXResponseParseError("HCX API 응답의 content가 문자열이 아닙니다.")

    return content


if __name__ == "__main__":
    selected_model = sys.argv[1] if len(sys.argv) > 1 else "HCX-005"
    sample_messages = [
        {
            "role": "system",
            "content": "당신은 한국어 통계 용어를 이해하는 AI입니다.",
        },
        {
            "role": "user",
            "content": "'재배면적'과 관련된 통계 검색 키워드 5개를 짧게 제시하세요.",
        },
    ]

    try:
        sample_response = call_hcx(selected_model, sample_messages)
        print(extract_hcx_content(sample_response))
    except HCXClientError as error:
        print(f"HCX 호출 오류: {error}")
