"""전체 파이프라인 "하드" 통합 테스트 - 실제 KOSIS/HCX API 버전
(2026-08-10).

이번 세션에 하나씩 부분 검증했던 실측 테스트들(test_sibling_real_api.py,
test_national_fallback_real_api.py, test_quarter_axis_real_api.py,
test_ai_reinterpretation_real_api.py)이 전부 PASS한 뒤, "이제 부분 검증은
다 됐으니 통합 테스트 하나로 한꺼번에 확인해달라"는 요청으로 작성했다.
개별 기능 하나씩이 아니라, **여러 수정 사항이 실제로 같이 물려 돌아가는지**
를 확인하는 게 목적이다.

[2026-08-10, 첫 실측 결과 이후 H-3 시나리오 교체] H-3은 원래 "분기가
분류축인 표 검색·확정" + "부정문 AI 재해석"을 문화산업 임금동향
(DT_113_STBL_1031340)으로 한 claim 안에서 동시에 태우려 했다. 실측
과정에서 이 표 자체의 leaf 코드 버그(축 "산업"(헤더) vs "문화산업"
(leaf) 오판 - kosis_resolution.py `_match_phrase_in_rows` 특이성
필터로 수정)는 잡았지만, 그다음 이 표가 속한 조사(문화체육관광
일자리현황조사)가 원지수/계절조정류 형제가 아니라 축 구조 자체가
다른 여러 표(임금 동향/종사상지위별 임금 동향/종사자 수 동향)를 같은
STAT_ID로 묶어 발행한다는 게 드러났다 - `find_sibling_tables`가 이걸
전부 "형제 표"로 묶어버려 값 대조로도 못 좁히는 모호함이 남았다. 이건
이번 세션 버그가 아니라 README 7장에 이미 열려있던 "표가 목적에
맞는가 최종 검증을 어디서 할지" 설계 결정과 같은 문제라, 사용자와
상의해 H-3을 형제 표가 2개뿐인(원지수/계절조정) "전산업생산지수"로
교체했다 - "분기가 분류축인 표" 검증은 H-5(직접 호출)가 계속 담당하고,
H-3은 "부정문 AI 재해석이 진짜 진입점에서도 맞물려 도는지"만 깨끗하게
검증한다(각 요소는 따로 이미 검증됐지만, 같이 얽혔을 때도 맞물려 도는지
확인하는 게 이 파일 전체의 목적이라는 점은 동일).

[정직하게 밝혀둘 것 - 두 시나리오는 "메인 파이프라인"이 아니라 직접
호출이다] `process_claim_group_keywords`(진짜 프로덕션 진입점)로 도달
가능한 시나리오(H-1/H-2/H-3)와, 설계상 아직 그 진입점에 안 물려 있는
기능을 직접 호출로 검증하는 시나리오(H-4/H-5)를 섞어뒀다:
  - `resolve_national_value_or_derive`는 2026-08-10 세션 중 사용자가
    명시적으로 "이번엔 메인 경로에 자동 연결하지 않는다"고 결정한 대로
    여전히 별도 호출이다.
  - 분기 범위(1~4분기 전체 조회, quarter_axis_for_matching 경로)는
    `process_claim_group_keywords`가 "전후 비교" claim을 절대값 2개 +
    파생 비교값 1개로 쪼개는 구조라(재배면적 시나리오와 동일 패턴),
    한 번의 fetch_kosis_data_range 호출에 여러 분기를 한꺼번에 담아
    보내는 경로 자체가 지금 claim 스키마로는 자연스럽게 트리거되지
    않는다 - 그래서 이 경로만은 직접 호출로 확인한다.
  이 두 시나리오를 "가짜로 메인 경로를 통과한 척" 만들지 않고 직접
  호출로 명시한 건, 실제로 메인 경로가 그렇게 동작하지 않기 때문이다
  (이 파일이 하려는 게 정확히 "진짜로 뭐가 물려 있고 뭐가 아직 안
  물려 있는지" 정직하게 보여주는 것이므로).

[실행 전 준비] test_integration_e2e_real_api.py와 동일 - 이 폴더에 실제
KOSIS_API_KEY(+NCP_CLOVASTUDIO_API_KEY)가 담긴 .env가 있으면 바로 실행
가능하다. HCX 키가 없으면 H-3의 AI 재해석 부분만 규칙 기반으로 폴백되고
나머지는 그대로 진행된다.

[성공 기준] 다른 실측 테스트들과 동일하게 "최종 판정이 VERIFIED인가"가
아니라 "검색/조회/판정이 끝까지 에러 없이 끝나고, 각 시나리오가 검증
하려던 그 지점(형제 표 확정/분기 정확 선택/AI 재해석 반영/대체 라벨
집계 인식/범위 분기 분리)이 실제로 맞았는가"다. H-1처럼 claimed_value가
"오늘 시점 실제 KOSIS 수치"에 의존하는 경우는 MISMATCH가 나와도 그
자체는 실패가 아니다 - 콘솔에 찍히는 실제 조회값을 직접 확인하면 된다.

실행: python3 test_integration_e2e_hard_real_api.py
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

sys.stdout.reconfigure(line_buffering=True)
for _h in logging.getLogger().handlers:
    if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
        _h.setLevel(logging.DEBUG)
        _h.stream = sys.stdout

import kosis_agent  # noqa: E402
from kosis_agent import KosisInteractiveAgent  # noqa: E402
from config import config  # noqa: E402
import adapter  # noqa: E402
import judgment  # noqa: E402


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
    if not config.NCP_CLOVASTUDIO_API_KEY or "PLACEHOLDER" in str(config.NCP_CLOVASTUDIO_API_KEY):
        print("[경고] NCP_CLOVASTUDIO_API_KEY가 없습니다 - H-3의 AI 재해석과")
        print("  표 후보가 여럿일 때의 LLM 랭킹 단계가 규칙 기반으로만")
        print("  폴백됩니다. 나머지 시나리오는 영향 없습니다.\n")
    return True


def build_real_agent() -> KosisInteractiveAgent:
    agent = KosisInteractiveAgent()
    agent.kosis = LoggingProxy(agent.kosis, "KOSIS")
    agent.hcx = LoggingProxy(agent.hcx, "HCX")
    return agent


# =======================================================================
# H-1. 형제 표 disambiguation (전산업생산지수 계절조정) - 진짜 진입점
# =======================================================================
def run_sibling_scenario() -> bool:
    stage("H-1. 형제 표 disambiguation - '전산업생산지수(계절조정)'"
          " (process_claim_group_keywords 실제 진입점)")
    claims = [
        {
            "claim_id": "H1", "claim": "전산업생산지수(계절조정)가 전월보다 상승했다",
            "metric": "생산지수", "value": 0, "unit": "", "period": "202506",
            "kosis_eligible": True,
        },
    ]
    keywords_by_claim_id = {"H1": ["전산업생산지수"]}
    print(pretty(claims))

    agent = build_real_agent()
    evidence = agent.process_claim_group_keywords(claims, keywords_by_claim_id, category_hint=None)
    print("\n[evidence 결과]")
    print(pretty(evidence))

    ev = evidence.get("H1", {})
    tbl_nm = ev.get("table_name") or ""
    ok = ev.get("query_status") == "success" and "계절조정" in tbl_nm
    print(f"\n-> 기대: query_status=success AND table_name에 '계절조정' 포함")
    print(f"   실제: query_status={ev.get('query_status')!r}, table_name={tbl_nm!r},"
          f" 조회값={ev.get('normalized_value')} {ev.get('normalized_unit')}")
    print("PASS" if ok else "FAIL")
    return ok


# =======================================================================
# H-2. NOT_FOUND/UNRESOLVED 실측 (존재하지 않는 지표) - 진짜 진입점
#
# [2026-08-10 첫 실행 FAIL 후 수정] 원래는 query_status가 정확히
# "not_found"여야 한다고 가정했는데, 실측 결과는 "unresolved"였다(사유:
# "표 후보들에서 관련 컬럼을 확인하지 못했습니다."). 로그를 보면 원인은
# 코드 버그가 아니라 이 테스트가 고른 키워드 자체의 문제다 - "화성"은
# "Mars"가 아니라 실존하는 지명(화성시/경기도)과 동음이의라서, 실제
# KOSIS 자유 검색이 "화성이주민수"에 대해 후보 표를 0개로 반환하지
# 않고(그러면 stage="table"+"찾지 못했습니다"->not_found) 화성시 관련
# 인구 통계표들을 후보로 걸어버린다 - 그 후보들 중 "화성 이주민수"라는
# 컬럼이 실제로 있는 표는 하나도 없으니 verify_table_candidates_by_meta가
# 전부 걸러내고, 그 결과가 stage="table"+"확인하지 못했습니다"(다른
# 사유) -> unresolved로 분류된다. _fetch_result_to_evidence의 문서화된
# 구분(7.x절/코드 주석 참고) 자체가 "후보가 아예 없음"=not_found,
# "후보는 있었는데 컬럼이 없음"=unresolved 이므로, 이건 스테이지 태깅
# 로직이 실제로 잘못 동작한 게 아니라 이 테스트의 원래 기대치("화성"
# 이라는 단어가 후보를 0개로 만들 것이다)가 틀렸던 것이다.
#
# 지명과 절대 겹치지 않는 "진짜 없는 개념"을 새로 고르는 대신(그런
# 단어도 KOSIS 검색엔진이 언제 후보를 0개로 반환할지 100% 보장할 수
# 없다 - 검색엔진 내부 동작이라 통제 밖이다), 이 시나리오가 실제로
# 검증하려는 더 근본적인 불변식으로 기대치를 바꿨다: "존재하지 않는
# 개념에 대해 not_found든 unresolved든 정직하게 실패로 보고하고,
# 마치 값을 찾은 것처럼 VERIFIED/MISMATCH를 내지 않는다"(Decision 003).
# 어느 세부 사유든 상관없이, 최종 verdict가 UNVERIFIED_* 계열이기만
# 하면 통과로 본다.
# =======================================================================
def run_not_found_scenario() -> bool:
    stage("H-2. 존재하지 않는 지표('화성이주민수') - stage 태깅이 실제 API"
          " 경로에서도 정직한 실패(not_found 또는 unresolved)로만 이어지고"
          " 거짓 VERIFIED/MISMATCH를 내지 않는지 (지금까지는 mock에서만"
          " 확인됨)")
    claims = [
        {
            "claim_id": "H2", "claim": "화성이주민수는 10만명을 넘어섰다",
            "metric": "화성이주민수", "value": 100000, "unit": "명", "period": "2025",
            "kosis_eligible": True,
        },
    ]
    keywords_by_claim_id = {"H2": ["화성이주민수"]}
    print(pretty(claims))

    agent = build_real_agent()
    evidence = agent.process_claim_group_keywords(claims, keywords_by_claim_id, category_hint=None)
    print("\n[evidence 결과]")
    print(pretty(evidence))

    ev = evidence.get("H2", {})
    ok_status = ev.get("query_status") in ("not_found", "unresolved")

    claim_dict = claims[0]
    verdict_ok = None
    if ok_status:
        claim_obj, actual, search_log = adapter.build_inputs(claim_dict, ev)
        result = judgment.judge_claim(claim_obj, actual, search_log, mode=judgment.Mode.TOLERANCE)
        verdict_ok = result.verdict in (
            judgment.Verdict.UNVERIFIED_NOT_FOUND,
            judgment.Verdict.UNVERIFIED_UNRESOLVED,
        )
        print(f"  [tolerance] {result.verdict.value}: {result.explanation}")

    ok = ok_status and bool(verdict_ok)
    print(f"\n-> 기대: query_status가 not_found 또는 unresolved 중 하나이고,"
          " verdict도 그에 대응하는 UNVERIFIED_* (거짓 VERIFIED/MISMATCH 없음)")
    print(f"   실제: query_status={ev.get('query_status')!r}, verdict 일치={verdict_ok}")
    print("PASS" if ok else "FAIL")
    return ok


# =======================================================================
# H-3. 부정문 AI 재해석 - 진짜 진입점
#
# [2026-08-10, 세 번째 원인 진단 후 시나리오 교체] 원래 이 시나리오는
# "분기가 분류축인 표 검색·확정" + "부정문 AI 재해석"을 DT_113_STBL_
# 1031340(문화산업 임금동향)으로 같이 태우려 했다. leaf 코드 버그(축
# "산업"(헤더) vs "문화산업"(leaf) 오판)까지는 고쳐서 이 표 자체는
# 정확히 353.6만원을 조회하는 데까지 성공했는데, 그다음 단계에서 또
# 막혔다: find_sibling_tables가 같은 조사(STAT_ID=2020003,
# 문화체육관광일자리현황조사)의 모든 표를 "형제 표"로 묶는데, 이 조사는
# H-1의 전산업생산지수(원지수/계절조정 - 진짜 같은 지표의 다른 시리즈)
# 와 달리 "임금 동향"/"종사상지위별 임금 동향"/"종사자 수 동향"처럼
# 축 구조 자체가 다른 별개 표들을 같이 발행한다. "종사상지위별 임금
# 동향"도 "문화산업" 카테고리가 있어 형제 후보로 끌려 들어오고
# (368.1만원 - 종사상지위 축을 안 찍어서 다른 부분집합), claim의
# 355와 두 값(353.6/368.1) 다 정확히 안 맞아 값 대조로도 못 좁혀서
# 다시 "여러 표라 모호함"(unresolved)으로 실패했다.
#
# 이건 이번 세션에 고친 버그가 아니라 README 7장에 이미 열려있던
# "표가 목적에 맞는가 최종 검증을 어디서 할지"와 같은 미정 설계
# 문제라, 사용자와 상의해 이 시나리오 자체를 형제 표가 거의 없는
# 지표로 교체하기로 했다 - H-1에서 이미 qualifier("계절조정")
# 기반으로 깔끔하게 단일 표로 확정되는 걸 검증한 "전산업생산지수"를
# 재사용한다(전산업생산지수는 원지수/계절조정 딱 2개뿐이라 이런 광범위
# 형제 묶음 문제가 없다). "넘어서지 못했다" 부정문 패턴은 judgment.py
# __main__ 데모 Case A/H-3 원안과 동일 - 규칙 기반은 "넘어서(다)"만
# 보고 at_least로 오독하고, AI가 올바르게 이하/미만으로 재해석해야
# 맞는 verdict가 나온다(judgment.py 300행대 주석 참고). claimed_value
# 는 실제 지수가 어느 쪽이든(위/아래) 상관없이 fetch된 실제값 기준으로
# 기대 verdict를 동적으로 계산하므로 정확한 사전 지식이 필요 없다.
# 분기가 분류축인 표 자체의 검증은 H-5(직접 호출)가 여전히 담당한다.
# =======================================================================
def run_quarter_plus_ai_scenario() -> bool:
    stage("H-3. 부정문 AI 재해석 - '전산업생산지수(계절조정)가 2025년 6월"
          " 110을 넘어서지 못했다' (process_claim_group_keywords 실제"
          " 진입점, 실제 조회값 기준 동적 대조)")
    claims = [
        {
            "claim_id": "H3",
            "claim": "전산업생산지수(계절조정)가 2025년 6월 110을 넘어서지 못했다",
            "metric": "생산지수", "value": 110, "unit": "", "period": "202506",
            "kosis_eligible": True,
        },
    ]
    keywords_by_claim_id = {"H3": ["전산업생산지수"]}
    print(pretty(claims))

    agent = build_real_agent()
    evidence = agent.process_claim_group_keywords(claims, keywords_by_claim_id, category_hint=None)
    print("\n[evidence 결과]")
    print(pretty(evidence))

    ev = evidence.get("H3", {})
    if ev.get("query_status") != "success":
        print(f"\n[!!] 조회 실패(query_status={ev.get('query_status')!r}) - 검색이 이 표를"
              " 못 찾았거나 다른 표로 샜을 수 있습니다. 위 [KOSIS.search_metadata]"
              " 로그에서 실제로 어떤 표가 확정됐는지 확인하세요(버그라기보다"
              " 검색 단계의 별개 이슈일 가능성).")
        return False

    print(f"\n[확인] 확정된 표: {ev.get('table_name')!r}, 실제 조회값:"
          f" {ev.get('normalized_value')} {ev.get('normalized_unit')}")

    claim_dict = claims[0]
    claim_obj, actual, search_log = adapter.build_inputs(claim_dict, ev)
    print(f"  Claim  = {claim_obj}")
    print(f"  Actual = {actual}")

    result_no_ai = judgment.judge_claim(claim_obj, actual, search_log, mode=judgment.Mode.TOLERANCE, hcx_client=None)
    print(f"  [AI 없이] {result_no_ai.verdict.value}: {result_no_ai.explanation}")

    result_ai = judgment.judge_claim(claim_obj, actual, search_log, mode=judgment.Mode.TOLERANCE, hcx_client=agent.hcx)
    print(f"  [AI 사용] {result_ai.verdict.value}: {result_ai.explanation}")
    if getattr(result_ai, "ai_note", None):
        print(f"  ai_note: {result_ai.ai_note}")

    # 실제 조회값이 claimed_value(110) 이하면(전산업생산지수(계절조정)가
    # 실제로 110을 넘지 못했다는 claim이 참이면) AI로 정확히 재해석했을
    # 때 VERIFIED가 나와야 한다. 실제 지수가 그 시점 기준 110을 넘었다면
    # 기대치도 자연히 반대로 달라지므로, 여기서는 하드코딩된 값을 그대로
    # 가정하지 않고 "실제 조회값 기준으로" 기대를 계산한다.
    claimed_value = claim_dict["value"]
    try:
        actual_value = float(str(ev.get("normalized_value")))
        expected_true = actual_value <= claimed_value
    except (TypeError, ValueError):
        expected_true = None

    ok = (
        expected_true is not None
        and (
            (expected_true and result_ai.verdict == judgment.Verdict.VERIFIED)
            or (not expected_true and result_ai.verdict == judgment.Verdict.MISMATCH)
        )
    )
    print(f"\n-> 실제 조회값({ev.get('normalized_value')})과 {claimed_value} 비교"
          f" 기준 기대 verdict: {'VERIFIED' if expected_true else 'MISMATCH'}")
    print(f"   AI 사용 실제 verdict: {result_ai.verdict.value}")
    print("PASS" if ok else "FAIL (AI 재해석 결과가 실제 값 기준 기대와 다름"
          " - 실제 HCX가 이 프롬프트에서 다른 선택을 했을 수 있음, ai_note 확인)")
    return ok


# =======================================================================
# H-4. (직접 호출 - 메인 파이프라인 미연결) 전국 집계 폴백, 다른 이름의
# 공식 집계 행 인식 - 농업소득(9도) "평균"
# =======================================================================
def run_national_alt_label_scenario() -> bool:
    stage("H-4. [직접 호출] 전국 집계 폴백 - '평균' 라벨의 공식 집계 행"
          " 인식 (농업소득 9도, resolve_national_value_or_derive는 메인"
          " 경로에 자동 연결돼 있지 않아 여기서만 직접 호출로 확인)")

    agent = build_real_agent()
    result = agent.resolve_national_value_or_derive(
        org_id="101", tbl_id="INH_1EA1501", tbl_nm="농업소득(9도)",
        itm_id="T210", itm_nm="농업소득", indicator="농업소득",
        period="2025", prd_se="Y",
    )
    print("\n[resolve_national_value_or_derive 결과]")
    print(pretty(result))

    ok = bool(result.get("success")) and result.get("derivation_used") is False
    print(f"\n-> 기대: success=True, derivation_used=False(자체 계산 아닌"
          " 공식값 그대로)")
    print(f"   실제: success={result.get('success')},"
          f" derivation_used={result.get('derivation_used')!r}")
    print("PASS" if ok else "FAIL")
    return ok


# =======================================================================
# H-5. (직접 호출 - 메인 파이프라인 미연결) 분기 범위(1~4분기 전체) -
# fetch_kosis_data_range의 quarter_axis_for_matching 경로
# =======================================================================
def run_quarter_range_scenario() -> bool:
    stage("H-5. [직접 호출] 분기 범위(1~4분기 전체) - 문화산업 임금동향"
          " (claim 스키마상 '전후 비교'는 절대값 claim 여러 개로 쪼개져서"
          " 처리되기 때문에, 한 번의 호출에 여러 분기를 담는 이 경로는"
          " process_claim_group_keywords로는 자연스럽게 트리거되지 않아"
          " 여기서만 직접 호출로 확인)")

    agent = build_real_agent()
    result = agent.fetch_kosis_data_range(
        org_id="113", tbl_id="DT_113_STBL_1031340", tbl_nm="임금 동향(1인당 월평균)",
        itm_id="13103135666T1", itm_nm="항목", indicator="임금동향",
        start_period="20241", end_period="20244", prd_se="Q",
        obj_axis=1, obj_code="131021356661.002001",
    )
    print("\n[fetch_kosis_data_range 결과 - 1~4분기 범위]")
    print(pretty(result))

    records = result.get("yearly_records") or {}
    ok_success = bool(result.get("success"))
    ok_all_keys = all(k in records for k in ("20241", "20242", "20243", "20244"))
    values = [records[k].get("value") for k in ("20241", "20242", "20243", "20244") if k in records]
    ok_distinct = len(set(values)) == len(values) == 4
    per_quarter_ok = all(
        records.get(k, {}).get("raw_dict", {}).get("C2_NM") == f"{k[-1]}분기"
        for k in ("20241", "20242", "20243", "20244")
        if k in records
    )

    ok = ok_success and ok_all_keys and ok_distinct and per_quarter_ok
    print(f"\n-> 기대: 4개 분기 키 전부 존재, 각각 정확한 분기, 4개 값 모두 다름")
    print(f"   실제: 키 4개={ok_all_keys}, 분기 일치={per_quarter_ok}, 값 4개 다름={ok_distinct}")
    print("PASS" if ok else "FAIL")
    return ok


def main() -> int:
    print("#" * 78)
    print("# KOSIS 팩트체크 파이프라인 - 하드 통합 테스트 (실제 API)")
    print("# 이번 세션 수정 사항 전체를 한 파일에서 결합 검증")
    print("#" * 78)

    if not check_api_keys():
        return 2

    results: Dict[str, bool] = {}
    results["H-1 형제 표 disambiguation"] = run_sibling_scenario()
    results["H-2 NOT_FOUND 실측"] = run_not_found_scenario()
    results["H-3 분기+AI 재해석 결합"] = run_quarter_plus_ai_scenario()
    results["H-4 전국 집계(다른 라벨) 직접호출"] = run_national_alt_label_scenario()
    results["H-5 분기 범위 직접호출"] = run_quarter_range_scenario()

    stage("최종 결과")
    for name, ok in results.items():
        print(f"  {name:30s}: {'PASS' if ok else 'FAIL'}")

    all_ok = all(results.values())
    if all_ok:
        print("\n[SUCCESS] 이번 세션 수정 사항 전체가 실제 API로 결합 검증까지"
              " 통과했습니다.")
        return 0
    print("\n[FAILURE] 일부 시나리오 실패 - 각 STAGE의 로그를 순서대로 따라가서")
    print("  확인하세요. H-1/H-2/H-3은 실제 검색 단계 결과에 따라 달라질 수")
    print("  있으니, 실패했다면 [KOSIS.search_metadata] 로그부터 먼저 보세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
