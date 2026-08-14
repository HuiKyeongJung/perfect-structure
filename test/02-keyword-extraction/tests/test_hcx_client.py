# HCX 모델별 요청 body의 thinking 설정을 검증합니다.
from unittest.mock import Mock

from src import hcx_client
from src.hcx_client import _build_request_body


def test_hcx_loads_unified_clova_environment_variable(monkeypatch):
    monkeypatch.setattr(hcx_client, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("NCP_CLOVASTUDIO_API_KEY", "test-key")

    assert hcx_client._load_api_key() == "test-key"


def test_hcx007_request_body_keeps_low_as_default_thinking_effort():
    body = _build_request_body("HCX-007", [])

    assert body["thinking"] == {"effort": "low"}


def test_hcx007_request_body_uses_explicit_thinking_effort():
    body = _build_request_body("HCX-007", [], thinking_effort="none")

    assert body["thinking"] == {"effort": "none"}


def test_hcx005_request_body_is_unchanged_by_thinking_effort():
    body = _build_request_body("HCX-005", [], thinking_effort="none")

    assert "thinking" not in body
    assert body["maxTokens"] == 300


def test_call_hcx_records_successful_api_usage(monkeypatch):
    monkeypatch.setattr(hcx_client, "_load_api_key", lambda: "test-key")
    response_json = {
        "result": {
            "message": {"content": "완료"},
            "usage": {
                "promptTokens": 40,
                "completionTokens": 10,
                "totalTokens": 50,
            },
        }
    }
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = response_json
    monkeypatch.setattr(hcx_client.requests, "post", Mock(return_value=response))
    clock = Mock(side_effect=[10.0, 10.125])
    monkeypatch.setattr(hcx_client, "perf_counter", clock)
    record = Mock()
    monkeypatch.setattr(hcx_client.api_usage_logger, "record_api_usage", record)

    result = hcx_client.call_hcx("HCX-005", [])

    assert result == response_json
    record.assert_called_once_with(
        service="HCX-005",
        input_tokens=40,
        output_tokens=10,
        total_tokens=50,
        latency_ms=125.0,
    )
