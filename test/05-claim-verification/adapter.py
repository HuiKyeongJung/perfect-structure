"""팀원 1(Claim)·4(KOSIS Evidence)가 넘겨주는 원본 JSON을 판정 모듈
(judgment.py)의 내부 타입으로 변환하는 어댑터 - 5번(나) 파트 전용.

[설계 확정 - 팀 논의 결과] 5번이 실제로 받는 입력은 결국 두 가지뿐이다:
(1) 1번 팀원의 Claim, (2) 4번 팀원이 찾아서 넘겨준 값. 2·3번 팀원이
내부적으로 어떻게 후보를 찾고 랭킹하는지는 5번이 알 필요가 없다 - 그
과정의 결과(찾았는지/못 찾았는지/확신하는지)는 전부 4번의 출력에
반영되어 나온다고 가정한다. 그래서 이전에 고려했던 "3번 table_candidate
JSON도 별도로 받기"는 버리고, 입력을 claim_payload + evidence_payload
두 개로 단순화했다.

이 파일이 존재하는지 judgment.py는 전혀 모른다(반대로도 마찬가지) -
판정 로직과 파싱 로직은 완전히 분리돼 있고, 팀원들이 필드명을 바꾸면
이 파일만 고치면 된다.
"""

import json
from typing import Any, Dict, List, Optional, Union

from judgment import ActualEvidence, Claim, EvidencePoint, SearchLog, UnitCategory

JsonLike = Union[str, Dict[str, Any]]


def _as_dict(payload: JsonLike) -> Dict[str, Any]:
    if isinstance(payload, str):
        return json.loads(payload)
    return payload or {}


def _first_present(d: Dict[str, Any], keys, default=None):
    """d에서 keys를 순서대로 시도해 처음 존재하는(None이 아닌) 값을
    반환한다. 팀원이 필드명을 조금씩 다르게 부를 가능성에 대비한
    안전판 - kosis_resolution.py의 _first_present와 같은 발상."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _to_float(value: Any) -> Optional[float]:
    """실제 KOSIS API는 수치를 JSON 문자열로 준다(예: "677421.7146").
    judgment.py는 float 연산(뺄셈/나눗셈)을 전제로 하므로 4번 출력이
    단일 시점이든(비교 아님) 다중 시점(비교)이든 이 함수를 거쳐 값을
    통일한다. 예전엔 단일 시점 분기에서만 float() 변환이 있고 비교
    분기는 문자열을 그대로 넘겨서, 실제 데이터로 비교 판정을 돌릴 때만
    'str - str' TypeError가 났다(모킹 테스트는 항상 float를 썼기 때문에
    안 잡혔음) - 2026-08-10 실사용 테스트에서 발견."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------
# 단위 -> 카테고리 추론(judgment.py 밖 파일을 import하지 않는다는 원칙을
# 지키기 위해 독립적으로 둔다).
# ---------------------------------------------------------------------
_UNIT_CATEGORY_MARKERS = (
    (UnitCategory.PERCENT, ("%", "퍼센트", "％")),
    (UnitCategory.MONEY, ("원", "달러", "USD", "KRW", "$")),
    (UnitCategory.PERSON, ("명", "인")),
    (UnitCategory.COUNT, ("개", "건", "곳", "대")),
)


def _infer_unit_category(unit: Optional[str]) -> str:
    if not unit:
        return UnitCategory.OTHER
    for category, markers in _UNIT_CATEGORY_MARKERS:
        if any(m in unit for m in markers):
            return category
    return UnitCategory.OTHER


# ---------------------------------------------------------------------
# 1번 팀원 출력 -> Claim
#
# direction: "13만 명 감소했다"류 claim에서 1번이 이미 뽑아준 방향
# 신호("increase"/"decrease"). 원문장의 근사/부등호 표현(hedge)은 여기서
# 뽑지 않는다 - 그건 judgment.py의 extract_hedge()가 raw_sentence를 보고
# 직접 처리한다(문장 해석은 1번이 아니라 5번이 한다고 확정됨).
# ---------------------------------------------------------------------
def parse_claim(payload: JsonLike) -> Claim:
    data = _as_dict(payload)
    value = _first_present(data, ("value", "claimed_value", "claim_value"))
    if value is None:
        raise ValueError(f"claim payload에 value(주장 수치)가 없습니다: {data}")
    unit = _first_present(data, ("unit", "claimed_unit"))
    return Claim(
        raw_sentence=_first_present(data, ("claim", "raw_sentence", "sentence"), ""),
        claimed_value=float(value),
        claimed_unit=unit,
        claimed_period=_first_present(data, ("period", "claimed_period")),
        unit_category=_infer_unit_category(unit),
        direction=_first_present(data, ("direction",)),
    )


# ---------------------------------------------------------------------
# 4번 팀원 출력 -> (ActualEvidence, SearchLog)
#
# 4번이 값을 어떻게 표현하는지 두 가지 모양을 모두 지원한다:
#   (a) 단일 시점: {"value"/"normalized_value": ..., "unit": ...}
#   (b) 다중 시점(비교 필요): {"is_comparison": true,
#        "values": [{"period","value","unit"}, ...]}
# 어느 쪽인지는 "is_comparison" 플래그로 명시적으로 구분한다 - claim이
# 몇 시점을 요구하는지는 4번이 이미 claim을 보고 판단해서 그만큼
# values에 채워 보낸다는 전제.
#
# 판단불가(NOT_FOUND/UNRESOLVED) 여부도 이제 4번 출력 하나에서 전부
# 읽는다 - status류 필드가 있으면 우선 쓰고, 없으면 "값이 있는지"
# 자체를 신호로 삼는다.
# ---------------------------------------------------------------------
def parse_evidence_and_log(payload: JsonLike) -> "tuple":
    data = _as_dict(payload)

    is_comparison = bool(_first_present(data, ("is_comparison",), False))
    raw_points = _first_present(data, ("values", "points"))

    if is_comparison or raw_points:
        points = [
            EvidencePoint(
                period=_first_present(p, ("period",)),
                value=_to_float(_first_present(p, ("value", "normalized_value"))),
                unit=_first_present(p, ("unit", "normalized_unit")),
            )
            for p in (raw_points or [])
        ]
        evidence = ActualEvidence(
            table_org_id=_first_present(data, ("org_id", "table_org_id")),
            table_tbl_id=_first_present(data, ("table_id", "tbl_id")),
            table_nm=_first_present(data, ("table_name", "table_nm", "item_name")),
            table_purpose=_first_present(data, ("table_purpose", "purpose")),
            is_comparison=True,
            values=points,
        )
    else:
        value = _first_present(data, ("normalized_value", "value", "raw_value"))
        evidence = ActualEvidence(
            value=_to_float(value),
            unit=_first_present(data, ("normalized_unit", "unit", "raw_unit")),
            table_org_id=_first_present(data, ("org_id", "table_org_id")),
            table_tbl_id=_first_present(data, ("table_id", "tbl_id")),
            table_nm=_first_present(data, ("table_name", "table_nm", "item_name")),
            table_purpose=_first_present(data, ("table_purpose", "purpose")),
        )

    # 판단불가 신호 - status류 필드가 명시적으로 있으면 그걸 우선한다.
    # 없으면 "값이 하나도 없다"는 사실 자체를 NOT_FOUND로 본다(4번까지
    # 왔는데 값이 정말 하나도 없다면, 그 이전 어딘가에서 표/컬럼을 아예
    # 못 찾았다는 뜻일 가능성이 높다 - 다만 이건 잠정 추론이라, 4번이
    # 명시적 status를 함께 주는 쪽이 항상 더 정확하다).
    status = _first_present(
        data, ("retrieval_status", "status", "query_status")
    )
    if status in ("success", "resolved", "RESOLVED"):
        retrieval_status = "RESOLVED"
    elif status in ("no_data", "error", "unresolved", "UNRESOLVED"):
        retrieval_status = "UNRESOLVED"
    elif status in ("not_found", "NOT_FOUND", "table_not_found"):
        retrieval_status = "NOT_FOUND"
    elif evidence.value is None and not (evidence.values or []):
        retrieval_status = "NOT_FOUND"
    else:
        retrieval_status = "RESOLVED"

    confident = bool(_first_present(data, ("confident", "selection_confident"), True))
    candidates = _first_present(data, ("candidates_tried", "candidates"), [])
    candidate_names: List[str] = [
        (c.get("table_name") or c.get("table_nm") or c.get("name") or str(c))
        if isinstance(c, dict) else str(c)
        for c in candidates
    ]
    derivation = _first_present(data, ("derivation",), {}) or {}

    search_log = SearchLog(
        retrieval_status=retrieval_status,
        confident=confident,
        candidates_tried=candidate_names,
        derivation_used=bool(derivation.get("used", False)),
        derivation_note=derivation.get("note"),
    )
    return evidence, search_log


def build_inputs(claim_payload: JsonLike, evidence_payload: JsonLike):
    """claim(1번)과 evidence(4번) 두 조각만 받아 (Claim, ActualEvidence,
    SearchLog)로 변환한다 - 5번이 실제로 받기로 확정한 입력 형태."""
    claim = parse_claim(claim_payload)
    actual, search_log = parse_evidence_and_log(evidence_payload)
    return claim, actual, search_log


# ---------------------------------------------------------------------
# [종합 프로젝트 - 2.5절] claim 라우팅: KOSIS에서 직접 검색해야 하는
# claim과, 이미 찾은 다른 claim들의 조합으로만 검증 가능한 파생 비교값
# claim을 구분한다.
#
# 1번 Task 출력은 같은 원문장에서 여러 claim_id로 쪼개져 나온다(예:
# "재배면적이 10만4943㏊로 작년 10만5959㏊보다 1.0% 감소했다"가 2025년
# 값/2024년 값/1.0% 감소율 3개의 claim_id로 분리됨). 이 중 증감률
# claim("1.0% 감소")은 KOSIS에 그런 컬럼이 애초에 없는 경우가 많아서,
# 검색을 시도하는 대신 이미 찾은 절대값 claim 2개를 judgment.py의
# is_comparison/EvidencePoint 경로로 넘기는 게 맞다 - 검색 자체를
# 아예 하지 말아야 하는 claim을 미리 걸러내는 게 이 함수의 역할이다.
# ---------------------------------------------------------------------
def route_claim_group(claims: List[Dict[str, Any]]) -> Dict[str, Any]:
    """claim 목록을 "직접 검색 대상"과 "파생 비교값"으로 분류한다.

    판별 기준(추측이 아니라 claim 자체의 필드로 결정론적으로 판별 -
    Decision 003 원칙: 확실하지 않으면 추측하지 않는다):
    - period가 없고(None/빈 값) unit이 "%"인 claim은 "증감률"류 파생값
      후보다.
    - 같은 원문장(claim 텍스트가 동일) 안에, period가 있고 metric이 같은
      절대값 claim이 2개 이상 있으면, 그 파생값 claim은 그 절대값들의
      비교로 검증 가능하다고 보고 "파생 비교값"으로 분류한다.
    - 짝이 되는 절대값 claim이 부족하면(원문장이 다르거나 형제 claim이
      1개 이하) "직접 검색 대상"으로 분류한다 - KOSIS가 등락률 자체를
      공식 컬럼으로 제공하는 지표도 있으므로(예: 소비자물가 상승률),
      "%"에 period가 없다는 사실만으로 무조건 파생값이라고 단정하지
      않는다. 그런 경우는 검색해봐야 알 수 있으므로 안전한 기본값(직접
      검색)으로 둔다.
    - kosis_eligible이 명시적으로 False인 claim은 애초에 검색 대상이
      아니므로 두 버킷 어디에도 넣지 않고 "excluded"로 따로 뺀다(1번
      Task가 이미 KOSIS로 확인 불가능하다고 판단한 claim - 예: 예측치,
      의견성 문장 등을 재추측하지 않는다).

    반환:
        {
          "direct": [claim, ...],
          "derived_comparison": [
              {"claim": claim, "sources": [claim_a, claim_b]}, ...
          ],
          "excluded": [claim, ...],
        }
    """
    eligible: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for c in claims:
        if c.get("kosis_eligible") is False:
            excluded.append(c)
        else:
            eligible.append(c)

    by_sentence: Dict[str, List[Dict[str, Any]]] = {}
    for c in eligible:
        by_sentence.setdefault(c.get("claim", ""), []).append(c)

    direct: List[Dict[str, Any]] = []
    derived: List[Dict[str, Any]] = []

    for group in by_sentence.values():
        absolute_by_metric: Dict[Any, List[Dict[str, Any]]] = {}
        for c in group:
            period = c.get("period")
            if period not in (None, "null", ""):
                absolute_by_metric.setdefault(c.get("metric"), []).append(c)

        for c in group:
            period = c.get("period")
            unit = str(c.get("unit", "")).strip()
            is_rate_shaped = period in (None, "null", "") and unit == "%"
            siblings = [
                s
                for s in absolute_by_metric.get(c.get("metric"), [])
                if s.get("claim_id") != c.get("claim_id")
            ]
            if is_rate_shaped and len(siblings) >= 2:
                derived.append({"claim": c, "sources": siblings[:2]})
            else:
                direct.append(c)

    return {"direct": direct, "derived_comparison": derived, "excluded": excluded}


if __name__ == "__main__":
    from judgment import Mode, judge_claim

    # (1) 단일 시점 케이스 - 최저임금류
    claim1 = {
        "claim": "내년도 최저임금은 시간당 9,860원으로 결정됐다",
        "value": 9860,
        "unit": "원",
        "period": "2026",
    }
    evidence1 = {
        "table_id": "DT_2OEEM1012",
        "table_name": "지방자치단체 외 최저임금 및 영향률",
        "normalized_value": 9860,
        "normalized_unit": "원",
        "query_status": "success",
    }
    c, a, s = build_inputs(claim1, evidence1)
    r = judge_claim(c, a, s, mode=Mode.TOLERANCE)
    print("[단일 시점]", r.verdict.value, "|", r.explanation)

    # (2) 다중 시점(증감) 케이스 - 취업자 수 감소, 4번이 is_comparison
    #     플래그와 values 2개를 채워 보낸 경우
    claim2 = {
        "claim": "2025년 1월 취업자 수는 13만 명 감소했다",
        "value": 130000,
        "unit": "명",
        "period": "2025-01",
        "direction": "decrease",
    }
    evidence2 = {
        "table_id": "DT_1DA7001S",
        "table_name": "성별 경제활동인구 총괄",
        "is_comparison": True,
        "values": [
            {"period": "2025-01", "value": 27748000, "unit": "명"},
            {"period": "2024-01", "value": 27878000, "unit": "명"},
        ],
        "query_status": "success",
    }
    c2, a2, s2 = build_inputs(claim2, evidence2)
    r2 = judge_claim(c2, a2, s2, mode=Mode.TOLERANCE)
    print("[증감 비교]", r2.verdict.value, "|", r2.explanation)

    # (3) 4번이 아예 못 찾은 케이스
    evidence3 = {"query_status": "error"}
    c3, a3, s3 = build_inputs(claim1, evidence3)
    r3 = judge_claim(c3, a3, s3, mode=Mode.TOLERANCE)
    print("[조회 실패]", r3.verdict.value, "|", r3.explanation)