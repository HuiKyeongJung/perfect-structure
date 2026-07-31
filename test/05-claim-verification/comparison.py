"""판정 확정 모듈 — 파이프라인의 마지막 단계(사용자 노출 직전).

같은 폴더의 "판정 로직 설계 노트.md"의 스펙을 그대로 구현한다. 이 모듈은
검색(retrieval)도 해석(resolution)도 하지 않는다 - 그건 이전 단계
(2주차 챗봇에서 검증한 resolve_target_table/resolve_target_item/
_resolve_table_with_verification 계열 로직, 종합 프로젝트에서는 Backend
6단계에 해당)의 책임이다. 이 모듈이 받는 건 이미 확정되었거나 확정
시도가 끝난 결과물뿐이고, 역할은 순수하게 "이 claim과 이 값을 놓고
VERIFIED/MISMATCH/UNVERIFIED(세부 사유) 중 뭐라고 부를 것인가"를
결정하는 것과, 그 이유를 사람이 읽을 수 있는 문장으로 만드는 것이다.

핵심 원칙(Decision Log 003 계승): "값이 맞다"는 "공식 확인됐다"와 다르다.
UNVERIFIED는 실패가 아니라 올바른 출력이고, 우리가 직접 계산한(파생)
값은 그 자체로 VERIFIED 취급하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


# ---------------------------------------------------------------------
# 단위 카테고리 - kosis_text_utils.py의 _unit_categories와 동일한 분류
# 체계를 그대로 재사용한다(카테고리별로 오차 기준의 "종류"(절대오차 vs
# 상대오차) 자체가 다르기 때문에, 이미 검증된 분류를 새로 만들지 않는다).
# 이 모듈이 kosis_text_utils.py와 같은 저장소에 놓인다면, 아래
# UnitCategory 상수 대신 TextUtilsMixin._unit_categories()를 직접 호출해
# 중복을 없애는 쪽을 권장한다 - 여기서는 이 모듈만 떼어 봐도 동작하도록
# 독립적으로 남겨둔다.
# ---------------------------------------------------------------------
class UnitCategory:
    PERSON = "person"
    MONEY = "money"
    PERCENT = "percent"
    COUNT = "count"
    OTHER = "other"


class Mode(str, Enum):
    """세 가지 판정 모드. 같은 입력에 대해 병렬로 돌려볼 수 있도록 순수
    함수 파라미터로 받는다 - 프론트에서 "엄격 기준/완화 기준"을 동시에
    보여주는 UI도 추가 개발 없이 지원 가능하다."""

    STRICT = "strict"
    TOLERANCE = "tolerance"
    RAW_ONLY = "raw_only"


class Verdict(str, Enum):
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    UNVERIFIED_NOT_FOUND = "UNVERIFIED_NOT_FOUND"
    UNVERIFIED_UNRESOLVED = "UNVERIFIED_UNRESOLVED"
    UNVERIFIED_DERIVED_NEEDED = "UNVERIFIED_DERIVED_NEEDED"
    RAW_ONLY = "RAW_ONLY"


# ---------------------------------------------------------------------
# 입력 스키마
# ---------------------------------------------------------------------
@dataclass
class Claim:
    """기사에서 뽑은 주장 하나.

    direction: "increase"|"decrease"|None - "13만 명 감소했다"처럼 claim
    자체가 두 시점 사이의 증감을 주장하는 경우에만 채워진다. 원문장의
    근사/부등호 표현(hedge, 3절)과는 다른 신호다 - hedge는 "그 숫자를
    얼마나 엄밀하게 주장했는가"를 나타내고, direction은 "그 숫자가
    애초에 증가량인가 감소량인가"를 나타낸다. 이 필드가 있으면
    ActualEvidence도 반드시 두 시점 값(is_comparison=True, values 2개)
    으로 와야 판정이 가능하다 - 단일 시점 절대값과 비교하면 안 된다
    (실측으로 확인된 문제: "13만 명 감소" 주장을 취업자 수 절대값
    2,787만 명과 그대로 비교하면 무의미한 MISMATCH가 나온다).
    """

    raw_sentence: str
    claimed_value: float
    claimed_unit: Optional[str] = None
    claimed_period: Optional[str] = None
    unit_category: str = UnitCategory.OTHER
    direction: Optional[str] = None


@dataclass
class EvidencePoint:
    """다중 시점 증거의 값 한 점(예: 기준시점 또는 비교시점 하나)."""

    period: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None


@dataclass
class ActualEvidence:
    """이전 단계(검색/해석 - 4번 팀원)가 실제로 찾은 값.

    is_comparison: 4번 팀원이 "이 claim은 여러 시점 값이 필요하다"고
    명시하는 플래그. False(기본값)면 value/unit 단일 값을 그대로 쓰고,
    True면 values(EvidencePoint 리스트)를 쓴다 - 두 표현 방식을 섞어
    쓰지 않는다. "몇 시점이 필요한지는 claim이 말해주니, 4번이 그만큼
    담아서 주면 된다"는 원칙 - 이 모듈은 몇 개의 시점이 오든 values
    리스트 하나로 받는다(정확히 2개면 증감 비교, 3개 이상은 아직 자동
    계산하지 않고 UNVERIFIED_DERIVED_NEEDED로 넘긴다 - Decision Log의
    #47[파생·복합 claim 평가]이 그대로 다루는 영역이라 여기서 새로
    풀지 않는다).
    """

    value: Optional[float] = None
    unit: Optional[str] = None
    table_org_id: Optional[str] = None
    table_tbl_id: Optional[str] = None
    table_nm: Optional[str] = None
    table_purpose: Optional[str] = None
    is_comparison: bool = False
    values: Optional[List[EvidencePoint]] = None


@dataclass
class SearchLog:
    """이전 단계의 탐색 과정 기록 - 판단불가 설명의 핵심 재료.

    retrieval_status: "RESOLVED" | "UNRESOLVED" | "NOT_FOUND".
    NOT_FOUND는 후보 표 자체가 하나도 안 나온 경우(리콜 실패),
    UNRESOLVED는 후보 표는 있었지만 그 안에서 개념을 컬럼/분류값으로
    확정 못 한 경우(해석 실패) - 2주차 챗봇의 검색/해석 구분을 그대로
    가져온 것이다.

    derivation_used: 최종 값이 KOSIS가 그대로 내려준 원본 값이 아니라,
    이전 단계가 두 시점 값을 빼거나 나누는 등 2차 가공(파생 계산)을 해서
    만든 값인지 여부. True면 모드와 무관하게 항상
    UNVERIFIED_DERIVED_NEEDED로 분리한다(Decision 003 원칙: 파생값을
    수동 계산해서 검증했다고 우기지 않는다).
    """

    retrieval_status: str = "RESOLVED"
    confident: bool = True
    candidates_tried: List[str] = field(default_factory=list)
    derivation_used: bool = False
    derivation_note: Optional[str] = None


@dataclass
class VerdictResult:
    verdict: Verdict
    explanation: str
    claimed_value: Optional[float] = None
    actual_value: Optional[float] = None
    hedge_type: Optional[str] = None
    mode: Optional[Mode] = None


# ---------------------------------------------------------------------
# 3절: 근사 표현(Hedge) 사전 - 규칙 기반, Decision 001 하이브리드 원칙.
# 표기가 유한한 문제(정해진 표현 목록)는 코드로 결정론적으로 처리하고,
# 사전에 없는 새 표현이 실측으로 나오면 그때 목록을 넓힌다.
#
# 우선순위 순서로 검사한다 - 방향성 표현(at_least/approach_below/
# at_most)이 "약"류의 대칭 근사(approx)보다 문장의 의도를 더 구체적으로
# 알려주므로 먼저 검사한다. 아무 것도 안 걸리면 "exact"(정확한 수치를
# 주장했다)로 취급한다 - 이게 가장 엄격한 기본값이다.
# ---------------------------------------------------------------------
# [실측으로 발견 - 데모 케이스 "65세 이상 고령인구 비율이 20.3%로..."]
# "이상"/"이하"/"미만"은 부등호 주장(돌파/육박류)뿐 아니라, "65세 이상"/
# "300인 이상"처럼 나이·규모 구간을 정의하는 관용구로도 극히 흔하게
# 쓰인다. 이 관용구는 claimed_value(20.3%)와 아무 관계 없이 그냥 "몇
# 세부터를 고령인구로 볼지"를 정의하는 말인데, 문장 전체를 훑는 단순
# 매칭으로는 이걸 "20.3% 이상이라고 주장했다"로 잘못 읽어버린다(실제로
# 이 버그가 데모 실행 중 재현됨 - strict 모드에서 20.2가 20.3 이상이 아니라는
# 이유로 MISMATCH가 나서 발견). kosis_text_utils.py가 "대비"/"동월"/
# "동기"류 단위 오탐을 사전에 strip하는 것과 동일한 방식으로, 숫자+
# (세|인|명|개|년|살) 뒤에 바로 붙는 "이상/이하/미만"은 구간 정의
# 관용구로 보고 hedge 매칭 전에 제거한다.
_HEDGE_FALSE_POSITIVE_STRIP_RE = re.compile(
    r"\d+\s*(?:세|인|명|개|년|살)\s*(?:이상|이하|미만)"
)

_HEDGE_PATTERNS = [
    ("at_least", re.compile(r"(돌파|넘어서|웃돌|초과|상회|이상)")),
    ("approach_below", re.compile(r"(육박|근접|다가서|채\s*못\s*미)")),
    ("at_most", re.compile(r"(밑돌|하회|이하|미만)")),
    ("approx", re.compile(r"(약|대략|가량|정도|무렵|안팎)")),
]

_HEDGE_DESCRIPTIONS = {
    "exact": "정확한 수치를 주장",
    "approx": "대략적인 근사치를 주장(대칭 오차 허용폭 확대)",
    "at_least": "이 값 이상이라고 주장(이상/초과 판정)",
    "approach_below": "이 값에 근접했다고 주장(이하이면서 근접해야 인정)",
    "at_most": "이 값 이하라고 주장(이하/미만 판정)",
}


def extract_hedge(raw_sentence: str) -> str:
    """원문장에서 근사/방향성 표현을 찾아 hedge 유형을 반환한다.

    모드와 완전히 독립적이다 - Strict 모드라고 해서 "돌파"라는 부등호
    주장을 등호 비교로 바꿔버리면 안 된다(문장을 잘못 읽은 것이 된다).
    모드가 달라지는 건 오차를 얼마나 허용할지(카테고리별 tolerance)이지,
    문장이 애초에 등호를 주장했는지 부등호를 주장했는지는 원문장 자체가
    정하는 사실이다.
    """
    if not raw_sentence:
        return "exact"
    scan_text = _HEDGE_FALSE_POSITIVE_STRIP_RE.sub("", raw_sentence)
    for hedge_type, pattern in _HEDGE_PATTERNS:
        if pattern.search(scan_text):
            return hedge_type
    return "exact"


# ---------------------------------------------------------------------
# 5절: 카테고리별 오차 허용 기준.
#
# percent는 절대오차(%p)를 쓴다 - 값 자체가 이미 비율이라 상대오차로
# 재면 왜곡된다(예: 0.1%p 차이가 20%에서는 상대오차 0.5%지만 2%에서는
# 5%가 되어버려 같은 절대 오차인데 판정이 크게 달라진다).
# money/person/count는 상대오차를 쓴다 - 규모가 표마다 크게 달라서
# 절대오차 하나로는 대응이 안 된다.
#
# [주의] "tolerance" 열은 실제 KOSIS 표준오차/신뢰구간 필드가 아니라,
# 팀이 합의한 고정 상대오차율을 "95% CI 근사치"로 대신 쓰는 것이다.
# 이유: 대부분의 KOSIS 집계표는 표본조사 표준오차 필드를 API로 안
# 준다(실측 확인 전이라 아직 모르는 표도 많음) - 표마다 있는지 없는지
# 확인하는 걸 전제로 설계하면 그 확인이 끝날 때까지 이 모듈 전체가
# 막히므로, 우선 결정론적으로 쓸 수 있는 고정값으로 시작한다. 나중에
# 실제 표준오차 필드를 제공하는 표를 발견하면, 그 표에 한해 아래
# _category_tolerance()가 고정값 대신 실제 CI를 계산해 반환하도록
# 확장하면 된다(이 함수 하나만 바꾸면 되는 확장 포인트로 설계함).
# ---------------------------------------------------------------------
_TOLERANCE_TABLE = {
    UnitCategory.PERCENT: {"kind": "absolute", Mode.STRICT: 0.05, Mode.TOLERANCE: 0.3},
    UnitCategory.MONEY: {"kind": "relative", Mode.STRICT: 0.005, Mode.TOLERANCE: 0.02},
    UnitCategory.PERSON: {"kind": "relative", Mode.STRICT: 0.005, Mode.TOLERANCE: 0.02},
    UnitCategory.COUNT: {"kind": "relative", Mode.STRICT: 0.005, Mode.TOLERANCE: 0.02},
    UnitCategory.OTHER: {"kind": "relative", Mode.STRICT: 0.01, Mode.TOLERANCE: 0.03},
}

# approx(근사치 주장) 문장은 이 배수를 한 번 더 곱해 오차 허용폭을
# 넓힌다 - "정확한 값이라고 주장한 적 없다"는 문장 자체의 신호를
# 반영하기 위함이다.
_APPROX_WIDEN_FACTOR = {Mode.STRICT: 2.0, Mode.TOLERANCE: 1.5}


def _category_tolerance(unit_category: str, mode: Mode, hedge_type: str) -> tuple:
    """(오차 종류("absolute"|"relative"), 오차 크기) 튜플을 반환한다."""
    row = _TOLERANCE_TABLE.get(unit_category, _TOLERANCE_TABLE[UnitCategory.OTHER])
    kind = row["kind"]
    base = row[mode]
    if hedge_type == "approx":
        base *= _APPROX_WIDEN_FACTOR.get(mode, 1.0)
    return kind, base


def _within_tolerance(claimed: float, actual: float, kind: str, epsilon: float) -> bool:
    if kind == "absolute":
        return abs(claimed - actual) <= epsilon
    # relative - actual이 0에 가까우면 상대오차가 무한대로 발산하니
    # 분모를 claimed와 actual 중 더 큰 절대값으로 잡아 방어한다.
    denom = max(abs(actual), abs(claimed), 1e-9)
    return abs(claimed - actual) / denom <= epsilon


def _compare_with_hedge(
    claimed: float, actual: float, hedge_type: str, kind: str, epsilon: float
) -> bool:
    """hedge 유형에 따라 등호/부등호 판정을 나눠서 적용한다."""
    if hedge_type in ("exact", "approx"):
        return _within_tolerance(claimed, actual, kind, epsilon)

    if hedge_type == "at_least":
        # "이 값 이상"이라고 주장 - 실제값이 주장값보다 살짝 낮아도
        # 반올림/표기 오차 범위 안이면 인정한다(완전히 엄격한 부등호만
        # 쓰면 "9.999는 10 이상이 아니다"처럼 지나치게 깐깐해진다).
        margin = epsilon if kind == "absolute" else abs(claimed) * epsilon
        return actual >= claimed - margin

    if hedge_type == "at_most":
        margin = epsilon if kind == "absolute" else abs(claimed) * epsilon
        return actual <= claimed + margin

    if hedge_type == "approach_below":
        # "이 값에 근접했다(아직 못 미침)" - 실제값이 주장값을 넘었다면
        # "근접"보다 강한 사실(이미 도달/초과)이므로 그 자체로 인정하고,
        # 못 미쳤다면 오차 허용폭 안에서 근접한 경우만 인정한다.
        if actual >= claimed:
            return True
        return _within_tolerance(claimed, actual, kind, epsilon)

    return _within_tolerance(claimed, actual, kind, epsilon)


# ---------------------------------------------------------------------
# 6절: UNVERIFIED 세분화. 우선순위: NOT_FOUND -> UNRESOLVED ->
# DERIVED_NEEDED 순으로 검사한다(표를 아예 못 찾았는데 파생 여부를
# 따지는 건 의미가 없으므로).
# ---------------------------------------------------------------------
def _check_unverified(search_log: SearchLog) -> Optional[VerdictResult]:
    if search_log.retrieval_status == "NOT_FOUND":
        tried = ", ".join(search_log.candidates_tried) or "(후보 없음)"
        return VerdictResult(
            verdict=Verdict.UNVERIFIED_NOT_FOUND,
            explanation=(
                f"{len(search_log.candidates_tried)}개 후보 표를 넓혀서 찾아봤지만"
                f"({tried}), 이 개념과 일치하는 통계표 자체를 찾지 못했습니다."
                " KOSIS 외 다른 데이터 소스가 필요할 수 있습니다."
            ),
        )
    if search_log.retrieval_status == "UNRESOLVED" or not search_log.confident:
        tried = ", ".join(search_log.candidates_tried) or "(후보 없음)"
        return VerdictResult(
            verdict=Verdict.UNVERIFIED_UNRESOLVED,
            explanation=(
                f"관련 통계표는 찾았지만({tried}), 이 개념이 정확히 어느"
                " 컬럼/분류값인지 확인하지 못했습니다(표 이름/설명만 보고"
                " 고른 추정)."
            ),
        )
    if search_log.derivation_used:
        note = search_log.derivation_note or "직접 계산"
        return VerdictResult(
            verdict=Verdict.UNVERIFIED_DERIVED_NEEDED,
            explanation=(
                "이 값은 KOSIS가 그대로 제공한 원본 수치가 아니라, 저희가"
                f" 두 시점 값으로 직접 계산({note})한 참고값입니다. 공식"
                " 확인된 값이 아니므로 판정에 포함하지 않습니다."
            ),
        )
    return None


# ---------------------------------------------------------------------
# 7절: 진입 함수
# ---------------------------------------------------------------------
def _resolve_comparison_evidence(
    claim: Claim, actual: ActualEvidence, mode: Mode
) -> "tuple":
    """actual.is_comparison=True일 때, 두 시점 값을 하나의 (부호 있는
    diff, 설명용 문구) 로 압축한다. 실패하면 VerdictResult를 반환하고,
    성공하면 (diff, note) 튜플을 반환한다 - 호출부가 타입으로 구분한다.
    """
    points = actual.values or []
    if len(points) < 2:
        return VerdictResult(
            verdict=Verdict.UNVERIFIED_UNRESOLVED,
            explanation=(
                "이 주장은 두 시점 비교가 필요한데, 비교에 쓸 시점 값이"
                f" {len(points)}개만 제공됐습니다(최소 2개 필요)."
            ),
            claimed_value=claim.claimed_value,
            mode=mode,
        )
    if len(points) > 2:
        # 3개 이상(예: "N분기 연속 증가"류)은 Decision Log #47(파생·복합
        # claim 평가)이 다루기로 이미 후순위로 미뤄둔 영역이다 - 여기서
        # 섣불리 다중 시점 추세를 자동 판정하지 않는다.
        return VerdictResult(
            verdict=Verdict.UNVERIFIED_DERIVED_NEEDED,
            explanation=(
                f"{len(points)}개 시점에 걸친 추세 주장은 아직 자동으로"
                " 계산하지 않습니다(다중 시점 계산 아키텍처는 별도 트랙)."
            ),
            claimed_value=claim.claimed_value,
            mode=mode,
        )

    # 정확히 2개 - claimed_period와 일치하는 쪽을 "기준시점"으로,
    # 나머지를 "비교시점"으로 삼는다. 매칭 안 되면(period 정보가 없거나
    # 형식이 다르면) 4번 팀원이 준 순서를 그대로 신뢰한다(첫 번째=기준,
    # 두 번째=비교) - 이 순서 규칙은 어댑터/4번 팀원과 미리 합의해둬야
    # 하는 지점이다.
    base, reference = points[0], points[1]
    if claim.claimed_period:
        for i, p in enumerate(points):
            if p.period == claim.claimed_period:
                base = p
                reference = points[1 - i]
                break

    if base.value is None or reference.value is None:
        return VerdictResult(
            verdict=Verdict.UNVERIFIED_UNRESOLVED,
            explanation="비교에 필요한 두 시점 값 중 일부가 비어있습니다.",
            claimed_value=claim.claimed_value,
            mode=mode,
        )

    diff = base.value - reference.value
    note = (
        f"기준({base.period}) {base.value}{base.unit or ''} vs"
        f" 비교({reference.period}) {reference.value}{reference.unit or ''}"
    )

    # [팀 문서 예시 그대로 반영] "공식 통계는 증가이나 기사는 감소로
    # 표현" - 방향이 정반대면 오차 허용 여부와 무관하게 그 자체로
    # MISMATCH다. 방향 신호가 없는 claim(순수 절대값 주장)에는 이 검사를
    # 적용하지 않는다.
    if claim.direction and diff != 0:
        actual_direction = "increase" if diff > 0 else "decrease"
        if claim.direction != actual_direction:
            return VerdictResult(
                verdict=Verdict.MISMATCH,
                explanation=(
                    f"{note} - 실제로는 {actual_direction}(이)나 주장은"
                    f" {claim.direction}이라고 해서 방향 자체가 반대입니다."
                ),
                claimed_value=claim.claimed_value,
                actual_value=abs(diff),
                mode=mode,
            )

    return abs(diff), note


def judge_claim(
    claim: Claim,
    actual: ActualEvidence,
    search_log: SearchLog,
    mode: Mode = Mode.TOLERANCE,
) -> VerdictResult:
    """claim과 actual을 놓고 최종 판정을 확정한다.

    이 함수는 검색/해석을 다시 하지 않는다 - search_log가 이미 그 과정을
    끝낸 결과라고 신뢰하고, 여기서는 오직 "판정"과 "설명"만 만든다.
    """
    unverified = _check_unverified(search_log)
    if unverified is not None:
        unverified.claimed_value = claim.claimed_value
        unverified.actual_value = actual.value
        unverified.mode = mode
        return unverified

    if mode == Mode.RAW_ONLY:
        if actual.is_comparison:
            points_desc = "; ".join(
                f"{p.period}: {p.value}{p.unit or ''}" for p in (actual.values or [])
            )
            explanation = (
                f"[{actual.table_nm}] 주장값 {claim.claimed_value}{claim.claimed_unit or ''}"
                f" / 조회된 시점들({points_desc}) - 판정 없이 원자료를 그대로"
                " 제공합니다. 최종 판단은 사용자에게 맡깁니다."
            )
        else:
            explanation = (
                f"[{actual.table_nm}] 주장값 {claim.claimed_value}{claim.claimed_unit or ''}"
                f" / 조회값 {actual.value}{actual.unit or ''} - 판정 없이 원자료를"
                " 그대로 제공합니다. 최종 판단은 사용자에게 맡깁니다."
            )
        return VerdictResult(
            verdict=Verdict.RAW_ONLY,
            explanation=explanation,
            claimed_value=claim.claimed_value,
            actual_value=actual.value,
            mode=mode,
        )

    comparison_note = None
    if actual.is_comparison:
        resolved = _resolve_comparison_evidence(claim, actual, mode)
        if isinstance(resolved, VerdictResult):
            return resolved
        actual_value, comparison_note = resolved
    elif actual.value is None:
        # 방어적 가드 - search_log가 RESOLVED라고 했는데 실제 값이 없는
        # 경우는 이 함수 스펙 밖의 상황이지만, 조용히 죽지 않고 판단불가로
        # 처리한다.
        return VerdictResult(
            verdict=Verdict.UNVERIFIED_UNRESOLVED,
            explanation="표는 확정됐지만 실제 값을 조회하지 못했습니다.",
            claimed_value=claim.claimed_value,
            mode=mode,
        )
    else:
        actual_value = actual.value

    hedge_type = extract_hedge(claim.raw_sentence)
    kind, epsilon = _category_tolerance(claim.unit_category, mode, hedge_type)
    matched = _compare_with_hedge(
        claim.claimed_value, actual_value, hedge_type, kind, epsilon
    )

    hedge_desc = _HEDGE_DESCRIPTIONS.get(hedge_type, hedge_type)
    diff = actual_value - claim.claimed_value
    value_desc = (
        comparison_note
        if comparison_note
        else f"조회값 {actual_value}{actual.unit or ''}"
    )
    if matched:
        explanation = (
            f"[{actual.table_nm}] 원문장은 \"{hedge_desc}\"으로 해석됩니다."
            f" 주장값 {claim.claimed_value}{claim.claimed_unit or ''} vs"
            f" {value_desc}"
            f" (차이 {diff:+.3f}) - {mode.value} 기준 허용 오차 이내로 일치합니다."
        )
        verdict = Verdict.VERIFIED
    else:
        explanation = (
            f"[{actual.table_nm}] 원문장은 \"{hedge_desc}\"으로 해석됩니다."
            f" 주장값 {claim.claimed_value}{claim.claimed_unit or ''} vs"
            f" {value_desc}"
            f" (차이 {diff:+.3f}) - {mode.value} 기준 허용 오차를 벗어났습니다."
        )
        verdict = Verdict.MISMATCH

    return VerdictResult(
        verdict=verdict,
        explanation=explanation,
        claimed_value=claim.claimed_value,
        actual_value=actual_value,
        hedge_type=hedge_type,
        mode=mode,
    )


# ---------------------------------------------------------------------
# 데모 - 2주차 발표/서비스 목업에서 썼던 것과 동일한 6개 사례.
# ---------------------------------------------------------------------
if __name__ == "__main__":
    cases = [
        (
            "최저임금(VERIFIED - exact)",
            Claim("내년도 최저임금은 시간당 9,860원으로 결정됐다", 9860, "원", "2026", UnitCategory.MONEY),
            ActualEvidence(9860, "원", "101", "DT_2OEEM1012", "지방자치단체 외 최저임금 및 영향률", None),
            SearchLog("RESOLVED", True, ["지방자치단체 외 최저임금 및 영향률"]),
        ),
        (
            "소비자물가지수(MISMATCH)",
            Claim("6월 소비자물가지수가 전년 동월 대비 3.5% 급등했다", 3.5, "%", "202606", UnitCategory.PERCENT),
            ActualEvidence(2.4, "%", "101", "DT_1J17009", "소비자물가지수(등락률)", None),
            SearchLog("RESOLVED", True, ["소비자물가지수(등락률)"]),
        ),
        (
            "배추가격(UNVERIFIED_NOT_FOUND)",
            Claim("배추 한 포기 가격이 3,000원에 육박하며 밥상물가에 비상", 3000, "원", "202607", UnitCategory.MONEY),
            ActualEvidence(),
            SearchLog("NOT_FOUND", False, ["농가판매가격지수", "채소류 소득조사", "식재료 구매 행태"]),
        ),
        (
            "고령인구비율(VERIFIED - tolerance, exact hedge)",
            Claim("65세 이상 고령인구 비율이 20.3%로 집계되며 초고령사회 진입", 20.3, "%", "202606", UnitCategory.PERCENT),
            ActualEvidence(20.2, "%", "101", "DT_1B040A3", "주민등록인구 및 세대현황(연령별)", None),
            SearchLog("RESOLVED", True, ["주민등록인구 및 세대현황(연령별)"]),
        ),
        (
            "전세가율(VERIFIED)",
            Claim("서울 아파트 평균 전세가율이 65%를 넘어섰다", 65.0, "%", "202606", UnitCategory.PERCENT),
            ActualEvidence(65.2, "%", "101", "DT_1YL13502E", "주택매매가격 및 전세가격 동향조사", None),
            SearchLog("RESOLVED", True, ["주택매매가격 및 전세가격 동향조사"]),
        ),
        (
            "청년실업률(MISMATCH)",
            Claim("6월 청년(15~29세) 실업률이 8.1%로 집계됐다", 8.1, "%", "202606", UnitCategory.PERCENT),
            ActualEvidence(6.8, "%", "101", "DT_1DA7002S", "연령별 경제활동인구 총괄", None),
            SearchLog("RESOLVED", True, ["연령별 경제활동인구 총괄"]),
        ),
        (
            "취업자 수 감소(VERIFIED - 증감 비교, 2시점)",
            Claim(
                "2025년 1월 취업자 수는 13만 명 감소했다", 130000, "명", "2025-01",
                UnitCategory.PERSON, direction="decrease",
            ),
            ActualEvidence(
                table_org_id="101", table_tbl_id="DT_1DA7001S",
                table_nm="성별 경제활동인구 총괄", is_comparison=True,
                values=[
                    EvidencePoint("2025-01", 27748000, "명"),
                    EvidencePoint("2024-01", 27878000, "명"),
                ],
            ),
            SearchLog("RESOLVED", True, ["성별 경제활동인구 총괄"]),
        ),
        (
            "취업자 수 방향 반대(MISMATCH - 방향 모순)",
            Claim(
                "2025년 1월 취업자 수는 13만 명 감소했다", 130000, "명", "2025-01",
                UnitCategory.PERSON, direction="decrease",
            ),
            ActualEvidence(
                table_org_id="101", table_tbl_id="DT_1DA7001S",
                table_nm="성별 경제활동인구 총괄", is_comparison=True,
                values=[
                    EvidencePoint("2025-01", 28008000, "명"),
                    EvidencePoint("2024-01", 27878000, "명"),
                ],
            ),
            SearchLog("RESOLVED", True, ["성별 경제활동인구 총괄"]),
        ),
    ]

    for name, claim, actual, log in cases:
        print(f"\n=== {name} ===")
        for mode in (Mode.STRICT, Mode.TOLERANCE, Mode.RAW_ONLY):
            result = judge_claim(claim, actual, log, mode=mode)
            print(f"[{mode.value:9s}] {result.verdict.value}: {result.explanation}")