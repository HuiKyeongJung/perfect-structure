# -*- coding: utf-8 -*-
"""P3 스키마·평가 하네스 테스트 — 골든 자가 채점 100% + 교란 픽스처(§5.6 매처 단위테스트)."""
from pathlib import Path

import pytest

from src.p3_schemas import ClaimRecord, ExcludedRecord, DocumentSet, derive_kosis_eligible, is_std_period
from src.p3_eval import evaluate, match_sentence, FIELDS

GOLDEN = Path("D:/part1/claim_silver_set_ver2.xlsx")


def mk(cid="A-C001", aid="A", sid="s001", metric="수출액", value="8.3", unit="%",
       period="2025-05", forecast="N", vtype="change_rate", direction="increase",
       code="", eligible=None, **kw) -> ClaimRecord:
    c = ClaimRecord(claim_id=cid, article_id=aid, sent_id=sid, posted_date="2025-06-23",
                    claim="문장", metric=metric, value=value, unit=unit, value_type=vtype,
                    direction=direction, period=period, forecast=forecast,
                    exclusion_code=code, kosis_eligible=eligible, **kw)
    return c.finalize()


class TestSchemas:
    def test_eligible_derivation(self):
        assert derive_kosis_eligible("2025", "N") is True
        assert derive_kosis_eligible("2025-05", "") is True
        assert derive_kosis_eligible("2025-Q1", "N") and derive_kosis_eligible("2025-H2", "N")
        assert derive_kosis_eligible("2025", "Y") is False           # 전망
        assert derive_kosis_eligible("", "N") is False               # 시점 미상
        assert derive_kosis_eligible("2025-06-01~2025-06-20", "N") is False  # 부분기간
        assert derive_kosis_eligible("2024-10~2025-03", "N") is False        # 월범위도 계약 밖
        assert derive_kosis_eligible("2003~2021", "N") is False              # 연범위

    def test_handoff_projection(self):
        c = mk(period="2025-05")
        h = c.to_handoff()
        assert set(h) == {"claim_id", "claim", "metric", "metric_normalized",
                          "value", "unit", "period", "kosis_eligible"}   # v0.4 8필드
        assert h["period"] == "2025-05" and h["kosis_eligible"] is True
        assert h["metric_normalized"] is None   # 미정규화 → null(2번은 metric 폴백)
        # 확장형 period는 사영 시 null
        c2 = mk(period="2025-06-01~2025-06-20", code="PARTIAL_PERIOD")
        assert c2.to_handoff()["period"] is None
        assert c2.to_handoff()["kosis_eligible"] is False

    def test_schema_issues(self):
        assert mk().schema_issues() == []
        assert any("value_type" in i for i in mk(vtype="ratio").schema_issues())
        assert any("PARTIAL_PERIOD" in i for i in mk(period="2025-07-11", code="").schema_issues())
        # 월범위는 코드 불요
        assert mk(period="2024-10~2025-03", code="").schema_issues() == []
        # 표준형에 PARTIAL_PERIOD 마킹은 오류
        assert any("표준형" in i for i in mk(period="2025-06", code="PARTIAL_PERIOD").schema_issues())

    def test_std_period_grammar(self):
        for p in ("2025", "2025-01", "2025-12", "2025-Q4", "2025-H1"):
            assert is_std_period(p)
        for p in ("2025-13", "2025-Q5", "2025-H3", "25-01", "2025-06-01", "2025-01~2025-03"):
            assert not is_std_period(p)

    def test_day_form_rejects_impossible_dates(self):
        from src.p3_schemas import is_valid_period_form
        assert is_valid_period_form("2025-07-11")
        assert is_valid_period_form("2025-06-01~2025-06-20")
        assert not is_valid_period_form("2025-13-45")   # 불가능 월·일
        assert not is_valid_period_form("2025-00-10")
        assert not is_valid_period_form("2025-02-30")   # 형식은 맞지만 달력에 없음
        assert not is_valid_period_form("2025-06-20~2025-06-01")  # 역전 범위
        assert not is_valid_period_form("2024-12~2024-02")
        assert not is_valid_period_form("2021~2003")

    def test_excluded_codes_match_section_5_3(self):
        # §5.3 5종 전부 통과, PARTIAL_PERIOD는 제외 코드가 아님(§4.8)
        for code in ("NON_STAT_NUMBER", "METAPHOR_COMPARISON", "AMBIGUOUS_METRIC",
                     "RELATIVE_NO_BASE", "DUPLICATE"):
            assert ExcludedRecord("A", "s001", "문장", code).schema_issues() == []
        assert ExcludedRecord("A", "s001", "문장", "PARTIAL_PERIOD").schema_issues()
        assert ExcludedRecord("A", "s001", "문장", "EXTRACTION_ERROR").schema_issues()

    def test_handoff_guards(self):
        # finalize 미호출(eligible=None)이어도 사영이 파생을 강제한다
        c = mk(eligible=None)
        c.kosis_eligible = None
        assert c.to_handoff()["kosis_eligible"] is True
        # eligible=true ∧ 비표준 period 모순 조합은 무경고 생산 금지
        bad = mk(period="2025-06-01~2025-06-20", code="PARTIAL_PERIOD")
        bad.kosis_eligible = True
        with pytest.raises(ValueError):
            bad.to_handoff()
        # unit '' → null 사영(§4.1)
        assert mk(unit="").to_handoff()["unit"] is None


class TestMatcher:
    def test_exact_match(self):
        g = [mk()]
        p = [mk()]
        pairs, fn, fp = match_sentence(g, p)
        assert len(pairs) == 1 and not fn and not fp

    def test_same_value_collision_pairs_by_metric(self):
        # 골든 실사례(A984ba465 s026): 브라질·태국이 같은 86% — 순서가 뒤집혀도 metric으로 짝지음
        g = [mk(cid="G1", metric="브라질 수입 전기차 중국산 비율", value="86", unit="%"),
             mk(cid="G2", metric="태국 수입 전기차 중국산 비율", value="86", unit="%")]
        p = [mk(cid="P1", metric="태국 수입 전기차 중국산 비율", value="86", unit="%"),
             mk(cid="P2", metric="브라질 수입 전기차 중국산 비율", value="86", unit="%")]
        pairs, fn, fp = match_sentence(g, p)
        assert len(pairs) == 2 and not fn and not fp
        for gc, pc in pairs:
            assert gc.metric == pc.metric  # 순서 아닌 의미로 매칭

    def test_value_corruption_recovered_as_field_error(self):
        # value가 틀려도 2차(수치 코어+metric)로 매칭 → 검출 실패가 아니라 필드 오류
        g = [mk(value="69", unit="%")]
        p = [mk(value="0.69", unit="%")]  # 값 변환 오류
        pairs, fn, fp = match_sentence(g, p)
        assert len(pairs) == 1 and not fn and not fp

    def test_split_count_mismatch(self):
        g = [mk(cid="G1", value="386억7200만", unit="달러", vtype="level"),
             mk(cid="G2", value="8.3", unit="%")]
        p = [mk(cid="P1", value="386억7200만", unit="달러", vtype="level")]
        pairs, fn, fp = match_sentence(g, p)
        assert len(pairs) == 1 and len(fn) == 1 and not fp

    def test_extra_pred_is_fp(self):
        g = [mk()]
        p = [mk(), mk(cid="P2", metric="완전히 다른 지표", value="999", unit="명", vtype="level")]
        pairs, fn, fp = match_sentence(g, p)
        assert len(pairs) == 1 and not fn and len(fp) == 1

    def test_stage2_rejects_different_metric_and_value(self):
        # 리뷰 반례: 지표도 수치도 다른 쌍이 느슨한 합산 점수로 TP 흡수되면 안 됨
        g = [mk(metric="abcdef", value="1", unit="%")]
        p = [mk(metric="abczzz", value="2", unit="명")]
        pairs, fn, fp = match_sentence(g, p)
        assert not pairs and len(fn) == 1 and len(fp) == 1

    def test_stage1_does_not_let_foreign_value_steal(self):
        # 리뷰 반례: 같은 값의 '남의 수치'(Pb)가 정확 키로 옳은 짝(Pa, 단위만 %p 오류)을 가로채면
        # unit 오류가 metric 오류로 뒤집혀 %↔%p 진단이 가려진다
        g = [mk(cid="G1", metric="수출 증가율", value="8.3", unit="%")]
        pa = mk(cid="Pa", metric="수출 증가율", value="8.3", unit="%p")
        pb = mk(cid="Pb", metric="설비 가동률", value="8.3", unit="%")
        pairs, fn, fp = match_sentence(g, [pa, pb])
        assert len(pairs) == 1
        assert pairs[0][1].claim_id == "Pa"  # 의미상 옳은 짝
        assert fp[0].claim_id == "Pb"


class TestEvaluate:
    def _ds(self, claims, excluded=()):
        return DocumentSet(claims=list(claims), excluded=list(excluded), version="t")

    def test_field_error_counted(self):
        g = self._ds([mk()])
        p = self._ds([mk(metric="수출")])  # metric만 오류
        rep = evaluate(g, p)
        assert rep.f1 == 1.0
        assert rep.acc("metric") == 0.0 and rep.acc("value") == 1.0
        assert rep.handoff_full == 0
        assert rep.risk_matched == 1  # eligible=true인데 핵심 필드 오류 → 위험 지표

    def test_risk_fp(self):
        g = self._ds([])
        p = self._ds([mk()])  # 과잉 Claim(eligible=true)
        rep = evaluate(g, p)
        assert rep.fp == 1 and rep.risk_fp == 1

    def test_excluded_scoring_and_coverage(self):
        e = ExcludedRecord(article_id="A", sent_id="s002", sentence="문장", exclusion_code="NON_STAT_NUMBER")
        g = self._ds([mk()], [e])
        p = self._ds([mk()], [])  # 제외 문장 누락
        rep = evaluate(g, p)
        assert rep.excl_fn == 1
        assert ("A", "s002") in rep.missing_sentences

    def test_extra_sentence_reported(self):
        # 골든 밖 문장에 Claim 생성(가짜 Claim 계통 신호) — FP + 대칭 커버리지에 노출
        g = self._ds([mk()])
        p = self._ds([mk(), mk(cid="X", sid="s999", metric="원달러 환율", value="1477.2", unit="원", vtype="level")])
        rep = evaluate(g, p)
        assert rep.fp == 1 and ("A", "s999") in rep.extra_sentences

    def test_sparse_field_no_trivial_inflation(self):
        # direction을 전혀 출력하지 않는 pred — 양쪽 빈값 쌍은 분모 제외, gold 채움 쌍만 오류로
        g = self._ds([mk(cid="G1", direction="increase"),
                      mk(cid="G2", sid="s002", value="10", direction="", vtype="level")])
        p = self._ds([mk(cid="P1", direction=""),
                      mk(cid="P2", sid="s002", value="10", direction="", vtype="level")])
        rep = evaluate(g, p)
        c, t = rep.field_acc["direction"]
        assert (c, t) == (0, 1)  # 자명 일치(G2)는 분모 제외, G1만 오류로

    def test_pred_eligible_type_guard(self):
        g = self._ds([mk()])
        bad = mk()
        bad.kosis_eligible = "FALSE"  # 문자열 truthy 함정
        with pytest.raises(TypeError):
            evaluate(g, self._ds([bad]))


@pytest.mark.skipif(not GOLDEN.exists(), reason="골든 xlsx 없음")
class TestGoldenSelfScore:
    def test_self_score_perfect(self):
        from src.p3_golden import load_golden
        gold = load_golden(GOLDEN)
        assert len(gold.claims) == 508 and len(gold.excluded) == 299
        rep = evaluate(gold, gold)
        assert rep.f1 == 1.0 and rep.fn == 0 and rep.fp == 0
        for f in FIELDS:
            assert rep.acc(f) == 1.0, f"{f} 자가 채점 {rep.acc(f)}"
        assert rep.handoff_full == rep.tp
        assert rep.risk_matched == 0 and rep.risk_fp == 0
        assert rep.excl_fn == 0 and rep.excl_fp == 0
        assert not rep.missing_sentences

    def test_golden_eligible_consistent_with_derivation(self):
        # 골든의 kosis_eligible 저장값이 §4.8 파생식과 전건 일치하는지
        from src.p3_golden import load_golden
        from src.p3_schemas import derive_kosis_eligible
        gold = load_golden(GOLDEN)
        diff = [c.claim_id for c in gold.claims
                if c.kosis_eligible != derive_kosis_eligible(c.period, c.forecast)]
        assert diff == [], f"파생식 불일치: {diff[:10]}"


class TestGoldenLoaderDefense:
    def test_date_and_float_cells_parsed(self, tmp_path):
        # 사용자가 xlsx 재편집 시 생기는 날짜·숫자 셀 오염 방어(§6 채점기 규약)
        import datetime
        from openpyxl import Workbook
        from src.p3_golden import load_golden, _COLS
        wb = Workbook()
        ws = wb.active
        ws.append(_COLS)
        row = {c: "" for c in _COLS}
        row.update({"claim_id": "A-C001", "article_id": "A", "sent_id": "s001",
                    "claim": "문장", "metric": "수출액", "kosis_eligible": "TRUE",
                    "forecast": "N", "period": None, "value": None, "posted_date": None})
        vals = [row[c] for c in _COLS]
        vals[_COLS.index("posted_date")] = datetime.datetime(2025, 6, 23)  # 날짜 셀
        vals[_COLS.index("value")] = 86.0                                   # 숫자 셀
        vals[_COLS.index("period")] = datetime.datetime(2025, 5, 1)
        ws.append(vals)
        p = tmp_path / "g.xlsx"
        wb.save(p)
        ds = load_golden(p)
        c = ds.claims[0]
        assert c.posted_date == "2025-06-23"
        assert c.value == "86"          # '86.0' 오염 방지
        assert c.period == "2025-05-01"  # 날짜형은 ISO로 — 형식 검사에서 잡히도록 보존

    def test_orphan_content_row_raises(self, tmp_path):
        from openpyxl import Workbook
        from src.p3_golden import load_golden, _COLS
        wb = Workbook()
        ws = wb.active
        ws.append(_COLS)
        vals = [""] * len(_COLS)
        vals[_COLS.index("metric")] = "고아 행"  # 키 없는 내용 행
        ws.append(vals)
        p = tmp_path / "g.xlsx"
        wb.save(p)
        with pytest.raises(ValueError):
            load_golden(p)
