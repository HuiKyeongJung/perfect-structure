# -*- coding: utf-8 -*-
"""P1 정제 테스트 — 규칙별 케이스 테이블 + 보존 인바리언트."""
import json

import pytest

from src.p1_clean import clean_text, find_prefix_end, find_suffix_start
from src.p1_clean import main as p1_main

TITLE = "취업자 수 13만 명 감소"


# ── 선두 규칙 ─────────────────────────────────────────────

class TestPrefix:
    def test_header_full(self):
        text = "정책 취업자 수 13만 명 감소 김기자 기자 입력 2025.06.23. 09:03 업데이트 2025.06.23. 17:40 0 본문이 시작된다."
        clean, spans = clean_text(text, TITLE)
        assert clean == "본문이 시작된다."
        assert spans[0]["rule"] == "prefix_header"

    def test_header_no_update_no_count(self):
        text = "사회 제목 반복 박기자 기자 입력 2025.10.20. 18:06 본문이 시작된다."
        clean, _ = clean_text(text, "제목 반복")
        assert clean == "본문이 시작된다."

    def test_header_count_not_eating_body_number(self):
        # 댓글수 없이 본문이 숫자로 시작 — '3분기'를 삼키면 안 된다
        text = "제목 김기자 기자 입력 2025.11.10. 12:00 3분기 수출이 역대 최대를 기록했다."
        clean, _ = clean_text(text, "제목")
        assert clean.startswith("3분기 수출이")

    def test_newsletter_header(self):
        text = "處 3개 늘리고 윤진우 기자 604호 2025.09.15 11:00 본문이 시작된다."
        clean, spans = clean_text(text, "處 3개 늘리고")
        assert clean == "본문이 시작된다."
        assert spans[0]["rule"] == "prefix_newsletter"

    def test_title_repeat_fallback(self):
        text = "정책 올해 쌀 생산, 작년보다 더 줄어… 통계에 따르면 생산량이 감소했다."
        clean, spans = clean_text(text, "올해 쌀 생산, 작년보다 더 줄어…")
        assert clean == "통계에 따르면 생산량이 감소했다."
        assert spans[0]["rule"] == "prefix_title_repeat"

    def test_title_repeat_multi_segment_keeps_subtitle(self):
        # 제목이 "헤드라인… 부제" 형태면 '…'까지만 절단 — 부제는 본문 리드로 남긴다
        text = "정책 올해 쌀 생산, 작년보다 더 줄어… 쌀 값 상승세 계속되나 쌀 생산량 0.3% 감소 전망 본문."
        clean, _ = clean_text(text, "올해 쌀 생산, 작년보다 더 줄어… 쌀 값 상승세 계속되나")
        assert clean.startswith("쌀 값 상승세 계속되나")

    def test_no_pattern_passthrough(self):
        text = "아무 노이즈도 없는 본문이다. 그대로 통과해야 한다."
        clean, spans = clean_text(text, TITLE)
        assert clean == text
        assert spans == []

    def test_late_timestamp_not_treated_as_header(self):
        # 본문 한참 뒤의 타임스탬프는 헤더가 아니다 (PREFIX_SEARCH_LIMIT)
        text = "본문. " * 400 + "입력 2025.01.01. 10:00 그 뒤 내용."
        assert find_prefix_end(text, TITLE) is None


# ── 말미 규칙 ─────────────────────────────────────────────

class TestSuffix:
    @pytest.mark.parametrize("tail,rule", [
        ("English 기사보기 김기자 기자 오늘의 핫뉴스", "english_button"),
        ("#반도체 #실적 반도체 더보기 다른 기사 제목", "hashtag_block"),
        ("김지섭 기자 2010년 조선일보에 입사해 사회부를 거쳤다.", "reporter_profile"),
        ("선정민 기자 staff writer, finance desk", "staff_writer"),
        ("코스피 코스닥 증권 100자평 도움말 삭제기준 AI 추천", "comment_widget"),
        ("100자평 도움말 삭제기준 By Taboola", "comment_widget"),
        ("Video Player is loading. Current Time 0:02", "video_player"),
        ("00:00 / 01:35 Technology and Trends", "video_time"),
        ("close Advertisements [custom_chain]", "ads_close"),
        ("당신이 좋아할 만한 콘텐츠 AD 높은 혈당", "recommend"),
        ("매일 조선일보에 실린 칼럼 5개가 담긴 뉴스레터를 받아보세요.", "newsletter_promo"),
    ])
    def test_anchor_families(self, tail, rule):
        body = "본문 첫 문장이다. 통계에 따르면 수치가 늘었다."
        clean, spans = clean_text(f"{body} {tail}", TITLE)
        assert clean == body
        assert any(sp["rule"] == rule for sp in spans)

    def test_body_entirely_ui_block_gives_empty(self):
        # [속보]류: 헤더 바로 뒤가 UI 블록이면 본문은 빈 문자열
        text = "정책 [속보] 제목 김기자 기자 입력 2025.09.07. 17:58 업데이트 2025.09.07. 18:33 English 기사보기 김기자 기자 오늘의 핫뉴스"
        clean, _ = clean_text(text, "[속보] 제목")
        assert clean == ""

    def test_hashtag_not_number(self):
        # '#1' 같은 순수 숫자는 해시태그 앵커가 아니다
        text = "빌보드 차트 #1 을 기록했다. 이는 최초의 성과다."
        clean, spans = clean_text(text, TITLE)
        assert clean == text

    def test_earliest_anchor_wins(self):
        body = "본문이다."
        text = f"{body} English 기사보기 김기자 기자 오늘의 핫뉴스 많이 본 뉴스"
        clean, spans = clean_text(text, TITLE)
        assert clean == body
        assert spans[-1]["rule"] == "english_button"


# ── 중간·꼬리 규칙 ────────────────────────────────────────

class TestMidAndTail:
    def test_mid_markers_preserved(self):
        # v4: 기사 고유 마커([칼럼 전문 링크]·<사진>·[편집자주])는 규칙화하지 않고 보존
        text = "첫 단락이다. [칼럼 전문 링크] 둘째 단락이다."
        clean, spans = clean_text(text, TITLE)
        assert clean == text
        assert spans == []

    def test_tail_keywords(self):
        text = "마지막 문장이 끝났다. 수박 배추 우럭 히트플레이션"
        clean, spans = clean_text(text, TITLE)
        assert clean == "마지막 문장이 끝났다."
        assert any(sp["rule"] == "tail_keywords" for sp in spans)

    def test_tail_reporter(self):
        text = "마지막 문장이 끝났다. 윤진우 기자"
        clean, _ = clean_text(text, TITLE)
        assert clean == "마지막 문장이 끝났다."

    def test_long_tail_kept(self):
        # 종결어미 없는 꼬리라도 길면 본문일 수 있다 — 지우지 않는다
        tail = "매우 " * 30 + "긴 무종결 구간"
        text = f"문장이 끝났다. {tail}"
        clean, _ = clean_text(text, TITLE)
        assert tail in clean


# ── 산출 파일 분리 (슬림 본문 / 트레이스 사이드카) ─────────

def test_main_writes_slim_and_trace(tmp_path):
    art = {
        "article_id": "Atest0001", "title": "제목", "posted_date": "2025-01-01",
        "url": "https://example.com/a",
        "text": "제목 김기자 기자 입력 2025.01.01. 10:00 본문이다. #태그 태그 더보기",
    }
    src_file = tmp_path / "in.jsonl"
    src_file.write_text(json.dumps(art, ensure_ascii=False) + "\n", encoding="utf-8")
    out, trace = tmp_path / "clean.jsonl", tmp_path / "clean_trace.jsonl"

    p1_main(["--input", str(src_file), "--output", str(out), "--trace", str(trace)])

    slim = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert set(slim.keys()) == {"article_id", "title", "posted_date", "url", "text"}  # 코어 5필드만
    assert slim["text"] == "본문이다."
    tr = json.loads(trace.read_text(encoding="utf-8").splitlines()[0])
    assert tr["article_id"] == "Atest0001"
    assert tr["pipeline_version"] and tr["removed_spans"]


# ── 보존 인바리언트 ───────────────────────────────────────

def test_reconstruction_invariant():
    text = ("정책 제목 김기자 기자 입력 2025.06.23. 09:03 업데이트 2025.06.23. 17:40 5 "
            "본문 첫 문장. [칼럼 전문 링크] 본문 둘째 문장이다. "
            "#태그 태그 더보기 추천 기사 제목")
    clean, spans = clean_text(text, "제목")  # clean_text 내부 assert가 보존 인바리언트 검사
    rebuilt = ""
    cursor = 0
    for sp in sorted(spans, key=lambda x: x["start"]):
        rebuilt += text[cursor:sp["start"]]
        cursor = sp["end"]
    rebuilt += text[cursor:]
    assert rebuilt.strip() == clean
