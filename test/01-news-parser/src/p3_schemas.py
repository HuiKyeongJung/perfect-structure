# -*- coding: utf-8 -*-
"""P3 Claim 추출 — 스키마·period 문법·eligible 파생·8필드 사영 (CLAUDE.md §5.6).

내부 표준 = 골든셋(claim_silver_set_ver2) 17필드. 공식 인수인계는 §4.1의 8필드 사영(v0.4 —
metric_normalized 포함, 50차 계약 변경).
- kosis_eligible = not(period가 표준 4형식이 아님 or forecast=Y)  — §4.8 파생식
- 사영 시 period는 표준 4형식만 통과, 확장형(부분기간·월범위·연범위)은 null화(원본은 full/trace 보존)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

PIPELINE_VERSION = "p3_v1"

# ── period 문법 ──────────────────────────────────────────────
# 표준 4형식 (KOSIS 조회 가능 · 7필드 계약 허용)
RE_PERIOD_STD = re.compile(r"^\d{4}(-(0[1-9]|1[0-2])|-Q[1-4]|-H[12])?$")
# 확장형 (내부 보존용 — 사영 시 null)
RE_MONTH_RANGE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])~\d{4}-(0[1-9]|1[0-2])$")
RE_YEAR_RANGE = re.compile(r"^\d{4}~\d{4}$")
_DAY = r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
RE_DAY_FORM = re.compile(rf"^{_DAY}(~{_DAY})?$")

# ── enum (계약·내부) ─────────────────────────────────────────
VALUE_TYPES = frozenset({"level", "change_rate", "change_amount", "share_ratio"})
DIRECTIONS = frozenset({"increase", "decrease"})
# 계약 제외 코드 — §5.3 현행 5종 전체. excluded 행 전용(PARTIAL_PERIOD는 제외가 아니라
# Claim 행의 eligible=false 신호이므로 여기 넣지 않는다 — §4.8)
CONTRACT_EXCLUSION_CODES = frozenset({
    "NON_STAT_NUMBER", "METAPHOR_COMPARISON", "AMBIGUOUS_METRIC",
    "RELATIVE_NO_BASE", "DUPLICATE",
})
# 내부 전용 코드 — errors.jsonl 소속. excluded.jsonl(계약 파일)에는 절대 넣지 않는다
INTERNAL_ERROR_CODE = "EXTRACTION_ERROR"
CLAIM_ALLOWED_CODES = frozenset({"", "PARTIAL_PERIOD"})  # kind=claim 행의 code 정합(§5.6 kind×code)


def is_std_period(period: str | None) -> bool:
    return bool(period) and bool(RE_PERIOD_STD.match(period.strip()))


def is_valid_period_form(period: str | None) -> bool:
    """내부 스키마에서 허용하는 period 표기 전체(빈값 포함).

    형식뿐 아니라 실재성도 본다 — 달력에 없는 날짜(2025-02-30)와
    역전 범위(시작>끝)는 반려(47차 "불가능 날짜 반려"의 완성).
    """
    if not period:
        return True
    p = period.strip()
    if RE_PERIOD_STD.match(p):
        return True
    if RE_MONTH_RANGE.match(p):
        a, b = p.split("~")
        return a <= b   # YYYY-MM 사전순 = 시간순
    if RE_YEAR_RANGE.match(p):
        a, b = p.split("~")
        return int(a) <= int(b)
    if RE_DAY_FORM.match(p):
        import datetime as _dt
        try:
            parts = [_dt.date.fromisoformat(x) for x in p.split("~")]
        except ValueError:
            return False   # 2025-02-30 등 달력 밖 날짜
        return len(parts) == 1 or parts[0] <= parts[1]
    return False


def derive_kosis_eligible(period: str | None, forecast: str | None) -> bool:
    """§4.8 파생식. forecast는 'Y'만 참으로 본다(빈값·'N'은 비전망)."""
    return is_std_period(period) and (forecast or "").strip().upper() != "Y"


# ── 레코드 ───────────────────────────────────────────────────
@dataclass
class ClaimRecord:
    """내부 표준 17필드 — 골든셋 스키마와 1:1."""

    claim_id: str
    article_id: str
    sent_id: str
    posted_date: str
    claim: str                      # 문장 원문 그대로 (재작성 금지)
    metric: str                     # verbatim — 구성 어휘가 기사에 실존해야 함
    metric_normalized: str = ""     # Stage D 산출(자유 합성 허용 열)
    value: str = ""                 # 기사 표기 그대로
    unit: str = ""
    value_type: str = ""            # VALUE_TYPES
    direction: str = ""             # DIRECTIONS
    period: str = ""                # period 문법 참조
    comparison_basis: str = ""
    forecast: str = "N"             # 'Y' | 'N'
    kosis_eligible: bool | None = None  # None이면 finalize()에서 파생
    exclusion_code: str = ""        # CLAIM_ALLOWED_CODES
    note: str = ""

    def finalize(self) -> "ClaimRecord":
        if self.kosis_eligible is None:
            self.kosis_eligible = derive_kosis_eligible(self.period, self.forecast)
        return self

    def to_handoff(self) -> dict:
        """§4.1 8필드 사영(v0.4) — 계약 파일(claims.jsonl) 행.

        v0.4(50차): `metric_normalized` 승격 — 2번의 확장 씨앗. 미정규화(사전 미스·미승인)는
        null로 나가고 2번은 verbatim metric으로 폴백한다.
        finalize()를 강제(멱등)해 eligible 미파생(None) 묵살을 차단하고,
        '기간 null ∧ eligible=true' 같은 §4.1 위반 조합은 예외로 막는다(무경고 계약 위반 생산 금지).
        """
        self.finalize()
        period_out = self.period if is_std_period(self.period) else None
        eligible = bool(self.kosis_eligible)
        if eligible and period_out is None:
            raise ValueError(
                f"{self.claim_id}: kosis_eligible=true인데 period가 비표준({self.period!r}) — §4.1 위반 조합"
            )
        return {
            "claim_id": self.claim_id,
            "claim": self.claim,
            "metric": self.metric,
            "metric_normalized": self.metric_normalized or None,
            "value": self.value,
            "unit": self.unit or None,
            "period": period_out,
            "kosis_eligible": eligible,
        }

    def schema_issues(self) -> list[str]:
        """형식 수준 검사(의미 검증은 Stage C 소관)."""
        issues = []
        if not self.claim_id:
            issues.append("claim_id 없음")
        if not self.claim:
            issues.append("claim(문장) 없음")
        if not self.metric:
            issues.append("metric 없음")
        if not self.value:
            issues.append("value 없음")
        if self.value_type and self.value_type not in VALUE_TYPES:
            issues.append(f"value_type 이탈: {self.value_type!r}")
        if self.direction and self.direction not in DIRECTIONS:
            issues.append(f"direction 이탈: {self.direction!r}")
        if (self.forecast or "").upper() not in ("Y", "N", ""):
            issues.append(f"forecast 이탈: {self.forecast!r}")
        if self.exclusion_code not in CLAIM_ALLOWED_CODES:
            issues.append(f"claim 행에 부적합한 code: {self.exclusion_code!r}")
        if not is_valid_period_form(self.period):
            issues.append(f"period 형식 이탈: {self.period!r}")
        if is_std_period(self.period) and self.exclusion_code == "PARTIAL_PERIOD":
            issues.append("표준형 period에 PARTIAL_PERIOD 마킹")
        if not is_std_period(self.period) and self.period and self.exclusion_code != "PARTIAL_PERIOD" \
                and not RE_MONTH_RANGE.match(self.period.strip()):
            # 월범위는 코드 불요(사용자 규약) — 그 외 확장형·연범위는 PARTIAL_PERIOD 필수
            issues.append(f"비표준 period인데 PARTIAL_PERIOD 아님: {self.period!r}")
        return issues

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExcludedRecord:
    """제외 대장 행 — kind=excluded 문장(계약 코드만)."""

    article_id: str
    sent_id: str
    sentence: str
    exclusion_code: str
    note: str = ""

    def schema_issues(self) -> list[str]:
        if self.exclusion_code not in CONTRACT_EXCLUSION_CODES:
            return [f"계약 밖 제외 코드: {self.exclusion_code!r}"]
        return []

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocumentSet:
    """한 실행분(또는 골든)의 Claim·제외 전체 + 메타."""

    claims: list[ClaimRecord] = field(default_factory=list)
    excluded: list[ExcludedRecord] = field(default_factory=list)
    version: str = ""               # 골든 파일 해시 또는 pipeline_version

    def sentence_keys(self) -> set[tuple[str, str]]:
        keys = {(c.article_id, c.sent_id) for c in self.claims}
        keys |= {(e.article_id, e.sent_id) for e in self.excluded}
        return keys
