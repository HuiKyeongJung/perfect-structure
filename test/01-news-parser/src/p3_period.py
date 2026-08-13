# -*- coding: utf-8 -*-
"""P3 Stage C — period 해소 룰 (§5.5 케이스 테이블 + §5.6 확장 문법).

분업 원칙: LLM(Stage B)은 시점의 표면형(period_expr)만 추출하고, 날짜 계산은 여기서
posted_date 기준으로 결정적으로 수행한다. 예외 2건(§5.6 명시): ① 비교기준 항목은
LLM이 앵커 시프트 표현("전년동기")을 출력 → 형제 Claim의 해소된 period에서 계산
② 당일 시세·현재 상태는 예약 토큰 AS_OF_POSTED → posted_date로 치환.

출력 계약: Resolved(period, partial, method)
- period: 표준 4형식 | 월범위 YYYY-MM~YYYY-MM | 확장형(일 단위·연범위) | None(해소 불가)
  잘못된 값(불가능 날짜·역전 범위)을 만드느니 None을 반환한다 — 억지 추정 금지(§5.5)
- partial: True면 exclusion_code=PARTIAL_PERIOD 마킹 대상(KOSIS 표준 주기 밖)
- method: 적용된 룰 이름(트레이스용)

당월 경계 해석(리뷰 확정):
- 무접두 "6월"(작성월=6월) → 올해 6월 — 한국어 기사에서 무접두 당월 월명은 거의 항상 당해
- "지난 6월"(작성월=6월) → 작년 6월 — '지난'은 과거 신호이고 당월 전용 표현('이달')이 따로 있음
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from src.p3_schemas import (RE_PERIOD_STD, RE_MONTH_RANGE, RE_YEAR_RANGE, RE_DAY_FORM,
                            is_valid_period_form)

AS_OF_POSTED = "AS_OF_POSTED"

# 앵커 시프트 표현(비교기준) — 형제 Claim의 period에서 -1년
ANCHOR_SHIFT_EXPRS = frozenset({
    "전년동기", "전년 동기", "전년동월", "전년 동월", "전년 같은 기간", "작년 같은 기간",
    "지난해 같은 기간", "지난해 동기", "1년 전", "일년 전", "전년 동기간", "작년 동기",
    "작년 이맘때",
})
VAGUE_EXPRS = ("최근", "요즘", "향후", "앞으로", "조만간", "당분간", "현재까지")

# 동일 기간 지시 표현 — 앵커를 시프트 없이 그대로 상속(문장 간 캐리 포함).
# dev 실측: period 불일치 35건 중 27건이 '이 기간'류 미해소였다(§5.1 원칙 ③ — 룰의 일).
SAME_PERIOD_EXPRS = frozenset({
    "이 기간", "이기간", "같은 기간", "동 기간", "해당 기간", "그 기간",
    "이 기간 동안", "같은 기간 동안", "이 시기", "같은 시기", "당시",
})
# 기간의 '길이'만 나타내는 표현 — 대상 시점이 아니다(골든은 구간 종점을 쓴다).
# 앵커가 있으면 그 시점을 쓰고, 없으면 미해소(억지 추정 금지). dev/test 실측 18건.
DURATION_EXPRS = re.compile(
    r"^(?:지난\s*)?\d+\s*(?:년|개월|달|주|일)\s*(?:간|새|동안|만에)$"   # 접미사 필수
    r"|^(?:지난\s*)?\d{1,2}\s*년$"                                      # 1~2자리 년 = 길이(4자리는 연도)
    r"|^(?:일주일|한\s*달|반년|수년)\s*(?:새|만에|동안)?$")
# 일·주 단위 길이 — 종점이 작성일 근방일 수밖에 없어, 앵커가 없으면 작성일을 종점으로 쓴다.
# 결과가 항상 일 단위(PARTIAL_PERIOD → eligible=false)라 오조회 위험이 없다는 것이 근거다.
# 연·개월 단위는 표준형(eligible=true)이 될 수 있으므로 이 폴백을 주지 않는다(억지 추정 금지).
DAY_SCALE_DURATION = re.compile(r"^(?:지난\s*)?\d+\s*(?:주|일)\s*(?:간|새|동안|만에)$"
                                r"|^일주일\s*(?:새|만에|동안)?$")
# 작성 시점의 현재 상태 — AS_OF_POSTED와 같은 뜻의 자연어 표면형(§5.6 예약 토큰 ②)
NOW_EXPRS = frozenset({"지금", "현재", "이날", "오늘", "현시점", "현 시점", "지금까지"})
# 형제 시점 기준의 상대 표현 — 문장에 명시 시점이 있으면 그 기준으로 -1 (작성일 기준 아님)
PREV_OF_SIBLING = frozenset({"전월", "전달", "전분기", "전기"})


@dataclass
class Resolved:
    period: str | None
    partial: bool = False
    method: str = ""


def _ymd(posted_date: str) -> dt.date:
    return dt.date.fromisoformat(posted_date)


def shift_year(period: str, delta: int) -> str:
    """해소된 period의 연도 성분을 delta만큼 이동(전년동기 계산)."""
    def rep(m: re.Match) -> str:
        return str(int(m.group(0)) + delta)
    shifted = re.sub(r"(?<!\d)\d{4}(?!\d)", rep, period)
    shifted = re.sub(r"(?<=-02-)29", "28", shifted)  # 윤일 보정
    return shifted


def _year_for_month(month: int, base: dt.date, past_signal: bool) -> int:
    """월의 연도 해소. past_signal=True('지난')면 당월도 작년, 아니면 당월은 올해."""
    if past_signal:
        return base.year if month < base.month else base.year - 1
    return base.year if month <= base.month else base.year - 1


def _resolve_month_token(tok_year: str | None, month: int, base: dt.date) -> int:
    if tok_year in ("지난해", "작년", "전년"):
        return base.year - 1
    if tok_year in ("올해", "금년", "올"):
        return base.year
    if tok_year == "내년":
        return base.year + 1
    return _year_for_month(month, base, past_signal=False)


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _unwrap_parens(e: str) -> str:
    """괄호 부연 처리 — 일 단위 정보가 든 괄호("6월(1~20일)")는 지우지 말고 풀어낸다."""
    def rep(m: re.Match) -> str:
        inner = m.group(1)
        return f" {inner} " if re.search(r"\d\s*일", inner) else ""
    return re.sub(r"\(([^)]*)\)", rep, e)


def resolve_period(expr: str | None, posted_date: str, anchor: str | None = None) -> Resolved:
    """period_expr(표면형) → 절대 시점. 이미 해소된 형식은 검증 후 통과(골든 패스스루 대칭)."""
    if not expr or not expr.strip():
        return Resolved(None, False, "empty")
    e = re.sub(r"\s+", " ", _unwrap_parens(expr)).strip()
    base = _ymd(posted_date)
    Y, M, D = base.year, base.month, base.day

    # 0) 이미 해소된 표기 — 형식·달력 검증 후 통과
    if RE_PERIOD_STD.match(e):
        return Resolved(e, False, "canonical")
    if RE_MONTH_RANGE.match(e):
        return (Resolved(e, False, "canonical_month_range") if is_valid_period_form(e)
                else Resolved(None, False, "reversed_range"))
    if RE_YEAR_RANGE.match(e):
        return (Resolved(e, True, "canonical_year_range") if is_valid_period_form(e)
                else Resolved(None, False, "reversed_range"))
    if RE_DAY_FORM.match(e):
        return (Resolved(e, True, "canonical_day") if is_valid_period_form(e)
                else Resolved(None, False, "invalid_date"))

    # 1) 예약 토큰·앵커 시프트 (§5.6 — LLM 계산 금지 원칙의 명시적 예외 2건)
    if e == AS_OF_POSTED or e in NOW_EXPRS:
        return Resolved(base.isoformat(), True, "as_of_posted")
    # 1-b) 형제 기준 상대 표현 — 문장에 명시 시점이 있으면 그 기준으로 -1(작성일 기준 아님).
    #      실측: "6월 수출은 … 전월(4.8%) 대비"에서 전월은 5월인데 작성일(7월) 기준이면 6월이 된다.
    if e in PREV_OF_SIBLING and anchor and is_valid_period_form(anchor):
        m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", anchor)
        if m and e in ("전월", "전달"):
            y, mo = int(m.group(1)), int(m.group(2))
            y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
            return Resolved(f"{y}-{mo:02d}", False, "sibling_prev_month")
        q = re.fullmatch(r"(\d{4})-Q([1-4])", anchor)
        if q and e in ("전분기", "전기"):
            y, qq = int(q.group(1)), int(q.group(2))
            y, qq = (y, qq - 1) if qq > 1 else (y - 1, 4)
            return Resolved(f"{y}-Q{qq}", False, "sibling_prev_quarter")

    if e in ANCHOR_SHIFT_EXPRS or e in SAME_PERIOD_EXPRS:
        # 앵커는 '해소된 period'여야 한다 — 표면형이 들어오면 무시프트 통과로
        # 전년동기가 올해 값이 되는 오류(리뷰 실측)를 차단
        if not anchor:
            return Resolved(None, False, "anchor_missing")
        if not re.search(r"\d{4}", anchor) or not is_valid_period_form(anchor):
            return Resolved(None, False, "anchor_invalid")
        if e in SAME_PERIOD_EXPRS:                       # 시프트 0 — 그대로 상속
            r = resolve_period(anchor, posted_date)
            return Resolved(r.period, r.partial, "same_period")
        shifted = shift_year(anchor, -1)
        r = resolve_period(shifted, posted_date)
        return Resolved(r.period, r.partial, "anchor_shift")

    # 2) 모호·미래 — 억지 추정 금지(§5.5)
    if any(e.startswith(v) or e == v for v in VAGUE_EXPRS):
        return Resolved(None, False, "vague")

    # 2-b) 기간 길이 표현("지난 5년"·"일주일 새") — 대상 시점은 구간의 종점이다.
    #      앵커(문장·기사 기준 시점)가 있으면 그것을 쓰고, 없으면 미해소.
    if DURATION_EXPRS.match(e):
        if anchor and is_valid_period_form(anchor):
            r = resolve_period(anchor, posted_date)
            return Resolved(r.period, r.partial, "duration_to_anchor")
        if DAY_SCALE_DURATION.match(e):
            return Resolved(base.isoformat(), True, "day_duration_to_posted")
        return Resolved(None, False, "duration_no_anchor")

    # 3) 연범위: "2003~2021년"
    m = re.fullmatch(r"(\d{4})\s*[~∼–-]\s*(\d{4})년?", e)
    if m:
        y1, y2 = int(m.group(1)), int(m.group(2))
        if y1 > y2:
            return Resolved(None, False, "reversed_range")
        return Resolved(f"{y1}~{y2}", True, "year_range")

    # 4) 절대 표기
    m = re.fullmatch(r"(\d{4})년\s*(\d{1,2})월", e)
    if m:
        mo = int(m.group(2))
        if not 1 <= mo <= 12:
            return Resolved(None, False, "invalid_date")
        return Resolved(f"{m.group(1)}-{mo:02d}", False, "absolute_ym")
    m = re.fullmatch(r"(\d{4})년\s*([1-4])분기", e)
    if m:
        return Resolved(f"{m.group(1)}-Q{m.group(2)}", False, "absolute_quarter")
    m = re.fullmatch(r"(\d{4})년\s*(상|하)반기", e)
    if m:
        return Resolved(f"{m.group(1)}-H{1 if m.group(2) == '상' else 2}", False, "absolute_half")
    m = re.fullmatch(r"(\d{4})년(?:\s*말|\s*초|\s*연말|\s*연초)?", e)
    if m:
        return Resolved(m.group(1), False, "absolute_year")
    # 절대연 월범위: "2024년 10월~2025년 3월" / "…부터 …까지"
    m = re.fullmatch(r"(\d{4})년\s*(\d{1,2})월\s*(?:[~∼–-]|부터)\s*(\d{4})년\s*(\d{1,2})월(?:까지)?", e)
    if m:
        y1, m1, y2, m2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        if not (1 <= m1 <= 12 and 1 <= m2 <= 12) or (y1, m1) > (y2, m2):
            return Resolved(None, False, "invalid_date")
        return Resolved(f"{y1}-{m1:02d}~{y2}-{m2:02d}", False, "absolute_month_range")

    # 5) 부분기간 — "올 들어 지난 20일까지" / "이달(지난) 1~20일" / "6월 1~20일"
    m = re.fullmatch(r"올\s*들어\s*(?:지난\s*)?(\d{1,2})일까지", e)
    if m:
        day = int(m.group(1))
        em, ey = (M, Y) if day <= D else ((M - 1, Y) if M > 1 else (12, Y - 1))  # 미래 종료일 방지
        if not _safe_date(ey, em, day):
            return Resolved(None, False, "invalid_date")
        return Resolved(f"{Y}-01-01~{ey}-{em:02d}-{day:02d}", True, "ytd_until_day")
    m = re.fullmatch(r"(?:(이달|이번 ?달|지난)\s*)?(?:(\d{1,2})월\s*)?(\d{1,2})\s*[~∼–-]\s*(\d{1,2})일", e)
    if m:
        month = int(m.group(2)) if m.group(2) else M
        if not 1 <= month <= 12:
            return Resolved(None, False, "invalid_date")
        year = _year_for_month(month, base, past_signal=False) if m.group(2) else Y
        d1, d2 = int(m.group(3)), int(m.group(4))
        if d1 > d2 or not (_safe_date(year, month, d1) and _safe_date(year, month, d2)):
            return Resolved(None, False, "invalid_date")
        return Resolved(f"{year}-{month:02d}-{d1:02d}~{year}-{month:02d}-{d2:02d}", True, "day_range")

    # 6) 월범위 — "작년 10월~올해 3월" / "3월부터 11월까지" / "1~5월"
    m = re.fullmatch(
        r"(?:(지난해|작년|전년|올해|금년|내년)\s*)?(\d{1,2})월\s*(?:[~∼–-]|부터)\s*"
        r"(?:(지난해|작년|전년|올해|금년|내년)\s*)?(\d{1,2})월(?:까지)?", e)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(4)) <= 12:
        m1, m2 = int(m.group(2)), int(m.group(4))
        y1 = _resolve_month_token(m.group(1), m1, base)
        if m.group(3):
            y2 = _resolve_month_token(m.group(3), m2, base)
        else:
            y2 = y1 if m2 >= m1 else y1 + 1   # 무한정어 뒤 토큰은 순방향(역전 방지 — 리뷰 실측)
        if (y1, m1) > (y2, m2):
            return Resolved(None, False, "reversed_range")
        return Resolved(f"{y1}-{m1:02d}~{y2}-{m2:02d}", False, "month_range")
    m = re.fullmatch(r"(?:(지난해|작년|전년|올해|금년|올|지난)\s*)?(\d{1,2})\s*[~∼–-]\s*(\d{1,2})월", e)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 12:   # "1~5월" 누계
        m1, m2 = int(m.group(2)), int(m.group(3))
        y1 = Y - 1 if m.group(1) in ("지난해", "작년", "전년") else Y
        y2 = y1 if m2 >= m1 else y1 + 1
        return Resolved(f"{y1}-{m1:02d}~{y2}-{m2:02d}", False, "compact_month_range")

    # 7) 특정일 — "지난 11일" / "11일" / "6월 19일" (불가능 날짜는 가장 가까운 과거 달로 후퇴)
    m = re.fullmatch(r"(?:지난\s*)?(?:(\d{1,2})월\s*)?(\d{1,2})일", e)
    if m:
        day = int(m.group(2))
        if m.group(1):
            month = int(m.group(1))
            if not 1 <= month <= 12:
                return Resolved(None, False, "invalid_date")
            year = _year_for_month(month, base, past_signal=False)
            if not _safe_date(year, month, day):
                return Resolved(None, False, "invalid_date")
        else:
            month, year = (M, Y) if day <= D else ((M - 1, Y) if M > 1 else (12, Y - 1))
            steps = 0
            while not _safe_date(year, month, day) and steps < 12:   # 6/31 → 5/31 후퇴
                month, year = (month - 1, year) if month > 1 else (12, year - 1)
                steps += 1
            if not _safe_date(year, month, day):
                return Resolved(None, False, "invalid_date")
        return Resolved(f"{year}-{month:02d}-{day:02d}", True, "single_day")

    # 8) 분기·반기 (연도 미명시 → 작성일 연도)
    m = re.fullmatch(r"(?:(지난해|작년|전년|올해|금년|올)\s*)?([1-4])분기", e)
    if m:
        year = Y - 1 if m.group(1) in ("지난해", "작년", "전년") else Y
        return Resolved(f"{year}-Q{m.group(2)}", False, "quarter")
    m = re.fullmatch(r"(?:(지난해|작년|전년|올해|금년|올)\s*)?(상|하)반기", e)
    if m:
        year = Y - 1 if m.group(1) in ("지난해", "작년", "전년") else Y
        return Resolved(f"{year}-H{1 if m.group(2) == '상' else 2}", False, "half")

    # 9) 상대 연·월 (§5.5 표)
    if e in ("올해", "금년", "올해 들어", "올 들어"):
        return Resolved(str(Y), False, "this_year")
    if e in ("지난해", "작년", "전년", "지난 해"):
        return Resolved(str(Y - 1), False, "last_year")
    if e == "재작년":
        return Resolved(str(Y - 2), False, "year_before_last")
    if e in ("내년", "내년도"):
        return Resolved(str(Y + 1), False, "next_year")
    if e in ("이달", "이번 달", "이번달"):
        return Resolved(f"{Y}-{M:02d}", False, "this_month")
    if e in ("지난달", "전월", "전달", "지난 달"):
        y, mo = (Y, M - 1) if M > 1 else (Y - 1, 12)   # 연 경계(§5.5 필수 케이스)
        return Resolved(f"{y}-{mo:02d}", False, "last_month")
    if e in ("연말", "올해 말", "올 연말", "연초", "올해 초"):
        return Resolved(str(Y), False, "year_end_as_year")      # §5.5: 연 단위로만
    if e in ("작년 말", "지난해 말", "작년 초", "지난해 초"):
        return Resolved(str(Y - 1), False, "year_end_as_year")

    # 10) 월 단독 표기 — "지난해 1월" 합성 / "지난 1월" / 무접두 "1월"
    m = re.fullmatch(r"(지난해|작년|전년|올해|금년|올|내년)\s*(\d{1,2})월", e)
    if m and 1 <= int(m.group(2)) <= 12:
        year = _resolve_month_token(m.group(1), int(m.group(2)), base)
        return Resolved(f"{year}-{int(m.group(2)):02d}", False, "qualified_month")
    m = re.fullmatch(r"지난\s*(\d{1,2})월", e)
    if m and 1 <= int(m.group(1)) <= 12:
        year = _year_for_month(int(m.group(1)), base, past_signal=True)
        return Resolved(f"{year}-{int(m.group(1)):02d}", False, "past_month")
    m = re.fullmatch(r"(\d{1,2})월", e)
    if m and 1 <= int(m.group(1)) <= 12:
        year = _year_for_month(int(m.group(1)), base, past_signal=False)   # 당월 → 올해
        return Resolved(f"{year}-{int(m.group(1)):02d}", False, "bare_month")

    return Resolved(None, False, "unresolved")
