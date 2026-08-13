"""전체 파이프라인 통합 테스트 - 실제 KOSIS/HCX API 버전 (2026-08-10).

`test_integration_e2e_keyword_pipeline.py`(목 버전)와 완전히 같은 구조와
같은 시나리오를 쓰지만, KosisApiClient/HCXClient를 목으로 바꿔치기하지
않고 **진짜 인스턴스를 그대로** 쓴다. 즉 실제로 KOSIS Open API/HCX(HyperCLOVA
X) 서버에 네트워크 요청을 보낸다 - 이 파일은 로컬에 진짜
KOSIS_API_KEY(+NCP_CLOVASTUDIO_API_KEY)가 있는 환경에서 실행하는 걸
전제로 한다. 이 세션(클라우드 샌드박스)에는 실제 키가 없어서 여기서는
실행/검증하지 못했다 - 로컬에서 직접 돌려봐야 한다.

목 버전과의 차이는 딱 두 가지뿐이다:
  1) `KosisInteractiveAgent()`를 그 무엇도 patch하지 않고 그대로 생성한다
     -> `self.kosis`/`self.hcx`가 실제 client.py의 KosisApiClient/
     HCXClient 인스턴스가 된다.
  2) 실제 인스턴스라도 "무엇을 요청했고 무엇을 받았는지"가 투명하게
     보여야 하므로, `LoggingProxy`로 감싸서 모든 메서드 호출/반환값을
     로그로 남긴다 - 이 프록시는 로직을 전혀 바꾸지 않고 통과시키기만
     한다(단순 위임).

나머지(STAGE 구분, adapter/judgment 호출, 판정 결과 출력)는 목 버전과
동일한 코드다 - 같은 파이프라인을 "가짜 데이터로 배선만 검증"했던 걸
이제 "진짜 데이터로 실제 검증"하는 것이라고 보면 된다.

[실행 전 준비]
  1. 이 outputs 폴더(또는 프로젝트 루트)에 `.env` 파일을 만들고 아래
     두 줄을 채운다(둘 다 없으면 이 스크립트가 바로 안내 메시지를 내고
     끝난다):
         KOSIS_API_KEY=발급받은_KOSIS_API_키
         NCP_CLOVASTUDIO_API_KEY=발급받은_HCX_API_키   (선택 - 없으면
           표 후보가 여럿일 때의 LLM 랭킹 단계만 건너뛴다는 안내만 뜨고
           나머지는 그대로 진행된다)
  2. `pip install requests python-dotenv` (이미 설치돼 있다면 생략)
  3. `python3 test_integration_e2e_real_api.py`

[주의] 이 테스트는 "오늘 시점의 실제 KOSIS 수치"를 그대로 조회한다. 아래
CLAIMS의 claimed_value(예: 최저임금 9,860원)는 이 프로젝트에서 과거에
검증했던 값을 그대로 옮겨둔 것이라, 시점이 바뀌면(다음 연도 최저임금
고시 등) 실제 값과 달라져 MISMATCH가 나는 게 오히려 정상일 수 있다.
그래서 이 스크립트는 "판정이 VERIFIED로 나와야 성공"이 아니라 "검색/조회
자체가 끝까지 실패 없이 끝나는가(query_status=success, 값이 실제로
찍히는가)"를 성공 기준으로 삼는다 - 최종 판정값은 참고용으로 출력만
하고, 사용자가 직접 눈으로 확인하도록 남겨둔다.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------
# 로깅 설정 - 목 버전과 동일한 이유/방식(순서 보정 포함)로 콘솔에 DEBUG
# 레벨까지 전부 노출하고, stdout/stderr 버퍼링 차이로 로그 순서가
# 뒤섞이지 않도록 로깅 콘솔 핸들러도 stdout으로 통일한다.
# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
# 실제 클라이언트를 감싸는 투명 로깅 프록시. 로직은 전혀 바꾸지 않고
# 원본 메서드를 그대로 호출한 뒤, 인자/반환값(또는 예외)만 콘솔에 남긴다.
# ---------------------------------------------------------------------
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
            except Exception as e:  # noqa: BLE001 - 실제 API 예외를 그대로 보여줘야 함
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
    """실제 키가 없으면 requests가 인증 실패로 죽거나 빈 결과만 반복해서
    디버깅이 괴로워진다 - 네트워크를 타기 전에 먼저 명확하게 안내한다."""
    missing_critical = []
    if not config.KOSIS_API_KEY or "PLACEHOLDER" in str(config.KOSIS_API_KEY):
        missing_critical.append("KOSIS_API_KEY")

    if missing_critical:
        print("=" * 78)
        print("[안내] 실제 API 키가 설정돼 있지 않습니다.")
        print(f"  누락: {', '.join(missing_critical)}")
        print("  이 outputs 폴더(또는 프로젝트 루트)에 `.env` 파일을 만들고")
        print("  KOSIS_API_KEY=발급받은_키 를 채운 뒤 다시 실행하세요.")
        print("  (이 테스트는 실제 KOSIS 서버로 네트워크 요청을 보내야")
        print("   해서, 키 없이는 애초에 진행할 수 없습니다.)")
        print("=" * 78)
        return False

    if not config.NCP_CLOVASTUDIO_API_KEY or "PLACEHOLDER" in str(config.NCP_CLOVASTUDIO_API_KEY):
        print("[경고] NCP_CLOVASTUDIO_API_KEY가 없습니다 - 표 후보가 여럿일 때의")
        print("  LLM 랭킹/검증 단계가 필요해지면 그 단계에서 실패할 수 있습니다.")
        print("  (이번 시나리오들은 후보가 하나로 좁혀질 가능성이 높아 영향이")
        print("   없을 수도 있습니다 - 실행해서 직접 확인하세요.)\n")

    return True


def build_real_agent() -> KosisInteractiveAgent:
    """목을 주입하지 않고 실제 KosisApiClient/HCXClient를 그대로 쓴다.
    다만 호출 투명성을 위해 LoggingProxy로 감싼다(로직은 안 바뀜)."""
    agent = KosisInteractiveAgent()
    agent.kosis = LoggingProxy(agent.kosis, "KOSIS")
    agent.hcx = LoggingProxy(agent.hcx, "HCX")
    return agent


# ---------------------------------------------------------------------
# 시나리오 A: 최저임금 - 이 프로젝트에서 가장 먼저, 가장 여러 번 실측
# 검증된 예시다(client.py의 search_metadata가 "최저임금" 키워드를 특별
# 취급해 DT_2OEEM1012를 최우선으로 재정렬하는 로직까지 있을 정도). 단일
# 시점 claim이라 파생 비교값 없이 가장 단순한 경로(직접 검색 1건)만
# 탄다 - 실제 API 연결 자체가 되는지 확인하는 첫 관문으로 적합하다.
#
# [주의] claimed_value(9,860원)는 과거 검증 시점의 값이다. 실제 최신
# 고시 최저임금과 다를 수 있으므로, 최종 판정이 MISMATCH로 나와도 그
# 자체는 파이프라인 문제가 아니다 - "실제 조회값이 얼마로 나왔는지"를
# 콘솔에서 직접 확인하고, 필요하면 아래 claimed_value를 그 값으로 고쳐서
# 재실행해보면 된다.
# ---------------------------------------------------------------------
def run_minimum_wage_scenario() -> bool:
    stage("시나리오 A. 최저임금 (단일 시점, 직접 검색)")
    claims = [
        {
            "claim_id": "W001", "claim": "내년도 최저임금은 시간당 9,860원으로 결정됐다",
            "metric": "최저임금", "value": 9860, "unit": "원", "period": "2026",
            "kosis_eligible": True,
        },
    ]
    keywords_by_claim_id = {"W001": ["최저임금"]}
    print(pretty(claims))

    agent = build_real_agent()

    stage("시나리오 A. process_claim_group_keywords 실행 (실제 KOSIS 조회)")
    evidence_by_claim_id = agent.process_claim_group_keywords(
        claims, keywords_by_claim_id, category_hint=None
    )
    print("\n[evidence 결과]")
    print(pretty(evidence_by_claim_id))

    status = evidence_by_claim_id.get("W001", {}).get("query_status")
    ok = status == "success"
    if not ok:
        print(f"\n[!!] 조회 실패(query_status={status!r}) - 위 [KOSIS.*] 로그를"
              " 순서대로 따라가서 어느 호출의 응답이 예상과 다른지 확인하세요.")
        return False

    stage("시나리오 A. adapter + judgment (최종 판정 - 참고용, 실패해도 파이프라인 문제 아님)")
    claim_obj, actual, search_log = adapter.build_inputs(claims[0], evidence_by_claim_id["W001"])
    print(f"  Claim  = {claim_obj}")
    print(f"  Actual = {actual}")
    print(f"  SearchLog = {search_log}")
    for mode in (judgment.Mode.STRICT, judgment.Mode.TOLERANCE):
        result = judgment.judge_claim(claim_obj, actual, search_log, mode=mode)
        print(f"  [{mode.value:9s}] {result.verdict.value}: {result.explanation}")

    print(f"\n[확인] 실제 KOSIS 조회값 = {evidence_by_claim_id['W001'].get('normalized_value')}"
          f"{evidence_by_claim_id['W001'].get('normalized_unit')}"
          f" (표: {evidence_by_claim_id['W001'].get('table_name')})")
    return True


# ---------------------------------------------------------------------
# 시나리오 B: 재배면적 - 3개 claim_id(2025년/2024년 절대값 + 1.0% 증감률
# 파생값)로 쪼개진 keyword-group 입력. 목 버전과 달리 표 ID를 미리
# 정해두지 않는다 - 실제 검색이 "재배면적" 키워드로 어떤 표를 찾아내는지
# 그 자체가 검증 대상이다(실제 KOSIS에는 재배면적 관련 표가 여러 개
# 있을 수 있어 목 버전보다 훨씬 더 "실전"에 가깝다 - 후보가 여럿이면
# _llm_rank_table_candidates/verify_table_candidates_by_meta 경로까지
# 실제로 타면서 HCX 호출도 함께 검증된다).
# ---------------------------------------------------------------------
def run_crop_area_scenario() -> bool:
    stage("시나리오 B. 재배면적 (2시점 직접검색 + 1개 파생 비교값)")
    raw_sentence = "재배면적이 10만4943㏊로 작년 10만5959㏊보다 1.0% 감소했다."
    claims = [
        {
            "claim_id": "C001", "claim": raw_sentence, "metric": "재배면적",
            "value": 104943, "unit": "ha", "period": "2025", "kosis_eligible": True,
        },
        {
            "claim_id": "C002", "claim": raw_sentence, "metric": "재배면적",
            "value": 105959, "unit": "ha", "period": "2024", "kosis_eligible": True,
        },
        {
            "claim_id": "C003", "claim": raw_sentence, "metric": "재배면적",
            "value": 1.0, "unit": "%", "period": None, "kosis_eligible": True,
        },
    ]
    keywords_by_claim_id = {"C001": ["재배면적"], "C002": ["재배면적"]}
    print(pretty(claims))

    stage("시나리오 B. route_claim_group")
    routing = adapter.route_claim_group(claims)
    print(pretty(routing))

    agent = build_real_agent()

    stage("시나리오 B. process_claim_group_keywords 실행 (실제 KOSIS 조회)")
    evidence_by_claim_id = agent.process_claim_group_keywords(
        claims, keywords_by_claim_id, category_hint=None
    )
    print("\n[evidence 결과]")
    print(pretty(evidence_by_claim_id))

    direct_ok = all(
        evidence_by_claim_id.get(cid, {}).get("query_status") == "success"
        for cid in ("C001", "C002")
    )
    if not direct_ok:
        print("\n[!!] C001/C002 중 하나 이상 조회 실패 - 실제 KOSIS에 '재배면적'")
        print("     검색이 이 예시와 다른 표로 연결됐거나, 그 표에 2024/2025년")
        print("     자료가 없을 수 있습니다. 위 [KOSIS.*] 로그에서 실제로 어떤")
        print("     표가 확정됐는지(search_metadata 반환값) 먼저 확인하세요.")
        return False

    stage("시나리오 B. adapter + judgment (최종 판정 - 참고용)")
    for c in claims:
        ev = evidence_by_claim_id.get(c["claim_id"], {})
        if ev.get("query_status") != "success":
            print(f"\n--- {c['claim_id']} (조회 실패: {ev}) ---")
            continue
        claim_obj, actual, search_log = adapter.build_inputs(c, ev)
        print(f"\n--- {c['claim_id']} ---")
        print(f"  Claim  = {claim_obj}")
        print(f"  Actual = {actual}")
        for mode in (judgment.Mode.STRICT, judgment.Mode.TOLERANCE):
            result = judgment.judge_claim(claim_obj, actual, search_log, mode=mode)
            print(f"  [{mode.value:9s}] {result.verdict.value}: {result.explanation}")

    return True


def main() -> int:
    print("#" * 78)
    print("# KOSIS 팩트체크 파이프라인 - 실제 API 통합 테스트")
    print("# (claim 목록 -> 실제 KOSIS/HCX 조회 -> adapter -> 최종 판정)")
    print("#" * 78)

    if not check_api_keys():
        return 2

    ok_a = run_minimum_wage_scenario()
    ok_b = run_crop_area_scenario()

    stage("최종 결과")
    print(f"  시나리오 A (최저임금, 단일 시점):            {'PASS' if ok_a else 'FAIL'}")
    print(f"  시나리오 B (재배면적, 2시점+파생비교값):     {'PASS' if ok_b else 'FAIL'}")
    print("\n[참고] 여기서 '성공' 기준은 검색/조회가 끝까지 에러 없이 끝났는가"
          "(query_status=success)이지, 최종 판정이 VERIFIED인가가 아닙니다.")
    print("  claimed_value가 실제 최신 KOSIS 수치와 다르면 MISMATCH가 나오는 게")
    print("  정상일 수 있습니다 - 위에 출력된 실제 조회값을 직접 확인하세요.")

    if ok_a and ok_b:
        print("\n[SUCCESS] 실제 KOSIS API로 전체 파이프라인이 끝까지 연결됨을 확인했습니다.")
        return 0
    print("\n[FAILURE] 위 STAGE 로그와 [KOSIS.*]/[HCX.*] 호출 로그를 순서대로"
          " 따라가서 어디서 막혔는지 확인하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
