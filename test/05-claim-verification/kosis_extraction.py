"""자동 분리된 모듈 (kosis_agent.py 리팩터링) - 동작은 기존과 동일합니다."""

import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple, Union

from kosis_config import (
    INDICATOR_ALIAS_MAP,
    ROW_CATEGORY_ALIAS_MAP,
    QUALIFIER_KEYWORDS,
    DEMOGRAPHIC_ROW_ALIAS_MAP,
)

logger = logging.getLogger("Task2.KosisChatAgent")


class ExtractionMixin:
    """사용자 입력에서 엔티티(지표/시점/카테고리)를 추출하고, 요청
    종류(mom/yoy/explicit_pair/single/forecast 등)를 분류한다.
    self.hcx/self.slots는 KosisInteractiveAgent(kosis_agent.py)가
    최종적으로 제공한다 (mixin이라 이 파일만으로는 동작하지 않음).
"""

    def extract_delta_entities(
        self, user_input: str
    ) -> Dict[str, Optional[Any]]:
        """[HCX + Rule Hybrid Extractor] HyperCLOVA X를 활용한 정밀 엔티티 추출"""
        extracted = {
            "extracted_indicator": None,
            "extracted_base_year": None,
            "extracted_base_month": None,  # YYYYMM
            "extracted_compare_month": None,  # YYYYMM - 기사에 두 시점이
            # 명시적으로 함께 나온 경우(예: "202509 대비 202409")에만 채움
            "needs_period_confirmation": False,
            "has_comparison_context": None,
            "has_mom_context": None,  # 전월/지난달 대비 (월간)
            "has_yoy_context": None,  # 전년/작년 대비 (연간)
            "extracted_unit_cat": None,
            "extracted_category_hint": None,
            # 기사/주장 문장이 실제로 인용한 수치 (예: "4,248명" -> 4248.0).
            # 이 값이 있으면, 통계표 후보가 여럿일 때 후보마다 실제 값을
            # 조회해 이 값과 가장 가까운 후보를 채택하는 데 쓴다
            # (resolve_target_table의 다중 표 비교 - gather_candidate_values).
            "extracted_claimed_value": None,
            # extracted_claimed_value 바로 뒤에 붙은 단위 글자(예: "4,248명"
            # -> "명", "1,200억원" -> "원"). 후보 표들의 실제 조회 값 단위와
            # 비교해서, 사람 수를 물었는데 "개"/"원" 단위 후보가 나오는
            # 것처럼 애초에 종류가 다른 값을 걸러내는 데 쓴다
            # (_unit_compatible 참고).
            "extracted_claimed_unit": None,
        }

        # 1. 🛡️ Rule-based 빠른 추출 (연도/월 및 키워드 안전장치)

        # 1-0. "202509 대비 202409"처럼 YYYYMM 형식 두 시점이 원문에 그대로
        # 나온 경우를 최우선으로 잡는다. 이런 경우는 "전월대비"/"전년동월대비"
        # 같은 문구로 자동 계산하면 안 되고(계산하면 자기 자신의 전월/전년을
        # 엉뚱하게 계산해버림 - 위 실제 사례처럼 202509가 통째로 무시되고
        # 202409의 전월인 202408이 비교시점으로 잘못 채워지는 사고가 난다),
        # 두 시점을 있는 그대로 써야 한다. 다만 "A 대비 B"에서 어느 쪽이
        # 기준(조회 대상) 시점이고 어느 쪽이 비교 시점인지는 문장만으로
        # 확신할 수 없으니, 이 경우는 process_turn에서 사용자에게 한 번
        # 되확인한다.
        yyyymm_matches = list(
            re.finditer(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(?!\d)", user_input)
        )
        if len(yyyymm_matches) >= 2:
            first, second = yyyymm_matches[0], yyyymm_matches[1]
            first_ym = first.group(1) + first.group(2)
            second_ym = second.group(1) + second.group(2)
            between = user_input[first.end() : second.start()]
            if "대비" in between:
                # "A 대비 B ...": B가 실제 조회/주장의 대상(기준) 시점,
                # A가 비교 기준선인 경우가 많다 ("전월대비 4.4%"와 같은
                # 관용구 어순과 동일).
                tentative_base, tentative_compare = second_ym, first_ym
            else:
                tentative_base, tentative_compare = second_ym, first_ym
            extracted["extracted_base_month"] = tentative_base
            extracted["extracted_compare_month"] = tentative_compare
            extracted["extracted_base_year"] = tentative_base[:4]
            extracted["needs_period_confirmation"] = True
        else:
            # "2024년 12월"처럼 연+월이 함께 명시된 경우를 잡는다.
            # (전월대비/월간 통계는 "몇 년도"만으로는 시점을 특정할 수 없다 -
            #  반드시 몇 년 몇 월인지까지 알아야 startPrdDe/endPrdDe(YYYYMM)를
            #  만들 수 있다.)
            month_match = re.search(
                r"(20\d{2}|19\d{2})\s*년\s*(1[0-2]|0?[1-9])\s*월", user_input
            )
            if month_match:
                yr, mo = month_match.group(1), int(month_match.group(2))
                extracted["extracted_base_month"] = f"{yr}{mo:02d}"
                extracted["extracted_base_year"] = yr
            else:
                year_match = re.search(r"(20\d{2}|19\d{2})", user_input)
                if year_match:
                    extracted["extracted_base_year"] = year_match.group(1)

        # "농림어업 포함/제외"처럼 같은 항목 안에서 카테고리가 갈리는 경우의
        # 명시적 언급을 잡아낸다. (사용자가 직접 말한 경우에만 채택 -
        # 기본값은 process_turn에서 DEFAULT_INDICATOR_METADATA로 처리)
        if "제외" in user_input:
            extracted["extracted_category_hint"] = "제외"
        elif "포함" in user_input:
            extracted["extracted_category_hint"] = "포함"
        else:
            # "대형 항공사"/"중소기업"처럼 규모 수식어가 나오면 실제 KOSIS
            # 분류 라벨("300인 이상" 등)로 바꿔서 category_hint에 채운다.
            # 이러면 기존 _select_target_row의 category_hint 매칭 로직을
            # 그대로 재사용해서 행(카테고리)까지 특정할 수 있다.
            for alias, kosis_label in ROW_CATEGORY_ALIAS_MAP.items():
                if alias in user_input:
                    extracted["extracted_category_hint"] = kosis_label
                    break
            else:
                # 규모 수식어가 없으면 "청년"/"고령자"/"여성" 같은 인구통계학적
                # 수식어도 같은 방식으로 시도해본다("청년 실업률" -> 연령별
                # 축의 "15~29세" 힌트). 정확한 라벨이 아닐 수 있어서 어차피
                # resolve_category_hint_axis가 실제 메타와 대조해 검증하고,
                # 안 맞으면 폴백 로직을 탄다.
                for alias, kosis_label in DEMOGRAPHIC_ROW_ALIAS_MAP.items():
                    if alias in user_input:
                        extracted["extracted_category_hint"] = kosis_label
                        break

        # 기사 주장 수치를 뽑아둔다. "4,248명"처럼 천단위 쉼표가 들어간
        # 숫자를 우선 채택한다 - "2023년"/"300인"처럼 쉼표 없는 숫자(연도,
        # 규모 등)와 구분하기 위한 실용적인 신호다. 없으면 쉼표 없는 숫자
        # 중 3자리 이상인 것(단순 연도로 보기 어려운 값)을 시도한다.
        # 주의: 앞뒤에 \b(단어 경계)를 걸면 안 된다 - Python 정규식의 \w는
        # 한글도 단어 문자로 취급해서, "6,806명"처럼 숫자 바로 뒤에 공백 없이
        # 한글 단위가 붙으면 숫자와 "명" 사이에 경계가 생기지 않아 매칭
        # 자체가 실패한다(실측으로 발견). 콤마 3자리 구분 패턴 자체가 이미
        # 충분히 구체적인 신호라 경계 없이 찾아도 오탐 위험은 낮다.
        comma_num = re.search(r"(?<!\d)\d{1,3}(?:,\d{3})+(?:\.\d+)?", user_input)
        if comma_num:
            extracted["extracted_claimed_value"] = float(
                comma_num.group(0).replace(",", "")
            )
            # 숫자 바로 뒤 한글/기호 단위를 잡는다("6,806명"->"명"). 조사가
            # 바로 붙는 경우("명이다")도 있으니 2글자까지만 보고, 그마저도
            # 나중에 categorize()에서 "명"/"원"/"%"/"개" 같은 핵심 글자
            # 포함 여부로만 판단하므로 뒤에 조사가 살짝 섞여도 괜찮다.
            unit_match = self._CLAIM_UNIT_RE.match(user_input[comma_num.end():])
            if unit_match:
                extracted["extracted_claimed_unit"] = unit_match.group(1)

        # [2026-07 실측 버그 수정] "통계청이 발표한 '9월 산업활동동향'에
        # 따르면 ... 설비투자지수는 ..." 같은 문장은 발표(보도자료) 제목을
        # 인용부호로 인용하면서, 정작 확인하려는 지표는 따옴표 밖에 따로
        # 나온다. "산업활동동향" 자체가 (생산/소비/투자를 한 번에 발표하는
        # 통계청 월간 보도자료 이름이라) 이미 INDICATOR_ALIAS_MAP에 별칭으로
        # 등록돼 있어서, 길이가 같은 "설비투자지수"와 동시에 매치되면 사전
        # 등록 순서상 먼저 있던 "산업활동동향"이 이겨버려 완전히 다른 표
        # (전산업생산지수)로 새는 사고가 실측됐다(설비투자지수 스트레스
        # 테스트로 발견). 게다가 이 1차 매치는 한 번 정해지면 아래 LLM
        # 결과로도 절대 안 덮어써진다("Rule 결과보다 우선"). 인용부호
        # ('...'/"..."/「...」/『...』) 안에 있는 별칭은 보통 발표/보도자료
        # 제목 그 자체를 인용한 것이지 확인 대상 지표가 아니므로, 따옴표
        # 밖에서 매치되는 별칭을 최우선으로 찾고, 없을 때만(=전체가 정말
        # 따옴표 안에만 있을 때) 기존처럼 따옴표 안 매치라도 채택한다.
        quoted_spans: List[tuple] = []
        for qpat in (r"'[^']*'", r'"[^"]*"', r"「[^」]*」", r"『[^』]*』"):
            for qm in re.finditer(qpat, user_input):
                quoted_spans.append((qm.start(), qm.end()))

        def _inside_quotes(start: int, end: int) -> bool:
            return any(qs <= start and end <= qe for qs, qe in quoted_spans)

        sorted_aliases = sorted(
            INDICATOR_ALIAS_MAP.keys(), key=len, reverse=True
        )
        # [2026-07-24 추가 - 부정 접두사 오탐 방지] "비경제활동인구"(노동
        # 시장 밖 인구)는 "경제활동인구"(노동 시장 안 인구)를 문자 그대로
        # 부분 문자열로 포함하는데, 이 순수 substring 매칭이 그걸 구분 못
        # 해 완전히 반대(때로는 무관한) 개념으로 잘못 매치하는 사고가
        # 실측됐다("쉬었음인구_7만명증가" 골든셋에서 indicator가 "쉬었음"
        # 대신 "경제활동인구"로 새어버림 - 이 1차 매치는 HCX 결과로도 절대
        # 안 덮어써지므로 한 번 잘못 걸리면 그대로 확정된다). "비경제활동/
        # 비농림어업/비금융/비정규직"처럼 "비-" 접두사가 붙으면 원래 지표와
        # 무관하거나 정반대인 KOSIS 개념이 되는 경우가 흔해, 매치 직전
        # 글자가 "비"면 이 매치는 건너뛰고 문장 내 다른 위치의(부정 접두사가
        # 안 붙은) 매치를 계속 찾는다.
        _NEGATION_PREFIX_CHARS = ("비",)
        # [2026-07-24 추가] "취업자도 실업자도 아닌 비경제활동인구..."처럼
        # 접두사가 아니라 뒤에 이어지는 "~도 아닌" 절로 앞선 후보들을
        # 통째로 부정하는 구문도 있다 - 매치 직후 짧은 구간(15자) 안에
        # "아닌"/"아니라"/"아니고"/"아니며"가 나오면 그 매치도 건너뛴다.
        _POST_NEGATION_RE = re.compile(r"아니라|아닌|아니고|아니며")

        def _find_alias_occurrence(alias: str) -> Optional[int]:
            start = 0
            while True:
                idx = user_input.find(alias, start)
                if idx == -1:
                    return None
                if idx > 0 and user_input[idx - 1] in _NEGATION_PREFIX_CHARS:
                    start = idx + 1
                    continue
                after = user_input[idx + len(alias):idx + len(alias) + 15]
                if _POST_NEGATION_RE.search(after):
                    start = idx + 1
                    continue
                return idx

        quoted_fallback = None
        for alias in sorted_aliases:
            idx = _find_alias_occurrence(alias)
            if idx is None:
                continue
            if _inside_quotes(idx, idx + len(alias)):
                if quoted_fallback is None:
                    quoted_fallback = alias
                continue
            extracted["extracted_indicator"] = alias
            break
        else:
            if quoted_fallback is not None:
                extracted["extracted_indicator"] = quoted_fallback

        # 월간(전월/지난달) vs 연간(전년/작년) 비교를 구분해서 잡는다.
        # 기존 has_comparison_context는 하위 호환을 위해 그대로 유지한다.
        if any(kw in user_input for kw in ["전월", "지난달", "지난 달"]):
            extracted["has_mom_context"] = True
        if any(kw in user_input for kw in ["전년", "작년"]):
            extracted["has_yoy_context"] = True
        if any(
            kw in user_input
            for kw in ["전년", "작년", "전월", "대비", "증가", "감소", "늘", "줄"]
        ):
            extracted["has_comparison_context"] = True

        # 2. 🚀 HyperCLOVA X (HCX) 호출을 통한 의미 기반 엔티티 추출
        system_instruction = (
            "당신은 통계 대화형 에이전트의 엔티티 추출기입니다.\n"
            "사용자의 입력 문장에서 아래 항목을 분석하여 JSON 형식으로만"
            " 응답하세요.\n\n"
            "- extracted_indicator: 통계 지표명 (최저임금, 물가, 수출액,"
            " 출생아수, 산업생산 등 / 없으면 null). 중요: 성별/연령대/규모"
            " 같은 수식어가 지표명 앞에 붙어 있으면(예: \"청년 실업률\","
            " \"여성 고용률\", \"대형 항공사 정비사\") 그 수식어를 빼고"
            " 표준 지표명만 남기지 말고, 수식어까지 포함한 원래 표현"
            " 그대로 추출하세요(\"청년 실업률\" -> \"청년 실업률\", 절대"
            " \"실업률\"만 남기지 마세요) - 그 수식어가 실제로 어느 통계를"
            " 찾아야 하는지 결정하는 핵심 조건입니다.\n"
            "- extracted_base_year: 4자리 기준 연도 (YYYY / 없으면 null)\n"
            "- extracted_base_month: 연+월이 함께 언급된 경우 YYYYMM 형식"
            " (예: 2024년 12월 -> 202412 / 없으면 null)\n"
            "- has_mom_context: 전월/지난달 대비 맥락이면 true, 아니면 false\n"
            "- has_yoy_context: 전년/작년 대비 맥락이면 true, 아니면 false\n"
            "- has_comparison_context: 위 둘 중 하나라도 해당하거나 대비/증가/"
            "감소 맥락이 있으면 true, 없으면 false\n"
            "- extracted_unit_cat: 세부 단위나 기준 (시간당, 출생아수 등 / 없으면"
            " null)\n\n"
            "반드시 순수 JSON 객체 형태로만 출력하세요 (마크다운 백틱 ``` 사용"
            " 금지)."
        )

        messages = [
            {"role": "system", "content": system_instruction},
            {
                "role": "user",
                "content": (
                    f"현재 슬롯 상태: {json.dumps(self.slots, ensure_ascii=False)}\n사용자"
                    f' 입력: "{user_input}"'
                ),
            },
        ]

        try:
            raw_res = self.hcx.generate_completion(messages, temperature=0.1)
            # 마크다운 백틱 제거 가드레일
            clean_res = re.sub(r"```json|```", "", raw_res).strip()
            llm_result = json.loads(clean_res)

            # Rule 결과보다 우선하거나 빈 항목을 HCX 결과로 보완
            for key, val in llm_result.items():
                if val is not None and extracted.get(key) is None:
                    extracted[key] = val

        except Exception as e:
            logger.warning(
                f"⚠️ [HCX 엔티티 추출 예외 발생 - Rule-based 결과 유지]: {e}"
            )

        # [HCX 스칼라 필드 방어] HCX-007은 문장에 개념이 여러 개 섞여
        # 있으면(예: "청년 실업자 수"와 "청년 실업률"이 한 문단에 같이
        # 나옴) 문자열이어야 할 필드에 리스트를 통째로 채워 보내는 경우가
        # 실측됐다(extracted_indicator -> ['청년 실업자 수', '청년
        # 실업률']). 이걸 그대로 쓰면 f-string으로 대괄호까지 검색어에
        # 섞여 들어가("청년 ['청년 실업자 수', ...]") 완전히 엉뚱한 표를
        # 검색하게 된다(2026-07 실측 - "청년고용률" 표로 잘못 감). 스칼라
        # 여야 하는 필드에 리스트가 오면, 원문에 그대로("그대로"가 핵심 -
        # 지어낸 축약형이 아니라 실제 언급 빈도로 판단) 가장 많이 등장하는
        # 항목을 고른다 - 그게 이 문단에서 실제로 검증하려는 핵심 개념일
        # 가능성이 높다.
        def _pick_best_scalar_from_list(values: List[Any]) -> Optional[str]:
            strs = [v for v in values if isinstance(v, str) and v.strip()]
            if not strs:
                return None

            def _score(s: str) -> "tuple[int, int]":
                count = user_input.count(s)
                pos = user_input.find(s) if s in user_input else len(user_input)
                return (-count, pos)  # 등장 횟수 많은 순 -> 먼저 나온 순

            strs.sort(key=_score)
            return strs[0]

        _SCALAR_STR_FIELDS = (
            "extracted_indicator",
            "extracted_category_hint",
            "extracted_base_year",
            "extracted_base_month",
            "extracted_compare_month",
            "extracted_unit_cat",
        )
        for field in _SCALAR_STR_FIELDS:
            val = extracted.get(field)
            if isinstance(val, list):
                fixed = _pick_best_scalar_from_list(val)
                logger.warning(
                    f"  └─ [HCX 스칼라 필드 방어] '{field}'에 리스트 {val}가"
                    f" 와서 '{fixed}'로 보정합니다."
                )
                extracted[field] = fixed
            elif val is not None and not isinstance(val, (str, int, float)):
                logger.warning(
                    f"  └─ [HCX 스칼라 필드 방어] '{field}'에 예상치 못한"
                    f" 타입({type(val).__name__})이 와서 버립니다: {val}"
                )
                extracted[field] = None

        # [지표명 수식어 유실 가드] 프롬프트에서 수식어를 보존하라고 지시는
        # 했지만, LLM이 여전히 "실업률"처럼 표준 지표명으로 정규화해버릴
        # 수 있다(실측: "청년 실업률" -> "실업률"). 프롬프트 준수를
        # 신뢰하는 대신, 원문에 QUALIFIER_KEYWORDS 중 하나가 실제로 있는데
        # extracted_indicator에는 빠져 있으면 결정적으로 다시 붙여 넣는다 -
        # 이 검증은 이번 세션 내내 지적된 "맨 앞단(추출) 결과를 검증할
        # 방법이 없다"는 문제에 대한 직접적인 대응이다.
        indicator = extracted.get("extracted_indicator")
        if indicator:
            for qualifier in QUALIFIER_KEYWORDS:
                if qualifier in user_input and qualifier not in indicator:
                    repaired = f"{qualifier} {indicator}"
                    logger.info(
                        "  └─ [지표명 수식어 복구] 추출된 지표명"
                        f" '{indicator}'에서 원문의 수식어 '{qualifier}'가"
                        f" 빠져있어 '{repaired}'로 복구합니다."
                    )
                    indicator = repaired
            extracted["extracted_indicator"] = indicator

        return extracted

    # ------------------------------------------------------------------
    # [다중 주장 추출] 문장 안의 모든 숫자 주장을 뽑아 교차 검증에 쓴다
    # ------------------------------------------------------------------

    # 알려진 단위 글자만 화이트리스트로 잡는다. 예전에는 숫자 뒤 아무
    # 한글 2글자("[가-힣%％]{1,2}")를 그냥 잡았는데, "28만9000명으로"처럼
    # 단위(명) 바로 뒤에 조사/어미("으로")가 붙어 있으면 "명으"까지
    # 통째로 잡혀버리는 실측 버그가 있었다. 단위로 흔히 쓰이는 글자만
    # 허용하면 조사가 섞여 들어올 일이 없다.
    # [2026-07 추가] 앞에 공백 하나 정도는 허용한다("8만 8천 건"처럼 "만/천"
    # 단위 뒤에 실제 단위(건/명 등)가 띄어서 나오는 표기가 흔해서, 공백을
    # 허용 안 하면 값은 맞게 뽑히는데 unit만 계속 None이 됐다.
    _CLAIM_UNIT_RE = re.compile(r"\s?(명|원|개|건|곳|대|동|실|달러|포인트|%|％|점|배)")
    # "YYYY년 (M월)?" 형태의 절대 시점만 인정한다("지난달"/"3월 기준"처럼
    # 상대 표현은 추가 맥락 없이는 정확한 YYYYMM을 만들 수 없어 제외한다 -
    # 틀린 시점을 만들어내는 것보다 그 주장을 교차검증에서 빼는 게 안전).
    # [2026-07 추가] "(\d)분기" 대안도 인정한다("2024년 4분기") - KOSIS
    # 실제 분기 코드 포맷이 "YYYYN"(연도4자리+분기1자리, kosis_table_info
    # 메타데이터로 실측 확인)이라 내부 표현도 그대로 5자리 문자열로 쓴다
    # (길이로 연간(4)/분기(5)/월간(6)을 구분 - 별도 "Q" 마커 불필요).
    _CLAIM_PERIOD_RE = re.compile(
        r"(20\d{2}|19\d{2})년\s*(?:(\d{1,2})\s*월|(\d)\s*분기)?"
    )
    # [2026-07-24 추가 - 지수 기준연도 오인식 버그] "117.42(2020년=100)"처럼
    # 지수류 통계에서 기준연도를 "YYYY년=100"으로 표기하는 관행이 흔한데,
    # 이 "2020년"을 _CLAIM_PERIOD_RE가 진짜 시점 표기로 오인해서, 근처의
    # 다른 claim(예: 뒤에 이어지는 "2.4% 상승")의 시점을 실제 보도 시점
    # (10월)이 아니라 기준연도(2020)로 잘못 채택하는 사고가 실측됐다
    # (소비자물가지수_10월_일치 골든셋 - found=100.0@2020으로 기준값 자체를
    # 조회해버려 확정적으로 틀린 값이 나옴, 차라리 판단불가가 나았던 사례).
    # "YYYY년" 매치 바로 뒤(공백 허용)에 "="/"＝"가 오면 시점이 아니라
    # 기준연도 각주이므로 후보에서 제외한다.
    _INDEX_BASE_YEAR_SUFFIX_RE = re.compile(r"\s*[=＝]")

    # [2026-07-24 추가 - #54 두 시점 diff claim] "쉬었음 인구가 1년 새
    # 7만명 넘게 늘어난 것으로 나타났다"처럼, claim 자체가 이미 "N년
    # 새/만에/동안 이만큼 변화"라는 두 시점 비교를 명시하는 문장이 있다.
    # Decision 003(manual_diff 미채택)과는 다르다 - 그건 "공식 등락률
    # 컬럼이 없을 때 우리가 임의로 계산법을 추측"하는 걸 막은 것이고,
    # 여기는 claim 문장 자체가 이미 "이 두 시점의 차이"라는 연산을
    # 명시하고 있어 추측이 필요 없다(두 시점 각각의 공식 확정값을
    # 그대로 빼기만 하면 claim이 말한 조건을 있는 그대로 검증하는 것).
    _DIFF_DURATION_RE = re.compile(r"(\d+)\s*년\s*(새|만에|동안|사이)")
    _DIFF_INCREASE_VERB_RE = re.compile(
        r"늘어난|늘었|증가|불어난|뛰었|오른|올랐"
    )
    _DIFF_DECREASE_VERB_RE = re.compile(
        r"줄어든|줄었|감소|줄인|떨어졌|하락"
    )
    # "X년 M월보다/대비/에 비해 ~ 늘어난/줄어든 Y" 같은 비교 구문에서, Y
    # 바로 앞의 "X년 M월"은 Y의 실제 시점이 아니라 "비교 대상 시점"이다
    # (예: "2025년 8월 출생아 수는 2024년 8월보다 764명 증가한 2만867명" -
    # 2만867은 2025년 8월 값이지, 2024년 8월 값이 아니다). 이 표시가 연도
    # 매치 바로 뒤에 붙어 있으면 "비교 시점"으로 보고 건너뛴다.
    # [2026-07 실측 발견 - NEWS_0458] "전년 같은 기간보다 10.1% 감소"처럼
    # 비교 대상 키워드와 "보다" 사이에 "같은 기간"/"동일 기간"이 끼어드는
    # 실제 기사 표현이 있다 - 원래는 키워드 바로 뒤에 "보다"가 붙는
    # 경우만 잡았는데, 그러면 이 문장에서 "전년"이 진짜 시점("10.1%"의
    # 실제 소속 시점)으로 오인되는 버그가 있었다(전년 같은 기간=비교
    # 기준일 뿐, 실제로는 앞 문장의 "지난해 4분기"가 진짜 시점).
    # [2026-07-24 추가/실측 - 소비자물가지수_10월] "1년 전과 비교해 2.4%
    # 상승했다"처럼 "비교해"/"비교하여"로 이어지는 표현도 "보다"/"대비"/
    # "에 비해"와 똑같이 순수 비교 기준점 마커다("1년 전"이 이 주장 자체의
    # 시점이 아니라 비교 대상 시점일 뿐). 이게 안 걸리면 "1년 전"을
    # 실제 시점으로 오인해서 KOSIS의 공식 등락률 컬럼이 실제로 인덱싱하는
    # "지금 시점"이 아니라 "1년 전 시점"으로 조회해버리는 사고가 난다
    # (MCP 실측: DT_1J22042 T03을 202510로 조회해야 "1년 전 대비 2.4%
    # 상승"이라는 값이 나옴 - 202410으로 조회하면 완전히 다른 값).
    _COMPARISON_MARKER_RE = re.compile(
        r"^\s*(?:같은\s*기간|동일\s*기간)?\s*(보다|대비|에\s*비해|(?:과|와)\s*비교)"
    )

    # [2026-07 추가 - 상대 시점 처리] 실제 기사(final_news.csv)를 전수
    # 스캔해서 뽑은 상대 시점 표현들(relative_date_edge_cases.md 참고).
    # "지난달"/"작년"처럼 뜻이 고정된 키워드는 LLM 없이도 순수 산술로
    # 100% 결정적으로 풀리므로(문맥적 판단이 필요 없는 닫힌 어휘라 조/억/만
    # 숫자 파서와 같은 이유로 규칙 기반을 우선한다), 여기서는 이 키워드
    # 테이블 + reference_date(기사 게재일) 산술만으로 처리한다. "동월/동기"
    # 처럼 "문장 내 다른 절대 시점"을 기준으로 삼아야 하는 진짜 애매한
    # 케이스만 별도 로직(추후 확장)으로 남겨둔다.
    #
    # 순서 중요: "재작년"이 "작년"보다 먼저 매치돼야 하고, "전년"은 "전년
    # 동월/동기"(뒤에 "동월"/"동기"가 바로 이어지는 경우) 형태면 여기서
    # 처리하지 않고 건너뛴다 - 그건 reference_date가 아니라 문장 내 다른
    # 절대 시점이 기준이 되는 별개 케이스라 잘못 게재일 기준으로 계산하면
    # 오히려 틀린 값을 만들어낸다(추측성 계산이라 strict 철학에 어긋남).
    _RELATIVE_YEAR_RE = re.compile(
        r"(재작년|(\d+)\s*년\s*전|작년|지난해|전년|올해|금년)"
        r"(?:\s*(\d{1,2})\s*월|\s*(\d)\s*분기)?"
    )
    _RELATIVE_MONTH_RE = re.compile(r"(지난달|전달|전월|이달|당월|(\d+)\s*개월\s*전)")
    # [2026-07 추가] "지난 12월"처럼 "지난"이 관용구 "지난달"이 아니라
    # 명시적인 월 숫자를 수식하는 경우("가장 최근에 지나간 M월"이라는 뜻) -
    # NEWS_0006 실측("지난 12월 수출액은...") 발견. "지난달"과 헷갈리지
    # 않도록 뒤에 바로 "월"이 오는 숫자가 있을 때만 매치한다("지난달"은
    # 이미 별도 키워드라 여기 안 걸림).
    _LAST_EXPLICIT_MONTH_RE = re.compile(r"지난\s*(\d{1,2})\s*월")

    # [2026-07-24 추가 - 소비자물가지수_10월 실측] "10월 소비자물가지수는
    # 117.42(...)로, 1년 전과 비교해 2.4% 상승했다"처럼, "N년 전"에는 월이
    # 안 붙어 있지만 문장 앞쪽에 이미 "10월"이라는 월 언급이 있는 경우가
    # 있다. 이 "117.42" 자체는 claim으로 안 뽑히는 사각지대(#53)라도, 그
    # 수치를 수식하는 "10월"이라는 시점 언급은 문장에 이미 명시돼 있으므로
    # 이걸 그대로 재사용하는 건 값을 추측하는 게 아니다(Decision 003의
    # "추측 금지"는 없는 값을 계산해내는 것을 막은 것이지, 문장에 이미
    # 적힌 시점 정보를 못 쓰게 막은 게 아니다). "지난 N월"(관용구, 이미
    # _LAST_EXPLICIT_MONTH_RE가 별도 처리)은 여기서 다시 집지 않도록
    # 제외한다.
    _BARE_MONTH_RE = re.compile(r"(\d{1,2})\s*월")

    def _find_bare_month_anchor(
        self, text: str, before_pos: int, lookback: int = 100
    ) -> Optional[int]:
        """before_pos 이전 lookback자 안에서 "지난 N월"류가 아닌 순수
        "N월"(생략형) 표현 중 가장 가까운 것의 월 숫자를 반환한다."""
        window_start = max(0, before_pos - lookback)
        window = text[window_start:before_pos]
        for m in reversed(list(self._BARE_MONTH_RE.finditer(window))):
            abs_start = window_start + m.start()
            prefix = text[max(0, abs_start - 3):abs_start]
            if prefix.rstrip().endswith("지난"):
                continue
            month = int(m.group(1))
            if 1 <= month <= 12:
                return month
        return None
    # [2026-07 추가 - 분기 지원] "전분기"/"지난 분기"/"이번 분기"(관용구
    # 오프셋) + "N분기 전"(숫자 오프셋). "N분기 전"은 "1분기 전망치"처럼
    # "전망"과 문자열이 우연히 겹치는 오탐이 실측 확인됐으므로(edge case
    # 문서 3번) 뒤에 "망"이 바로 이어지면 매치하지 않는 부정 전방탐색을
    # 넣는다.
    _RELATIVE_QUARTER_RE = re.compile(
        r"(전분기|지난\s*분기|이번\s*분기|이분기|금분기|(\d+)\s*분기\s*전(?!망))"
    )
    _SAME_PERIOD_FOLLOW_RE = re.compile(r"^\s*(동월|동기)")

    # [2026-07 실측 발견 - NEWS_0458] "지난해 1분기도...15.6%...2분기
    # (20.9%)"처럼, 앞 절에서 이미 밝힌 연도를 뒤 절에서 "N분기"라고만
    # 줄여 쓰는 생략형 표현 - "전분기"/"지난분기" 같은 키워드도 없고
    # "YYYY년"도 안 붙어서 기존 절대/상대 패턴 어디에도 안 걸린다. 이걸
    # 그냥 두면 _resolve_relative_period가 더 멀리 있는 "지난해 1분기"
    # 전체(연도+분기 세트)를 앵커로 잘못 재사용해 분기 번호까지 틀리게
    # 나온다(20242여야 할 게 20241로 나옴) - "N분기 전"(오프셋 표현)과
    # 겹치지 않도록 뒤에 "전"이 오면 제외한다.
    _BARE_QUARTER_RE = re.compile(r"(\d)\s*분기(?!\s*전)")

    # "N분기" 앞에 이 표현이 바로 붙어 있으면 이미 절대/상대 패턴
    # (_CLAIM_PERIOD_RE의 "YYYY년"이나 _RELATIVE_YEAR_RE/
    # _RELATIVE_QUARTER_RE의 키워드)이 그 분기까지 통째로 처리하므로
    # "생략형(bare)"이 아니다 - 이걸 놓치면 "지난해 4분기"의 "4분기"까지
    # bare로 오인해서 정상 케이스를 깨뜨린다(실측으로 발견).
    _QUARTER_ALREADY_CLAIMED_SUFFIX_RE = re.compile(
        r"(?:년|재작년|작년|지난해|전년|올해|금년|"
        r"전분기|지난\s*분기|이번\s*분기|이분기|금분기)\s*$"
    )

    def _has_bare_quarter_nearby(self, text: str, pos: int, lookback: int = 15) -> bool:
        """pos 바로 앞(lookback자 이내)에 절대/상대 연도 키워드가 안 붙은
        순수 "N분기"(생략형) 표현이 있는지 확인한다."""
        window_start = max(0, pos - lookback)
        window = text[window_start:pos]
        for m in self._BARE_QUARTER_RE.finditer(window):
            # 주의: prefix는 window가 아니라 원문 text 기준 절대 위치로 잘라야
            # 한다. "N분기" 매치가 lookback 윈도우 시작 지점 근처에 있으면
            # window 기준 슬라이싱은 "올해"/"작년" 같은 보호 키워드가 윈도우
            # 밖(더 앞)에 있어도 못 보고 빈 문자열을 반환하는 버그가 있었다.
            abs_start = window_start + m.start()
            prefix = text[max(0, abs_start - 6):abs_start]
            if self._QUARTER_ALREADY_CLAIMED_SUFFIX_RE.search(prefix):
                continue
            return True
        return False

    def _resolve_bare_quarter_reference(
        self, claims: List[Dict[str, Any]], text: str
    ) -> None:
        """period가 아직 None인 주장 중, 바로 앞에 "년" 없는 순수 "N분기"
        표현이 있는 것을 찾아 - 문장 내 앞쪽(_pos가 더 작은)에 이미 분기
        시점이 확정된 다른 주장의 "연도"만 빌려오고 분기 번호는 이 표현의
        숫자를 쓴다("지난해 1분기"=20241이 앵커일 때 "2분기"는 20242,
        연도만 앵커에서 물려받고 분기는 새로 지정)."""
        for c in claims:
            if c.get("period"):
                continue
            pos = c.get("_pos")
            if pos is None:
                continue
            window_start = max(0, pos - 15)
            window = text[window_start:pos]
            bare_match = None
            for m in self._BARE_QUARTER_RE.finditer(window):
                # _has_bare_quarter_nearby와 동일한 이유로 절대 위치 기준으로
                # prefix를 잘라야 한다(윈도우 경계 절단 버그 방지).
                abs_start = window_start + m.start()
                prefix = text[max(0, abs_start - 6):abs_start]
                if self._QUARTER_ALREADY_CLAIMED_SUFFIX_RE.search(prefix):
                    continue
                bare_match = m
            if bare_match is None:
                continue
            quarter_digit = bare_match.group(1)
            if quarter_digit not in ("1", "2", "3", "4"):
                continue
            candidates = [
                other
                for other in claims
                if other is not c
                and other.get("period")
                and len(other["period"]) == 5  # 분기 표기(YYYYN)만 앵커로 인정
                and other.get("_pos") is not None
                and other["_pos"] < pos
            ]
            if not candidates:
                continue
            anchor = max(candidates, key=lambda x: x["_pos"])
            anchor_year = anchor["period"][:4]
            c["period"] = f"{anchor_year}{quarter_digit}"
    _YEAR_KEYWORD_OFFSET = {
        "재작년": -2, "작년": -1, "지난해": -1, "전년": -1,
        "올해": 0, "금년": 0,
    }
    _MONTH_KEYWORD_OFFSET = {
        "지난달": -1, "전달": -1, "전월": -1, "이달": 0, "당월": 0,
    }
    # 키는 공백을 뺀 형태로 통일한다("지난 분기"/"지난분기" 표기가 둘 다
    # 있어서 매치된 원문 공백을 그대로 키로 쓰면 조회가 어긋날 수 있다).
    _QUARTER_KEYWORD_OFFSET = {
        "전분기": -1, "지난분기": -1, "이번분기": 0, "이분기": 0, "금분기": 0,
    }

    # [2026-07 추가 - rate_preference 연동 준비] "1300조원을 넘어설
    # 전망이다"처럼 아직 발생하지 않은 전망/예측치 주장은 KOSIS 확정치와
    # 성격 자체가 달라서(미래값이라 원천적으로 존재하지 않음) 어떤 시점으로
    # 조회해도 항상 불일치가 나올 수밖에 없다 - 실측(국가채무 로드테스트)
    # 에서 이런 값이 계속 "근거 없는 불일치"로 섞여 나왔다. 숫자 바로
    # 뒤(30자 이내)에 전망/예측 표현이 나오면 그 주장은 애초에 KOSIS
    # 확정치 대조 대상에서 제외한다.
    _FORECAST_LOOKAHEAD_RE = re.compile(
        r"^.{0,30}?(전망|예상|예측|것으로\s*보인다|될\s*것으로)"
    )

    def _is_forecast_claim(self, text: str, end_pos: int) -> bool:
        # [2026-07 통합테스트에서 발견/수정] 40자 룩어헤드가 문장 경계를
        # 안 가리다 보니 "...8% 증가했다. 내년 1분기는...전망이다"처럼
        # 바로 다음 문장의 "전망"까지 앞 문장 값에 잘못 붙는 오탐이
        # 나왔다 - 마침표(및 "다."/줄바꿈)를 만나면 그 앞까지만 본다.
        window = text[end_pos:end_pos + 40]
        boundary = re.search(r"[.!?\n]", window)
        if boundary:
            window = window[:boundary.end()]
        return bool(self._FORECAST_LOOKAHEAD_RE.match(window))

    # [2026-07 추가] "동월/동기" 자체를 찾는 키워드 - _resolve_relative_period
    # 안의 _SAME_PERIOD_FOLLOW_RE("전년 동월"처럼 앞에 연도 키워드가 붙는
    # 형태)와 달리, 여기서는 "전년 동월 대비"/"동기 대비"처럼 그 자체가
    # 하나의 비교 마커로 쓰이는 문맥 전체를 찾는다(연도 키워드 없이 그냥
    # "동월과 동일했다"처럼 쓰이는 경우도 포함).
    _SAME_PERIOD_KEYWORD_RE = re.compile(r"동월|동기")

    def _resolve_same_period_last_year(
        self, claims: List[Dict[str, Any]], text: str
    ) -> None:
        """period가 아직 None인 주장 중, 바로 앞(30자 이내)에 "동월"/"동기"
        표현이 있는 것을 찾아 "문장 내에서 이미 시점이 확정된 다른 주장"을
        기준(anchor)으로 "같은 달/연도만 -1"을 계산해 채운다.

        예: "지난 12월 수출액은 614억달러로 전년 동월 대비 6.6% 증가했다"
        - "614억달러"가 이미 202412로 확정돼 있으면, "6.6%"의 "전년 동월"은
        202312가 된다. anchor 후보는 "이 주장보다 앞쪽(_pos가 더 작은)에
        있으면서 이미 period가 채워진 claims 중 가장 가까운 것"으로 고른다
        - 한 문장 안에서 "기준 시점 -> 그 시점 대비 등락률" 순서로 쓰는 게
        기사 문체의 일반적인 어순이기 때문이다.
        """
        for c in claims:
            if c.get("period"):
                continue
            pos = c.get("_pos")
            if pos is None:
                continue
            window_start = max(0, pos - 30)
            if not self._SAME_PERIOD_KEYWORD_RE.search(text[window_start:pos]):
                continue
            candidates = [
                other
                for other in claims
                if other is not c
                and other.get("period")
                and other.get("_pos") is not None
                and other["_pos"] < pos
            ]
            if not candidates:
                continue
            anchor = max(candidates, key=lambda x: x["_pos"])
            anchor_period = anchor["period"]
            if len(anchor_period) == 6:
                year, month = int(anchor_period[:4]), anchor_period[4:6]
                c["period"] = f"{year - 1}{month}"
            elif len(anchor_period) == 5:
                # 분기(YYYYN, kosis_table_info 실측 포맷) - "동기" 대비도
                # 같은 분기, 연도만 -1.
                year, quarter = int(anchor_period[:4]), anchor_period[4:5]
                c["period"] = f"{year - 1}{quarter}"
            elif len(anchor_period) == 4:
                c["period"] = str(int(anchor_period) - 1)

    def _resolve_own_period_from_comparison_marker(
        self,
        claims: List[Dict[str, Any]],
        text: str,
        reference_date: "Optional[Union[str, date]]",
    ) -> None:
        """[2026-07-24 추가/MCP 실측 - 소비자물가지수_10월] "1년 전과
        비교해 2.4% 상승했다"처럼, "N년 전"류 표현이 이 주장 자체의 시점이
        아니라 순수 비교 기준점 마커로만 쓰인 경우(_COMPARISON_MARKER_RE에
        걸려 _resolve_relative_period가 일부러 시점 계산을 건너뛴 경우),
        이 주장은 사실 "지금(기사가 보도하는 현재 시점)" 값을 가리킨다.

        MCP로 실측한 KOSIS 등락률 표(DT_1J22042 전년동월비)는 "비교 기준
        시점"이 아니라 "지금 시점"으로 인덱싱된다 - 202510으로 조회해야
        "1년 전(202410) 대비 2.4% 상승"이라는 값이 나오고, 202410으로
        조회하면 완전히 다른(1년 더 과거의) 값이 나온다. 문장에 이미
        언급된 순수 "N월"(생략형 - "10월 소비자물가지수는...") 이 있으면
        그 달을 reference_date의 연도(또는 그 달이 게재월보다 미래면
        전년도 - "지난 M월" 처리와 동일 원리)와 합쳐 쓰고, 없으면
        reference_date 자체(YYYYMM)를 그대로 쓴다 - 둘 다 값을 추측하는
        게 아니라 문장/게재일에 이미 있는 정보를 그대로 재사용하는 것.
        """
        if reference_date is None:
            return
        if isinstance(reference_date, str):
            try:
                ref = date.fromisoformat(reference_date[:10])
            except ValueError:
                return
        else:
            ref = reference_date
        for c in claims:
            if c.get("period"):
                continue
            pos = c.get("_pos")
            if pos is None:
                continue
            window_start = max(0, pos - 30)
            window = text[window_start:pos]
            has_marker = False
            for m in reversed(list(self._RELATIVE_YEAR_RE.finditer(window))):
                after = text[window_start + m.end():pos]
                if self._COMPARISON_MARKER_RE.match(after):
                    has_marker = True
                    break
            if not has_marker:
                continue
            bare_month = self._find_bare_month_anchor(text, pos, lookback=100)
            if bare_month is not None:
                target_year = ref.year if bare_month <= ref.month else ref.year - 1
                c["period"] = f"{target_year}{bare_month:02d}"
            else:
                c["period"] = f"{ref.year}{ref.month:02d}"

    @staticmethod
    def _shift_year_month(year: int, month: int, delta_months: int) -> Tuple[int, int]:
        """month(1~12)에 delta_months(음수 가능)를 더해 (연도, 월)을 반환.

        표준 라이브러리 date 산술 대신 직접 계산하는 이유: date는 일(day)
        까지 필요한데 여기선 "몇 년 몇 월"만 있으면 되고, 매달 일수가 달라
        date로 하면 불필요하게 day 보정 로직이 끼어든다. 0-index로 바꿔서
        나눗셈/나머지로 처리하면 1월-1개월=전년 12월 같은 롤오버가 별도
        분기 없이 자연히 처리된다.
        """
        zero_based = (month - 1) + delta_months
        new_year = year + zero_based // 12
        new_month = zero_based % 12 + 1
        return new_year, new_month

    @staticmethod
    def _shift_year_quarter(year: int, quarter: int, delta_quarters: int) -> Tuple[int, int]:
        """_shift_year_month와 동일한 원리의 분기 버전(1~4 롤오버)."""
        zero_based = (quarter - 1) + delta_quarters
        new_year = year + zero_based // 4
        new_quarter = zero_based % 4 + 1
        return new_year, new_quarter

    def _resolve_relative_period(
        self, text: str, pos: int, reference_date: "Union[str, date]"
    ) -> Optional[str]:
        """pos 앞쪽 가까운 곳에서 상대 시점 키워드를 찾아 reference_date
        기준으로 절대 YYYYMM(또는 월을 특정 못 하면 YYYY)을 계산한다.

        reference_date는 "YYYY-MM-DD" 문자열 또는 date 객체 - 보통 기사
        게재일(final_news.csv의 작성일 등)이다.
        """
        if isinstance(reference_date, str):
            try:
                ref = date.fromisoformat(reference_date[:10])
            except ValueError:
                return None
        else:
            ref = reference_date

        # [2026-07 추가] 바로 근처(20자 이내)에 "동월"/"동기"가 있으면 이
        # 주장은 reference_date에서 직접 계산할 대상이 아니라, 문장 내
        # 다른 주장의 이미 확정된 시점을 앵커로 삼아야 하는 케이스다
        # (extract_all_claims의 _resolve_same_period_last_year 후처리 몫).
        # 여기서 그냥 넓은 창까지 계속 찾다 보면 "동월"과 무관한 더 먼
        # 시점(예: "지난 12월")을 엉뚱하게 앵커로 집어버리는 문제가 실측
        # 됐다(NEWS_0006 - "6.6%"가 앵커("전년 동월", 202312)가 아니라
        # "지난 12월"(202412)로 잘못 계산됨) - 아예 여기서 시도 자체를
        # 접고 후처리에 위임한다.
        if self._SAME_PERIOD_KEYWORD_RE.search(text[max(0, pos - 20):pos]):
            return None

        # [2026-07 추가] 바로 앞에 "년" 없는 순수 "N분기"(생략형)가 있으면
        # 여기서 reference_date로 계산하면 안 된다 - 더 멀리 있는 무관한
        # 연도+분기 세트를 통째로 앵커로 삼아 분기 번호까지 잘못 가져올
        # 위험이 있다(NEWS_0458 실측). extract_all_claims의
        # _resolve_bare_quarter_reference 후처리에 맡긴다.
        if self._has_bare_quarter_nearby(text, pos):
            return None

        def _scan(window_start: int, window: str) -> Optional[str]:
            """window 안에서(가까운 것부터) 비교 마커/동월-동기 팔로업이
            안 붙은 첫 상대 시점 매치를 찾아 계산한다. 없으면 None."""
            # 연 단위(재작년/작년/지난해/전년/올해/금년, "N년 전", "작년
            # 12월"류)
            for m in reversed(list(self._RELATIVE_YEAR_RE.finditer(window))):
                after = text[window_start + m.end():pos]
                # "전년 동월"/"작년 동기"처럼 뒤에 동월/동기가 바로 이어지면
                # reference_date 기준 계산 대상이 아니다(별도 케이스).
                if self._SAME_PERIOD_FOLLOW_RE.match(after):
                    continue
                # "작년 대비"/"전년보다"처럼 이 키워드 자체가 비교 대상으로
                # 쓰인 경우(절대 연도 매칭과 동일한 함정) - 진짜 시점이
                # 아니므로 건너뛴다.
                if self._COMPARISON_MARKER_RE.match(after):
                    continue
                keyword = m.group(1)
                n_years_ago, explicit_month, explicit_quarter = (
                    m.group(2), m.group(3), m.group(4)
                )
                if n_years_ago:
                    offset = -int(n_years_ago)
                else:
                    offset = self._YEAR_KEYWORD_OFFSET.get(keyword)
                if offset is None:
                    continue
                target_year = ref.year + offset
                if explicit_month:
                    return f"{target_year}{int(explicit_month):02d}"
                if explicit_quarter:
                    return f"{target_year}{explicit_quarter}"
                # [2026-07-24 추가] "1년 전과 비교해 2.4%"처럼 이 매치
                # 자체에는 월이 안 붙어 있어도, 문장 앞쪽에 이미 언급된
                # 순수 "N월"(생략형)이 있으면 그걸 빌려 쓴다(위
                # _find_bare_month_anchor 참고 - 추측이 아니라 문장에
                # 이미 있는 시점 정보 재사용).
                bare_month = self._find_bare_month_anchor(
                    text, window_start + m.start()
                )
                if bare_month is not None:
                    return f"{target_year}{bare_month:02d}"
                return str(target_year)

            # "지난 M월"(관용구 "지난달"이 아니라 명시적 월 숫자) - 그 달이
            # reference_date의 달보다 이후(아직 안 지난 달)면 작년, 아니면
            # 올해("지난 12월"을 1월에 말하면 작년 12월, 6월에 말하면 올해
            # 6월).
            for m in reversed(list(self._LAST_EXPLICIT_MONTH_RE.finditer(window))):
                after = text[window_start + m.end():pos]
                if self._SAME_PERIOD_FOLLOW_RE.match(after):
                    continue
                if self._COMPARISON_MARKER_RE.match(after):
                    continue
                month = int(m.group(1))
                if not (1 <= month <= 12):
                    continue
                target_year = ref.year if month <= ref.month else ref.year - 1
                return f"{target_year}{month:02d}"

            # 월 단위(지난달/전달/전월/이달/당월, "N개월 전")
            for m in reversed(list(self._RELATIVE_MONTH_RE.finditer(window))):
                after = text[window_start + m.end():pos]
                if self._COMPARISON_MARKER_RE.match(after):
                    continue
                keyword, n_months_ago = m.group(1), m.group(2)
                if n_months_ago:
                    offset = -int(n_months_ago)
                elif keyword in self._MONTH_KEYWORD_OFFSET:
                    offset = self._MONTH_KEYWORD_OFFSET[keyword]
                else:
                    continue
                target_year, target_month = self._shift_year_month(
                    ref.year, ref.month, offset
                )
                return f"{target_year}{target_month:02d}"

            # 분기 단위(전분기/지난분기/이번분기/이분기/금분기, "N분기 전")
            for m in reversed(list(self._RELATIVE_QUARTER_RE.finditer(window))):
                after = text[window_start + m.end():pos]
                if self._COMPARISON_MARKER_RE.match(after):
                    continue
                keyword_raw, n_quarters_ago = m.group(1), m.group(2)
                keyword = re.sub(r"\s+", "", keyword_raw)
                if n_quarters_ago:
                    offset = -int(n_quarters_ago)
                elif keyword in self._QUARTER_KEYWORD_OFFSET:
                    offset = self._QUARTER_KEYWORD_OFFSET[keyword]
                else:
                    continue
                ref_quarter = (ref.month - 1) // 3 + 1
                target_year, target_quarter = self._shift_year_quarter(
                    ref.year, ref_quarter, offset
                )
                return f"{target_year}{target_quarter}"

            return None

        # 1단계: 좁은 창(30자) - 절대 시점과 동일한 이유로 기사 문체는
        # 대부분 이 정도 거리 안에 시점 표현이 붙어 나온다.
        narrow_start = max(0, pos - 30)
        found = _scan(narrow_start, text[narrow_start:pos])
        if found is not None:
            return found

        # 2단계: 좁은 창에서 못 찾았으면(비교 마커에 다 걸렸거나 그냥 멀리
        # 있는 경우) - 절대 시점 탐색과 동일하게 문장 경계까지, 최대 120자로
        # 넓혀서 재시도한다("작년 한국의 수입액은 전년보다 1.6% 감소한
        # 6320억달러로, 518억달러의 무역 흑자를 기록했다"처럼 진짜 주어
        # 시점("작년")이 앞쪽 멀리 있고 그 사이에 비교 구문이 끼어 있는
        # 경우를 위함).
        wide_start = max(0, pos - 120)
        sentence_boundary = text.rfind("다.", wide_start, pos)
        if sentence_boundary != -1:
            wide_start = max(wide_start, sentence_boundary + 2)
        return _scan(wide_start, text[wide_start:pos])

    def _find_claim_period(
        self,
        text: str,
        pos: int,
        reference_date: "Optional[Union[str, date]]" = None,
        _skip_same_period_guard: bool = False,
    ) -> Optional[str]:
        """숫자가 등장한 위치(pos) 앞쪽에서 절대 시점을 찾는다.

        기사 문체("2021년 6월(8.9%)", "2023년 3월(30만명)")는 시점이 숫자
        바로 앞에 붙어 나오는 경우가 대부분이라 기본은 30자 이내를 본다.
        다만 "2025년 8월 출생아 수는 2024년 8월보다 764명 증가한 2만867명"
        처럼 문장 주어 시점과 숫자 사이에 "비교 시점"이 끼어 있는 경우,
        가장 가까운 시점이 "보다/대비/에 비해"로 바로 이어지면 그건 비교
        대상일 뿐이므로 건너뛰고 그 앞(문장 시작 쪽)의 시점을 대신 쓴다.
        이 경우를 위해 윈도우를 문장 경계(마침표)까지, 최대 120자까지 넓힌다.

        _skip_same_period_guard: extract_all_claims의 최종 폴백
        (_resolve_bare_quarter_reference 다음 단계)에서만 True로 넘긴다 -
        아래 참고.
        """
        # [2026-07 추가] 바로 근처(20자 이내)에 "동월"/"동기"가 있으면
        # 넓은 창까지 뒤져서 나오는 절대 시점(예: "2024년 4분기 매출은
        # 16조원으로 전년 동기 대비 5%"에서 "5%" 입장에서 보이는 "2024년
        # 4분기")을 이 주장의 시점으로 잘못 채택하면 안 된다 - "동기 대비"
        # 자체가 "이 값은 앵커(같은 문장의 다른 확정된 주장)의 시점에서
        # 연도만 -1"이라는 별도 계산이 필요하다는 신호라, 이 함수(절대
        # 표기 탐색)가 나서면 안 되고 extract_all_claims의
        # _resolve_same_period_last_year 후처리에 맡겨야 한다
        # (_resolve_relative_period에도 동일한 가드가 있음 - 실측
        # 회귀 확인).
        #
        # [2026-07 실측 발견 - 근원물가지수 힌트 테스트] "2025년 9월
        # 근원물가지수는 전년동월대비 2.1% 상승했다"처럼, 문장 안에 이
        # 값의 시점을 밝혀줄 "다른 주장"이 아예 없는(claim이 이거 하나뿐인)
        # 경우가 있다 - 여기서 "전년동월대비"는 별도 앵커를 가리키는 게
        # 아니라 그냥 "이 %가 전년 동월 대비 증감률로 계산됐다"는 계산
        # 방식 설명일 뿐이고, 진짜 시점은 문장 맨 앞의 "2025년 9월"(이
        # 값 자체가 속한 시점) 그대로다. 이 경우 위 가드 때문에
        # period=None으로 남고, _resolve_same_period_last_year도 앵커
        # 후보(다른 확정 claim)가 없어서 못 채운다 - extract_all_claims가
        # 이 함수를 가드 없이(_skip_same_period_guard=True) 한 번 더
        # 불러서 문장 자체의 절대 시점으로 안전하게 폴백한다.
        if not _skip_same_period_guard and self._SAME_PERIOD_KEYWORD_RE.search(
            text[max(0, pos - 20):pos]
        ):
            return None

        def _pick(window_start: int, window: str) -> Optional[re.Match]:
            """window 안에서(가까운 것부터) 비교 마커가 안 붙은 첫 매치를
            반환한다. 전부 비교 마커면 None(호출부에서 더 넓게 재시도하거나
            최후 폴백을 쓰게 한다)."""
            matches = list(self._CLAIM_PERIOD_RE.finditer(window))
            for m in reversed(matches):
                after = text[window_start + m.end():pos]
                if self._INDEX_BASE_YEAR_SUFFIX_RE.match(after):
                    continue
                if self._COMPARISON_MARKER_RE.match(after):
                    continue
                return m
            return None

        narrow_start = max(0, pos - 30)
        narrow_window = text[narrow_start:pos]
        picked = _pick(narrow_start, narrow_window)

        if picked is None:
            # 좁은 창(30자) 안에서 비교 마커 없는 시점을 못 찾았다 - 문장
            # 주어 시점이 더 앞쪽에 있을 수 있으니(예: "2025년 8월 출생아
            # 수는 2024년 8월보다 764명 증가한 2만867명") 문장 경계까지,
            # 최대 120자까지 창을 넓혀서 다시 찾는다.
            wide_start = max(0, pos - 120)
            sentence_boundary = text.rfind("다.", wide_start, pos)
            if sentence_boundary != -1:
                wide_start = max(wide_start, sentence_boundary + 2)
            wide_window = text[wide_start:pos]
            picked = _pick(wide_start, wide_window)

            if picked is None:
                # 넓은 창에서도 비교 마커 없는 시점이 없으면(예외적인
                # 경우) 안전하게 가장 가까운 매치로 폴백한다(기존 동작).
                # 단, 지수 기준연도 각주("YYYY년=100")는 절대 시점으로
                # 오인해선 안 되므로 여기서도 동일하게 걸러낸다.
                fallback_matches = [
                    m
                    for m in self._CLAIM_PERIOD_RE.finditer(wide_window)
                    if not self._INDEX_BASE_YEAR_SUFFIX_RE.match(
                        text[wide_start + m.end():pos]
                    )
                ]
                if not fallback_matches:
                    # [2026-07 추가] 절대 "YYYY년" 표기가 아예 없는
                    # 경우에만 상대 시점 해석을 시도한다 - 원문에 명시된
                    # 절대 표기가 있으면 항상 그게 우선이고(추측보다 명시가
                    # 낫다는 원칙), reference_date가 없으면(호출부에서 안
                    # 넘겼으면) 기존과 동일하게 그냥 None을 반환한다.
                    if reference_date is not None:
                        return self._resolve_relative_period(
                            text, pos, reference_date
                        )
                    return None
                picked = fallback_matches[-1]

        year, month, quarter = picked.group(1), picked.group(2), picked.group(3)
        if month:
            return f"{year}{int(month):02d}"
        if quarter:
            return f"{year}{quarter}"
        return year

    def extract_all_claims(
        self,
        user_input: str,
        reference_date: "Optional[Union[str, date]]" = None,
    ) -> List[Dict[str, Any]]:
        """문장/문단 안의 모든 숫자 주장을 찾아낸다 (하나만 뽑던 기존
        extracted_claimed_value와 달리 리스트 전체를 반환).

        reference_date: 기사 게재일("YYYY-MM-DD" 또는 date). 넘기면
        "지난달"/"작년"처럼 절대 연도 표기가 없는 상대 시점 주장도
        게재일 기준 산술로 절대 YYYYMM을 채운다(relative_date_edge_cases.md
        참고). 안 넘기면 기존과 동일하게 절대 "YYYY년" 표기만 인정한다.

        배경: 실제 기사 발췌문(예: "청년 실업률은 7.5%로... 2021년
        6월(8.9%) 이후 최고... 2021년 3월 10% 이후... 전체 연령대
        실업률(3.1%)의 2.4배")에는 검증 대상 숫자가 보통 여러 개 동시에
        들어있다. 이 숫자들을 다 함께 대조하면(같은 표에서 여러 시점의
        값이 동시에 다 맞아떨어지는지) 숫자 하나만 비교하는 것보다 훨씬
        강하게 표가 맞는지 검증할 수 있다 - 우연히 숫자 하나가 비슷할
        확률보다 여러 개가 동시에 맞을 확률이 훨씬 낮기 때문이다. 이건
        MCP로 직접 기사를 검증할 때 실제로 쓴 방법(같은 문단의 여러
        숫자를 한 표에서 다 찾아 대조)을 코드로 옮긴 것이다.

        한국 기사는 콤마 구분("4,248명")뿐 아니라 "28만9000명"/"30만명"
        같은 만 단위 표기, "7.5%"처럼 콤마 없는 소수도 흔히 쓰므로 세
        패턴을 모두 찾는다. "2.4배"처럼 두 통계의 비율로 표현된 수치는
        원본 KOSIS 값과 직접 비교할 수 없어(파생값이라) 제외한다.

        반환: [{"value": 7.5, "unit": "%", "period": "202106" 또는 None,
                "raw_text": "8.9%"}, ...] (문장에 나온 순서대로)
        """
        claims: List[Dict[str, Any]] = []
        used_spans: List[tuple] = []

        def overlaps(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e in used_spans)

        # [2026-07 추가] 큰수 표기(조/억/만) 그룹의 계수/나머지 자릿수에
        # 쓰는 숫자 패턴 - 콤마 유무 둘 다 받는다("2,916만"/"6,838억"처럼
        # 계수 자체가 콤마로 묶여 나오는 기사, "23만8,300"/"97만4,000"처럼
        # "만" 뒤 나머지 자릿수가 콤마로 묶여 나오는 기사가 실제로 흔했다
        # - 아래 [2026-07 순서 변경/버그 수정] 참고.
        _NUM = r"\d+(?:,\d{3})*(?:\.\d+)?"

        def _decimals(num_str: Optional[str]) -> int:
            if not num_str:
                return 0
            m = re.search(r"\.(\d+)$", num_str)
            return len(m.group(1)) if m else 0

        # 1) [2026-07 리팩터링] "28만9000"/"8만 8천"/"542억3천만"/"2401조"
        # 같은 한국어 큰수 자릿수 표기(조/억/만/천) 범용 파서.
        #
        # 배경: 처음엔 "만" 전용 패턴 하나였는데, "억"/"조" 단위(수입액/
        # 무역수지/GDP 기사 - "542억3천만 달러", "2401조원")가 새로 발견될
        # 때마다 정규식을 하나씩 옆에 추가하는 식으로 대응했다. 그런데 한국어
        # 큰수 표기는 조(10^12)/억(10^8)/만(10^4)이 내림차순으로 이어지는
        # 정해진 자릿수 문법이라, 조합이 늘어난다고 규칙 자체가 늘어나는 건
        # 아니다 - "패치를 계속 추가"하는 대신 이 문법 하나를 통째로
        # 표현하는 정규식 하나로 통합하는 게 맞다고 판단해 리팩터링했다.
        #
        # 처리하는 조합:
        #   - 조/억/만 각각 단독 (예: "2401조", "38억", "30만")
        #   - 억+천만 (예: "542억3천만" = 542억 + 3*1000*10000) - 여기서
        #     "천"은 "만" 앞에 붙어 그 자릿수의 계수를 키우는 역할이라(3천만
        #     = 3천 곱하기 만 = 3*10^7), 별개 항이 아니라 "만" 자릿수
        #     계수와 하나로 묶어서 계산한다.
        #   - 만+(공백)천 (예: "8만 8천" = 8만 + 8천, 여기서는 "천"이 "만"
        #     뒤에 붙어 그냥 더해지는 별개 항이다 - 위 "천만"과 정반대
        #     위치라 의미가 다르다는 점에 주의)
        #   - 만+공백없는 나머지 숫자 (예: "2만867" = 2만 + 867)
        #
        # 왜 숫자 파싱을 LLM이 아니라 규칙(정규식)으로 하는가: 한국어
        # 큰수 표기는 문맥적 판단이 필요 없는 고정된 위치 문법(자릿수를
        # 4자리씩 끊어 읽는 규칙)이라 규칙만으로 100% 결정론적으로 풀린다.
        # 반대로 LLM은 자릿수를 밀리거나 반올림하는 실수를 구조적으로
        # 저지르기 쉬운 영역이고, 이 파이프라인이 검증하려는 게 바로
        # "숫자가 정확히 일치하는가"이므로 파싱 단계에서부터 확률적 요소가
        # 섞이는 건 strict 검증 철학과 맞지 않는다.
        # [주의 - 실측으로 발견한 함정] 맨 처음 시도는 cheon/bare 그룹을
        # 최상위 형제로 뒀는데, 그러면 "\s*(숫자)" 부분이 조/억/만 등
        # 아무 자릿수 단위도 안 붙은 순수 숫자(예: "2025년"의 "2025",
        # "9월"의 "9")에도 단독으로 매치돼버렸다 - "산업통상자원부에 따르면
        # 2025년 9월 수입액은 542억3천만 달러..." 전체 문장에 돌려보니
        # "542억3천만"이 통째로 안 잡히고 " 2025"/" 9"/" 542"/"3천만"으로
        # 쪼개지는 실사용 버그가 났다(finditer가 "542" 앞의 공백 위치에서
        # 조/억을 기다리지 않고 그보다 먼저 "숫자만" 매치를 확정해버려서,
        # 정작 "542억"으로 이어지는 진짜 매치 시도까지 못 감). 그래서
        # cheon/bare는 반드시 man_coef(만/천만) 매치 안에 중첩시켜서, "만"이
        # 실제로 나온 뒤에만 이어지는 나머지 자릿수로 붙게 했다 - 조/억/만
        # 단위가 하나도 없는 순수 숫자는 이 패스에서 아예 후보가 안 된다.
        # [2026-07 추가] "12조3천억원"의 "3천억"도 "3천만"과 같은 종류의
        # 조합("천"이 바로 뒤 자릿수 단위의 계수로 곱해짐)인데, 처음엔
        # "억" 앞에는 이 조합을 안 만들어놔서 "12조"만 잡히고 "3천억"이
        # 통째로 빠지는 버그가 있었다(건설기성액 테스트 문장 작성 중
        # 발견). man_coef/man_scale과 완전히 같은 구조로 eok_coef/
        # eok_scale을 만들어 대칭을 맞췄다.
        # [2026-07 순서 변경/버그 수정 - 팀원 골든셋 실측] 이 패스를 콤마
        # 숫자 패스(옛 1번)보다 먼저 돌리도록 순서를 바꿨다. 예전 순서(콤마
        # 먼저)에서는 "2,916만명"/"6,838억달러"처럼 콤마 숫자 바로 뒤에
        # 조/억/만이 붙는 기사에서 콤마 패스가 "2,916"/"6,838"만 먼저
        # 채가버리고(used_spans 선점), 이 패스가 뒤이어 "만"/"억" 자릿수를
        # 붙이려 해도 겹침(overlap)으로 막혀서 그 값이 "만"/"억" 배율 없이
        # 그대로 claimed_value가 되는 실사용 버그가 있었다(예: "2,916만명"이
        # 29,160,000이 아니라 2916으로 파싱됨 - 팀원 골든셋 취업자수/총수출액
        # 케이스에서 실측). "23만8,300명"/"97만4,000가구"처럼 "만" 뒤
        # 나머지 자릿수가 콤마로 묶여 나오는 반대 방향도 마찬가지로 값이
        # 깨졌다(만 단위 앞부분이 통째로 빠짐). 이 패스를 먼저 돌리고,
        # 계수/나머지 자릿수 정규식에 콤마를 허용(_NUM)하면 두 방향 모두
        # 한 번에 온전히 잡힌다 - 콤마 패스(뒤에 남음)는 "만/억/조"가 전혀
        # 안 붙은 순수 콤마 숫자만 마저 잡게 된다.
        _large_num_re = re.compile(
            r"(?<!\d)"
            rf"(?:(?P<jo>{_NUM})조)?"
            rf"(?:(?P<eok_coef>{_NUM})(?P<eok_scale>천억|억))?"
            rf"(?:(?P<man_coef>{_NUM})(?P<man_scale>천만|만)"
            rf"(?:\s*(?P<cheon>{_NUM})천)?"
            rf"(?:\s*(?P<bare>{_NUM}))?)?"
        )
        for m in _large_num_re.finditer(user_input):
            jo = m.group("jo")
            eok_coef, eok_scale = m.group("eok_coef"), m.group("eok_scale")
            man_coef, man_scale = m.group("man_coef"), m.group("man_scale")
            cheon, bare = m.group("cheon"), m.group("bare")
            if not (jo or eok_coef or man_coef):
                continue  # 조/억/만 중 아무 자릿수 단위도 없으면 무시
            if overlaps(m.start(), m.end()):
                continue
            used_spans.append((m.start(), m.end()))
            value = 0.0
            # [2026-07 추가] 이 주장이 실제로 "얼마나 정밀한 수치"로 취급돼야
            # 하는지(허용오차 계산용)를 여기서 함께 계산해 claim에 담아둔다.
            # 예전에는 _claim_value_matches가 raw_text 전체를 다시 정규식
            # 파싱해서 "소수점 자릿수"만 봤는데, "4156억"처럼 조/억/만
            # 배율이 붙은 값은 raw_text의 표면 자릿수(4156, 소수점 0자리)와
            # 실제 절대값(415,600,000,000)의 정밀도가 완전히 다르다 -
            # 그대로 두면 허용오차가 ±0.5로 계산돼(실제 필요한 건 억 단위
            # 반올림 오차 ±0.5억) 정상적으로 일치하는 값도 전부 불일치로
            # 잡히는 실사용 회귀가 있었다(외환보유액/수출액 등 팀원 골든셋
            # 재실행 중 발견). 가장 작은(가장 뒤에 오는) 자릿수 단위를
            # 기준으로 정밀도를 계산해 claim에 실어보내고,
            # _claim_value_matches는 이 값이 있으면 그대로 쓴다.
            if bare:
                precision = 0.5 * (10 ** -_decimals(bare))
            elif cheon:
                precision = 500.0
            elif man_coef:
                unit = 10_000_000 if man_scale == "천만" else 10_000
                precision = 0.5 * unit * (10 ** -_decimals(man_coef))
            elif eok_coef:
                unit = 100_000_000_000 if eok_scale == "천억" else 100_000_000
                precision = 0.5 * unit * (10 ** -_decimals(eok_coef))
            else:
                precision = 0.5 * 1_000_000_000_000 * (10 ** -_decimals(jo))
            if jo:
                value += float(jo.replace(",", "")) * 1_000_000_000_000
            if eok_coef:
                value += float(eok_coef.replace(",", "")) * (
                    100_000_000_000 if eok_scale == "천억" else 100_000_000
                )
            if man_coef:
                value += float(man_coef.replace(",", "")) * (
                    10_000_000 if man_scale == "천만" else 10_000
                )
            if cheon:
                value += float(cheon.replace(",", "")) * 1_000
            if bare:
                value += float(bare.replace(",", ""))
            unit_m = self._CLAIM_UNIT_RE.match(user_input[m.end():])
            claims.append({
                "value": value,
                "unit": unit_m.group(1) if unit_m else None,
                "period": self._find_claim_period(
                    user_input, m.start(), reference_date=reference_date
                ),
                "raw_text": m.group(0),
                "is_forecast": self._is_forecast_claim(user_input, m.end()),
                "_pos": m.start(),
                "precision": precision,
            })

        # 2) 콤마 구분 숫자 (기존 extracted_claimed_value와 동일 패턴).
        # 위 큰수 패스보다 뒤에서 돈다 - 조/억/만이 붙은 콤마 숫자는 이미
        # 위에서 다 잡혔으므로, 여기 남는 건 배율이 안 붙은 순수 콤마
        # 숫자("4,248명")뿐이다.
        for m in re.finditer(
            r"(?<!\d)\d{1,3}(?:,\d{3})+(?:\.\d+)?", user_input
        ):
            if overlaps(m.start(), m.end()):
                continue
            used_spans.append((m.start(), m.end()))
            value = float(m.group(0).replace(",", ""))
            unit_m = self._CLAIM_UNIT_RE.match(user_input[m.end():])
            claims.append({
                "value": value,
                "unit": unit_m.group(1) if unit_m else None,
                "period": self._find_claim_period(
                    user_input, m.start(), reference_date=reference_date
                ),
                "raw_text": m.group(0),
                "is_forecast": self._is_forecast_claim(user_input, m.end()),
                "_pos": m.start(),
            })

        # 3) "7.5%"처럼 콤마도 만 단위도 아닌 퍼센트 소수
        # [2026-07 실측 버그 발견 - 골든셋] "성장률이 -0.2%로 마이너스(-)
        # 성장을 하자"처럼 마이너스(음수) 성장률 표기에서 부호가 통째로
        # 유실되는 버그가 있었다 - 기존 패턴이 "-"를 아예 인식하지 않아서
        # "-0.2%"가 "0.2%"(양수)로 뽑혔다. 이러면 우연히 KOSIS 실측값도
        # 같은 크기의 양수와 비교돼 "matched=True"로 잘못 통과하거나(부호
        # 반대인데 숫자가 같아 보이는 정확도 착시), 실제 KOSIS 값이 음수인
        # 케이스와 비교될 때 값이 달라 보여 억울하게 불일치로 나올 수 있다.
        # "-"가 숫자 바로 앞에 스페이스 없이 붙는 표기("-0.2%" - 실측된
        # 실제 뉴스 표기)만 허용하고, 앞이 이미 숫자/소수점인 경우(중간에
        # 낀 하이픈, 만에 하나 있을 "1-2%" 같은 범위 표기)는 제외해 오탐을
        # 막는다.
        # [2026-07 골든셋 원문 대조 중 추가 발견] 조선일보 등 실제 기사는
        # 마이너스 부호로 아스키 하이픈("-", U+002D) 대신 엔대시("–",
        # U+2013)를 쓰는 경우가 있다("성장률이 –0.2%로" - 골든셋 a017-1
        # 원문 그대로). 둘 다 부호로 인식해야 부호 유실 버그가 완전히
        # 막힌다.
        for m in re.finditer(
            r"(?<![\d.])([-–]?\d+(?:\.\d+)?)\s*[%％]", user_input
        ):
            if overlaps(m.start(), m.end()):
                continue
            used_spans.append((m.start(), m.end()))
            raw_value = m.group(1).replace("–", "-")
            claims.append({
                "value": float(raw_value),
                "unit": "%",
                "period": self._find_claim_period(
                    user_input, m.start(), reference_date=reference_date
                ),
                "raw_text": m.group(0),
                "is_forecast": self._is_forecast_claim(user_input, m.end()),
                "_pos": m.start(),
            })

        # 4) [2026-07 실측 발견] "3.8건"/"0.75명"처럼 콤마도 "만" 단위도
        # %도 아닌, 단위가 바로 붙는 순수 소수/정수. "조혼인율은 3.8건으로"/
        # "합계출산율은 0.75명으로" 같은 문장이 위 세 패턴 어디에도 안
        # 걸려서 claims가 통째로 0개로 나오는(=extract_all_claims 첫 단계
        # 에서부터 UNVERIFIED로 조기 종료) 실사용 버그가 인구/출산 지표군
        # 실측 중 발견됐다.
        # 주의 1: "배"는 일부러 뺀다("전체 연령대 실업률의 2.4배" 같은 표현은
        # 두 통계의 비율일 뿐 KOSIS 원본 값과 직접 비교할 수 없는 파생값이라
        # claims에서 계속 제외해야 한다 - 위 함수 docstring에 있는 기존
        # 설계 결정과 동일).
        # 주의 2: "개" 뒤에 "월"이 오면("3년 9개월") 그건 수량 단위 "개"가
        # 아니라 기간을 세는 "개월"이므로 제외한다(음성 전방탐색) - 이걸
        # 안 걸면 "9개월"이 "9개"라는 가짜 주장으로 잡히는 오탐이 생긴다
        # (실측 회귀 테스트로 발견).
        # [2026-07 추가] "포인트"(지수 값 - 생산자물가지수/소매판매액지수/
        # 설비투자지수 같은 "2020=100" 류 지수는 단위 없이 숫자만 쓰거나
        # "120.34포인트"처럼 "포인트"를 붙여 쓴다) - 없으면 지수형 지표는
        # claims 자체가 안 뽑히는 사각지대가 생긴다(신규 지표 힌트 추가
        # 중 발견).
        for m in re.finditer(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*(명|원|개(?!월)|건|곳|대|동|실|점|포인트)",
            user_input,
        ):
            if overlaps(m.start(), m.end()):
                continue
            used_spans.append((m.start(), m.end()))
            claims.append({
                "value": float(m.group(1)),
                "unit": m.group(2),
                "period": self._find_claim_period(
                    user_input, m.start(), reference_date=reference_date
                ),
                "raw_text": m.group(0),
                "is_forecast": self._is_forecast_claim(user_input, m.end()),
                "_pos": m.start(),
            })

        # 5) [2026-07 실측 발견] "소비자심리지수는 101.2로 전월대비 1.5포인트
        # 상승했다"처럼, 지수형 지표의 절대 수준값은 단위 글자 없이 조사
        # "로"만 붙어 나오는 경우가 흔하다(경기종합지수/CSI류 - "2020=100"
        # 같은 기준연도 표기 자체가 단위이지 "포인트" 등 글자 단위가 항상
        # 따라붙는 게 아님). 패턴 4는 단위 글자가 바로 뒤에 있어야만 잡는데,
        # 이 경우 "101.2"가 claims에서 통째로 빠지고 뒤따르는 증감분("1.5
        # 포인트")만 잡혀 절대값 대조 자체가 안 되는 사각지대가 있었다.
        # 소수점이 있는 숫자로 한정해서(정수만 있는 연도 등과 구분) 오탐
        # 위험을 낮춘다.
        for m in re.finditer(r"(?<!\d)(\d+\.\d+)(?=\s*로(?:\s|[,.]|$))", user_input):
            if overlaps(m.start(), m.end()):
                continue
            used_spans.append((m.start(), m.end()))
            claims.append({
                "value": float(m.group(1)),
                "unit": None,
                "period": self._find_claim_period(
                    user_input, m.start(), reference_date=reference_date
                ),
                "raw_text": m.group(0),
                "is_forecast": self._is_forecast_claim(user_input, m.end()),
                "_pos": m.start(),
            })

        claims.sort(key=lambda c: c["_pos"])

        # [2026-07 추가] "전년 동월"/"지난해 동기"처럼 "문장 내 다른 절대
        # 시점"이 기준이 되는 경우 - _find_claim_period가 이런 표현은
        # reference_date로 잘못 계산하지 않으려고 일부러 건너뛰므로(스킵된
        # 채 period=None으로 남아있음) 여기서 별도로 채운다. "동월 대비"
        # 자체가 이미 결정적인 신호(같은 달, 연도만 -1)라 LLM 없이 코드
        # 산술로 충분하다 - relative_date_edge_cases.md 4번 참고.
        self._resolve_same_period_last_year(claims, user_input)

        # [2026-07 추가] "지난해 1분기도...15.6%...2분기(20.9%)"처럼 앞
        # 절의 연도를 생략형으로 물려받는 "N분기" 표현 후처리(위와 같은
        # 이유로 여기서 처리 - 동월/동기 해석 다음에 돌려서 그 결과로 새로
        # 채워진 앵커도 활용할 수 있게 한다).
        self._resolve_bare_quarter_reference(claims, user_input)

        # [2026-07-24 추가/MCP 실측 - 소비자물가지수_10월] "1년 전과
        # 비교해 2.4% 상승"처럼 "N년 전"류가 이 주장의 시점이 아니라 순수
        # 비교 기준점 마커로만 쓰여(_COMPARISON_MARKER_RE) period=None으로
        # 남은 claim - "지금(reference_date)" 시점 값으로 채운다(KOSIS
        # 등락률 컬럼이 실제로 "지금 시점"에 인덱싱된다는 MCP 실측 근거).
        self._resolve_own_period_from_comparison_marker(
            claims, user_input, reference_date
        )

        # [2026-07 추가] 동월/동기 근접 가드 때문에 period=None으로 남았고
        # (_resolve_same_period_last_year로도) 앵커를 못 찾은 claim은
        # 마지막으로 가드 없이 한 번 더 시도한다 - "OO월 지표는 전년동월
        # 대비 X% 상승했다"처럼 앵커가 될 다른 claim이 애초에 없는
        # 단일-주장 문장을 위한 안전한 폴백(문장 자체의 절대 시점 그대로
        # 채택 - 위 _find_claim_period 주석 참고).
        for c in claims:
            if c.get("period"):
                continue
            pos = c.get("_pos")
            if pos is None:
                continue
            if not self._SAME_PERIOD_KEYWORD_RE.search(
                user_input[max(0, pos - 20):pos]
            ):
                continue
            c["period"] = self._find_claim_period(
                user_input, pos, reference_date=reference_date,
                _skip_same_period_guard=True,
            )

        # [2026-07-24 추가 - #54] 절대/상대 시점 해석이 전부 실패해 여전히
        # period가 없는 claim 중, "N년 새/만에 늘어난/줄어든" 류 두 시점
        # 비교 표현이 있는 것을 찾아 diff 평가용 메타를 붙인다. _pos가
        # 지워지기 전에(바로 아래) 돌려야 한다.
        self._resolve_diff_duration_claims(claims, user_input)

        for c in claims:
            c.pop("_pos", None)

        if claims:
            logger.info(
                "  └─ [다중 주장 추출] "
                + ", ".join(
                    f"{c['raw_text']}"
                    f"(시점={c['period'] or '?'})"
                    + (
                        f"[diff:{c['diff_years']}년 {c['diff_direction']}]"
                        if c.get("is_diff_claim")
                        else ""
                    )
                    for c in claims
                )
            )
        return claims

    def _resolve_diff_duration_claims(
        self, claims: List[Dict[str, Any]], text: str
    ) -> None:
        """[#54 - 두 시점 diff claim] period가 여전히 None인 claim 중,
        claim 값 바로 앞에 "N년 새/만에/동안/사이" 기간 표현이 있고 claim
        값 바로 뒤에 증가/감소 동사가 있으면, 그 claim에 diff 평가용
        메타(diff_years/diff_direction)를 붙인다. 이미 period가 확정된
        claim은 diff 표현이 아니라 그냥 그 시점의 절대값 주장으로 보고
        건드리지 않는다(period와 diff는 상호 배타적인 해석이다).
        """
        for c in claims:
            if c.get("period"):
                continue
            pos = c.get("_pos")
            if pos is None:
                continue
            before = text[max(0, pos - 20):pos]
            m = self._DIFF_DURATION_RE.search(before)
            if not m:
                continue
            after = text[pos:pos + 30]
            inc = self._DIFF_INCREASE_VERB_RE.search(after)
            dec = self._DIFF_DECREASE_VERB_RE.search(after)
            if not inc and not dec:
                continue
            if inc and dec:
                direction = "increase" if inc.start() < dec.start() else "decrease"
            else:
                direction = "increase" if inc else "decrease"
            c["is_diff_claim"] = True
            c["diff_years"] = int(m.group(1))
            c["diff_direction"] = direction

    def _assign_claims_to_indicator_groups(
        self, user_input: str, claims: List[Dict[str, Any]]
    ) -> "Dict[str, List[Dict[str, Any]]]":
        """[2026-07 실측] 한 문단에 서로 다른 지표(예: "혼인건수는 24만
        건으로... 조혼인율은 4.7건... 이혼건수는 8만 8천 건... 조이혼율은
        1.7건")가 섞여 있으면, fact_check_text가 지표/컬럼을 하나만 확정해
        전부 그 컬럼 기준으로 대조하는 바람에 실제로는 맞는 값인데도
        "일치 안 함"으로 나오는 문제가 실측됐다("혼인·이혼 2025" 기사
        테스트에서 6개 중 1개만 일치로 나온 사례).

        _find_claim_period("숫자 앞 30자 안에서 20XX년을 찾는다")와 같은
        계열의 근접 매칭이지만, 지표 키워드는 보통 문장 맨 앞에 한 번만
        나오고 그 뒤 여러 숫자(원값+등락률 등)가 딸려 나오는 경우가 많아
        고정 윈도우 대신 "지금까지 나온 것 중 가장 최근 키워드"를 계속
        이어받는 방식을 쓴다(왼쪽에서 오른쪽으로 훑으면서 상태를 유지).

        LLM은 여기 새로 투입하지 않는다 - INDICATOR_ALIAS_MAP은 이미
        정해진(닫힌) 어휘 사전이라 이건 의미 판단이 아니라 문자열 위치
        비교 문제고, 어차피 사전에 없는 표현(진짜 모호한 케이스)은 LLM을
        불러도 우리 DEFAULT_INDICATOR_METADATA에 없으면 검증할 방법이
        없다(vectorDB/라벨링 오라클로 남겨둔 장기 과제와 동일한 범위).
        키워드가 하나도 안 걸리는 숫자는 그냥 이 dict에서 빠지고, 호출
        쪽(fact_check_text)에서 이미 LLM(HCX)으로 뽑아둔 대표 지표에
        귀속시킨다 - 그게 실질적인 "모호할 때 LLM 폴백"이다.

        반환: {정규화된 지표명(INDICATOR_ALIAS_MAP의 값): [claim, ...]}
        지표 키워드가 아예 없는 문단이면 빈 dict.
        """
        sorted_aliases = sorted(
            INDICATOR_ALIAS_MAP.keys(), key=len, reverse=True
        )

        # 1) 텍스트 안에 등장하는 모든 키워드 위치를 찾는다(긴 별칭 우선 -
        # "합계출산율"을 먼저 잡아야 그 부분문자열인 "출산율"이 같은
        # 자리에서 또 걸리지 않는다).
        keyword_hits: List[tuple] = []  # (pos, 정규화된 지표명)
        covered: List[tuple] = []

        def overlaps(s: int, e: int) -> bool:
            return any(cs < e and s < ce for cs, ce in covered)

        for alias in sorted_aliases:
            start = 0
            while True:
                idx = user_input.find(alias, start)
                if idx == -1:
                    break
                end = idx + len(alias)
                if not overlaps(idx, end):
                    keyword_hits.append((idx, INDICATOR_ALIAS_MAP[alias]))
                    covered.append((idx, end))
                start = idx + 1

        if not keyword_hits:
            return {}
        keyword_hits.sort(key=lambda h: h[0])

        # 2) 각 숫자 주장을, 그 앞에서 가장 마지막(최근)으로 나온 키워드에
        # 배정한다. 앞에 키워드가 하나도 없으면 이 그룹핑에서 제외한다.
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for claim in claims:
            claim_pos = user_input.find(claim["raw_text"])
            if claim_pos == -1:
                continue
            current = None
            for pos, mapped in keyword_hits:
                if pos <= claim_pos:
                    current = mapped
                else:
                    break
            if current is not None:
                groups.setdefault(current, []).append(claim)

        return groups

    # ------------------------------------------------------------------
    # [분류 전담 모델] 이 요청이 어떤 종류인지 판단만 한다
    # ------------------------------------------------------------------
    # extract_delta_entities/규칙 기반 정규식은 "숫자/키워드를 뽑는" 역할만
    # 맡고, "이게 전월대비/전년대비/두 시점 직접비교/단일값/전망치 중 뭔지",
    # "KOSIS 공식 등락률 컬럼을 찾아야 하는지 원자료를 직접 비교해야
    # 하는지" 같은 판단은 별도 모델 호출로 분리한다. 이렇게 역할을 좁혀서
    # 주면(날짜 숫자를 새로 만들어내지 말고, 이미 뽑힌 힌트만 보고
    # 분류만 하라고 시키면) 모델이 날짜 형식을 잘못 만들어내는 사고를
    # 줄일 수 있고, 나중에 새로운 케이스(분기/반기, 지역 등)가 생겨도
    # 정규식을 계속 늘리는 대신 이 프롬프트만 넓히면 된다.

    def classify_request(
        self, user_input: str, rule_hints: Dict[str, Any]
    ) -> Dict[str, Any]:
        system_instruction = (
            "당신은 통계 팩트체크 요청을 분류하는 전담 모듈입니다. 이미"
            " 규칙 기반으로 추출된 시점 힌트(rule_hints)를 참고해서, 아래"
            " 항목만 판단해 JSON으로 응답하세요. 날짜 숫자는 새로 만들어내지"
            " 말고 rule_hints에 있는 값을 그대로 신뢰하세요.\n\n"
            "- period_type: 다음 중 하나만 선택\n"
            '  "mom" (전월/지난달 대비 비교),\n'
            '  "yoy" (전년/작년 대비 비교),\n'
            '  "explicit_pair" (두 시점이 문장에 직접 명시되어 그 둘을'
            " 비교),\n"
            '  "single" (비교 없이 특정 한 시점의 수치만 확인),\n'
            '  "forecast" (아직 발생하지 않은 미래 시점의 전망/예측치'
            " 주장),\n"
            '  "unclear" (문장만으로 판단 불가)\n'
            "- is_forecast: 전망/예측치 주장이면 true, 아니면 false\n"
            "- rate_preference: 기사가 KOSIS가 공식 제공하는 등락률/증감률"
            ' 수치 자체를 언급하는 것 같으면 "official_rate", 서로 다른'
            ' 두 시점의 원자료를 직접 비교하는 것 같으면 "manual_diff",'
            ' 판단이 안 되면 "unspecified"\n'
            "- period_direction_confidence: 시점이 두 개 언급된 경우에만"
            " 해당 - 어느 쪽이 조회 대상(기준) 시점이고 어느 쪽이 비교"
            ' 기준선인지 문장 어순만으로 확신할 수 있으면 "high", 애매하면'
            ' "low" (시점이 하나뿐이면 "high"로 응답)\n\n'
            "반드시 순수 JSON 객체 형태로만 출력하세요 (마크다운 백틱 ```"
            " 사용 금지)."
        )
        messages = [
            {"role": "system", "content": system_instruction},
            {
                "role": "user",
                "content": (
                    f"규칙 기반 추출 힌트: {json.dumps(rule_hints, ensure_ascii=False)}\n"
                    f'사용자 입력: "{user_input}"'
                ),
            },
        ]
        default: Dict[str, Any] = {
            "period_type": "unclear",
            "is_forecast": False,
            "rate_preference": "unspecified",
            "period_direction_confidence": "high",
        }
        try:
            raw = self.hcx.generate_completion(messages, temperature=0.1)
            clean = re.sub(r"```json|```", "", raw).strip()
            parsed = json.loads(clean)
            for k, v in default.items():
                if parsed.get(k) is None:
                    parsed[k] = v
            logger.info(f"  └─ [요청 분류] {parsed}")
            return parsed
        except Exception as e:
            logger.warning(f"⚠️ [요청 분류 예외 - 기본값 사용]: {e}")
            return default

    # ------------------------------------------------------------------
    # [Step A] 키워드 기반 통계표 동적 검색
    # ------------------------------------------------------------------
