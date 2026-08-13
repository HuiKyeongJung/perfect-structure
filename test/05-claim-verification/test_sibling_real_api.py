"""형제 표(원지수/계절조정 등) disambiguation - 실제 KOSIS/HCX API 버전
(2026-08-10).

`test_sibling.py`(Fake KOSIS 클라이언트로 만든 인위적 시나리오)와 같은
로직을 검증하지만, 이번엔 KosisApiClient/HCXClient를 목으로 바꿔치기하지
않고 진짜 인스턴스를 그대로 쓴다 - 실제로 원지수/계절조정 두 표로 갈라져
있는 실제 KOSIS 지표("전산업생산지수", 2026-08-05 KOSIS 통합검색 실측:
DT_1JH20201=원지수/DT_1JH20202=계절조정지수)로 오늘 새로 짠
disambiguation 로직(kosis_agent.py의 _resolve_table_for_claim_keywords/
_expand_clean_matches_with_siblings/_disambiguate_table_candidates)이
실제 검색 응답에서도 똑같이 동작하는지 확인한다.

`test_integration_e2e_real_api.py`와 구조/관례가 동일하다(LoggingProxy로
실제 클라이언트 호출을 투명하게 로깅, check_api_keys로 키 없으면 조기
종료, "성공" 기준은 최종 판정 VERIFIED가 아니라 query_status=success).

[검증 포인트 - 특히 눈여겨봐야 할 것]
  1. find_sibling_tables가 STAT_ID 없이도(또는 있어도) "전산업생산지수"
     계열 접미사 제거 매칭으로 두 표(원지수/계절조정지수)를 실제로 형제로
     묶어내는지 - 이 부분은 2026-08-05 세션에서 "실측 미검증"으로 남겨둔
     폴백 경로라 이번이 처음 실측이다.
  2. 시나리오 D-1(claim에 "계절조정" 명시) - 최종 확정된 표 이름에
     "계절조정"이 들어있어야 한다(HCX 없이 원문 신호만으로 결정돼야 함).
  3. 시나리오 D-2(계열 명시 없음, 실값만으로 판단) - claimed_value를
     사용자가 먼저 시나리오 D-1로 실제 조회해본 값 중 하나로 맞춰 넣게
     되어 있다(아래 TODO 참고) - 실제 KOSIS 수치는 매번 갱신되므로 이
     파일 안에 미리 정확한 값을 박아둘 수 없다.

[실행 전 준비] test_integration_e2e_real_api.py와 동일 - 이 폴더에 실제
KOSIS_API_KEY가 담긴 .env가 있으면 바로 실행 가능하다.

실행: python3 test_sibling_real_api.py
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

import kosis_agent  # noqa: E402

sys.stdout.reconfigure(line_buffering=True)
for _h in logging.getLogger().handlers:
    if isinstance(_h, logging.StreamHandler) and not isinstance(_h, logging.FileHandler):
        _h.setLevel(logging.DEBUG)
        _h.stream = sys.stdout

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
    """test_integration_e2e_real_api.py와 동일한 투명 로깅 프록시."""

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
    missing_critical = []
    if not config.KOSIS_API_KEY or "PLACEHOLDER" in str(config.KOSIS_API_KEY):
        missing_critical.append("KOSIS_API_KEY")

    if missing_critical:
        print("=" * 78)
        print("[안내] 실제 API 키가 설정돼 있지 않습니다.")
        print(f"  누락: {', '.join(missing_critical)}")
        print("  이 폴더의 `.env`에 KOSIS_API_KEY=발급받은_키 를 채운 뒤 다시")
        print("  실행하세요.")
        print("=" * 78)
        return False

    if not config.NCP_CLOVASTUDIO_API_KEY or "PLACEHOLDER" in str(config.NCP_CLOVASTUDIO_API_KEY):
        print("[경고] NCP_CLOVASTUDIO_API_KEY가 없습니다 - claim 원문/실값"
              " 대조로도 못 좁혀지는 케이스(마지막 수단 LLM 판단)는 이 표에서")
        print("  실패할 수 있습니다. 시나리오 D-1/D-2는 원문 명시어/실값"
              " 대조만으로 끝나도록 짜여 있어 영향이 없을 가능성이 높습니다.\n")

    return True


def build_real_agent() -> KosisInteractiveAgent:
    agent = KosisInteractiveAgent()
    agent.kosis = LoggingProxy(agent.kosis, "KOSIS")
    agent.hcx = LoggingProxy(agent.hcx, "HCX")
    return agent


# ---------------------------------------------------------------------
# 시나리오 D-1: claim 원문에 "계절조정"이 명시된 경우 - HCX 없이 원문
# 신호만으로 계절조정지수 표가 확정돼야 한다(_disambiguate_table_
# candidates의 1단계, kosis_agent.py). claimed_value는 실제로 알 수 없는
# 최신 수치라 임의값(0)을 넣는다 - 이 시나리오의 목적은 판정 결과가
# 아니라 "어느 표가 확정됐는가"이므로, judgment 결과는 참고만 하고
# 확정된 표 이름/실제 조회값을 콘솔에서 직접 확인한다.
# ---------------------------------------------------------------------
def run_qualifier_scenario() -> Optional[Dict[str, Any]]:
    stage("시나리오 D-1. claim 원문 계열 명시('계절조정')로 형제 표 확정")
    claims = [
        {
            "claim_id": "P001",
            "claim": "전산업생산지수(계절조정)가 전월보다 상승했다",
            "metric": "생산지수", "value": 0, "unit": "", "period": "202506",
            "kosis_eligible": True,
        },
    ]
    keywords_by_claim_id = {"P001": ["전산업생산지수"]}
    print(pretty(claims))

    agent = build_real_agent()

    stage("시나리오 D-1. process_claim_group_keywords 실행 (실제 KOSIS 조회)")
    evidence = agent.process_claim_group_keywords(
        claims, keywords_by_claim_id, category_hint=None
    )
    print("\n[evidence 결과]")
    print(pretty(evidence))

    ev = evidence.get("P001", {})
    tbl_nm = ev.get("table_name") or ""
    ok = ev.get("query_status") == "success" and "계절조정" in tbl_nm
    print(f"\n-> 기대: query_status=success AND table_name에 '계절조정' 포함")
    print(f"   실제: query_status={ev.get('query_status')!r}, table_name={tbl_nm!r}")
    print(f"   실제 조회값: {ev.get('normalized_value')} {ev.get('normalized_unit')}")
    print("PASS" if ok else "FAIL (실제 KOSIS에 원지수/계절조정 형제 표 구조가"
          " 없거나, find_sibling_tables가 못 묶었을 수 있음 - 위 [KOSIS.*]"
          " 로그에서 search_metadata 반환값을 직접 확인하세요)")
    return ev if ok else None


# ---------------------------------------------------------------------
# 시나리오 D-2: claim 원문에 계열 명시가 없고, D-1에서 실제로 조회된
# 계절조정지수 값을 claimed_value로 그대로 넣는다 - 그러면 실값 대조
# 단계(_score_keyword_group_candidate_against_claims)가 원지수가 아니라
# 계절조정지수 표를 골라야 정상이다(값 자체가 계절조정 쪽 실측값이므로).
# D-1이 실패하면 이 시나리오는 건너뛴다(비교 기준값을 모르기 때문).
# ---------------------------------------------------------------------
def run_value_match_scenario(reference_evidence: Dict[str, Any]) -> bool:
    stage("시나리오 D-2. 원문 명시 없이 실값 대조로 형제 표 확정")
    value = reference_evidence.get("normalized_value")
    try:
        claimed_value = float(str(value))
    except (TypeError, ValueError):
        print(f"[건너뜀] D-1 조회값을 숫자로 못 바꿈: {value!r}")
        return False

    claims = [
        {
            "claim_id": "P002",
            "claim": f"전산업생산지수가 {claimed_value}을 기록했다",
            "metric": "생산지수", "value": claimed_value, "unit": "",
            "period": "202506", "kosis_eligible": True,
        },
    ]
    keywords_by_claim_id = {"P002": ["전산업생산지수"]}
    print(pretty(claims))
    print(f"(D-1에서 실제로 조회된 계절조정지수 값 {claimed_value}을 그대로"
          " claimed_value로 사용 - 원문에는 계열 명시가 없음)")

    agent = build_real_agent()

    stage("시나리오 D-2. process_claim_group_keywords 실행 (실제 KOSIS 조회)")
    evidence = agent.process_claim_group_keywords(
        claims, keywords_by_claim_id, category_hint=None
    )
    print("\n[evidence 결과]")
    print(pretty(evidence))

    ev = evidence.get("P002", {})
    tbl_nm = ev.get("table_name") or ""
    ok = ev.get("query_status") == "success" and "계절조정" in tbl_nm
    print(f"\n-> 기대: query_status=success AND table_name에 '계절조정' 포함"
          " (실값이 계절조정 쪽과 일치하므로)")
    print(f"   실제: query_status={ev.get('query_status')!r}, table_name={tbl_nm!r}")
    print("PASS" if ok else "FAIL")
    return ok


def main() -> int:
    print("#" * 78)
    print("# 형제 표(원지수/계절조정) disambiguation - 실제 API 통합 테스트")
    print("#" * 78)

    if not check_api_keys():
        return 2

    d1_evidence = run_qualifier_scenario()
    ok_d1 = d1_evidence is not None

    if ok_d1:
        ok_d2 = run_value_match_scenario(d1_evidence)
    else:
        print("\n[건너뜀] 시나리오 D-1이 실패해 D-2의 비교 기준값을 구하지"
              " 못했습니다.")
        ok_d2 = False

    stage("최종 결과")
    print(f"  시나리오 D-1 (원문 계열 명시):   {'PASS' if ok_d1 else 'FAIL'}")
    print(f"  시나리오 D-2 (실값 대조):        {'PASS' if ok_d2 else 'FAIL'}")
    print("\n[참고] 이 테스트는 실제 KOSIS에 '전산업생산지수' 원지수/계절조정")
    print("  형제 표가 실제로 검색되는지부터 확인합니다 - D-1이 실패하면")
    print("  위 [KOSIS.search_metadata]/[형제 표 발견] 로그를 먼저 확인해")
    print("  실제 표 이름이 이 파일이 가정한 '...(계절조정)'/'...(원지수)'")
    print("  패턴과 다른지 확인하세요(다르면 kosis_agent.py의")
    print("  _SERIES_QUALIFIER_WORDS/_SERIES_SUFFIX_RE 어휘 목록을 그에")
    print("  맞게 보강해야 할 수 있습니다).")

    if ok_d1 and ok_d2:
        print("\n[SUCCESS] 실제 KOSIS 데이터에서도 형제 표 disambiguation이"
              " 정상 동작함을 확인했습니다.")
        return 0
    print("\n[FAILURE] 위 로그를 순서대로 따라가서 어디서 기대와 달랐는지"
          " 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
