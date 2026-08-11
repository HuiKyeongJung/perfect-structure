"""분기가 "주기"(prd_se=Q)가 아니라 분류축(objL)으로 존재하는 표 - 실제
KOSIS API 버전 (2026-08-10).

사용자가 실제로 찾아온 표(문화체육관광부 KOSIS 페이지,
https://kosis.kr/statHtml/statHtml.do?orgId=113&tblId=DT_113_STBL_1031340)
로 발견된 케이스: 이 표는 KOSIS 수록정보(getMeta type=PRD)상 주기가
"년"(Y) 하나뿐이다(2022~2024). 그런데 실제로 연도 하나를 조회하면 그
안에 "1분기"~"4분기"라는 이름의 분류축(objL, OBJ_ID_SN=2)이 산업분류
축(전체/문화산업/관광산업/스포츠산업, OBJ_ID_SN=1)과 조합되어 16개 행이
한꺼번에 나온다(KOSIS MCP 커넥터로 2026-08-10 실측 확인 - 이 파일에
있는 16개 값 전부 실제 조회 결과 그대로).

[버그였던 것] `resolve_national_value_or_derive`를 실측하다 발견한
버그들과 별개로, `kosis_fetch.py`의 `fetch_kosis_data_range`는 표가
요청한 주기(prd_se="Q")를 지원 안 하면 그냥 연도만 남기고 분기 숫자를
통째로 버렸다("주기 자동 대체" - 원래는 "월간 요청했는데 표에 월간
자체가 아예 없는" 경우를 위한 안전장치였다). 이 표처럼 "분기 정보가
아예 없는 게 아니라 분류축 형태로 존재하는" 경우엔, 그냥 잘라버리면
4개 분기 값이 뒤섞인 응답(16행 중 어느 게 맞는지 신호 없음)을 받게
되고, 이후 행 선택 로직이 근거 없이 하나를 골라야 하는 상황에
몰린다("확실하지 않으면 추측하지 않는다", Decision 003 위반 위험).

[수정] `fetch_kosis_data_range`가 이제 Q->Y 대체 전에 먼저
`resolve_category_hint_axis(org_id, tbl_id, "N분기")`로 "N분기"가 이
표의 분류축 값으로 실제 존재하는지 확인한다. 있으면 그 축을
extra_obj_axes에 pin해서 연간 주기로도 정확한 분기 값만 서버에서
필터링해 가져온다(연간 평균/합계로 뭉개지 않음). 단일 시점 요청
(start_period == end_period)이고 5자리 분기 표기(YYYYN)일 때만
시도한다 - 범위 조회는 이번 수정 범위 밖.

이 파일은 fetch_kosis_data_range를 직접 호출한다(표/컬럼 확정 단계는
이미 다른 테스트들이 검증했고, 이번에 고친 건 그 이후의 분기 처리
로직이므로 - 문화산업 컬럼은 미리 확정됐다고 가정하고 obj_axis/obj_code
로 직접 넘긴다).

[실행 전 준비] 이 폴더에 실제 KOSIS_API_KEY가 담긴 .env가 있으면 바로
실행 가능. HCX는 필요 없음.

실행: python3 test_quarter_axis_real_api.py
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, Optional

sys.stdout.reconfigure(line_buffering=True)
for _h in logging.getLogger().handlers:
    if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
        _h.setLevel(logging.DEBUG)
        _h.stream = sys.stdout

from kosis_agent import KosisInteractiveAgent  # noqa: E402
from config import config  # noqa: E402


def stage(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"[STAGE] {title}")
    print("=" * 78)


def pretty(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return repr(obj)


class LoggingProxy:
    def __init__(self, target: Any, label: str):
        self._target = target
        self._label = label

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def wrapper(*args, **kwargs):
            print(f"    >> [{self._label}.{name}] args={args} kwargs={kwargs}")
            try:
                result = attr(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                print(f"       -> 예외 발생: {type(e).__name__}: {e}")
                raise
            if isinstance(result, list):
                desc = f"{len(result)}건"
                if result:
                    desc += f" (첫 항목: {pretty(result[0])[:200]})"
            else:
                desc = repr(result)
                if len(desc) > 300:
                    desc = desc[:300] + "...(생략)"
            print(f"       -> {desc}")
            return result

        return wrapper


def check_api_keys() -> bool:
    if not config.KOSIS_API_KEY or "PLACEHOLDER" in str(config.KOSIS_API_KEY):
        print("=" * 78)
        print("[안내] 실제 KOSIS_API_KEY가 설정돼 있지 않습니다.")
        print("  이 폴더의 `.env`에 KOSIS_API_KEY=발급받은_키 를 채운 뒤 다시")
        print("  실행하세요.")
        print("=" * 78)
        return False
    return True


def build_real_agent() -> KosisInteractiveAgent:
    agent = KosisInteractiveAgent()
    agent.kosis = LoggingProxy(agent.kosis, "KOSIS")
    return agent


ORG_ID = "113"
TBL_ID = "DT_113_STBL_1031340"
TBL_NM = "임금 동향(1인당 월평균)"
ITM_ID = "13103135666T1"  # "항목" - 이 표의 실제 값 자체는 분류축(문화산업
# 등)으로 구분되지 결측치는 아니다. 실측(2026-08-10)에서 이 ITM_ID로
# itmId=all 조회가 정상 동작함을 확인했다.
ITM_NM = "항목"
INDICATOR = "임금동향"
# 2026-08-10 실측(같은 커넥터로 직접 조회) - 문화산업 컬럼 코드를 미리
# 안다고 가정(표/컬럼 확정 단계는 이번 테스트의 범위 밖).
CULTURE_INDUSTRY_AXIS = 1
CULTURE_INDUSTRY_CODE = "131021356661.002001"
# 2026-08-10 실측 기준값 - 이 스크립트가 실행되는 시점에 KOSIS 원자료가
# 갱신됐을 수 있으니 참고용. 최종 판단은 스크립트가 실제로 받아온 값.
REFERENCE_Q3_VALUE = "353.6"


def run_quarter_axis_scenario() -> Optional[Dict[str, Any]]:
    stage("Q-1. fetch_kosis_data_range - 표 메타는 '년' 주기뿐이지만"
          " 실제론 분기가 분류축으로 있는 표(문화체육관광일자리현황조사)"
          "에서 '2024년 3분기 문화산업' 값을 정확히 골라내는지 실측")

    agent = build_real_agent()

    result = agent.fetch_kosis_data_range(
        org_id=ORG_ID,
        tbl_id=TBL_ID,
        tbl_nm=TBL_NM,
        itm_id=ITM_ID,
        itm_nm=ITM_NM,
        indicator=INDICATOR,
        start_period="20243",
        end_period="20243",
        prd_se="Q",
        obj_axis=CULTURE_INDUSTRY_AXIS,
        obj_code=CULTURE_INDUSTRY_CODE,
    )
    print("\n[fetch_kosis_data_range 결과]")
    print(pretty(result))

    ok_success = bool(result.get("success"))
    record = (result.get("yearly_records") or {}).get("20243")
    value = record.get("value") if isinstance(record, dict) else None
    category_path = record.get("category_path") if isinstance(record, dict) else None
    note = result.get("period_note") or ""

    # 정확히 "3분기" 하나만 골라졌는지가 핵심이다 - 값이 있다는 것만으로는
    # 부족하다(4개 분기 중 아무거나 하나가 우연히 걸렸을 수도 있으므로,
    # category_path/raw_dict에 "3분기"가 실제로 찍혀 있는지까지 본다).
    raw_dict = record.get("raw_dict", {}) if isinstance(record, dict) else {}
    c_names = [
        v for k, v in raw_dict.items() if k.endswith("_NM") and isinstance(v, str)
    ]
    ok_is_q3 = any("3분기" in v for v in c_names) or (
        isinstance(category_path, str) and "3분기" in category_path
    )
    ok_note = "분류축" in note

    print(f"\n-> 기대: success=True, '20243' 키에 값 존재, 그 행이 정확히"
          " '3분기'로 표시됨, period_note에 '분류축' 언급")
    print(f"   실제: success={ok_success}, value={value!r},"
          f" category_path={category_path!r}, 3분기 확인={ok_is_q3},"
          f" period_note={note!r}")

    ok = ok_success and record is not None and ok_is_q3 and ok_note
    print("PASS" if ok else "FAIL (record가 없거나, 있어도 정확히 3분기인지"
          " 확인 안 됨 - 4개 분기 중 뒤섞였을 수 있음)")
    return result if ok else None


def run_cross_check(q3_result: Dict[str, Any]) -> bool:
    stage("Q-2. (교차 검증) 같은 표를 objL 필터 없이 통째로 조회해, 3분기"
          " 값이 정말 여러 후보 중 하나였는지(=이 문제가 실제로 존재했는지)"
          " 확인하고, Q-1이 그중 정확히 맞는 걸 골랐는지 재확인")

    agent = build_real_agent()
    # objL2(분기축)를 안 넘기고 문화산업만 pin해서, 원래대로라면 4개
    # 분기가 다 섞여 나온다는 걸 직접 보여준다.
    fetch_res = agent.fetch_kosis_data_range(
        org_id=ORG_ID,
        tbl_id=TBL_ID,
        tbl_nm=TBL_NM,
        itm_id=ITM_ID,
        itm_nm=ITM_NM,
        indicator=INDICATOR,
        start_period="2024",
        end_period="2024",
        prd_se="Y",
        obj_axis=CULTURE_INDUSTRY_AXIS,
        obj_code=CULTURE_INDUSTRY_CODE,
    )
    print("\n[objL2 없이 문화산업만 pin한 결과 - 4개 분기가 뒤섞여야 정상]")
    print(pretty(fetch_res))

    q1_value = None
    record = (q3_result.get("yearly_records") or {}).get("20243")
    if isinstance(record, dict):
        q1_value = record.get("value")

    print(f"\n-> Q-1이 골라낸 3분기 값: {q1_value!r}"
          f" (2026-08-10 실측 참고값: {REFERENCE_Q3_VALUE!r})")
    print("[참고] 이 단계 자체는 PASS/FAIL 판정 없음 - Q-1의 필터링이 왜")
    print("  필요했는지(objL2 없이는 여러 분기가 섞인다는 것) 눈으로 확인")
    print("  하는 용도.")
    return True


# 2026-08-10 실측 기준값(같은 커넥터로 직접 조회) - 문화산업 1~4분기
# 전체. 스크립트 실행 시점에 KOSIS 원자료가 갱신됐을 수 있으니 참고용.
REFERENCE_ALL_QUARTERS = {"20241": "342.9", "20242": "351.3", "20243": "353.6", "20244": "363.2"}


def run_full_year_range_scenario() -> bool:
    stage("Q-3. (사용자 질문: '전분기(1~4분기 전체)' 범위 조회) 단일 시점이"
          " 아니라 2024년 1~4분기 전체를 한 번에 요청했을 때, 4개 분기 값이"
          " 뒤섞이지 않고 각각 정확히 자기 분기로 분리되는지 실측")

    agent = build_real_agent()
    result = agent.fetch_kosis_data_range(
        org_id=ORG_ID,
        tbl_id=TBL_ID,
        tbl_nm=TBL_NM,
        itm_id=ITM_ID,
        itm_nm=ITM_NM,
        indicator=INDICATOR,
        start_period="20241",
        end_period="20244",
        prd_se="Q",
        obj_axis=CULTURE_INDUSTRY_AXIS,
        obj_code=CULTURE_INDUSTRY_CODE,
    )
    print("\n[fetch_kosis_data_range 결과 - 1~4분기 범위]")
    print(pretty(result))

    records = result.get("yearly_records") or {}
    ok_success = bool(result.get("success"))
    ok_all_keys = all(k in records for k in ("20241", "20242", "20243", "20244"))

    per_quarter_ok = {}
    for key in ("20241", "20242", "20243", "20244"):
        rec = records.get(key)
        raw = rec.get("raw_dict", {}) if isinstance(rec, dict) else {}
        expected_label = f"{key[-1]}분기"
        c_names = [v for k, v in raw.items() if k.endswith("_NM") and isinstance(v, str)]
        is_correct_quarter = expected_label in c_names or (
            isinstance(rec, dict)
            and isinstance(rec.get("category_path"), str)
            and expected_label in rec["category_path"]
        )
        per_quarter_ok[key] = is_correct_quarter
        print(f"   {key}: value={rec.get('value') if isinstance(rec, dict) else None!r}"
              f" (2026-08-10 참고값 {REFERENCE_ALL_QUARTERS.get(key)!r}),"
              f" 분기 일치={is_correct_quarter}")

    # 4개 값이 전부 있고, 전부 정확한 분기로 확인되고, 4개 값이 서로 달라야
    # 한다(뒤섞여서 같은 행이 4번 반복되는 경우를 걸러내기 위함).
    values = [records[k].get("value") for k in ("20241", "20242", "20243", "20244") if k in records]
    ok_distinct = len(set(values)) == len(values) == 4

    ok = ok_success and ok_all_keys and all(per_quarter_ok.values()) and ok_distinct
    print(f"\n-> 기대: 4개 분기 키 전부 존재, 각각 자기 분기로 정확히 표시,"
          " 4개 값이 서로 다름(뒤섞임 없음)")
    print(f"   실제: 키 4개 존재={ok_all_keys}, 분기별 정확={per_quarter_ok},"
          f" 값 4개 모두 다름={ok_distinct}")
    print("PASS" if ok else "FAIL (범위 조회에서 분기가 뒤섞였거나 일부 분기"
          " 누락 - kosis_fetch.py의 quarter_axis_for_matching 관련 로직 확인)")
    return ok


def main() -> int:
    print("#" * 78)
    print("# 분기 분류축 인식 - 실제 API 검증")
    print("#" * 78)

    if not check_api_keys():
        return 2

    q1 = run_quarter_axis_scenario()
    ok_q1 = q1 is not None

    if ok_q1:
        run_cross_check(q1)
    else:
        print("\n[건너뜀] Q-1이 실패해 Q-2 교차 검증을 건너뜁니다.")

    ok_q3 = run_full_year_range_scenario()

    stage("최종 결과")
    print(f"  Q-1 (단일 분기, 분류축으로 정확한 값 선택): {'PASS' if ok_q1 else 'FAIL'}")
    print(f"  Q-3 (1~4분기 범위, 뒤섞이지 않고 각각 분리): {'PASS' if ok_q3 else 'FAIL'}")

    if ok_q1 and ok_q3:
        print("\n[SUCCESS] 실제 KOSIS 데이터에서도 '주기가 아니라 분류축인"
              " 분기' 처리가 단일 시점/범위 조회 둘 다 정상 동작함을"
              " 확인했습니다.")
        return 0
    print("\n[FAILURE] 위 로그를 순서대로 따라가서 어디서 기대와 달랐는지"
          " 확인하세요 - resolve_category_hint_axis는 agent 메서드라 직접"
          " 로그로는 안 찍히니, 대신 [KOSIS.get_itm_meta_list] 호출 횟수와,"
          " 최종 objl_fixed/quarter_axis_for_matching 관련 동작을"
          " [KOSIS.fetch_actual_statistics_bounded_retry] 호출 args와 그"
          " 이후 '[N분기 대조]' 관련 로그에서 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
