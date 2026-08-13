# -*- coding: utf-8 -*-
"""LLM 사용량 계측 테스트 — 요금 계산은 요금표와 1:1로 고정한다(단가가 바뀌면 여기서 깨진다)."""
import json

import pytest

from src.llm_meter import (PRICING_PER_1K, VAT_RATE, UsageMeter, aggregate,
                           cost_krw, load_records, render_report)


class TestCost:
    def test_hcx005_matches_price_table(self):
        # API/CLOVA_요금.pdf — 기본 HCX-005: 입력 1,000토큰 1.25원 · 출력 1,000토큰 5원
        assert PRICING_PER_1K["HCX-005"] == (1.25, 5.0)
        # 입력 1,000 + 출력 1,000 = 1.25 + 5 = 6.25원
        assert cost_krw("HCX-005", 1000, 1000) == pytest.approx(6.25)
        # 실제 규모 예: 입력 4,000 · 출력 500 → 5.0 + 2.5 = 7.5원
        assert cost_krw("HCX-005", 4000, 500) == pytest.approx(7.5)

    def test_vat(self):
        assert cost_krw("HCX-005", 1000, 1000, vat=True) == pytest.approx(6.25 * (1 + VAT_RATE))

    def test_dash_is_cheaper(self):
        """DASH-002 전환 검토의 근거 — 같은 토큰이면 1/5 가격이어야 한다."""
        assert cost_krw("HCX-DASH-002", 1000, 1000) == pytest.approx(1.25)

    @pytest.mark.parametrize("model,i,o", [
        ("UNKNOWN-MODEL", 100, 100),   # 단가 미상
        ("HCX-005", None, 100),        # 토큰 미상(응답에 usage 없음)
        ("HCX-005", 100, None),
    ])
    def test_unknown_returns_none(self, model, i, o):
        assert cost_krw(model, i, o) is None


class TestMeter:
    def test_appends_jsonl_and_computes_cost(self, tmp_path):
        m = UsageMeter(tmp_path / "usage.jsonl", model="HCX-005", prompt_version="v1")
        m.record(input_tokens=2000, output_tokens=400, latency_ms=1500.0)
        rows = [json.loads(l) for l in (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["model"] == "HCX-005" and rows[0]["prompt_version"] == "v1"
        # 2.5 + 2.0 = 4.5원
        assert rows[0]["cost_krw"] == pytest.approx(4.5)

    def test_cached_rows_are_free(self, tmp_path):
        m = UsageMeter(tmp_path / "u.jsonl", model="HCX-005")
        m.record(cached=True, attempt="replay")
        assert m.records[0].cost_krw is None
        assert m.summary() == {"calls_total": 1, "calls_api": 0, "calls_cached": 1,
                               "input_tokens": 0, "output_tokens": 0, "cost_krw": 0}

    def test_disabled_writes_nothing(self, tmp_path):
        p = tmp_path / "off.jsonl"
        m = UsageMeter(p, model="HCX-005", enabled=False)
        m.record(input_tokens=10, output_tokens=10)
        assert not p.exists() and len(m.records) == 1


class TestAggregate:
    def _rows(self):
        return [
            {"model": "HCX-005", "attempt": "initial", "article_id": "A1", "sent_id": "s1",
             "ok": True, "cached": False, "input_tokens": 3000, "output_tokens": 300,
             "latency_ms": 1000.0, "cost_krw": 5.25, "http_retries": 0},
            {"model": "HCX-005", "attempt": "repair", "article_id": "A1", "sent_id": "s1",
             "ok": True, "cached": False, "input_tokens": 3500, "output_tokens": 200,
             "latency_ms": 3000.0, "cost_krw": 5.375, "http_retries": 1},
            {"model": "HCX-005", "attempt": "replay", "article_id": "A2", "sent_id": "s9",
             "ok": True, "cached": True},
            {"model": "HCX-005", "attempt": "initial", "article_id": "A2", "sent_id": "s2",
             "ok": False, "cached": False, "input_tokens": None, "output_tokens": None,
             "latency_ms": 500.0, "cost_krw": None, "http_retries": 3},
        ]

    def test_counts_and_totals(self):
        a = aggregate(self._rows())
        assert a["calls_total"] == 4 and a["calls_api"] == 3 and a["calls_cached"] == 1
        assert a["calls_failed"] == 1 and a["http_retries"] == 4
        assert a["input_tokens"] == 6500 and a["output_tokens"] == 500
        assert a["cost_krw"] == pytest.approx(10.625)
        assert a["cost_krw_vat"] == pytest.approx(10.625 * 1.1)

    def test_cached_excluded_from_speed_and_cost(self):
        """캐시 재생은 요금 0이고 지연 통계에도 들어가면 안 된다(측정 왜곡)."""
        a = aggregate(self._rows())
        assert a["latency"]["n"] == 2          # 성공 실호출 2건만
        assert a["latency"]["max"] == 3000.0

    def test_per_unit_costs(self):
        a = aggregate(self._rows())
        assert a["sentences"] == 3 and a["articles"] == 2
        assert a["cost_per_article"] == pytest.approx(10.625 / 2)
        assert a["calls_per_sentence"] == pytest.approx(3 / 3)

    def test_by_attempt_split(self):
        a = aggregate(self._rows())
        assert a["by_attempt"]["initial"]["calls"] == 2
        assert a["by_attempt"]["repair"]["krw"] == pytest.approx(5.375)

    def test_empty_is_safe(self):
        a = aggregate([])
        assert a["calls_total"] == 0 and a["cost_krw"] == 0
        assert "요금" in render_report(a)      # 0건이어도 리포트는 렌더링돼야 한다

    def test_report_projection(self):
        a = aggregate(self._rows())
        md = render_report(a, target_articles=2695, concurrency=4)
        assert "2,695" in md and "규모 추정" in md

    def test_load_records_missing_file(self, tmp_path):
        assert load_records(tmp_path / "none.jsonl") == []
