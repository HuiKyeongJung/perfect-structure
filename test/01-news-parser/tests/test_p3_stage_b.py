# -*- coding: utf-8 -*-
"""Stage B(캐시·파싱·수리 루프)·fake 클라이언트 테스트 — HCX 무호출."""
import json
from pathlib import Path

import pytest

from src.p3_cache import ReplayCache
from src.p3_stage_b import (parse_items, enum_deviations, build_user_message,
                            make_hcx_extractor, ITEM_KEYS)
from src.p3_stage_a import SentenceCandidate

GOOD = json.dumps([{"kind": "claim", "forecast": "N", "metric": "수출", "value": "8.3",
                    "unit": "%", "value_type": "change_rate", "direction": "increase",
                    "period": "올해", "exclusion_code": "", "comparison_basis": "", "note": ""}],
                  ensure_ascii=False)


def cand(sid="s001"):
    return SentenceCandidate(article_id="A", sent_id=sid, text="수출이 8.3% 늘었다.",
                             posted_date="2025-06-23", title="제목")


class FakeClient:
    """호출마다 준비된 응답을 차례로 반환 — 수리·재샘플 경로 재현용."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, system, messages, temperature=0.0, **kw):
        self.calls.append({"messages": messages, "temperature": temperature})
        return self.responses.pop(0)


class TestParse:
    def test_clean_and_fenced(self):
        assert parse_items(GOOD)[0] is not None
        fenced = f"```json\n{GOOD}\n```"
        items, _ = parse_items(fenced)
        assert items and set(items[0]) == set(ITEM_KEYS)

    def test_garbage(self):
        for bad in ("", "설명만 있는 응답", "[{broken json]"):
            items, err = parse_items(bad)
            assert items is None and err

    def test_hcx_double_brace_repaired(self):
        # 스파이크 실측 기형(50건 중 10건): 객체 닫힘 중복 '}}]' — 결정적 수리로 회수
        malformed = ('[{"kind":"claim","exclusion_code":"","forecast":"N","metric":"나랏빚",'
                     '"value":"126조","unit":"원","value_type":"level","direction":"",'
                     '"period":"올해","comparison_basis":"","note":"실측 기형 재현"}}]')
        items, err = parse_items(malformed)
        assert items is not None and items[0]["value"] == "126조"
        # 트레일링 콤마도 회수
        items2, _ = parse_items('[{"kind":"excluded","exclusion_code":"NON_STAT_NUMBER",}]')
        assert items2 is not None

    def test_unescaped_inner_quotes_scanned(self):
        # 스파이크 v1.1 잔여 기형: note 문자열 안의 비이스케이프 큰따옴표 — 관용 스캐너로 회수
        malformed = ('[{"kind":"claim","exclusion_code":"","forecast":"N","metric":"국가 채무",'
                     '"value":"1300조","unit":"원","value_type":"level","direction":"",'
                     '"period":"올해 말","comparison_basis":"",'
                     '"note":"뒤 문장 상속."1300조원대"는 앞 문장의 "1301조9000억원"과 동급."},'
                     '{"kind":"claim","exclusion_code":"","forecast":"N","metric":"국가 채무",'
                     '"value":"1100조","unit":"원","value_type":"level","direction":"",'
                     '"period":"2023~2024년","comparison_basis":"","note":"비교 과거"}}]')
        items, err = parse_items(malformed)
        assert items is not None and len(items) == 2
        assert items[0]["value"] == "1300조" and items[1]["period"] == "2023~2024년"
        assert '1300조원대' in items[0]["note"]   # 내부 따옴표 내용이 값 안에 보존

    def test_enum_deviations(self):
        items, _ = parse_items(GOOD)
        assert enum_deviations(items) == []
        items[0]["kind"] = "Claim"   # 대소문자 이탈
        items[0]["forecast"] = "YES"
        assert len(enum_deviations(items)) >= 1


class TestCache:
    def test_roundtrip_and_version_isolation(self, tmp_path):
        p = tmp_path / "replay.jsonl"
        c1 = ReplayCache(p, "extract_v1", "HCX-005")
        assert c1.get("payload") is None
        c1.put("payload", GOOD, chain=[{"attempt": "initial", "response": GOOD}])
        assert c1.get("payload")["response"] == GOOD
        # 재적재(디스크 왕복)
        c2 = ReplayCache(p, "extract_v1", "HCX-005")
        assert c2.get("payload")["chain"][0]["attempt"] == "initial"
        # 프롬프트 버전이 다르면 같은 payload도 미스(버전별 코퍼스 분리)
        c3 = ReplayCache(p, "extract_v2", "HCX-005")
        assert c3.get("payload") is None


class TestExtractorRetry:
    IDX = {"A": [{"article_id": "A", "sent_id": "s001", "text": "수출이 8.3% 늘었다."}]}

    def _extractor(self, tmp_path, responses):
        client = FakeClient(responses)
        cache = ReplayCache(tmp_path / "r.jsonl", "extract_v1", "HCX-005")
        sys_prompt = "규칙"
        return make_hcx_extractor(client, cache, sys_prompt, self.IDX), client, cache

    def test_success_first_try_and_replay(self, tmp_path):
        ex, client, cache = self._extractor(tmp_path, [GOOD])
        items = ex(cand())
        assert items[0]["value"] == "8.3" and len(client.calls) == 1
        # 두 번째 호출은 캐시 재생 — 클라이언트 무호출
        items2 = ex(cand())
        assert items2 == items and len(client.calls) == 1

    def test_repair_path(self, tmp_path):
        ex, client, cache = self._extractor(tmp_path, ["깨진 응답", GOOD])
        items = ex(cand())
        assert items[0]["value"] == "8.3"
        assert len(client.calls) == 2
        # 수리 대화에 오류 피드백 + 원 응답이 포함돼야 함(동일 프롬프트 재호출 금지)
        repair_msgs = client.calls[1]["messages"]
        assert any("문제가 있다" in m["content"] for m in repair_msgs if m["role"] == "user")
        assert cache.get(build_user_message(cand(), self.IDX))["chain"][1]["attempt"] == "repair"

    def test_resample_path_uses_higher_temp(self, tmp_path):
        ex, client, _ = self._extractor(tmp_path, ["깨짐1", "깨짐2", GOOD])
        items = ex(cand())
        assert items and len(client.calls) == 3
        assert client.calls[2]["temperature"] == 0.5   # temp 0 동일 재시도 금지(§5.6)

    def test_exhausted_raises(self, tmp_path):
        ex, client, _ = self._extractor(tmp_path, ["깨짐1", "깨짐2", "깨짐3"])
        with pytest.raises(RuntimeError, match="EXTRACTION_ERROR"):
            ex(cand())

    def test_empty_array_triggers_repair_not_cached(self, tmp_path):
        # 리뷰 high: 빈 배열이 성공으로 캐시되면 해당 문장이 영구 오류로 고착
        ex, client, cache = self._extractor(tmp_path, ["[]", GOOD])
        items = ex(cand())
        assert items and items[0]["value"] == "8.3"
        assert len(client.calls) == 2                      # 수리 대화 가동
        # 캐시에는 최종 성공 응답만 — 재실행 시 GOOD 재생
        items2 = ex(cand())
        assert items2 == items and len(client.calls) == 2

    def test_stale_empty_cache_hit_demoted_to_miss(self, tmp_path):
        # 과거 실행이 남긴 빈 배열 캐시 히트는 재검증에서 미스로 강등 → 재호출
        p = tmp_path / "r.jsonl"
        stale = ReplayCache(p, "extract_v1", "HCX-005")
        payload = build_user_message(cand(), self.IDX)
        stale.put(payload, "[]")
        ex, client, _ = self._extractor(tmp_path, [GOOD])
        items = ex(cand())
        assert items and len(client.calls) == 1            # 캐시 우회 재호출

    def test_kind_code_conflict_caught_at_gate(self):
        items, _ = parse_items(GOOD)
        items[0]["exclusion_code"] = "NON_STAT_NUMBER"     # kind=claim과 모순
        assert any("모순" in d for d in enum_deviations(items))


class TestUserMessage:
    def test_context_assembly(self):
        idx = {"A": [
            {"article_id": "A", "sent_id": "s001", "text": "리드 문장이다."},
            {"article_id": "A", "sent_id": "s002", "text": "이번 조사는 2월 24일~3월 21일 실시됐다."},
            {"article_id": "A", "sent_id": "s003", "text": "대상 문장 숫자 10개."},
            {"article_id": "A", "sent_id": "s004", "text": "뒤 문장이다."},
        ]}
        c = SentenceCandidate(article_id="A", sent_id="s003", text="대상 문장 숫자 10개.",
                              posted_date="2025-06-23", title="제목")
        msg = build_user_message(c, idx)
        assert "대상 문장: 대상 문장 숫자 10개." in msg
        assert "리드" in msg and "뒤 문장이다" in msg
        assert "시점 앵커 후보" in msg and "실시됐다" in msg   # 룰 선추출(§5.6)
