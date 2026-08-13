# -*- coding: utf-8 -*-
"""전국 집계 폴백(resolve_national_value_or_derive, 2.3절) - 4번째 경로
(자체 산술평균 derivation) 합성 데이터 유닛테스트 (2026-08-11).

[이 파일이 필요한 이유] 4번째 경로(집계 행이 하나도 없어서 지역별 값을
직접 산술평균으로 계산하는 "진짜 파생" 경로, derivation_used=True)는
지금까지 이 프로젝트에서 실제 KOSIS 표로 한 번도 트리거된 적이 없다.
`explore_national_fallback_candidates.py`로 여러 후보(지역내총생산·
최저생계비·지역별 개인소득·주택보급률 등)를 실측 탐색했지만, 전부 "전국"
축이 있거나(1번 경로) 농업소득(9도)처럼 다른 이름의 공식 집계 행이
있어서(3번 경로) 4번째 경로까지 내려간 적이 없다 - KOSIS가 시도별 분해
표에는 거의 항상 집계 행을 함께 낸다는 뜻으로 보인다(우연이 반복된 것치고
표본이 너무 일관됨).

그렇다고 이 경로를 아예 검증 안 한 채로 "해결된 문제"로 넘기는 건 위험하다
- 코드 자체는 이미 짜여 있고(kosis_agent.py `resolve_national_value_or_
derive` 4번째 분기), 실제 KOSIS 데이터 특성(검색 모호성, 동명이의 등 이번
세션 다른 버그들의 원인)에 기대는 로직이 아니라 순수 결정론적 파이썬
계산(산술평균 + disclosure 문구 조립)이므로, 합성 메타 데이터로도 정확성을
100% 검증할 수 있다. `_find_region_axis_and_codes`의 "UP_ITM_ID로 참조되지
않는 행=leaf" 판별 로직 자체도 이미 실제 KOSIS 응답 두 개(농업소득 9도의
flat 구조, 가구소득의 2단 구조)를 시뮬레이션으로 검증한 전례가 있다(같은
방법론).

[검증하는 것 - 시나리오 2개]
  A. 집계 행이 정말 하나도 없는 표(4개 지역 leaf만 있음) -> 4번째 경로
     (자체 산술평균)를 타야 하고, derivation_used=True, 계산값이 실제
     산술평균과 정확히 같고, derivation_note에 "비가중"/계산식이 투명하게
     노출돼야 한다.
  B. 같은 축에 "평균"이라는 이름의 공식 집계 행이 섞여 있는 표(농업소득
     9도와 동일 패턴, 다만 공식 집계값을 산술평균과 "일부러 다르게" 설정) ->
     3번째 경로(공식 집계 행 그대로 사용)를 타야 하고, derivation_used=
     False, 반환값이 (재계산한 평균이 아니라) 공식 집계값 그대로여야 한다.
     이건 이번 세션에 고친 "다른 이름 집계 행 우선" 버그의 회귀 방지
     테스트이기도 하다 - B가 실수로 A 분기를 타면(즉 공식값을 무시하고
     4개 지역만으로 재계산하면) 이 테스트가 바로 잡아낸다.

[실행] python3 test_national_derivation_mock.py - 실제 API 키/네트워크
불필요(agent.kosis.get_itm_meta_list와 agent.fetch_kosis_data_range를
합성 데이터로 완전히 대체함).
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from kosis_agent import KosisInteractiveAgent


def make_region_rows(include_official_average: Optional[float]) -> List[Dict[str, Any]]:
    """OBJ_ID_SN=1인 flat 지역축 4개(서울/부산/대구/인천) + (옵션) "평균"
    집계 행 1개. 실제 getMeta(type=ITM) 응답과 같은 필드명을 쓴다
    (kosis_text_utils.py `_split_meta_rows`/`_row_id`/`_row_name` 기준)."""
    rows = [
        {"OBJ_ID": "ITEM", "ITM_ID": "T1", "ITM_NM": "가상지표(테스트용 합성 데이터)"},
        {"OBJ_ID": "C", "OBJ_ID_SN": "1", "ITM_ID": "11", "ITM_NM": "서울특별시", "UP_ITM_ID": None},
        {"OBJ_ID": "C", "OBJ_ID_SN": "1", "ITM_ID": "26", "ITM_NM": "부산광역시", "UP_ITM_ID": None},
        {"OBJ_ID": "C", "OBJ_ID_SN": "1", "ITM_ID": "27", "ITM_NM": "대구광역시", "UP_ITM_ID": None},
        {"OBJ_ID": "C", "OBJ_ID_SN": "1", "ITM_ID": "28", "ITM_NM": "인천광역시", "UP_ITM_ID": None},
    ]
    if include_official_average is not None:
        rows.append(
            {"OBJ_ID": "C", "OBJ_ID_SN": "1", "ITM_ID": "99", "ITM_NM": "평균", "UP_ITM_ID": None}
        )
    return rows


REGION_VALUES = {"11": 100.0, "26": 200.0, "27": 150.0, "28": 50.0}
OFFICIAL_AVERAGE = 130.5  # 산술평균(125.0)과 일부러 다르게 설정 - "재계산 안 함"을 증명하기 위함


def build_agent(meta_rows: List[Dict[str, Any]], values: Dict[str, float]) -> KosisInteractiveAgent:
    agent = KosisInteractiveAgent()

    def fake_get_itm_meta_list(org_id: str, tbl_id: str):
        return meta_rows

    agent.kosis.get_itm_meta_list = fake_get_itm_meta_list  # type: ignore[assignment]

    def fake_fetch_kosis_data_range(
        org_id, tbl_id, tbl_nm, itm_id, itm_nm, indicator,
        start_period, end_period, prd_se="Y", category_hint=None,
        obj_axis=None, obj_code=None, extra_obj_axes=None, extra_obj_axes_fallback=None,
    ):
        code = (extra_obj_axes or {}).get(1)
        if code not in values:
            return {"success": False, "message": f"합성 데이터에 없는 코드: {code}"}
        return {
            "success": True,
            "orgId": org_id,
            "tblId": tbl_id,
            "tblNm": tbl_nm,
            "yearly_records": {str(start_period): {"value": values[code], "unit": "테스트단위"}},
        }

    agent.fetch_kosis_data_range = fake_fetch_kosis_data_range  # type: ignore[assignment]
    return agent


def run_scenario_a_pure_derivation() -> bool:
    print("=" * 78)
    print("[시나리오 A] 집계 행이 전혀 없음 -> 4번째 경로(자체 산술평균) 기대")
    print("=" * 78)

    meta_rows = make_region_rows(include_official_average=None)
    agent = build_agent(meta_rows, REGION_VALUES)

    result = agent.resolve_national_value_or_derive(
        org_id="TEST", tbl_id="TEST_TBL_A", tbl_nm="가상 시도별 지표(테스트용 합성 데이터)",
        itm_id="T1", itm_nm="가상지표", indicator="가상지표",
        period="2025", prd_se="Y",
        sibling_tables=[{"ORG_ID": "TEST", "TBL_ID": "TEST_TBL_A", "TBL_NM": "가상 시도별 지표(테스트용 합성 데이터)"}],
    )
    print(f"결과: {result}")

    expected_avg = sum(REGION_VALUES.values()) / len(REGION_VALUES)  # (100+200+150+50)/4 = 125.0
    ok = (
        bool(result.get("success"))
        and result.get("derivation_used") is True
        and result.get("value") == expected_avg
        and isinstance(result.get("derivation_note"), str)
        and "비가중" in result["derivation_note"]
    )
    print(f"-> 기대: success=True, derivation_used=True, value={expected_avg}, "
          "derivation_note에 '비가중' 명시 포함")
    print(f"   실제: success={result.get('success')}, derivation_used={result.get('derivation_used')!r}, "
          f"value={result.get('value')!r}")
    print("PASS" if ok else "FAIL")
    return ok


def run_scenario_b_official_row_wins() -> bool:
    print("\n" + "=" * 78)
    print("[시나리오 B] 다른 이름(\"평균\")의 공식 집계 행이 있음 -> 3번째 경로"
          " (공식값 그대로) 기대, 4번째 경로(재계산)로 새지 않아야 함")
    print("=" * 78)

    meta_rows = make_region_rows(include_official_average=OFFICIAL_AVERAGE)
    values = dict(REGION_VALUES)
    values["99"] = OFFICIAL_AVERAGE
    agent = build_agent(meta_rows, values)

    result = agent.resolve_national_value_or_derive(
        org_id="TEST", tbl_id="TEST_TBL_B", tbl_nm="가상 시도별 지표(테스트용 합성 데이터, 집계행 포함)",
        itm_id="T1", itm_nm="가상지표", indicator="가상지표",
        period="2025", prd_se="Y",
        sibling_tables=[{"ORG_ID": "TEST", "TBL_ID": "TEST_TBL_B", "TBL_NM": "가상 시도별 지표(테스트용 합성 데이터, 집계행 포함)"}],
    )
    print(f"결과: {result}")

    naive_avg = sum(REGION_VALUES.values()) / len(REGION_VALUES)  # 125.0 - 이 값이 나오면 안 됨(재계산 오염)
    record = (result.get("yearly_records") or {}).get("2025")
    returned_value = record.get("value") if isinstance(record, dict) else result.get("value")

    ok = (
        bool(result.get("success"))
        and result.get("derivation_used") is False
        and returned_value == OFFICIAL_AVERAGE
        and returned_value != naive_avg
    )
    print(f"-> 기대: success=True, derivation_used=False, 반환값={OFFICIAL_AVERAGE}"
          f"(재계산한 평균 {naive_avg}가 아니라 공식 집계값 그대로)")
    print(f"   실제: success={result.get('success')}, derivation_used={result.get('derivation_used')!r}, "
          f"반환값={returned_value!r}")
    print("PASS" if ok else "FAIL")
    return ok


def main() -> int:
    print("#" * 78)
    print("# 전국 집계 폴백 - 4번째 경로(자체 파생) 합성 데이터 유닛테스트")
    print("# (실제 KOSIS 표로는 못 찾은 트리거 케이스를 합성 데이터로 대체 검증)")
    print("#" * 78 + "\n")

    results = {
        "A. 집계 행 없음 -> 자체 산술평균": run_scenario_a_pure_derivation(),
        "B. 다른 이름 집계 행 있음 -> 공식값 우선(회귀 방지)": run_scenario_b_official_row_wins(),
    }

    print("\n" + "=" * 78)
    print("[최종 결과]")
    for name, ok in results.items():
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    all_ok = all(results.values())
    print("\n[SUCCESS] 4번째 경로(자체 파생) 로직을 합성 데이터로 검증 완료."
          if all_ok else "\n[FAILURE] 위 로그를 확인하세요.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
