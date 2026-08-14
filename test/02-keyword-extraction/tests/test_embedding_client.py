# CLOVA Studio Embedding v2 클라이언트의 요청과 오류 처리를 검증합니다.
from unittest.mock import Mock

import pytest
import requests

from src import embedding_client


def _set_api_key(monkeypatch):
    monkeypatch.setattr(embedding_client, "_load_api_key", lambda: "test-api-key")


def test_get_embedding_returns_float_list(monkeypatch):
    _set_api_key(monkeypatch)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": {"code": "20000", "message": "OK"},
        "result": {"embedding": [0.1, 2, -0.3], "inputTokens": 3},
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(embedding_client.requests, "post", post)
    clock = Mock(side_effect=[20.0, 20.075])
    monkeypatch.setattr(embedding_client, "perf_counter", clock)
    record = Mock()
    monkeypatch.setattr(embedding_client.api_usage_logger, "record_api_usage", record)

    result = embedding_client.get_embedding("  국가채무 증가율  ")

    assert result == [0.1, 2.0, -0.3]
    assert all(isinstance(value, float) for value in result)
    assert post.call_args.kwargs["json"] == {"text": "국가채무 증가율"}
    assert post.call_args.kwargs["timeout"] == 30
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-api-key"
    record.assert_called_once_with(
        service="Embedding v2",
        input_tokens=3,
        output_tokens=0,
        total_tokens=3,
        latency_ms=75.0,
    )


@pytest.mark.parametrize("text", ["", "   \t\n"])
def test_get_embedding_rejects_empty_text_without_api_call(monkeypatch, text):
    post = Mock()
    monkeypatch.setattr(embedding_client.requests, "post", post)

    with pytest.raises(embedding_client.EmbeddingInputError):
        embedding_client.get_embedding(text)

    post.assert_not_called()


@pytest.mark.parametrize("text", [None, 123, ["국가채무 증가율"]])
def test_get_embedding_rejects_non_string_without_api_call(monkeypatch, text):
    post = Mock()
    monkeypatch.setattr(embedding_client.requests, "post", post)

    with pytest.raises(embedding_client.EmbeddingInputError):
        embedding_client.get_embedding(text)

    post.assert_not_called()


def test_get_embedding_fails_when_api_key_is_missing(monkeypatch):
    monkeypatch.setattr(embedding_client, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("NCP_CLOVASTUDIO_API_KEY", raising=False)
    post = Mock()
    monkeypatch.setattr(embedding_client.requests, "post", post)

    with pytest.raises(
        embedding_client.EmbeddingConfigurationError,
        match="NCP_CLOVASTUDIO_API_KEY가 설정되어 있지 않습니다",
    ):
        embedding_client.get_embedding("국가채무 증가율")

    post.assert_not_called()


def test_get_embedding_raises_on_http_error(monkeypatch):
    _set_api_key(monkeypatch)
    response = Mock(status_code=401)
    response.raise_for_status.side_effect = requests.HTTPError("unauthorized")
    monkeypatch.setattr(
        embedding_client.requests,
        "post",
        Mock(return_value=response),
    )

    with pytest.raises(embedding_client.EmbeddingRequestError, match="status=401"):
        embedding_client.get_embedding("국가채무 증가율")


def test_get_embedding_rejects_unexpected_response_json(monkeypatch):
    _set_api_key(monkeypatch)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"status": {"code": "20000"}, "result": {}}
    monkeypatch.setattr(
        embedding_client.requests,
        "post",
        Mock(return_value=response),
    )

    with pytest.raises(
        embedding_client.EmbeddingResponseParseError,
        match="result.embedding",
    ):
        embedding_client.get_embedding("국가채무 증가율")
