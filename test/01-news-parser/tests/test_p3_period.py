# -*- coding: utf-8 -*-
"""period resolver 테스트 — §5.5 케이스 테이블(작성일 파라미터화) + §5.6 확장 문법."""
import pytest

from src.p3_period import resolve_period, shift_year, Resolved

P = "2025-06-23"  # §5.5 예시 작성일


# §5.5 룰 표 — (표면형, 기대 period, 기대 partial)
CASES_5_5 = [
    ("2025년", "2025", False),
    ("2025년 1월", "2025-01", False),
    ("올해", "2025", False),
    ("금년", "2025", False),
    ("지난해", "2024", False),
    ("작년", "2024", False),
    ("재작년", "2023", False),
    ("이달", "2025-06", False),
    ("이번 달", "2025-06", False),
    ("지난달", "2025-05", False),
    ("전월", "2025-05", False),
    ("지난 1월", "2025-01", False),      # 이미 지난 달 → 올해
    ("지난 7월", "2024-07", False),      # 아직 안 온 달 → 작년
    ("지난해 1월", "2024-01", False),
    ("1분기", "2025-Q1", False),
    ("3분기", "2025-Q3", False),
    ("상반기", "2025-H1", False),
    ("하반기", "2025-H2", False),
    ("지난해 4분기", "2024-Q4", False),
    ("연말", "2025", False),             # 연 단위로만(§5.5)
    ("올해 말", "2025", False),
    ("작년 말", "2024", False),
    ("최근", None, False),
    ("향후", None, False),
]


class TestSection55Table:
    @pytest.mark.parametrize("expr,period,partial", CASES_5_5)
    def test_case(self, expr, period, partial):
        r = resolve_period(expr, P)
        assert (r.period, r.partial) == (period, partial), f"{expr}: {r}"

    def test_year_boundary_last_month(self):
        # 연 경계(§5.5 필수 케이스): 1월 기사의 지난달 → 전년 12월
        r = resolve_period("지난달", "2025-01-10")
        assert r.period == "2024-12"

    def test_bare_quarter_uses_posted_year(self):
        assert resolve_period("2분기", "2024-11-01").period == "2024-Q2"


class TestCurrentMonthBoundary:
    """당월 경계 해석(리뷰 확정) — 무접두는 올해, '지난' 접두는 작년."""

    def test_bare_current_month_is_this_year(self):
        # "6월 소비자심리지수는…" (6월 작성) — 전년 오해소 시 eligible=true로 계약 도달하던 결함
        r = resolve_period("6월", "2025-06-25")
        assert (r.period, r.partial) == ("2025-06", False)

    def test_past_prefixed_current_month_is_last_year(self):
        assert resolve_period("지난 6월", "2025-06-23").period == "2024-06"

    def test_paren_day_info_preserved(self):
        # "6월(1~20일)" — 괄호 소거로 전년 6월 전체(eligible=true)가 되던 결함
        r = resolve_period("6월(1~20일)", "2025-06-23")
        assert (r.period, r.partial) == ("2025-06-01~2025-06-20", True)


class TestExtendedGrammar:
    def test_partial_day_range_with_month(self):
        r = resolve_period("6월 1~20일", P)
        assert (r.period, r.partial) == ("2025-06-01~2025-06-20", True)

    def test_partial_day_range_without_month(self):
        r = resolve_period("지난 1~20일", P)
        assert (r.period, r.partial) == ("2025-06-01~2025-06-20", True)

    def test_ytd_until_day(self):
        r = resolve_period("올 들어 지난 20일까지", P)
        assert (r.period, r.partial) == ("2025-01-01~2025-06-20", True)

    def test_single_day(self):
        r = resolve_period("지난 11일", "2025-07-14")
        assert (r.period, r.partial) == ("2025-07-11", True)
        # 아직 안 온 일자 → 전월
        r2 = resolve_period("지난 25일", "2025-07-14")
        assert r2.period == "2025-06-25"

    def test_month_range(self):
        r = resolve_period("작년 10월~올해 3월", P)
        assert (r.period, r.partial) == ("2024-10~2025-03", False)  # 월범위는 코드 불요

    def test_year_range(self):
        r = resolve_period("2003~2021년", P)
        assert (r.period, r.partial) == ("2003~2021", True)

    def test_paren_stripped(self):
        assert resolve_period("1분기(1~3월)", P).period == "2025-Q1"

    def test_day_range_year_resolution(self):
        # 명시 월의 연도도 nearest-past — 12월 범위를 6개월 미래로 만들던 결함
        r = resolve_period("지난 12월 1~20일", P)
        assert r.period == "2024-12-01~2024-12-20"
        r2 = resolve_period("12월 1~20일", "2026-01-05")   # 1월 기사의 전년 12월 인용
        assert r2.period == "2025-12-01~2025-12-20"

    def test_ytd_end_not_in_future(self):
        # 작성일(7/3)보다 미래의 종료일(7/20)을 만들던 결함 — 전월로 후퇴
        r = resolve_period("올 들어 지난 20일까지", "2025-07-03")
        assert r.period == "2025-01-01~2025-06-20"

    def test_impossible_day_steps_back(self):
        # 6월 31일은 없음 → 실재하는 가장 가까운 과거 달(5/31)로 후퇴
        assert resolve_period("지난 31일", "2025-07-01").period == "2025-05-31"
        assert resolve_period("지난 29일", "2025-03-01").period == "2025-01-29"  # 평년 2월 건너뜀

    def test_invalid_dates_return_none(self):
        assert resolve_period("13월 5일", P).period is None
        assert resolve_period("6월 1~40일", P).period is None
        assert resolve_period("2025-02-30", P).period is None   # 캐노니컬도 달력 검증

    def test_month_range_no_reversal(self):
        # 무한정어 교차 범위가 역전(2025-03~2024-11)되던 결함 — 순방향 보정
        assert resolve_period("3월부터 11월까지", P).period == "2025-03~2025-11"
        assert resolve_period("12월~2월", "2025-02-10").period == "2024-12~2025-02"

    def test_compact_month_range(self):
        assert resolve_period("1~5월", P).period == "2025-01~2025-05"

    def test_absolute_month_range(self):
        assert resolve_period("2024년 10월~2025년 3월", P).period == "2024-10~2025-03"

    def test_coverage_additions(self):
        assert resolve_period("내년", P).period == "2026"
        assert resolve_period("올 상반기", P).period == "2025-H1"
        assert resolve_period("전년 4분기", P).period == "2024-Q4"

    def test_duration_expressions(self):
        # 기간 '길이'는 대상 시점이 아니다 — 앵커가 있으면 그 종점, 없으면 미해소
        assert resolve_period("지난 5년", P).period is None
        assert resolve_period("지난 5년", P, anchor="2025-09").period == "2025-09"
        assert resolve_period("일주일 새", P, anchor="2025-07-11").period == "2025-07-11"
        # 연도·특정일은 duration이 아니다(과잉 매칭 방지)
        assert resolve_period("2025년", P).period == "2025"
        assert resolve_period("지난 11일", "2025-07-14").period == "2025-07-11"

    def test_sibling_prev_month(self):
        # "6월 수출은 … 전월(4.8%) 대비" — 전월은 문장 기준월(6월)의 앞달이지 작성일 기준이 아니다
        r = resolve_period("전월", "2025-07-13", anchor="2025-06")
        assert (r.period, r.method) == ("2025-05", "sibling_prev_month")
        r2 = resolve_period("전분기", P, anchor="2025-Q3")
        assert r2.period == "2025-Q2"
        # 앵커가 없으면 §5.5 기본 룰(작성일 기준)
        assert resolve_period("전월", "2025-07-13").period == "2025-06"

    def test_resolver_grammar_gaps(self):
        # test 실측에서 unresolved로 죽던 정당한 표면형들
        assert resolve_period("올 6월", P).period == "2025-06"        # '올' 접두 누락이었음
        assert resolve_period("내년도", P).period == "2026"
        assert resolve_period("지난해 1~5월", P).period == "2024-01~2024-05"
        assert resolve_period("올 1~5월", P).period == "2025-01~2025-05"


class TestSpecialTokens:
    def test_as_of_posted(self):
        r = resolve_period("AS_OF_POSTED", "2025-07-11")
        assert (r.period, r.partial, r.method) == ("2025-07-11", True, "as_of_posted")

    def test_anchor_shift_std(self):
        r = resolve_period("전년 동기", P, anchor="2025-Q1")
        assert (r.period, r.method) == ("2024-Q1", "anchor_shift")

    def test_anchor_shift_day_range_partial(self):
        r = resolve_period("전년동기", P, anchor="2025-06-01~2025-06-20")
        assert (r.period, r.partial) == ("2024-06-01~2024-06-20", True)

    def test_anchor_missing(self):
        r = resolve_period("전년 동기", P)
        assert r.period is None and r.method == "anchor_missing"

    def test_same_period_inherits_anchor(self):
        # dev 실측 최빈 오류: "이 기간"류가 미해소로 죽던 것 — 시프트 0 상속
        r = resolve_period("이 기간", P, anchor="2025-06-01~2025-06-20")
        assert (r.period, r.partial, r.method) == ("2025-06-01~2025-06-20", True, "same_period")
        r2 = resolve_period("같은 기간", P, anchor="2025-Q1")
        assert (r2.period, r2.method) == ("2025-Q1", "same_period")
        # 앵커가 없으면 만들어내지 않는다
        assert resolve_period("이 기간", P).period is None

    def test_anchor_must_be_resolved_period(self):
        # 표면형 앵커("1~20일")가 무시프트로 올해 값이 되던 결함 — 형식 검증으로 차단
        r = resolve_period("전년동기", P, anchor="1~20일")
        assert r.period is None and r.method == "anchor_invalid"
        r2 = resolve_period("지난해 같은 기간", P, anchor="2024-10~2025-03")
        assert r2.period == "2023-10~2024-03"


class TestPassthroughForms:
    @pytest.mark.parametrize("form,partial", [
        ("2025", False), ("2025-05", False), ("2025-Q1", False), ("2025-H2", False),
        ("2024-10~2025-03", False), ("2003~2021", True),
        ("2025-07-11", True), ("2025-06-01~2025-06-20", True),
    ])
    def test_canonical_passthrough(self, form, partial):
        # 골든 패스스루 대칭: 이미 해소된 값은 그대로 + partial 판정만
        r = resolve_period(form, P)
        assert (r.period, r.partial) == (form, partial)


class TestShiftYear:
    @pytest.mark.parametrize("period,expect", [
        ("2025", "2024"), ("2025-06", "2024-06"), ("2025-Q1", "2024-Q1"),
        ("2025-H1", "2024-H1"), ("2024-10~2025-03", "2023-10~2024-03"),
        ("2025-06-01~2025-06-20", "2024-06-01~2024-06-20"),
    ])
    def test_shift(self, period, expect):
        assert shift_year(period, -1) == expect

    def test_leap_day_clamp(self):
        assert shift_year("2024-02-29", -1) == "2023-02-28"


class TestUnresolved:
    def test_garbage(self):
        assert resolve_period("문재인 정부 첫해", P).period is None
        assert resolve_period("", P).period is None
        assert resolve_period(None, P).period is None
