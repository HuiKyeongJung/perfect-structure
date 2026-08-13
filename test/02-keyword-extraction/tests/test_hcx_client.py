# HCX 모델별 요청 body의 thinking 설정을 검증합니다.
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
