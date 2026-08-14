# CLOVA Studio API 사용량 로그의 토큰 집계와 비용 계산을 검증합니다.
from src import api_usage_logger


def test_extract_hcx_usage_reads_official_usage_fields():
    response = {
        "result": {
            "usage": {
                "promptTokens": 120,
                "completionTokens": 30,
                "totalTokens": 150,
            }
        }
    }

    assert api_usage_logger.extract_hcx_usage(response) == (120, 30, 150)


def test_extract_embedding_tokens_reads_input_tokens():
    response = {"result": {"inputTokens": 17, "embedding": [0.1]}}

    assert api_usage_logger.extract_embedding_tokens(response) == 17


def test_calculate_estimated_cost_uses_verified_prices_only():
    assert api_usage_logger.calculate_estimated_cost("HCX-007", 1000, 1000) == 6.25
    assert api_usage_logger.calculate_estimated_cost("Embedding v2", 1000, 0) == 0.2
    assert api_usage_logger.calculate_estimated_cost("HCX-005", 1000, 1000) is None


def test_record_api_usage_overwrites_previous_run_and_summarizes_current_run(
    monkeypatch,
    tmp_path,
):
    log_path = tmp_path / "API_USAGE_LOG.md"
    monkeypatch.setattr(api_usage_logger, "USAGE_LOG_PATH", log_path)
    monkeypatch.setattr(api_usage_logger, "_current_run_id", lambda: "TEST-RUN")
    api_usage_logger._RUN_EVENTS.clear()
    log_path.write_text(
        api_usage_logger._base_document().replace(
            "아직 기록된 실제 API 호출이 없습니다.",
            "### 실행 OLD-RUN\n\n| 호출 합계 | 99회 |",
        ),
        encoding="utf-8",
    )

    api_usage_logger.record_api_usage(
        service="HCX-007",
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        latency_ms=125.5,
        force=True,
    )
    api_usage_logger.record_api_usage(
        service="Embedding v2",
        input_tokens=10,
        output_tokens=0,
        total_tokens=10,
        latency_ms=74.5,
        force=True,
    )

    content = log_path.read_text(encoding="utf-8")
    assert "OLD-RUN" not in content
    assert "TEST-RUN" in content
    assert "HCX-007" in content
    assert "Embedding v2" in content
    assert "호출 합계 | 2회" in content
    assert "입력 토큰 합계 | 110" in content
    assert "125.50ms" in content
    assert "평균 응답 시간 | 100.00ms" in content
    api_usage_logger._RUN_EVENTS.clear()


def test_record_api_usage_does_not_write_during_pytest(monkeypatch, tmp_path):
    log_path = tmp_path / "API_USAGE_LOG.md"
    monkeypatch.setattr(api_usage_logger, "USAGE_LOG_PATH", log_path)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "active")

    api_usage_logger.record_api_usage("Embedding v2", 3, 0, 3, latency_ms=10.0)

    assert not log_path.exists()
