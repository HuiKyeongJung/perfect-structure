"""전국 집계 폴백(resolve_national_value_or_derive, 2.3절) - 실제 KOSIS API
버전 (2026-08-10).

여태 이 함수를 실측할 진짜 지표가 없었다("전국 평균 임금"은 설계 당시의
가상 예시일 뿐이었다). 이번 세션에 실제 뉴스 기사(조선일보, 시도별 임금
기사)를 계기로 KOSIS MCP 커넥터(프로젝트 자체 API 키가 아니라 이미 연결된
별도 조회 도구 - 실측 탐색 전용, 파이프라인 실행에는 안 씀)로 여러 실제
표를 뒤진 끝에 진짜 후보를 찾았다:

  - 표: 농업소득(9도) [orgId=101, tblId=INH_1EA1501], 기관: 국가데이터처,
    조사: 농가경제조사
  - 항목: 농업소득(ITM_ID=T210), 단위: 천원, 주기: Y(연)
  - 지역축(OBJ_ID=C, OBJ_ID_SN=1): 특별시/광역시가 표본에 없는 9개 도만
    있음(경기·강원·충북·충남·전북·전남·경북·경남·제주) + 집계 행
    "평균"(ITM_ID="000")
  - 2026-08-10 실측(같은 커넥터로 직접 조회): 2025년 값 -
    평균=11706.52711, 경기=10093.69616, 강원=10647.30326, 충북=12885.32118,
    충남=11695.83346, 전북=9794.5489, 전남=9999.03465, 경북=20017.16254,
    경남=9328.94819, 제주=24803.5678 (단위 천원)

[중요 - 이 파일의 목적이 세션 중간에 바뀌었다] 처음에는 이 표를 "3번째
경로(자체 파생, 지역별 값을 단순 평균)"의 실측 사례로 쓰려고 했다. 그런데
사용자가 지적한 대로 그건 설계 원칙에 안 맞았다 - "평균"이라는 이름의
집계 행이 이미 표 안에 있는데, 그걸 버리고 우리가 직접(비가중으로) 다시
계산하는 건 오히려 덜 정확한 값을 만드는 것이다. 파생(derivation)은
"집계 행이 정말 하나도 없을 때만" 쓰는 최후 수단이어야 한다.

그래서 kosis_agent.py의 resolve_national_value_or_derive 자체를 고쳤다
(같은 세션, 이 테스트 작성 직후):
  - `_find_region_axis_and_codes`가 이제 지역축 안의 집계 행(이름이
    "전국"이 아니어도 _NATIONAL_AGGREGATE_LABELS - 전국/계/합계/소계/
    전체/평균 - 에 속하면)을 aggregate_row로 함께 반환한다.
  - `resolve_national_value_or_derive`는 "전국" 정확 매칭(1번), 형제 표
    "전국"(2번)에 이어 새로 3번째 단계를 탄다: 지역축에 다른 이름의
    집계 행이 있으면 그 값을 그대로 쓴다(derivation_used=False - 우리
    계산이 아니라 KOSIS 공식값이므로). 진짜로 집계 행이 하나도 없을
    때만 4번째 단계(자체 산술평균, derivation_used=True)로 간다.

그 결과 농업소득(9도)은 더 이상 "자체 파생" 경로의 실측 사례가 아니라,
새로 생긴 "다른 이름의 공식 집계 행 인식" 경로의 실측 사례가 됐다 - 이
파일은 그걸 검증한다. [알려진 남은 공백] "집계 행이 진짜로 하나도 없는"
표(4번째 경로, 진짜 derivation)는 이번에도 실제 후보를 못 찾았다 - 다음
세션에서 이어서 찾아야 한다.

[이번 세션에 이 표를 실측하다가 드러난 또 다른 버그 - kosis_agent.py에
이미 수정 완료]
  `_REGION_PROBE_NAMES`가 "서울특별시"/"서울"만 프로브해서, 서울이 아예
  없는 이 표에서는 지역축 자체를 못 찾고 (None, None, [])을 반환했다
  (전국 폴백이 조용히 완전 실패) - 17개 시도 전체 명칭으로 프로브를
  넓혔다.

[이 테스트가 확인하는 것]
  1. resolve_national_value_or_derive가 이 표에서 새 3번째 경로(다른
     이름의 집계 행 사용, derivation_used=False)를 타는지.
  2. 반환된 값이 KOSIS가 실제로 발표한 "평균" 행 값과 정확히 일치하는지
     (자체 계산이 아니므로 근사치가 아니라 정확히 같아야 한다).
  3. derivation_note가 "전국"이 아니라 "평균"이라는 이름으로 집계 행을
     썼다는 사실을 투명하게 밝히는지.

[실행 전 준비] 이 폴더에 실제 KOSIS_API_KEY가 담긴 .env가 있으면 바로
실행 가능. HCX 키는 이 표에는 필요 없다(전 과정이 축/코드 메타 매칭과
순수 조회라 LLM 호출 지점이 없음).

실행: python3 test_national_fallback_real_api.py
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
    """test_sibling_real_api.py/test_integration_e2e_real_api.py와 동일한
    투명 로깅 프록시."""

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


# ---------------------------------------------------------------------
# 표/항목은 2026-08-10 KOSIS 실측으로 이미 확정된 값을 그대로 하드코딩한다
# (이 테스트가 검증하려는 건 표/항목을 "찾는" 단계가 아니라, 표가 이미
# 확정된 뒤 resolve_national_value_or_derive 자체가 실제 API 응답에서도
# 맞게 동작하는지이므로 - 형제 표 disambiguation은 이미
# test_sibling_real_api.py가 별도로 검증했다).
# ---------------------------------------------------------------------
ORG_ID = "101"
TBL_ID = "INH_1EA1501"
TBL_NM = "농업소득(9도)"
ITM_ID = "T210"
ITM_NM = "농업소득"
INDICATOR = "농업소득"
PERIOD = "2025"
PRD_SE = "Y"


def run_alternate_aggregate_scenario() -> Optional[Dict[str, Any]]:
    stage("N-1. resolve_national_value_or_derive - '전국'은 없지만 '평균'"
          " 집계 행이 있는 표(농업소득 9도)에서 새 3번째 경로(다른 이름의"
          " 공식 집계 행 사용) 실측")

    agent = build_real_agent()

    result = agent.resolve_national_value_or_derive(
        org_id=ORG_ID,
        tbl_id=TBL_ID,
        tbl_nm=TBL_NM,
        itm_id=ITM_ID,
        itm_nm=ITM_NM,
        indicator=INDICATOR,
        period=PERIOD,
        prd_se=PRD_SE,
    )
    print("\n[resolve_national_value_or_derive 결과]")
    print(pretty(result))

    ok_success = bool(result.get("success"))
    # 새 3번째 경로는 "우리가 계산한 값"이 아니라 "KOSIS가 이미 발표한
    # 값을 그대로 쓴 것"이므로 derivation_used는 False여야 한다(True가
    # 나오면 아직 4번째 경로로 잘못 떨어진 것 - 코드 수정이 덜 됐거나
    # aggregate_row 탐지에 실패했다는 뜻).
    ok_not_derived = result.get("derivation_used") is False
    note = result.get("derivation_note") or ""
    ok_note_mentions_average = "평균" in note

    print(f"\n-> 기대: success=True, derivation_used=False(자체 계산 아님),"
          " derivation_note에 '평균' 언급")
    print(f"   실제: success={ok_success}, derivation_used={result.get('derivation_used')!r},"
          f" derivation_note={note!r}")

    ok = ok_success and ok_not_derived and ok_note_mentions_average
    print("PASS" if ok else "FAIL (derivation_used=True로 나왔다면 새로 추가한"
          " aggregate_row 인식 경로가 아니라 옛 자체 파생 경로로 떨어진 것 -"
          " kosis_agent.py의 _find_region_axis_and_codes/"
          " resolve_national_value_or_derive 수정이 제대로 적용됐는지 확인)")
    return result if ok else None


def run_value_cross_check(national_result: Dict[str, Any]) -> bool:
    stage("N-2. (교차 검증) 같은 표의 '평균' 행을 다른 경로로 직접 재조회해"
          " N-1이 반환한 값과 정확히 일치하는지 확인 - 근사치가 아니라 진짜"
          " 같은 값을 그대로 가져온 것인지 이중 확인")

    agent = build_real_agent()
    fetch_res = agent.fetch_kosis_data_range(
        org_id=ORG_ID,
        tbl_id=TBL_ID,
        tbl_nm=TBL_NM,
        itm_id=ITM_ID,
        itm_nm=ITM_NM,
        indicator=INDICATOR,
        start_period=PERIOD,
        end_period=PERIOD,
        prd_se=PRD_SE,
        extra_obj_axes={1: "000"},
    )
    print("\n['평균' 행 직접 재조회 결과]")
    print(pretty(fetch_res))

    record = (fetch_res or {}).get("yearly_records", {}).get(str(PERIOD))
    cross_value = record.get("value") if isinstance(record, dict) else None
    n1_value = national_result.get("yearly_records", {}).get(str(PERIOD), {})
    n1_value = n1_value.get("value") if isinstance(n1_value, dict) else national_result.get("value")

    print(f"\n-> N-1이 반환한 값: {n1_value!r}")
    print(f"   직접 재조회한 '평균' 값: {cross_value!r}")

    try:
        ok = float(str(n1_value)) == float(str(cross_value))
    except (TypeError, ValueError):
        ok = False
    print("PASS (완전히 일치 - 자체 계산 없이 공식값을 그대로 가져왔음이 확인됨)"
          if ok else "FAIL (두 값이 다름 - 코드가 어딘가에서 값을 변형했을 수 있음)")
    return ok


def main() -> int:
    print("#" * 78)
    print("# 전국 집계 폴백(resolve_national_value_or_derive) - 실제 API 검증")
    print("#" * 78)

    if not check_api_keys():
        return 2

    n1 = run_alternate_aggregate_scenario()
    ok_n1 = n1 is not None

    ok_n2 = run_value_cross_check(n1) if ok_n1 else False
    if not ok_n1:
        print("\n[건너뜀] N-1이 실패해 N-2 교차 검증을 건너뜁니다.")

    stage("최종 결과")
    print(f"  N-1 (다른 이름의 공식 집계 행 사용 실측): {'PASS' if ok_n1 else 'FAIL'}")
    print(f"  N-2 (반환값 교차 검증):                  {'PASS' if ok_n2 else 'FAIL'}")

    print("\n[참고] 이 테스트는 '집계 행이 다른 이름으로 존재하는' 경로만")
    print("  검증합니다. '집계 행이 진짜로 하나도 없어서 직접 계산해야 하는'")
    print("  4번째 경로(진짜 derivation)는 아직 실제 KOSIS 후보를 못 찾았습니다")
    print("  - 이 부분은 여전히 미검증 상태로 남아있습니다.")

    if ok_n1 and ok_n2:
        print("\n[SUCCESS] 실제 KOSIS 데이터에서도 '다른 이름의 전국 집계 행'"
              " 인식이 정상 동작함을 확인했습니다.")
        return 0
    print("\n[FAILURE] 위 로그를 순서대로 따라가서 어디서 기대와 달랐는지"
          " 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
