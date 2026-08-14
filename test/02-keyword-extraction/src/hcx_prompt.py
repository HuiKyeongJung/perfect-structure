# HCX 키워드 생성과 필터링용 messages를 만듭니다.
"""HCX 키워드 확장 단계의 프롬프트 messages를 생성한다."""

from typing import Dict, List


def build_hcx005_expansion_messages(
    original_keyword: str,
    seed_keyword: str,
    max_keywords: int = 10,
) -> List[Dict[str, str]]:
    """원 metric 문맥을 기준으로 관련 통계 검색 후보를 생성하는 messages를 만든다."""

    system_prompt = """당신은 한국 공식통계 검색어를 돕는 전문가입니다.
목표는 원본 metric과 완전히 같은 표현만 찾는 것이 아니라, 다음 통합검색 단계의 검색 후보의 recall을 높이는 것입니다.
현재 호출의 증폭 대상은 seed_keyword 하나입니다. original_keyword는 의미를 이해하기 위한 참고 문맥입니다.
seed_keyword가 original_keyword와 다르면 seed_keyword를 독립적으로 증폭하세요. original_keyword의 대상·문맥을 모든 후보에 강제로 포함하지 마세요.
seed_keyword의 원래 문자열도 모든 후보에 강제로 포함할 필요가 없습니다. 의미적으로 연결된 공식 통계 표현으로 변환할 수 있습니다.
뉴스 표현을 KOSIS 통계 용어 공간으로 변환하세요. 입력 seed와 관련된 짧은 명사형 통계 검색 후보를 다양하게 생성하세요.
seed_keyword를 단독으로 일반 확장해도 됩니다. 원본의 대상이 빠진 상위·일반 통계 표현도 허용합니다.
KOSIS 통합검색에 직접 입력할 가치가 있는 공식 통계 지표명 또는 통계표 제목형 표현을 반드시 포함하세요.
생성 우선순위는 공식 지표명에 가까운 표현, 분야명+지표, 대상+지표, 분류축+지표, 시간축+지표 순서입니다.
seed_keyword가 original_keyword와 같고 원본 metric에 대상·주제어가 있으면 이를 유지한 대상+지표 후보도 반드시 생성하세요. 이 경우 대상이 빠진 일반 통계 후보와 대상이 유지된 후보를 함께 생성하세요.
분류축에는 산업별·품목별·지역별·국가별·연령별·성별을, 시간축에는 월별·연간·일평균·연도별을 활용할 수 있습니다.
상위·하위 통계 개념, 일반 표현과 공식 통계 표현의 변환, 띄어쓰기·붙여쓰기, 영문 약어를 허용합니다.
공식 표현 변환 예시는 물가 → 소비자물가지수, 취업자 → 취업자 수, 출산율 → 합계출산율, 노인 → 고령인구입니다.
금액·규모·비율·증감률·증가율·감소율·현황·추이·실적·지수·수·인구·잔액처럼 통계 검색에 쓰이는 측정 표현을 활용할 수 있습니다.
고용·인구·교육·소득·경제·재정·산업·무역처럼 넓은 단어 하나보다 구체적인 복합 통계 표현을 우선하세요.
예를 들어 고용 → 고용률, 취업자 수, 산업별 취업자 수처럼 확장하세요.
단순 연관어보다 원본·seed에서 직접 이어지는 통계 지표 표현을 우선하되, 하나의 정답으로 좁히지 마세요.
원본 metric에 방향 표현이 있으면 원본 metric과 반대 방향의 표현은 생성하지 마세요. 흑자/적자, 증가/감소, 상승/하락, 증가율/감소율은 서로 반대 방향입니다.
원본 metric과 직접 연결되지 않는 정책·기술·분석방법·기관명·설명문·원인 해석·일반 상식·기업·시장 전략 표현은 생성하지 마세요.
실제 존재 여부가 확실하지 않은 구체적인 조사명을 임의로 만들지 마세요. 확실하지 않으면 조사명이 아닌 분야명 수준으로 작성하세요.
응답은 문자열만 가진 JSON 배열만 반환하세요. Markdown, 번호, 설명, 이유, 앞뒤 문장은 금지합니다."""
    user_prompt = (
        f"원본 metric: {original_keyword}\n"
        f"확장할 seed: {seed_keyword}\n"
        f"최대 {max_keywords}개의 후보를 생성하세요."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def build_hcx007_filter_messages(
    original_keyword: str,
    seed_keyword: str,
    candidates: List[str],
) -> List[Dict[str, str]]:
    """HCX-005 후보 중 관련 통계 검색어만 선택하는 messages를 만든다."""

    system_prompt = """당신은 한국 공식통계 검색어 후보의 명백한 노이즈를 제거하는 전문가입니다.
유지할 후보의 index만 JSON 정수 배열로 반환하세요. 후보 문자열을 다시 출력하거나 새 후보를 만들지 마세요.
강한 의미 일치 필터가 아니라 명백한 비통계 노이즈를 제거하는 역할만 합니다. 명백한 비통계 노이즈만 REMOVE하세요. 애매하면 KEEP하세요.
후보를 최소화하는 것이 목표가 아닙니다. 좋은 후보를 제거하는 false negative가 불필요한 후보를 남기는 false positive보다 더 위험합니다.
후보가 KOSIS 검색에 조금이라도 유용할 수 있으면 KEEP하세요. original_keyword와 가장 정확히 일치하는 후보만 고르지 마세요.
후보 10개 중 7~10개를 KEEP하는 것도 정상적인 결과입니다. 개수를 맞추지 말고 실제 검색 가치가 있는 후보를 모두 KEEP하세요.
최종 검색 결과와 관련도 판단은 downstream KOSIS 통합검색 단계가 수행합니다. 후보를 1~2개로 과도하게 줄이지 마세요.
원본보다 넓은 의미라도 통계 검색어로 활용 가능하면 유지하세요. original_keyword의 대상이 없어도 seed_keyword와 관련된 통계 표현이면 KEEP할 수 있습니다.
원본 subject 문자열이 없어도 seed 기반의 정상적인 통계 표현이면 KEEP하세요.
공식 지표명에 가까운 표현, 대상+지표, 분야+지표, 분류축+지표, 시간축+지표, 상위·하위 통계 개념은 적극적으로 KEEP하세요.
시간축이 달라지거나 생략돼도 통계 검색 가치가 있으면 KEEP하세요. 세입결손처럼 같은 통계 분야의 관련 지표도 검색 경로가 될 수 있으므로 KEEP할 수 있습니다.
시간축·분류축이 달라도 seed와 관련된 통계 검색어는 적극적으로 KEEP하세요.
월별·연간·일평균·연도별·분기별 + 지표는 KEEP 가능한 시간축 후보입니다.
산업별·지역별·국가별·품목별·연령별·성별 + 지표는 KEEP 가능한 분류축 후보입니다.
원본 metric에 방향 표현이 있으면 명백한 반대 방향 후보는 반드시 REMOVE하세요. 원본 metric과 반대 방향의 후보는 제거하세요. 흑자/적자, 증가/감소, 상승/하락, 확대/축소, 증가율/감소율은 서로 반대 방향입니다.
방향이 없는 상위 지표는 KEEP할 수 있습니다.
정책·절차·행정 표현, 일반적인 서술 표현, 기술, 기관명, 조사·연구·분석 방법, 설명문, 원인·결과 해석, 통계 지표로 보기 어려운 비정상 문장형 표현만 REMOVE하세요. 감소 원인 분석·효과 분석·문제 심화·악화 정도와 국회 심의 과정·제출 시기·승인 절차는 명백한 비통계 노이즈로 REMOVE하세요. 국회 심의·심사, 제출 시기, 승인 절차는 통계 지표가 아니므로 REMOVE하세요.
유지할 후보가 없으면 []를 반환하세요.
설명하지 마세요. Markdown을 사용하지 마세요. 번호를 붙이지 마세요. JSON 정수 배열 하나만 출력하세요."""
    indexed_candidates = "\n".join(
        f"{index}: {candidate}" for index, candidate in enumerate(candidates)
    )
    user_prompt = (
        f"원본 metric: {original_keyword}\n"
        f"판단 기준 seed: {seed_keyword}\n"
        f"candidates:\n{indexed_candidates}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def build_hcx007_retry_messages(
    original_keyword: str,
    seed_keyword: str,
    candidates: List[str],
) -> List[Dict[str, str]]:
    """HCX-007의 JSON 형식 오류 시 사용할 재시도 messages를 만든다."""

    messages = build_hcx007_filter_messages(
        original_keyword,
        seed_keyword,
        candidates,
    )
    retry_instruction = """
직전 응답은 출력 형식이 올바르지 않았습니다. 같은 판단을 다시 수행하세요.
설명하지 마세요. Markdown을 사용하지 마세요. 번호를 붙이지 마세요.
반드시 JSON 정수 배열 하나만 반환하세요. 입력 candidates에 실제 존재하는 index만 반환하세요.
정상 예: [0, 2]
잘못된 예: 선택 결과는 다음과 같습니다. 또는 ```json 코드 블록"""

    return [
        {"role": "system", "content": messages[0]["content"] + retry_instruction},
        messages[1],
    ]
