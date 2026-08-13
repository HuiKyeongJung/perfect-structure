# CLOVA Studio Embedding v2 API의 독립 클라이언트를 제공합니다.
"""텍스트를 CLOVA Studio Embedding v2 벡터로 변환한다."""

import os
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

import requests
from dotenv import load_dotenv


EMBEDDING_V2_URL = (
    "https://clovastudio.stream.ntruss.com/v1/api-tools/embedding/v2"
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = 30


class EmbeddingClientError(RuntimeError):
    """Embedding 클라이언트 처리 중 발생하는 기본 예외."""


class EmbeddingConfigurationError(EmbeddingClientError):
    """Embedding API 설정이 올바르지 않을 때 발생한다."""


class EmbeddingInputError(EmbeddingClientError):
    """Embedding 입력 텍스트가 올바르지 않을 때 발생한다."""


class EmbeddingRequestError(EmbeddingClientError):
    """Embedding API 네트워크 또는 HTTP 요청이 실패했을 때 발생한다."""


class EmbeddingResponseParseError(EmbeddingClientError):
    """Embedding API 응답 형식이 예상과 다를 때 발생한다."""


def _load_api_key() -> str:
    """프로젝트 루트의 .env에서 CLOVA Studio API 키를 읽는다."""

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("NCP_CLOVASTUDIO_API_KEY", "").strip()
    if not api_key:
        raise EmbeddingConfigurationError(
            "NCP_CLOVASTUDIO_API_KEY가 설정되어 있지 않습니다."
        )
    return api_key


def _normalize_text(text: str) -> str:
    """입력 텍스트를 검증하고 앞뒤 공백을 제거한다."""

    if not isinstance(text, str):
        raise EmbeddingInputError("Embedding 입력은 비어 있지 않은 문자열이어야 합니다.")

    normalized_text = text.strip()
    if not normalized_text:
        raise EmbeddingInputError("Embedding 입력은 비어 있지 않은 문자열이어야 합니다.")
    return normalized_text


def _extract_embedding(response_json: Dict[str, Any]) -> List[float]:
    """공식 응답의 result.embedding을 실수 리스트로 반환한다."""

    try:
        embedding = response_json["result"]["embedding"]
    except (KeyError, TypeError) as error:
        raise EmbeddingResponseParseError(
            "Embedding API 응답에 result.embedding이 없습니다."
        ) from error

    if not isinstance(embedding, list) or not embedding:
        raise EmbeddingResponseParseError(
            "Embedding API 응답의 embedding이 유효한 배열이 아닙니다."
        )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in embedding
    ):
        raise EmbeddingResponseParseError(
            "Embedding API 응답의 embedding에 숫자가 아닌 값이 있습니다."
        )

    return [float(value) for value in embedding]


def get_embedding(text: str) -> List[float]:
    """text를 Embedding v2로 벡터화해서 실수 리스트로 반환한다."""

    normalized_text = _normalize_text(text)
    api_key = _load_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid4()),
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            EMBEDDING_V2_URL,
            headers=headers,
            json={"text": normalized_text},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as error:
        raise EmbeddingRequestError(
            "Embedding API 네트워크 오류가 발생했습니다."
        ) from error

    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise EmbeddingRequestError(
            f"Embedding API HTTP 오류가 발생했습니다. status={response.status_code}"
        ) from error

    try:
        response_json = response.json()
    except ValueError as error:
        raise EmbeddingResponseParseError(
            "Embedding API 응답을 JSON으로 읽을 수 없습니다."
        ) from error

    if not isinstance(response_json, dict):
        raise EmbeddingResponseParseError(
            "Embedding API 응답이 JSON 객체가 아닙니다."
        )
    return _extract_embedding(response_json)


if __name__ == "__main__":
    sample_text = "국가채무 증가율"

    try:
        sample_embedding = get_embedding(sample_text)
        print("Embedding API 호출 성공")
        print(f"text: {sample_text}")
        print(f"dimension: {len(sample_embedding)}")
        print(f"first_values: {sample_embedding[:5]}")
    except EmbeddingClientError as error:
        print(f"Embedding API 호출 오류: {error}")
