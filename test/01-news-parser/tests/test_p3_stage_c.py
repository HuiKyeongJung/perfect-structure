# -*- coding: utf-8 -*-
"""Stage C 검증기 테스트 + 골든 패스스루 스모크(§5.6 구현 순서 ③ — HCX 0콜)."""
from pathlib import Path

import pytest

from src.p3_stage_c import (value_in_sentence, metric_missing_words, rule_direction,
                            rule_value_type, audit_flags, destructive_issues, run_passthrough)
from test_p3_eval import mk

GOLDEN = Path("D:/part1/claim_silver_set_ver2.xlsx")
ARTICLES = Path("D:/news-parser/data/articles_clean.jsonl")


class TestReverseCheck:
    def test_value_unit_combined(self):
        s = "관세청에 따르면 지난 1~20일 수출액은 386억7200만달러로 전년 동기 대비 8.3% 증가했다."
        assert value_in_sentence("386억7200만", "달러", s)
        assert value_in_sentence("8.3", "%", s)
        assert not value_in_sentence("386억7200만", "원", s)   # 단위 불일치
        assert not value_in_sentence("0.69", "%", "답변 비율이 각각 69%, 58.6%였다.")  # 값 변환 환각

    def test_space_insensitive(self):
        assert value_in_sentence("13만", "명", "취업자 수는 13만 명 감소했다.")

    def test_truncation_hallucination_blocked(self):
        # 리뷰 반례: 절단 환각(부분 문자열)은 좌측 숫자 경계로 차단
        assert not value_in_sentence("3", "%", "수출이 8.3% 늘었다.")
        assert not value_in_sentence("9", "%", "답변 비율이 각각 69%였다.")
        assert not value_in_sentence("300조", "원", "나랏빚이 1300조원을 넘어섰다.")
        assert not value_in_sentence("2000억", "원", "예산은 1조2000억원이다.")

    def test_unitless_needs_right_boundary(self):
        assert not value_in_sentence("13만", "", "취업자가 13만4000명 늘었다.")   # 우측 절단
        assert not value_in_sentence("1300", "", "나랏빚 1300조원.")             # 수사 앞 절단
        # 정당한 unit 없는 값(소제목 '126조') — 공백·문장부호 경계는 허용
        assert value_in_sentence("126조", "", "◇나랏빚 증가 폭 126조 '역대 최대'")


class TestMetricExistence:
    ART = "반도체와 자동차 등 주력 품목 수출이 크게 늘면서 우리나라 수출이 증가했다."

    def test_combination_allowed(self):
        assert metric_missing_words("반도체 수출", self.ART) == []
        assert metric_missing_words("자동차 수출", self.ART) == []

    def test_creation_rejected(self):
        assert metric_missing_words("대미 수출", self.ART) == ["대미"]

    def test_enumeration_distribution(self):
        # 판례 4 준용: "2·3·4등급" 압축 나열은 각 등급의 실존으로 본다
        art = "1등급 시스템 복구율은 77.5%이고, 2·3·4등급 복구율은 각각 67.6%, 61.3%, 47.1%다."
        for m in ("1등급 시스템 복구율", "2등급 복구율", "3등급 복구율", "4등급 복구율"):
            assert metric_missing_words(m, art) == [], m
        assert metric_missing_words("5등급 복구율", art) == ["5등급"]

    def test_enumeration_digit_boundary(self):
        # 리뷰 반례: '12·3등급'의 12 꼬리에서 '2등급'이 실존 판정되면 안 됨
        art = "12·3등급 판정 복구율 통계다."
        assert metric_missing_words("2등급 복구율", art) == ["2등급"]
        assert metric_missing_words("12등급 복구율", art) == []
        assert metric_missing_words("3등급 복구율", art) == []


class TestRuleClassifiers:
    def test_direction(self):
        assert rule_direction("수출이 8.3% 증가했다") == "increase"
        assert rule_direction("수출이 1.3% 감소했다") == "decrease"
        assert rule_direction("수출은 386억달러였다") is None
        assert rule_direction("수출은 늘었지만 수입은 줄었다") is None  # 혼재 → 보류

    def test_value_type(self):
        assert rule_value_type("수출이 8.3% 증가했다", "8.3", "%") == "change_rate"
        assert rule_value_type("재정 적자는 GDP의 4.2%에 달한다", "4.2", "%") == "share_ratio"
        assert rule_value_type("고용률이 1.5%포인트 하락했다", "1.5", "%포인트") == "change_amount"
        assert rule_value_type("채무가 76조6000억원이나 늘어났다", "76조6000억", "원") == "change_amount"

    def test_value_type_review_counterexamples(self):
        # 괄호 병기(골든 최빈 패턴): 소수점 창 통과 — '(3.8%) 증가'가 level로 오판되던 결함
        s = "취업자가 764명(3.8%) 증가했다."
        assert rule_value_type(s, "764", "명") == "change_amount"
        assert rule_value_type(s, "3.8", "%") == "change_rate"
        # 수준값 + 근처의 남의 증감(%p): 45.6을 change_rate로 오발하던 결함 — 사이 숫자 차단
        s2 = "고용률은 45.6%로 1%포인트 하락했다."
        assert rule_value_type(s2, "45.6", "%") is None
        assert rule_value_type(s2, "1", "%포인트") == "change_amount"
        # '(으)로 도달' 구문은 증감액이 아니라 수준값
        assert rule_value_type("국가 채무가 1301조9000억원으로 뛰게 됐다", "1301조9000억", "원") == "level"

    def test_audit_forecast_flag_not_promotion(self):
        # 사전 히트 + N → 플래그만(자동 승격 금지 — §5.6)
        c = mk(forecast="N")
        flags = audit_flags(c, "시장 예상치를 밑돌았다")
        assert "forecast_lexicon_hit_but_N" in flags
        assert c.forecast == "N"  # 값은 불변


class TestDestructive:
    def test_clean_claim_passes(self):
        s = "수출액은 386억7200만달러로 증가했다."
        c = mk(metric="수출액", value="386억7200만", unit="달러", vtype="level", direction="")
        assert destructive_issues(c, s, "우리나라 수출액 통계 기사다.") == []

    def test_hallucinated_value_caught(self):
        c = mk(value="999", unit="%")
        assert any("역검증" in i for i in destructive_issues(c, "수출이 8.3% 늘었다.", "수출 기사"))


@pytest.mark.skipif(not (GOLDEN.exists() and ARTICLES.exists()), reason="골든/기사 데이터 없음")
class TestGoldenPassthrough:
    def test_rules_do_not_destroy_golden(self):
        r = run_passthrough(GOLDEN, ARTICLES)
        assert r["claims"] == 508 and r["excluded"] == 299
        assert r["destroyed"] == [], f"룰이 골든을 파괴: {r['destroyed'][:5]}"
        assert r["excluded_bad"] == []
        assert r["handoff_ok"] == 508          # 7필드 사영 전건 성공(계약 위반 조합 0)
        assert r["eligible_true"] == 359
