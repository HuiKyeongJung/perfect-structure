# -*- coding: utf-8 -*-
"""P2 정규식 스플리터 테스트 — 보호·경계 규칙 케이스 테이블 + 전수 보존 인바리언트."""
import pytest

from src.p2_split import split_spans, split_text


class TestBasic:
    def test_two_sentences(self):
        assert split_text("첫 문장이다. 둘째 문장이다.") == ["첫 문장이다.", "둘째 문장이다."]

    def test_question_exclaim(self):
        assert split_text("정말인가? 그렇다! 끝이다.") == ["정말인가?", "그렇다!", "끝이다."]

    def test_empty_and_whitespace(self):
        assert split_text("") == []
        assert split_text("   ") == []

    def test_single_no_terminal(self):
        assert split_text("종결부호 없는 한 덩어리") == ["종결부호 없는 한 덩어리"]


class TestProtections:
    def test_decimal_not_split(self):
        # 소수점은 뒤가 공백이 아니라 숫자 → 후보 자체가 안 됨
        assert split_text("물가가 1.0% 올랐다. 이는 0.3%p 상승이다.") == \
            ["물가가 1.0% 올랐다.", "이는 0.3%p 상승이다."]

    def test_paren_number(self):
        assert split_text("고용률은 작년 5월(-0.7%포인트)부터 감소했다. 다음 문장이다.") == \
            ["고용률은 작년 5월(-0.7%포인트)부터 감소했다.", "다음 문장이다."]

    def test_terminal_inside_paren_protected(self):
        assert split_text("그는 (정말이다. 진짜다) 라고 썼다.") == ["그는 (정말이다. 진짜다) 라고 썼다."]

    def test_multi_sentence_quote_protected(self):
        # 골든 실사례 유형: 인용 안의 종결은 자르지 않는다
        text = "“집행유예라 끝장이에요. 간절하게 써주세요. 네?” 그는 부탁했다."
        assert split_text(text) == ["“집행유예라 끝장이에요. 간절하게 써주세요. 네?”", "그는 부탁했다."]

    def test_quote_then_josa_not_split(self):
        # “…했다.”며 — 종결부호가 따옴표 안 + 닫은 뒤 공백 없음 → 경계 아님
        assert split_text("그는 “좋다.”며 웃었다. 끝이다.") == ["그는 “좋다.”며 웃었다.", "끝이다."]

    def test_straight_quotes_protected(self):
        assert split_text('"A다. B다." 그가 말했다.') == ['"A다. B다."', "그가 말했다."]

    def test_inverted_curly_quotes_protected(self):
        # 실데이터 사례: 닫는 따옴표를 여는 모양(“)으로 쓴 기사 — 토글 처리로 보호 유지
        text = "그는 ”싫다. 그만두겠다“고 했다. 다음 문장이다."
        assert split_text(text) == ["그는 ”싫다. 그만두겠다“고 했다.", "다음 문장이다."]


class TestSymbols:
    def test_heading_and_list_symbols(self):
        text = "본문이 끝났다. ◇소제목이다 ▲항목 하나 ▲항목 둘"
        assert split_text(text) == ["본문이 끝났다.", "◇소제목이다", "▲항목 하나", "▲항목 둘"]

    def test_marker_standalone(self):
        text = "앞 문장이다. [ 칼럼 전문 링크 ] ◇제목 문장"
        assert split_text(text) == ["앞 문장이다.", "[ 칼럼 전문 링크 ]", "◇제목 문장"]

    def test_glossary_symbol(self):
        text = "설명이 끝난다. ☞히트플레이션(Heatflation) 폭염과 인플레이션의 합성어다."
        out = split_text(text)
        assert out[0] == "설명이 끝난다."
        assert out[1].startswith("☞히트플레이션")

    def test_symbols_without_space(self):
        # 인사 발령 기사: 기호가 공백 없이 이어져도 절단 + ▷ 하위 항목 지원
        text = "▲과학기술정보통신부◇국장급 전보▷정책관 김민표▷협력관 최동원"
        assert split_text(text) == ["▲과학기술정보통신부", "◇국장급 전보", "▷정책관 김민표", "▷협력관 최동원"]

    def test_ellipsis_not_terminal(self):
        # 칼럼 제목 속 말줄임은 종결이 아니다 (…, ... 모두)
        assert split_text("반성문 대필 의뢰 좀 그만… 판사는 안 속아요") == \
            ["반성문 대필 의뢰 좀 그만… 판사는 안 속아요"]
        assert split_text("통계는 건축인데... 국가데이터처가 웬말인가") == \
            ["통계는 건축인데... 국가데이터처가 웬말인가"]


class TestKnownLimits:
    """설계상 알고 있는 한계 — 현재 동작을 명시해 두는 문서용 테스트."""

    def test_heading_end_merges_with_next(self):
        # 기호 소제목의 '끝'은 신호가 없어 다음 문장과 병합된다 (골든과 불일치 — 물량 측정 대상)
        text = "문장이 끝났다. ◇제목 새 문장이 시작된다."
        assert split_text(text) == ["문장이 끝났다.", "◇제목 새 문장이 시작된다."]

    def test_quote_final_question_overcut(self):
        # "…하나요?" 같은 — 인용이 끝나도 문장이 이어지는 드문 사례는 과잉 절단된다
        text = "“꼭 해야 하나요?” 같은 질문이 올라온다."
        assert split_text(text) == ["“꼭 해야 하나요?”", "같은 질문이 올라온다."]


class TestOffsets:
    def test_spans_match_text(self):
        text = "  첫 문장이다.  둘째 문장이다.  "
        spans = split_spans(text)
        assert [text[s:e] for s, e in spans] == ["첫 문장이다.", "둘째 문장이다."]
        for s, e in spans:
            assert not text[s].isspace() and not text[e - 1].isspace()

    @pytest.mark.parametrize("text", [
        "첫 문장이다. 둘째 문장이다.",
        "물가가 1.0% 올랐다. (주석이다. 그렇다) 끝났다. ◇소제목 ▲항목",
        "“A다. B다.” 그가 말했다. [ 칼럼 전문 링크 ] ◇제목",
    ])
    def test_preservation_invariant(self, text):
        joined = "".join(split_text(text))
        assert "".join(c for c in joined if not c.isspace()) == \
            "".join(c for c in text if not c.isspace())
