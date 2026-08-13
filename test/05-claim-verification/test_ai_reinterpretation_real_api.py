"""판정 단계 AI 재해석(A/B/C/D, README 3장) - 실제 HCX API 버전
(2026-08-10).

judgment.py의 __main__ 데모(A/B/C/D/H 9개 케이스)는 지금까지
`_DemoHCXClient`(사람이 정답을 미리 채워둔 문자열 매칭 목)로만 검증됐다
- "배선이 맞는가"는 확인했지만 "실제 HCX가 이 프롬프트에 기대한 6개
선택지 중 하나로 정확히 답하는가"는 아직 확인 전이었다(README 5장 마지막
문단에 명시).

이 파일은 KOSIS는 전혀 안 건드리고(합성 ActualEvidence를 그대로 씀 -
검증 대상은 표/컬럼 검색이 아니라 AI 재해석 자체), `hcx_client`만 진짜
`client.HCXClient()`로 바꿔서 judgment.py의 __main__ 데모 중 AI가 실제로
필요한 4개 케이스(A/B/C/D 각 1개 - AI 없으면 오판하고 AI가 있어야 정정
되는 케이스만 골랐다. 규칙만으로 이미 해결되는 케이스와 H는 AI를 안 쓰므로
제외)를 그대로 재현한다.

[성공 기준] "실제 HCX가 여기 쓴 그대로 정답을 준다"고 매번 보장할 수는
없다(LLM이라 프롬프트 표현에 따라 흔들릴 수 있음) - 그래서 판정 자체가
"AI 없을 때의 오판"에서 "AI 있을 때의 정답"으로 실제로 바뀌는지를 성공
기준으로 삼는다. 만약 실제 HCX가 다른 표현/다른 선택을 해서 기대와 다른
결과가 나오면, 그건 코드 버그가 아니라 "이 프롬프트가 실제 HCX에게
얼마나 잘 먹히는지"에 대한 유용한 신호이니 ai_note를 그대로 출력해서
눈으로 확인할 수 있게 했다.

실행: python3 test_ai_reinterpretation_real_api.py
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from config import config
from client import HCXClient
from judgment import (
    ActualEvidence,
    Claim,
    EvidencePoint,
    Mode,
    SearchLog,
    UnitCategory,
    judge_claim,
)


def stage(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"[STAGE] {title}")
    print("=" * 78)


def check_api_key() -> bool:
    if not config.NCP_CLOVASTUDIO_API_KEY or "PLACEHOLDER" in str(config.NCP_CLOVASTUDIO_API_KEY):
        print("=" * 78)
        print("[안내] NCP_CLOVASTUDIO_API_KEY가 설정돼 있지 않습니다.")
        print("  이 테스트는 AI 재해석 자체를 검증하는 게 목적이라 HCX 키가")
        print("  필수입니다. .env에 채운 뒤 다시 실행하세요.")
        print("=" * 78)
        return False
    return True


# ---------------------------------------------------------------------
# 4개 케이스 - judgment.py __main__의 A/B/C/D 데모와 완전히 같은 Claim/
# ActualEvidence를 그대로 재사용한다(값이 달라지면 "AI가 실제로 오판을
# 정정했는가"를 비교할 기준 자체가 흔들리므로 그대로 옮겨왔다).
# 각 항목: (이름, Claim, ActualEvidence, 기대 verdict(AI 있을 때, tolerance
# 모드 기준), AI 없이 돌리면 나오는 오판 verdict - 대조용으로 같이 출력)
# ---------------------------------------------------------------------
CASES = [
    (
        "A. 부정문 반전 - '실업률이 9%를 넘어서지 못했다'",
        Claim("실업률이 9%를 넘어서지 못했다", 9.0, "%", None, UnitCategory.PERCENT),
        ActualEvidence(8.5, "%", "101", "DT_UNEMP", "실업률", None),
        "VERIFIED",
    ),
    (
        "B. '이상'의 다의어 - '이상 기후로 인해 배추 가격이 30% 폭등했다'",
        Claim("이상 기후로 인해 배추 가격이 30% 폭등했다", 30.0, "%", None, UnitCategory.PERCENT),
        ActualEvidence(55.0, "%", "101", "DT_CABBAGE", "농산물 가격동향", None),
        "MISMATCH",
    ),
    (
        "C. 사전에 없는 근사 표현 - '실업률이 8%에 가까운 수준을 기록했다'",
        Claim("실업률이 8%에 가까운 수준을 기록했다", 8.0, "%", None, UnitCategory.PERCENT),
        ActualEvidence(7.65, "%", "101", "DT_UNEMP", "실업률", None),
        "VERIFIED",
    ),
    (
        "D. 사전에 없는 배수 표현 - '전세가율이 작년보다 곱절로 뛰었다'",
        Claim(
            "전세가율이 작년보다 곱절로 뛰었다", 2.0, "배", None, UnitCategory.OTHER,
        ),
        ActualEvidence(
            table_org_id="101", table_tbl_id="DT_JEONSE", table_nm="전세가율 동향",
            is_comparison=True,
            values=[EvidencePoint("2026", 130.0, "%"), EvidencePoint("2025", 65.0, "%")],
        ),
        "VERIFIED",
    ),
]


def run_case(name, claim, actual, expected_verdict, hcx_client) -> bool:
    stage(name)
    log = SearchLog("RESOLVED", True, [actual.table_nm or "(합성 데이터)"])

    print("--- AI 없이(대조군 - 오판이 그대로 나오는지 확인용) ---")
    no_ai = judge_claim(claim, actual, log, mode=Mode.TOLERANCE, hcx_client=None)
    print(f"  [tolerance] {no_ai.verdict.value}: {no_ai.explanation}")

    print("\n--- 실제 HCX 사용 ---")
    result = judge_claim(claim, actual, log, mode=Mode.TOLERANCE, hcx_client=hcx_client)
    ai_tag = " [AI 사용]" if result.ai_used else " [AI 미사용 - 규칙만으로 이미 해결됐거나 호출 실패]"
    print(f"  [tolerance]{ai_tag} {result.verdict.value}: {result.explanation}")
    if result.ai_note:
        print(f"  ai_note: {result.ai_note}")

    ok = result.verdict.value == expected_verdict
    print(f"\n-> 기대 verdict(AI 있을 때): {expected_verdict} / 실제: {result.verdict.value}")
    print("PASS" if ok else "FAIL (실제 HCX가 다른 선택을 했을 수 있음 - 위 ai_note 확인)")
    return ok


def main() -> int:
    print("#" * 78)
    print("# 판정 단계 AI 재해석(A/B/C/D) - 실제 HCX API 검증")
    print("#" * 78)

    if not check_api_key():
        return 2

    hcx_client = HCXClient()

    results = {}
    for name, claim, actual, expected in CASES:
        results[name] = run_case(name, claim, actual, expected, hcx_client)

    stage("최종 결과")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    all_ok = all(results.values())
    if all_ok:
        print("\n[SUCCESS] 실제 HCX로도 A/B/C/D 재해석이 기대한 판정으로 이어짐을 확인했습니다.")
        return 0
    print("\n[일부 실패] 코드 버그가 아니라 실제 HCX가 이 프롬프트에서 다른 선택을")
    print("  했을 가능성이 높습니다 - 위 ai_note에 실제 HCX 응답이 남아있으니")
    print("  프롬프트를 더 명확하게 다듬을지 판단하는 데 참고하세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
