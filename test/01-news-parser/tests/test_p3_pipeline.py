# -*- coding: utf-8 -*-
"""Stage A·E·파이프라인 테스트 — 회계 인바리언트 + 무HCX E2E 스모크(§5.6 ③)."""
from pathlib import Path

import pytest

from src.p3_stage_a import is_numeric_sentence, SentenceCandidate
from src.p3_schemas import ExcludedRecord
from src.p3_emit import assign_claim_ids, accounting_check, AccountingError
from src.p3_pipeline import process_sentence
from test_p3_eval import mk

GOLDEN = Path("D:/part1/claim_silver_set_ver2.xlsx")
DATA = Path("D:/news-parser/data")


def cand(aid="A", sid="s001", text="수출이 8.3% 늘었다.", posted="2025-06-23") -> SentenceCandidate:
    return SentenceCandidate(article_id=aid, sent_id=sid, text=text,
                             posted_date=posted, title="제목")


class TestStageA:
    def test_numeric_filter(self):
        assert is_numeric_sentence("수출이 8.3% 늘었다.")
        assert is_numeric_sentence("A4용지 2장")
        assert not is_numeric_sentence("숫자가 없는 문장이다.")
        assert not is_numeric_sentence("두 명의 부총리 체제가 된다.")   # 한글 수사는 ver1 비대상
        assert not is_numeric_sentence("")
        assert not is_numeric_sentence(None)


class TestEmit:
    def test_claim_id_assignment(self):
        claims = [mk(cid="", aid="A"), mk(cid="", aid="A", sid="s002"), mk(cid="", aid="B")]
        assign_claim_ids(claims)
        assert [c.claim_id for c in claims] == ["A-C001", "A-C002", "B-C001"]

    def test_accounting_missing_raises(self):
        cands = [cand(sid="s001"), cand(sid="s002")]
        with pytest.raises(AccountingError, match="미회계"):
            accounting_check(cands, [mk(sid="s001")], [], [])

    def test_accounting_leak_raises(self):
        # 숫자 아닌(후보 밖) 문장에서 산출 발생 = 가짜 Claim 계통 신호
        with pytest.raises(AccountingError, match="유출"):
            accounting_check([cand(sid="s001")], [mk(sid="s999")],
                             [], [{"article_id": "A", "sent_id": "s001"}])

    def test_accounting_mixed_sentence_ok(self):
        # 한 문장에 Claim 행과 제외 행 공존(골든 실재 4건) — 커버리지라 통과해야 함
        e = ExcludedRecord("A", "s001", "문장", "NON_STAT_NUMBER")
        s = accounting_check([cand(sid="s001")], [mk(sid="s001")], [e], [])
        assert s["claims"] == 1 and s["excluded"] == 1

    def test_accounting_duplicate_candidates_raise(self):
        # 집합 회계의 중복 무감 방지 — 같은 키 후보 2개는 회계 훼손 신호
        with pytest.raises(AccountingError, match="중복"):
            accounting_check([cand(sid="s001"), cand(sid="s001")], [mk(sid="s001")], [], [])

    def test_emit_atomic_no_partial_bundle(self, tmp_path):
        # 선평가에서 예외(계약 위반 조합) → 디스크 무변경(혼합 번들 금지)
        from src.p3_emit import emit_all
        bad = mk(period="2025-06-01~2025-06-20", code="PARTIAL_PERIOD")
        bad.kosis_eligible = True   # period null 사영 ∧ eligible=true → to_handoff 예외
        with pytest.raises(ValueError):
            emit_all(tmp_path, [cand(sid="s001")], [bad], [], [], [])
        assert list(tmp_path.iterdir()) == []   # 어떤 파일도 생기지 않아야 함


class TestNormalizeValueUnit:
    def test_unit_duplicated_in_value(self):
        # dev 실측 최빈 오류: '441만4000명'+'명' → 역검증 '명명' 실패하던 것
        from src.p3_pipeline import normalize_value_unit as nvu
        assert nvu("441만4000명", "명") == ("441만4000", "명")
        assert nvu("1300조원", "원") == ("1300조", "원")
        assert nvu("70.3%", "%") == ("70.3", "%")

    def test_approx_suffix_stripped(self):
        from src.p3_pipeline import normalize_value_unit as nvu
        assert nvu("8% 이상", "%") == ("8", "%")
        assert nvu("3% 이내", "%") == ("3", "%")
        assert nvu("약 13만", "명") == ("13만", "명")

    def test_unit_space_and_tail_split(self):
        from src.p3_pipeline import normalize_value_unit as nvu
        assert nvu("7", "% p") == ("7", "%p")
        assert nvu("3만원", "") == ("3만", "원")      # unit 누락 시 꼬리 분리
        assert nvu("20%", "") == ("20", "%")
        assert nvu("126조", "") == ("126조", "")      # 단위 없는 값은 그대로(골든 실재)

    def test_clean_values_unchanged(self):
        from src.p3_pipeline import normalize_value_unit as nvu
        assert nvu("386억7200만", "달러") == ("386억7200만", "달러")
        assert nvu("8.3", "%") == ("8.3", "%")

    def test_range_dae_stripped(self):
        # dev 2회차 실측: '1000조원대' → 골든은 value '1000조' + unit '원'
        from src.p3_pipeline import normalize_value_unit as nvu
        assert nvu("1000조원대", "") == ("1000조", "원")
        assert nvu("100조원대", "") == ("100조", "원")
        # 단위로서의 '대'(자동차 3대)는 보존
        assert nvu("3", "대") == ("3", "대")


class TestStageCRepairLoop:
    def test_repair_adopted_when_improved(self):
        from src.p3_pipeline import run  # noqa — run 경유 대신 process 수준으로 검증
        # 파이프라인 수준: 오염 stub → repair가 교정본 반환 → 채택
        from src.p3_pipeline import process_sentence
        c = cand(text="수출이 8.3% 증가했다.")
        art = "수출 기사"
        bad = [{"kind": "claim", "metric": "수출", "value": "8.3% 이상", "unit": "%",
                "period": "올해", "forecast": "N"}]
        # 정규화만으로 회수되는지(경계어) — repair 없이도 통과해야 함
        claims, _, errors, _ = process_sentence(c, bad, art)
        assert len(claims) == 1 and not errors
        assert claims[0].value == "8.3" and claims[0].unit == "%"

    def test_run_level_repair_hook(self, tmp_path):
        # extractor.repair가 있으면 Stage C 실패 문장을 1회 재추출해 회수
        from src.p3_pipeline import run
        from src.p3_stage_a import SentenceCandidate

        GOOD_ITEM = {"kind": "claim", "metric": "수출", "value": "8.3", "unit": "%",
                     "period": "올해", "forecast": "N"}
        BAD_ITEM = {"kind": "claim", "metric": "수출", "value": "999", "unit": "%",
                    "period": "올해", "forecast": "N"}   # 역검증 실패(정규화 불가)

        import json as _json
        sents = tmp_path / "sentences.jsonl"
        arts = tmp_path / "articles.jsonl"
        sents.write_text(_json.dumps({"article_id": "A", "sent_id": "s001",
                                      "text": "수출이 8.3% 증가했다."}, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        arts.write_text(_json.dumps({"article_id": "A", "posted_date": "2025-06-23",
                                     "title": "제목", "text": "수출이 8.3% 증가했다."},
                                    ensure_ascii=False) + "\n", encoding="utf-8")

        def extractor(cand):
            return [dict(BAD_ITEM)]

        calls = []

        def repair(cand, feedback):
            calls.append(feedback)
            return [dict(GOOD_ITEM)]

        extractor.repair = repair
        summary = run(extractor, tmp_path / "out", sentences_path=sents, articles_path=arts,
                      normalizer=False)
        assert summary["claims"] == 1 and summary["errors"] == 0
        assert calls and "역검증" in calls[0]


class TestProcessSentence:
    ART = "수출액과 수입액 통계. 지난 1~20일 수출이 8.3% 증가했다. 전년 동기 대비다."

    def test_partial_period_recomputed(self):
        c = cand(text="지난 1~20일 수출이 8.3% 증가했다.")
        items = [{"kind": "claim", "metric": "수출", "value": "8.3", "unit": "%",
                  "period": "지난 1~20일", "value_type": "change_rate",
                  "direction": "increase", "forecast": "N"}]
        claims, excluded, errors, traces = process_sentence(c, items, self.ART)
        assert len(claims) == 1 and not errors
        assert claims[0].period == "2025-06-01~2025-06-20"
        assert claims[0].exclusion_code == "PARTIAL_PERIOD"    # resolver partial → 자동 마킹
        assert claims[0].kosis_eligible is False
        assert traces[0]["period_method"] == "day_range"

    def test_anchor_from_sibling(self):
        c = cand(text="수출액은 300억달러로 지난달 대비 늘었고 전년 동기에는 200억달러였다.")
        art = "수출액 통계 기사."
        items = [
            {"kind": "claim", "metric": "수출액", "value": "300억", "unit": "달러",
             "period": "지난달", "value_type": "level", "forecast": "N"},
            {"kind": "claim", "metric": "수출액", "value": "200억", "unit": "달러",
             "period": "전년 동기", "value_type": "level", "forecast": "N"},
        ]
        claims, _, errors, _ = process_sentence(c, items, art)
        assert not errors
        assert claims[0].period == "2025-05"
        assert claims[1].period == "2024-05"    # 형제 앵커에서 -1년

    def test_hallucination_routed_to_errors(self):
        c = cand(text="수출이 8.3% 증가했다.")
        items = [{"kind": "claim", "metric": "수출", "value": "3", "unit": "%",
                  "period": "올해", "forecast": "N"}]   # '3%'는 절단 환각
        claims, _, errors, _ = process_sentence(c, items, "수출 기사")
        assert not claims and len(errors) == 1
        assert "역검증" in errors[0]["reason"]

    def test_internal_code_not_in_excluded(self):
        c = cand()
        items = [{"kind": "excluded", "exclusion_code": "EXTRACTION_ERROR"}]
        claims, excluded, errors, _ = process_sentence(c, items, "기사")
        assert not excluded and len(errors) == 1   # 내부 코드는 errors.jsonl로

    def test_unknown_kind_routed_to_errors(self):
        # 리뷰 high: 미지 kind가 eligible=true Claim으로 폴스루하던 결함
        c = cand()
        for bad in ("EXCLUDED", "qual", "", "cliam"):
            items = [{"kind": bad, "metric": "수출", "value": "8.3", "unit": "%",
                      "period": "올해", "forecast": "N"}]
            claims, excluded, errors, _ = process_sentence(c, items, "수출 기사")
            assert not claims and not excluded and len(errors) == 1, bad
            assert "kind" in errors[0]["reason"]

    def test_kind_code_conflict_routed_to_errors(self):
        # kind=claim + 제외 코드 = 저신뢰 신호(수리 대상) — 조용한 소거 금지
        c = cand()
        items = [{"kind": "claim", "exclusion_code": "NON_STAT_NUMBER", "metric": "수출",
                  "value": "8.3", "unit": "%", "period": "올해", "forecast": "N"}]
        claims, _, errors, _ = process_sentence(c, items, "수출 기사")
        assert not claims and len(errors) == 1 and "모순" in errors[0]["reason"]

    def test_forecast_missing_routed_to_errors(self):
        # §5.6 필수 2값 — 기본값 'N' 부여는 eligible=true 방향의 위험 비대칭
        c = cand()
        items = [{"kind": "claim", "metric": "수출", "value": "8.3", "unit": "%", "period": "올해"}]
        claims, _, errors, _ = process_sentence(c, items, "수출 기사")
        assert not claims and len(errors) == 1 and "forecast" in errors[0]["reason"]

    def test_partial_anchor_allowed(self):
        # 리뷰 high(골든 s010 실사례): 형제가 전부 부분기간이어도 '전년 동기'가 계산돼야 함
        c = cand(text="올 들어 지난 20일까지 흑자는 213억달러로 전년 동기 대비 31.9% 늘었다.")
        art = "무역수지 흑자 통계."
        items = [
            {"kind": "claim", "metric": "흑자", "value": "213억", "unit": "달러",
             "period": "올 들어 지난 20일까지", "forecast": "N"},
            {"kind": "claim", "metric": "흑자", "value": "31.9", "unit": "%",
             "period": "전년 동기", "forecast": "N"},
        ]
        claims, _, errors, _ = process_sentence(c, items, art)
        assert not errors
        assert claims[0].period == "2025-01-01~2025-06-20"
        assert claims[1].period == "2024-01-01~2024-06-20"    # partial 앵커 -1년
        assert claims[1].exclusion_code == "PARTIAL_PERIOD"

    def test_direction_rule_fills_only_change_types(self):
        # §5.6 'direction은 룰 우선' — 단 증감형에서만(골든: 비증감형 direction 100% 공백)
        c = cand(text="채무가 76조6000억원이나 늘어났다.")
        art = "채무 통계 기사. 채무가 76조6000억원이나 늘어났다."
        base = {"kind": "claim", "metric": "채무", "value": "76조6000억", "unit": "원",
                "period": "올해", "forecast": "N", "direction": ""}
        # 증감형 → 룰이 LLM 공백을 메움
        claims, _, errors, _ = process_sentence(c, [dict(base, value_type="change_amount")], art)
        assert not errors and claims[0].direction == "increase"
        # 수준값 → 문장에 증감 어휘가 있어도 direction은 비운다(골든 대칭)
        claims2, _, _, _ = process_sentence(c, [dict(base, value_type="level")], art)
        assert claims2[0].direction == ""

    def test_carry_anchor_across_sentences(self):
        # 문장 간 상속: 앞 문장이 "지난 1~20일"이면 이 문장의 "이 기간"이 그것을 물려받는다
        c = cand(text="이 기간 하루 평균 수출액은 27억6000달러였다.")
        art = "이 기간 하루 평균 수출액은 27억6000달러였다."
        items = [{"kind": "claim", "metric": "하루 평균 수출액", "value": "27억6000",
                  "unit": "달러", "period": "이 기간", "forecast": "N"}]
        claims, _, errors, _ = process_sentence(c, items, art,
                                                carry_anchor="2025-06-01~2025-06-20")
        assert not errors and claims[0].period == "2025-06-01~2025-06-20"
        assert claims[0].exclusion_code == "PARTIAL_PERIOD"
        # 캐리 앵커가 없으면 미해소(억지 추정 금지)
        claims2, _, _, _ = process_sentence(c, items, art)
        assert claims2[0].period == ""

    def test_duration_uses_article_anchor(self):
        # "지난 5년" 류는 기사 기준 시점(거리 무제한)을 종점으로 상속 — 실측 18건
        c = cand(text="식료품 물가는 지난 5년간 22.9% 올랐다.")
        art = "식료품 물가는 지난 5년간 22.9% 올랐다."
        items = [{"kind": "claim", "metric": "식료품 물가", "value": "22.9", "unit": "%",
                  "period": "지난 5년", "forecast": "N", "value_type": "change_rate"}]
        # 문장·근접 앵커가 없어도 기사 앵커가 있으면 해소
        claims, _, errors, traces = process_sentence(c, items, art, article_anchor="2025-09")
        assert not errors and claims[0].period == "2025-09"
        assert traces[0]["period_method"] == "duration_to_anchor"
        # 기사 앵커도 없으면 억지 추정하지 않는다
        claims2, _, _, _ = process_sentence(c, items, art)
        assert claims2[0].period == ""

    def test_no_cascading_anchor_shift(self):
        # 실측 버그: 앵커 항목이 직전 앵커의 이미 시프트된 값을 다시 앵커로 잡아
        # 한 문장의 3분기 3개가 2024·2023·2022-Q3로 흩어졌다 → 전부 1회만 시프트되어야
        c = cand(text="대기업 5.1%, 중견기업 7.0%, 중소기업 11.9% 모두 전년 동기 대비 3분기 수출이 늘었다.")
        art = "수출 통계 기사."
        base = {"kind": "claim", "unit": "%", "forecast": "N", "value_type": "change_rate"}
        items = [
            dict(base, metric="수출", value="5.1", period="3분기"),
            dict(base, metric="수출", value="7.0", period="전년 동기"),
            dict(base, metric="수출", value="11.9", period="전년 동기"),
        ]
        claims, _, errors, _ = process_sentence(c, items, art)
        assert not errors
        assert claims[0].period == "2025-Q3"
        assert claims[1].period == claims[2].period == "2024-Q3"   # 누적 시프트 없음

    def test_anchor_prefers_preceding_sibling(self):
        # 리뷰 반례: '1분기 100억, 2분기 200억, 전년 동기(150억)' — 직전(2분기) 기준이어야
        c = cand(text="1분기 100억원, 2분기 200억원으로 전년 동기 150억원보다 늘었다.")
        art = "영업이익 기사."
        items = [
            {"kind": "claim", "metric": "영업이익", "value": "100억", "unit": "원",
             "period": "1분기", "forecast": "N"},
            {"kind": "claim", "metric": "영업이익", "value": "200억", "unit": "원",
             "period": "2분기", "forecast": "N"},
            {"kind": "claim", "metric": "영업이익", "value": "150억", "unit": "원",
             "period": "전년 동기", "forecast": "N"},
        ]
        claims, _, errors, _ = process_sentence(c, items, art)
        assert not errors
        assert claims[2].period == "2024-Q2"   # 첫 값(2025-Q1)이 아니라 직전(2025-Q2) 기준

    def test_order_violation_flagged_not_destroyed(self):
        c = cand(text="수출은 300억달러, 수입은 200억달러였다.")
        art = "수출액 수입액 기사."
        items = [  # 원문 역순 출력
            {"kind": "claim", "metric": "수입", "value": "200억", "unit": "달러",
             "period": "올해", "forecast": "N"},
            {"kind": "claim", "metric": "수출", "value": "300억", "unit": "달러",
             "period": "올해", "forecast": "N"},
        ]
        claims, _, errors, traces = process_sentence(c, items, art)
        assert len(claims) == 2 and not errors   # 비파괴
        assert any("item_order_violation" in t["audit_flags"] for t in traces)


@pytest.mark.skipif(not (GOLDEN.exists() and DATA.exists()), reason="골든/데이터 없음")
class TestRunLevelBehaviors:
    def test_claim_id_slot_stability(self, tmp_path):
        # 한 item이 검증 실패로 빠져도 뒤 Claim의 id가 밀리지 않는다(§4.1 조인 키 보호)
        from src.p3_pipeline import run, make_golden_stub_extractor
        base = make_golden_stub_extractor(GOLDEN)

        def corrupting(cand):
            items = base(cand)
            if cand.key == ("Ae21581c3", "s002") and items:
                items = [dict(it) for it in items]
                items[0]["value"] = "999999"   # 첫 item만 역검증 실패 유도
            return items

        summary = run(corrupting, tmp_path)
        assert summary["errors"] == 1
        import json
        rows = [json.loads(l) for l in (tmp_path / "claims.jsonl").read_text(encoding="utf-8").splitlines()]
        ids = [r["claim_id"] for r in rows if r["claim_id"].startswith("Ae21581c3")]
        # 실패 슬롯(C00n)은 결번 — 골든 대비 같은 문장의 두 번째 item 이후 id가 동일해야 함
        gold_ids = {c.claim_id for c in __import__("src.p3_golden", fromlist=["load_golden"]).load_golden(GOLDEN).claims
                    if c.article_id == "Ae21581c3"}
        assert set(ids) == gold_ids - {sorted(gold_ids)[1]}  # C002 하나만 결번

    def test_circuit_breaker(self, tmp_path):
        from src.p3_pipeline import run
        from src.p3_emit import AccountingError

        def broken(cand):
            raise RuntimeError("HCX 죽음")

        with pytest.raises(AccountingError, match="서킷브레이커"):
            run(broken, tmp_path)


@pytest.mark.skipif(not GOLDEN.exists(), reason="골든 없음")
class TestStageD:
    def _normalizer(self):
        from src.p3_golden import load_golden
        from src.p3_stage_d import build_seed_entries, MetricNormalizer
        return MetricNormalizer(build_seed_entries(load_golden(GOLDEN)))

    def test_dictionary_has_no_active_entries(self):
        # 사전 폐지 검증 — 현재 파일에 활성(approved) 항목이 없어야 한다
        from src.p3_stage_d import load_dictionary, ACTIVE_STATUSES
        active = [e for e in load_dictionary() if e.get("status") in ACTIVE_STATUSES]
        assert active == [], f"미검증 항목이 활성화됨: {active[:3]}"

    def test_candidates_dumped_as_unverified(self):
        # 55차: 골든 매핑은 참고 후보일 뿐 — 전부 llm_unverified로 덤프되고 적용되지 않는다
        from src.p3_golden import load_golden
        from src.p3_stage_d import build_seed_entries
        entries = build_seed_entries(load_golden(GOLDEN))
        assert entries and all(e["status"] == "llm_unverified" for e in entries)
        assert all(e["normalized"] is None for e in entries)   # 적용 금지
        assert all(e["candidates"] for e in entries)           # 후보는 보존

    def test_unverified_never_applied(self):
        # 골든 유래 매핑(우럭→조피볼락 등 미검증 동의어)은 절대 치환되지 않는다
        n = self._normalizer()
        for m in ("국가 채무", "우럭 1kg당 도매 가격", "재정 적자", "미국 수출", "중국 수출"):
            assert n.normalize(m) == (None, "unverified"), m

    def test_approved_entry_applied(self):
        # 3번의 KOSIS 검색을 통과해 approved로 승격된 항목만 활성
        from src.p3_stage_d import MetricNormalizer
        n = MetricNormalizer([
            {"metric": "국가 채무", "normalized": "국가채무", "status": "approved"},
            {"metric": "우럭 1kg당 도매 가격", "normalized": "조피볼락 도매가격",
             "status": "llm_unverified"},
        ])
        assert n.normalize("국가 채무") == ("국가채무", "approved_hit")
        assert n.normalize("우럭 1kg당 도매 가격") == (None, "unverified")

    def test_verbatim_fallback_instead_of_null(self):
        # 사용자 결정: 검증된 표준명이 없으면 null이 아니라 verbatim metric 복사(의미 훼손 0)
        n = self._normalizer()
        c = mk(metric="듣도 보도 못한 지표")
        c.metric_normalized = ""
        stats = n.apply([c])
        assert c.metric_normalized == "듣도 보도 못한 지표"
        assert stats["fallback"] == 1 and stats["unverified"] == 1
        # 추출기 제공값은 우선
        c2 = mk(metric="국가 채무")
        c2.metric_normalized = "이미 있음"
        assert n.apply([c2])["already"] == 1 and c2.metric_normalized == "이미 있음"
        # 폴백을 끄면 종전대로 null
        c3 = mk(metric="듣도 보도 못한 지표")
        c3.metric_normalized = ""
        n.apply([c3], fallback_to_metric=False)
        assert c3.metric_normalized == ""

    def test_pipeline_fills_normalized_when_stub_omits(self, tmp_path):
        # 추출기가 normalized를 안 주는 상황(실 LLM 경로) — 사전이 비모호 전건을 채움
        from src.p3_pipeline import run, make_golden_stub_extractor
        base = make_golden_stub_extractor(GOLDEN)

        def stripped(cand):
            items = [dict(it) for it in base(cand)]
            for it in items:
                it.pop("metric_normalized", None)
            return items

        summary = run(stripped, tmp_path, normalizer=self._normalizer())
        import json
        rows = [json.loads(l) for l in (tmp_path / "claims.jsonl").read_text(encoding="utf-8").splitlines()]
        # 새 정책(verbatim 폴백): 계약 필드에 null이 남지 않는다
        assert [r["claim_id"] for r in rows if not r["metric_normalized"]] == []
        # 사전 폐지(55차) — 검증분이 없으므로 전건이 verbatim metric과 동일
        assert all(r["metric_normalized"] == r["metric"] for r in rows)

    def test_normalizer_autowired_without_dictionary_file(self, tmp_path, monkeypatch):
        """사전 파일이 없어도 Stage D는 돌아야 한다 — 신규 클론 환경(data/ 미포함) 회귀.

        존재 여부를 배선 조건으로 걸면 Stage D가 통째로 건너뛰어지고 계약 필드
        metric_normalized가 전건 null로 나간다(감수 지적).
        """
        import json
        from src import p3_stage_d
        from src.p3_pipeline import run, make_golden_stub_extractor

        monkeypatch.setattr(p3_stage_d, "DICTIONARY_DEFAULT", tmp_path / "없는사전.jsonl")
        base = make_golden_stub_extractor(GOLDEN)

        def stripped(cand):
            items = [dict(it) for it in base(cand)]
            for it in items:
                it.pop("metric_normalized", None)
            return items

        out = tmp_path / "out"
        summary = run(stripped, out)                      # normalizer 미주입 = 자동 배선
        assert summary["stage_d"] != "skipped"
        assert summary["dictionary_version"] is None      # 사전 없음은 버전 없음으로 기록
        rows = [json.loads(l) for l in (out / "claims.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows and all(r["metric_normalized"] == r["metric"] for r in rows)


@pytest.mark.skipif(not (GOLDEN.exists() and DATA.exists()), reason="골든/데이터 없음")
class TestE2EWithGoldenStub:
    """무HCX 스모크 ③ — stub Stage B(골든 라벨)로 A→E 전 구간 + 채점 자가 일치."""

    def test_full_pipeline(self, tmp_path):
        from src.p3_pipeline import run, make_golden_stub_extractor
        from src.p3_emit import load_documents_jsonl
        from src.p3_golden import load_golden
        from src.p3_eval import evaluate, FIELDS

        summary = run(make_golden_stub_extractor(GOLDEN), tmp_path)
        assert summary["numeric_sentences"] == 534
        assert summary["claims"] == 508 and summary["excluded"] == 299
        assert summary["errors"] == 0
        assert summary["eligible_true"] == 359

        # 산출물 재적재 → 골든 채점 = 완전 일치 (룰이 골든을 왕복 보존)
        pred = load_documents_jsonl(tmp_path / "claims_full.jsonl", tmp_path / "excluded.jsonl")
        gold = load_golden(GOLDEN)
        rep = evaluate(gold, pred)
        assert rep.f1 == 1.0 and rep.fn == 0 and rep.fp == 0
        for f in FIELDS:
            assert rep.acc(f) == 1.0, f"{f}: {rep.acc(f)}"
        assert not rep.missing_sentences and not rep.extra_sentences

        # 8필드 인수인계 파일 형태 검증(v0.4 — metric_normalized 승격)
        import json
        rows = [json.loads(l) for l in (tmp_path / "claims.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 508
        assert set(rows[0]) == {"claim_id", "claim", "metric", "metric_normalized",
                                "value", "unit", "period", "kosis_eligible"}
        assert all(r["metric_normalized"] for r in rows)   # 골든 stub 경로 — 전건 채움
        assert sum(1 for r in rows if r["kosis_eligible"]) == 359
        # 확장형 period는 null화됐는지(§4.8)
        assert all(r["period"] is None or "~" not in r["period"] for r in rows)

        # trace에 claim_id·해소 방법 병기
        tr = [json.loads(l) for l in (tmp_path / "claims_trace.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(tr) == 508 and all(t["claim_id"] and t["period_method"] for t in tr)
