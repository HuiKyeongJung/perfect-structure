import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Union

# Gemini 대신 HCXClient 연동
from client import HCXClient, KosisApiClient
from adapter import route_claim_group
from kosis_config import INDICATOR_ALIAS_MAP, DEFAULT_INDICATOR_METADATA
from kosis_text_utils import TextUtilsMixin
from kosis_extraction import ExtractionMixin
from kosis_resolution import ResolutionMixin
from kosis_fetch import FetchMixin

# 로깅 설정
# [2026-07 변경] 표 후보를 여러 개 순회하며 컬럼/차원/기간을 검증하는
# 로그(딥서치 캐스케이드)가 화면을 너무 많이 채워서, 화면(콘솔)에는
# INFO 이상만 보이게 하고 전체 DEBUG 로그는 파일에만 쌓이게 나눴다.
# 개별 캐스케이드성 로그 호출(logger.debug로 낮춘 것들)은 kosis_agent.py/
# kosis_resolution.py/kosis_fetch.py/client.py 전반에 흩어져 있는데,
# 전부 같은 루트 로거를 쓰므로 여기 한 곳에서만 핸들러를 잡으면 된다.
_LOG_FILE = "kosis_factcheck.log"

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)

_file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_console_handler, _file_handler],
)
logger = logging.getLogger("Task2.KosisChatAgent")

# [2.4절] 표제목 끝에 붙는 "계열 구분" 접미사 - 진짜 형제 표(같은 지표,
# 시리즈만 다름)는 예외 없이 이 패턴으로만 구별된다(예: "전산업생산지수
# (원지수)" vs "전산업생산지수(계절조정지수)", 2026-08-05 KOSIS 통합검색
# 실측 확인). [2026-08-10 수정] 예전엔 STAT_ID(같은 조사)가 있으면 그걸
# 우선 신호로 쓰고 이 접미사 비교는 STAT_ID가 없을 때만 쓰는 폴백이었는데,
# 실측(문화체육관광일자리현황조사)에서 STAT_ID 기준이 축·컬럼 구조가
# 전혀 다른 표들까지 "같은 조사"라는 이유로 형제로 묶어버리는 문제가
# 드러났다. 표제목이 계열 접미사만 다르고 나머지가 완전히 같다는 게
# KOSIS에서 실제로 확인된 유일하게 신뢰할 수 있는 형제 표 신호라, 이제는
# find_sibling_tables가 STAT_ID 없이 이 접미사 비교 하나로만 형제를
# 판정한다.
_SERIES_SUFFIX_RE = re.compile(
    r"\s*[\(（]"
    r"(원지수|계절조정지수|계절조정|추세변동치|추세변동|추세치|추세|원계열|불변|경상)"
    r"[\)）]\s*$"
)


def _series_stripped_tbl_nm(tbl_nm: Optional[str]) -> Optional[str]:
    if not tbl_nm:
        return tbl_nm
    return _SERIES_SUFFIX_RE.sub("", tbl_nm).strip()


class KosisInteractiveAgent(
    ExtractionMixin, ResolutionMixin, FetchMixin, TextUtilsMixin
):
    """KOSIS 완전자동화 팩트체크 에이전트 컨트롤러.

    실제 기능은 역할별로 분리된 mixin에 있다:
      - ExtractionMixin (kosis_extraction.py): 엔티티 추출/요청 분류
      - ResolutionMixin (kosis_resolution.py): 통계표/컬럼/분류값 확정
      - FetchMixin (kosis_fetch.py): 실데이터 조회 및 행 선택
      - TextUtilsMixin (kosis_text_utils.py): 문자열/메타 파싱 유틸

    [2026-07-25 변경] 예전 대화형 턴 컨트롤러(process_turn/
    _process_turn_inner)는 chatbot.py(Streamlit)로 완전히 옮겼다. 이
    파일에는 문단 하나를 통째로 검증하는 완전자동화 진입점
    (fact_check_text 이하)만 남아있다. self.slots는 process_turn 시절의
    잔재지만, rate_preference/claimed_unit 등 일부 키는 자동화 경로
    (kosis_resolution.py)에서도 여전히 읽으므로 그대로 유지한다 -
    chatbot.py에서 이 클래스를 쓸 때도 이 슬롯들을 그대로 재사용하면 된다.
"""

    def __init__(self):
        # 🎯 GeminiClient 대신 HCXClient 바인딩
        self.hcx = HCXClient()
        self.kosis = KosisApiClient()

        # 이번 턴에 실제로 실데이터를 조회한(fetch_kosis_data_range를
        # 호출한) 통계표 ID들을 순서대로 기록한다(중복 제거). 표 자동
        # 전환/값 비교 구제 과정에서 로그가 한 턴에 수십 줄씩 찍히다 보니
        # "결국 어떤 표들을 실제로 건드렸는지"를 한눈에 보기 어렵다는
        # 피드백이 있었다 - process_turn이 끝날 때 이 목록을 요약 한 줄로
        # 남겨서 디버깅 시 스크롤 없이 바로 확인할 수 있게 한다.
        # process_turn 시작마다 초기화된다(kosis_fetch.fetch_kosis_data_range
        # 참고).
        self._queried_table_trail: List[str] = []

        # 슬롯 메모리
        self.slots: Dict[str, Optional[Any]] = {
            "target_indicator": None,
            "base_year": None,
            "compare_year": None,
            "target_unit_cat": None,
            "is_comparison": False,
            # --- 통계표/컬럼(ITM) 확정 상태 ---
            "resolved_org_id": None,
            "resolved_tbl_id": None,
            "resolved_tbl_nm": None,
            "resolved_itm_id": None,
            "resolved_itm_nm": None,
            # resolved_itm_nm은 리프 이름만("300인 이상") 담는데, 같은 축
            # 번호 안에도 산업분류/종사자규모/매출액규모처럼 서로 다른
            # 그룹이 섞여 있을 수 있어 리프 이름만으론 어느 그룹인지 알 수
            # 없다. "루트그룹 > 리프이름" 형태의 경로(예: "종사자규모 >
            # 300인 이상")를 여기 별도로 담아, 최종 응답에서 "매칭 항목"으로
            # 투명하게 보여줘 사용자가 개념이 맞는지 스스로 판단하게 한다.
            "resolved_itm_breadcrumb": None,
            # extract_all_claims()가 뽑아낸 문장/문단 안의 모든 숫자 주장
            # 목록 - 여러 개(2개 이상)면 표 후보를 놓고 값 하나만이 아니라
            # 여러 시점 값을 동시에 대조해 훨씬 강한 확신으로 자동 채택하는
            # 데 쓴다 (score_candidate_against_claims 참고).
            "extracted_claims": [],
            # score_candidate_against_claims 결과(다중 주장 대조로 자동
            # 확정된 경우에만 채워짐) - 최종 응답에서 "몇 개 중 몇 개
            # 일치"를 투명하게 보여주는 데 쓴다.
            "claims_match_summary": None,
            # 확정된 컬럼이 사실 ITM이 아니라 OBJ 분류값이었던 경우(예:
            # "정비사"가 실제로는 "특성별" 분류축의 코드값), 그 축 번호와
            # 코드값. objL{resolved_obj_axis}=resolved_obj_code로 서버에
            # 직접 필터를 걸어 정확한 값만 받아오는 데 쓴다.
            "resolved_obj_axis": None,
            "resolved_obj_code": None,
            # 확정된 통계표의 실제 수록기간(YYYY~YYYY) - 요청한 연도가 이
            # 범위 밖이면 KOSIS를 호출하기 전에 미리 안내한다 (예: 표가
            # 1992~1996년까지만 있는데 2023년을 요청한 경우).
            "resolved_period_start": None,
            "resolved_period_end": None,
            # "202509 대비 202409"처럼 두 시점이 함께 나왔을 때, 어느 쪽이
            # 기준/비교 시점인지 사용자에게 확인받는 동안 잠깐 담아두는 슬롯
            "pending_period_confirmation": None,
            # 컬럼이 여러 개로 모호할 때 되묻기 위한 후보 목록
            "pending_item_candidates": [],
            # 통계표 후보가 여럿일 때 되묻기 위한 후보 목록 (커스텀 힌트가
            # 없는 지표에서, 내용 기반 검색 결과가 1개보다 많을 때 채워짐)
            "pending_table_candidates": [],
            # "농림어업 포함/제외" 처럼 같은 항목 안에서도 카테고리가 갈리는
            # 경우를 사용자가 명시했을 때 저장 (예: "포함", "제외")
            "category_hint": None,
            # --- 시점(연/월) 관련 ---
            # period_mode: "Y"(연간, 전년대비) | "M"(월간, 전월대비)
            "period_mode": "Y",
            "base_month": None,  # YYYYMM
            "compare_month": None,  # YYYYMM
            # --- [분류 전담 모델] classify_request() 결과 ---
            # period_type: "mom"|"yoy"|"explicit_pair"|"single"|"forecast"|"unclear"
            "period_type": None,
            # rate_preference: "official_rate"|"manual_diff"|"unspecified" -
            # 컬럼 탐색 시 "등락률"/"증감률" 같은 KOSIS 공식 증감률 컬럼도
            # 후보에 포함시킬지 판단하는 데 쓴다.
            "rate_preference": None,
            # 예측/전망치 주장이면 KOSIS 실측 데이터로는 애초에 검증 불가 -
            # 이 경우 조회를 아예 시도하지 않고 바로 안내한다.
            "is_forecast": False,
            # "202509 대비 202409"처럼 두 시점이 함께 나왔을 때, 어느 쪽이
            # 기준/비교 시점인지 사용자에게 확인받는 동안 잠깐 담아두는 슬롯
            "pending_period_confirmation": None,
            # 비교 시점이 전혀 언급 안 된 단일 시점 요청일 때, 한 번은
            # "비교하실 다른 시점 있으신가요?"라고 물어봤는지/그 답을
            # 기다리는 중인지 표시 (같은 지표에 계속 다시 묻지 않기 위함)
            "asked_for_comparison": False,
            "awaiting_comparison_reply": False,
            # 기사/주장 문장이 인용한 수치(예: "4,248명" -> 4248.0). 있으면
            # 통계표 후보가 여럿일 때 후보별 실제 값을 조회해 이 값과 가장
            # 가까운 후보를 자동으로 채택하는 데 쓴다.
            "claimed_value": None,
            # claimed_value 바로 뒤에 붙은 단위("4,248명" -> "명"). 사람 수를
            # 물었는데 단위가 "개"/"원"/"%"인 후보처럼, 숫자가 우연히
            # 가까워도 애초에 종류가 다른 값을 걸러내는 데 쓴다.
            "claimed_unit": None,
            # 위 다중 표 비교에서 실제로 값까지 조회해본 후보들 (표/항목/값) -
            # 최종 응답에서 "다른 후보는 얼마였는지"를 투명하게 보여주는 데
            # 쓴다.
            "multi_table_comparison": [],
        }

    # ------------------------------------------------------------------
    # [2026-07-25 제거] 예전 대화형(process_turn/_process_turn_inner) 턴
    # 컨트롤러는 여기 있었다 - Streamlit 챗봇(chatbot.py)으로 완전히
    # 옮기면서 제거했다. 자동화 경로(fact_check_text 이하)는 이 파일에
    # 그대로 남아있고, self.slots(위 __init__)도 자동화 경로 일부
    # (rate_preference/claimed_unit 등, kosis_resolution.py에서 계속 읽음)가
    # 의존하고 있어 그대로 유지한다.
    # ------------------------------------------------------------------


    def _broad_search_table_candidates(
        self, indicator: str
    ) -> List[Dict[str, Any]]:
        """지표명으로 검색어를 넓혀가며(수식어 제거/유사어) 찾을 수 있는
        통계표 후보를 최대한 모은다. _resolve_table_for_claims(힌트 없는
        경로)와 _find_alternate_table_match(동명이표 재검색) 양쪽이
        똑같은 탐색 로직을 쓰므로 공용 헬퍼로 분리했다.

        [2026-08-10 수정 - 무조건 HCX 호출 제거] 예전엔 `_suggest_broader_
        search_terms`("딥서치" 상위 검색어 제안, HCX 1회)를 검색 결과가
        이미 충분해도 매번 호출했다 - 실측(전산업생산지수)에서 1차 검색이
        이미 16건을 찾아 정답 표가 1순위였는데도 이 호출이 그대로 나갔고,
        추가로 얻어온 검색어 중 하나("경제활동 인구 및 산업별 생산성
        조사")는 오히려 무관한 표("노동 통계")로 이어졌다. 이 함수의
        docstring이 원래 밝힌 존재 이유("정비사"처럼 표 제목에 키워드가
        아예 없어서 1차 검색이 완전히 실패하는 경우)에 맞춰, 1차 검색이
        이미 후보를 찾았으면 이 HCX 호출 자체를 생략한다 - 형제 표 발견은
        이제 별도의 저비용 경로(find_sibling_tables, 7.8절)가 맡고
        있어서, 1차 검색에 걸린 경우까지 이 딥서치로 보강할 필요가
        줄었다.
        """
        raw_candidates = self.search_table_candidates(indicator)
        seen_ids = {(c.get("ORG_ID"), c.get("TBL_ID")) for c in raw_candidates}
        broader_terms = (
            list(self._suggest_broader_search_terms(indicator))
            if not raw_candidates
            else []
        )
        stripped_term = self._qualifier_stripped_search_term(indicator)
        if stripped_term:
            broader_terms.append(stripped_term)
        for term in broader_terms:
            search_top_n = 15 if term == stripped_term else 5
            search_result_count = 50 if term == stripped_term else 20
            for c in self.search_table_candidates(
                term, top_n=search_top_n, result_count=search_result_count
            ):
                key = (c.get("ORG_ID"), c.get("TBL_ID"))
                if key not in seen_ids:
                    raw_candidates.append(c)
                    seen_ids.add(key)
        return raw_candidates

    def _broad_search_table_candidates_for_keywords(
        self, keywords: List[str]
    ) -> List[Dict[str, Any]]:
        """[종합 프로젝트 - 검색 단계 확장] 키워드 그룹(여러 개의 독립된
        검색어 - 예: "학교급 성별"/"독서 실태"처럼 한 claim에 대해 여러
        키워드가 동시에 들어오는 경우) 각각에 대해 _broad_search_table_
        candidates를 호출하고, 결과를 (ORG_ID, TBL_ID) 기준으로 하나의
        set으로 합쳐 중복을 제거한다.

        같은 통계표가 서로 다른 키워드의 검색 결과에 동시에 나올 수 있는데
        (예: "학교급 성별"과 "독서한 적 있음"이 같은 표에서 나오는 경우),
        그 표에 대한 상세 조회(메타/값 - verify_table_candidates_by_meta/
        gather_candidate_values)는 통계표 하나당 딱 한 번만 하면 된다.
        여기서 미리 합치고 중복 제거를 끝내두면, 뒤이은 상세 조회 단계가
        같은 표를 키워드 개수만큼 중복 호출하는 낭비를 막을 수 있다.

        기존 _broad_search_table_candidates(단일 키워드 하나에 대한
        검색어 확장 로직 - 유사어/수식어 제거로 넓혀가며 찾는 부분)는
        건드리지 않고, 여러 키워드를 순회하며 그 함수를 그대로 재사용하는
        얇은 래퍼로만 짰다 - 이미 검증된 단일 키워드 검색 로직 위에 새
        기능(키워드 그룹 처리)을 얹은 것이지, 기존 로직을 다시 짠 게
        아니다.

        raw_sentence(claim 원문장)는 여기서 전혀 쓰지 않는다 - 검색은
        순수하게 키워드 기반이고, 원문장은 판정 단계(hedge/근사 표현
        해석)에서만 필요하다. 그래서 이 함수의 입출력에도 원문장을 억지로
        끼워 넣지 않았다 - claim 객체(adapter.py의 Claim.raw_sentence)가
        파이프라인 전체에 걸쳐 그대로 들고 다니면 되고, 검색 단계는 그
        값을 몰라도(건드리지 않아도) 무방하다.
        """
        merged: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for keyword in keywords:
            if not keyword:
                continue
            for c in self._broad_search_table_candidates(keyword):
                key = (c.get("ORG_ID"), c.get("TBL_ID"))
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                merged.append(c)
        return merged

    def find_sibling_tables(
        self, org_id: str, tbl_id: str, tbl_nm: str
    ) -> List[Dict[str, Any]]:
        """[종합 프로젝트 - 2.4절] 확정된 표(org_id/tbl_id/tbl_nm)와 표제목이
        "계열 접미사"(원지수/계절조정 등, _SERIES_SUFFIX_RE)만 다르고
        나머지는 완전히 같은 형제 표를 함께 찾는다. 반환 리스트에는 확정된
        표 자기 자신도 포함한다 - 호출부(2.3절의 전국 집계 폴백 등)가
        "이 계열의 표 전부"를 한 번에 다루기 편하도록.

        기존 _find_alternate_table_match(동명이표 재검색)는 값이 하나라도
        안 맞을 때 대체 표 하나를 찾는 1:1 스왑이라 목적이 다르다 - 이
        함수는 스왑이 아니라 "그룹 전체 수집"이고, unmatched_claims 같은
        판정 결과에 의존하지 않고 표가 확정되는 즉시 호출 가능하다.

        표제목(TBL_NM)으로 그대로 재검색하면 형제 표를 놓친다 -
        "...(원지수)"와 "...(계절조정지수)"는 문자열 자체가 다르기
        때문이다(2026-08-05 KOSIS 통합검색 실측: "전산업생산지수"가 정확히
        이 패턴으로 DT_1JH20201/DT_1JH20202 두 표로 나뉘어 있음을 확인).
        그래서 계열 접미사를 뗀 기본 이름으로 재검색한 뒤, 후보들도 같은
        방식으로 접미사를 뗐을 때 완전히 같은 이름인 경우만 형제로 인정한다.

        [2026-08-10 실측 수정 - 사용자 지적] 원래는 이 표제목 비교를
        "STAT_ID(같은 조사)가 있으면 그걸 우선, 없을 때만 표제목 비교로
        폴백"하는 순서였다. 실측(문화체육관광일자리현황조사,
        DT_113_STBL_1031340)에서 STAT_ID 기준이 너무 넓다는 게 드러났다 -
        "임금 동향"/"종사상지위별 임금 동향"/"종사자 수 동향"처럼 컬럼·축
        구조 자체가 다른 표들이 같은 조사(같은 STAT_ID) 밑에 같이
        발행되는데, 이걸 전부 "형제"로 묶어버려 값 대조로도 못 좁히는
        모호함으로 이어졌다. 근본 원인은: 우리가 KOSIS에서 받을 수 있는
        건 표 메타(제목/축 목록)뿐이고, 두 표가 "진짜 같은 구조"인지는
        그 메타만으로 안정적으로 판별할 방법이 없다는 것이다 - STAT_ID는
        "같은 조사에서 나왔다"는 것만 보장하지 "축 구성이 같다"는 것까지는
        보장하지 않는다. 반대로 KOSIS가 실제로 "원지수"/"계절조정" 같은
        진짜 형제 표를 만들 때는 예외 없이 표제목 자체를 "같은 이름 +
        계열 구분만 괄호로 추가"하는 규칙을 쓴다는 게 이번 세션에 확인된
        유일하게 신뢰할 수 있는 신호였다. 그래서 STAT_ID 매칭은 완전히
        빼고, 표제목의 계열 접미사만 뗀 뒤 완전히 같은 문자열인 경우로만
        형제 판정을 좁혔다 - "임금 동향(1인당 월평균)"과 "종사상지위별
        임금 동향(1인당 월평균)"은 "(1인당 월평균)"이 _SERIES_SUFFIX_RE가
        아는 계열 접미사가 아니라 그대로 남아있으므로 애초에 두 문자열이
        같지 않아 형제로 안 묶인다(실측 확인).
        """
        base_name = _series_stripped_tbl_nm(tbl_nm) or tbl_nm
        candidates: List[Dict[str, Any]] = []
        if base_name:
            candidates = self.search_table_candidates(
                base_name, top_n=20, result_count=30
            )

        seen_ids = {(org_id, tbl_id)}
        siblings: List[Dict[str, Any]] = [
            {"ORG_ID": org_id, "TBL_ID": tbl_id, "TBL_NM": tbl_nm}
        ]

        for c in candidates:
            key = (c.get("ORG_ID"), c.get("TBL_ID"))
            if key in seen_ids:
                continue
            if _series_stripped_tbl_nm(c.get("TBL_NM")) == base_name:
                seen_ids.add(key)
                siblings.append(c)

        if len(siblings) > 1:
            logger.info(
                f"  └─ [형제 표 발견] '{tbl_nm}'({org_id}_{tbl_id})와 같은 조사로"
                f" 판단된 표 {len(siblings) - 1}개: "
                + ", ".join(
                    f"{s.get('TBL_NM')}({s.get('ORG_ID')}_{s.get('TBL_ID')})"
                    for s in siblings[1:]
                )
            )
        return siblings

    # 지역(시도)별 분류축을 찾기 위한 프로브 이름 - 이 중 하나라도 걸리는
    # 축을 "지역별" 축으로 본다.
    # [2026-08-10 실측 수정] 서울만 프로브하던 예전 버전은 실제 KOSIS
    # 표(농업소득/농가부채 "9도" - INH_1EA1501/INH_1EA1611)로 검증하다가
    # 실패로 드러났다: 농가경제조사는 특별시/광역시가 표본에 없는 9개
    # 도만 다뤄서 "서울특별시"/"서울" 둘 다 안 걸리고, 지역축이 분명히
    # 있는데도 _find_region_axis_and_codes가 (None, [])을 반환해 버렸다
    # (전국 집계 폴백이 조용히 완전 실패). 17개 시도의 정식/약칭 표기를
    # 모두 프로브 후보로 넣어, 특별시·광역시가 빠진 표에서도 도 단위
    # 표기 하나는 걸리게 한다.
    _REGION_PROBE_NAMES = (
        "서울특별시", "서울",
        "부산광역시", "부산",
        "대구광역시", "대구",
        "인천광역시", "인천",
        "광주광역시", "광주",
        "대전광역시", "대전",
        "울산광역시", "울산",
        "세종특별자치시", "세종",
        "경기도", "경기",
        "강원특별자치도", "강원도", "강원",
        "충청북도", "충북",
        "충청남도", "충남",
        "전북특별자치도", "전라북도", "전북",
        "전라남도", "전남",
        "경상북도", "경북",
        "경상남도", "경남",
        "제주특별자치도", "제주도", "제주",
    )
    # [2026-08-10 실측 추가] "평균"도 집계 라벨에 추가 - 농업소득(9도)
    # 표의 집계 행 이름이 "전국"이 아니라 "평균"이었다(ITM_ID="000").
    # resolve_category_hint_axis("전국")는 정확 일치/부분 일치 모두 이
    # 라벨을 못 찾으므로(의도적으로 "전국"과 동등 취급하지 않음 - 어떤
    # 표의 "평균"이 실제 전국값과 일치하는지 확인 없이 함부로 단정하지
    # 않는다, Decision 003), 이 표는 여전히 3번(자체 파생) 경로로 간다.
    # 다만 "평균"을 이 목록에 넣지 않으면 _find_region_axis_and_codes가
    # 그 행을 10번째 "지역"으로 착각해 자기 자신을 포함해 평균내는
    # 이중 오염이 생긴다 - 그것만 막는 목적이다.
    _NATIONAL_AGGREGATE_LABELS = ("전국", "계", "합계", "소계", "전체", "평균")

    def _find_region_axis_and_codes(
        self, org_id: str, tbl_id: str
    ) -> "tuple[Optional[int], Optional[Dict[str, Any]], List[Dict[str, Any]]]":
        """[2.3절] 지역(시도)별 분류축 번호, 그 축 안에 이미 있는 집계 행
        (있으면), 그리고 개별 지역 leaf 행 목록을 찾는다. "서울(특별시)"류
        프로브로 축과 부모 그룹을 먼저 확인한 뒤, 같은 부모 밑의 형제
        행을 전부 모은다 - 그중 이름이 _NATIONAL_AGGREGATE_LABELS(전국/
        계/합계/소계/전체/평균)에 속하는 행은 "이미 집계된 행"으로 따로
        분리한다(자체 파생 계산용 지역 목록에는 안 섞는다 - 자기 자신을
        포함해 평균내는 이중 오염 방지).

        [2026-08-10 수정 1] 예전엔 집계 행을 찾자마자 그냥 버렸다(호출부가
        "전국"이라는 정확한 이름으로만 집계 여부를 따로 확인했기 때문).
        하지만 실측(농업소득 9도 - INH_1EA1501)으로 드러났듯, 집계 행이
        "전국"이 아니라 "평균" 같은 다른 이름으로 존재하는 표가 실제로
        있다 - 이런 표에서는 이미 KOSIS가 발표한 진짜 집계값이 있는데도
        비가중 자체 평균으로 대체해 버리면 오히려 덜 정확한 값을 쓰게
        된다. 그래서 이제 이 함수가 집계 행 자체를 찾으면 함께 반환해서,
        호출부가 "정말로 집계 행이 하나도 없을 때만" 자체 파생으로 넘어
        가게 한다.

        [2026-08-10 수정 2 - 더 근본적인 버그] 예전엔 "probe_row의 부모
        (없으면 probe_row 자신)를 root로 잡고, UP_ITM_ID가 정확히 그
        root와 같은 행만" 형제로 모았다. 실제 KOSIS 응답 두 개를 직접
        시뮬레이션해보니 둘 다 이 방식으로 깨졌다:
          - 농업소득(9도, INH_1EA1501): 지역 행에 UP_ITM_ID가 아예 없는
            완전 평평한(flat) 구조라, root=probe_row 자신이 되고, 그
            자신을 UP_ITM_ID로 가진 행은 하나도 없어서 결과가 통째로
            빈 리스트가 됐다(지역축을 찾고도 지역이 0개로 나옴).
          - 가구소득(지역별, INH_1HD_AAA01_01): "수도권"/"비수도권" 두
            상위 그룹 밑에 각각 시도가 딸린 2단 구조라, root가 그중
            먼저 매칭된 부모(예: 수도권) 하나로 고정되면서 그 부모의
            자식(서울/인천/경기)만 모이고 다른 그룹(비수도권 산하
            부산/대구/...)은 통째로 누락됐다 - 조용히 절반 넘는 지역을
            빼먹은 채 "성공"으로 보고할 뻔했다.
        고쳐서 이제는 "같은 축(axis_sn) 안에서, 다른 행의 UP_ITM_ID로
        참조되지 않는 행"을 leaf로 본다 - 참조되는 행(수도권/비수도권
        같은 중간 그룹 헤더)은 자동으로 제외되고, flat 구조(아무도 아무
        행도 참조 안 함)에서는 축의 모든 행이 그대로 leaf가 된다. 두
        실측 구조 모두 이 규칙 하나로 올바르게 처리됨을 시뮬레이션으로
        확인했다(실제 KOSIS 키가 없어도 재현 가능한 순수 로직 검증).
        """
        raw_list = self.kosis.get_itm_meta_list(org_id, tbl_id)
        _, category_rows = self._split_meta_rows(raw_list)

        probe_row = None
        for name in self._REGION_PROBE_NAMES:
            matches = self._match_phrase_in_rows(category_rows, name)
            if matches:
                probe_row = matches[0]
                break
        if not probe_row:
            return None, None, []

        axis_sn = probe_row.get("OBJ_ID_SN")
        try:
            axis = int(axis_sn)
        except (TypeError, ValueError):
            return None, None, []

        axis_rows = [r for r in category_rows if r.get("OBJ_ID_SN") == axis_sn]
        parent_ids = {r.get("UP_ITM_ID") for r in axis_rows if r.get("UP_ITM_ID")}
        leaf_rows = [r for r in axis_rows if self._row_id(r) not in parent_ids]

        aggregate_row = next(
            (r for r in leaf_rows if self._row_name(r) in self._NATIONAL_AGGREGATE_LABELS),
            None,
        )
        region_rows = [
            r for r in leaf_rows if self._row_name(r) not in self._NATIONAL_AGGREGATE_LABELS
        ]
        return axis, aggregate_row, region_rows

    def resolve_national_value_or_derive(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        itm_id: str,
        itm_nm: Optional[str],
        indicator: str,
        period: str,
        prd_se: str = "Y",
        sibling_tables: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """[종합 프로젝트 - 2.3절] claim이 전국 단위 값을 요구하는데,
        확정된 표에는 시도별 자료만 있고 전국 집계가 없을 수 있다(예:
        "전국 평균 임금"인데 표엔 17개 시도 값만 있는 경우). 순서대로
        시도한다:

        1) 확정된 표 자체에 "전국"이 지역 분류축 값으로 이미 있는지
           확인한다(resolve_category_hint_axis) - 있으면 그 축 코드로
           그냥 조회한다. 새 계산이 필요 없는, 가장 흔하고 안전한 경우.
        2) 없으면 형제 표(find_sibling_tables, 2.4절) 중에 "전국"이 있는
           표가 있는지 확인한다.
        3) [2026-08-10 추가] 그래도 없으면, 지역 분류축 자체를 찾아
           그 축 안에 "전국"은 아니지만 다른 이름의 집계 행(계/합계/
           소계/전체/평균 등, _NATIONAL_AGGREGATE_LABELS)이 있는지
           확인한다 - 있으면 그 행을 그대로 쓴다(자체 계산 없음). 실측
           (농업소득 9도, INH_1EA1501)에서 실제로 나온 경우: 집계 행
           이름이 "전국"이 아니라 "평균"이었다. 이런 행도 KOSIS가 이미
           발표한 진짜 집계값이므로, 자체 계산한 비가중 평균보다
           신뢰도가 높다 - "확실하지 않으면 추측하지 않는다"(Decision
           003)는 원칙상 이미 존재하는 공식값을 두고 우리가 새로
           근사치를 만들어낼 이유가 없다.
        4) 그래도 집계 행이 전혀 없으면(진짜로 지역별 값만 있는 경우)
           지역별 값을 전부 가져와 단순 산술평균(비가중)으로 자체
           집계한다 - derivation_used=True와 함께, 계산식을 포함한
           disclosure 문구를 derivation_note에 담아 반환한다.
           judgment.py의 SearchLog.derivation_note가 이 문구를 그대로
           판정 설명에 반영하도록 이미 설계돼 있다(README 2.3절).

        [알려진 한계 - README 2.3절에 이미 명시] 4번 경로의 단순평균은
        임금/물가처럼 지역별 가중치가 실제로 다른 지표에서는 KOSIS 공식
        전국값과 어긋날 수 있다. 그래서 계산식을 항상 함께 노출해
        "이 값은 근사치"라는 걸 투명하게 알린다 - 판정 단계가 이 값을
        일반 VERIFIED와 같은 확신도로 다루지 않도록 하는 안전장치다.
        3번 경로(다른 이름의 공식 집계 행 사용)는 이 한계가 적용되지
        않는다 - 우리가 계산한 값이 아니라 KOSIS가 직접 발표한 값이다.

        [2026-08-10 추가] sibling_tables: 호출부(예: 형제 표 disambiguation
        단계, _expand_clean_matches_with_siblings)가 이 표의 find_sibling_
        tables 결과를 이미 갖고 있으면 그대로 넘겨받아 같은 KOSIS 검색을
        중복하지 않는다(find_sibling_tables와 동일하게 자기 자신이 맨 앞에
        포함된 형태를 기대한다). 안 넘기면(기존 호출부와 하위호환) 예전처럼
        여기서 직접 조회한다.
        """
        national = self.resolve_category_hint_axis(org_id, tbl_id, "전국")
        use_org, use_tbl, use_tbl_nm = org_id, tbl_id, tbl_nm
        use_itm_id, use_itm_nm = itm_id, itm_nm

        if not national:
            siblings = (
                sibling_tables
                if sibling_tables is not None
                else self.find_sibling_tables(org_id, tbl_id, tbl_nm)
            )
            for sib in siblings[1:]:
                s_org, s_tbl, s_nm = (
                    sib.get("ORG_ID"),
                    sib.get("TBL_ID"),
                    sib.get("TBL_NM"),
                )
                if not s_org or not s_tbl:
                    continue
                cand_national = self.resolve_category_hint_axis(s_org, s_tbl, "전국")
                if not cand_national:
                    continue
                item_info = self.resolve_target_item(s_org, s_tbl, indicator)
                if not item_info.get("matched") or item_info.get("candidates"):
                    continue
                national = cand_national
                use_org, use_tbl, use_tbl_nm = s_org, s_tbl, s_nm
                use_itm_id = item_info["itm_id"]
                use_itm_nm = item_info.get("itm_nm")
                logger.info(
                    f"  └─ [전국 집계 - 형제 표에서 발견] '{tbl_nm}'엔 없지만"
                    f" 형제 표 '{s_nm}'에 전국 집계가 있어 그 표로 전환합니다."
                )
                break

        if national:
            fetch_res = self.fetch_kosis_data_range(
                org_id=use_org,
                tbl_id=use_tbl,
                tbl_nm=use_tbl_nm,
                itm_id=use_itm_id,
                itm_nm=use_itm_nm,
                indicator=indicator,
                start_period=period,
                end_period=period,
                prd_se=prd_se,
                extra_obj_axes={national["obj_axis"]: national["obj_code"]},
            )
            if fetch_res.get("success"):
                fetch_res["derivation_used"] = False
                fetch_res["derivation_note"] = None
            return fetch_res

        # 3)/4) 원래 확정 표 기준으로 지역 분류축을 찾는다(형제 표에서도
        # 전국을 못 찾았으므로). 같은 호출로 "전국"은 아니지만 다른
        # 이름의 집계 행이 이미 있는지도 함께 확인한다.
        axis, aggregate_row, region_rows = self._find_region_axis_and_codes(
            org_id, tbl_id
        )
        if not axis:
            return {
                "success": False,
                "message": "전국 집계값도, 지역별 분류축도 확인하지 못했습니다.",
            }

        if aggregate_row:
            agg_code = self._row_id(aggregate_row)
            agg_name = self._row_name(aggregate_row)
            if agg_code:
                logger.info(
                    f"  └─ [전국 집계 - 다른 이름으로 발견] '{tbl_nm}' 표에"
                    f" '전국'은 없지만 같은 지역축에 '{agg_name}' 집계 행이"
                    " 있어 자체 계산 없이 그 값을 그대로 씁니다."
                )
                fetch_res = self.fetch_kosis_data_range(
                    org_id=org_id,
                    tbl_id=tbl_id,
                    tbl_nm=tbl_nm,
                    itm_id=itm_id,
                    itm_nm=itm_nm,
                    indicator=indicator,
                    start_period=period,
                    end_period=period,
                    prd_se=prd_se,
                    extra_obj_axes={axis: agg_code},
                )
                if fetch_res.get("success"):
                    fetch_res["derivation_used"] = False
                    fetch_res["derivation_note"] = (
                        f"'{tbl_nm}' 표는 집계 행 이름이 \"전국\"이 아니라"
                        f" \"{agg_name}\"이지만, KOSIS가 직접 발표한 공식"
                        " 집계값이므로 그대로 사용했습니다(자체 계산 아님)."
                    )
                    return fetch_res
                # 집계 행은 찾았는데 실제 조회가 실패하면(예: 해당 시점
                # 데이터 없음) 아래 자체 파생으로 계속 진행한다 - 조용히
                # 포기하지 않는다.

        if not region_rows:
            return {
                "success": False,
                "message": "전국 집계값도, 지역별 분류축도 확인하지 못했습니다.",
            }

        values: List[float] = []
        used_regions: List[str] = []
        for row in region_rows:
            code = self._row_id(row)
            name = self._row_name(row)
            if not code:
                continue
            fetch_res = self.fetch_kosis_data_range(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                itm_id=itm_id,
                itm_nm=itm_nm,
                indicator=indicator,
                start_period=period,
                end_period=period,
                prd_se=prd_se,
                extra_obj_axes={axis: code},
            )
            record = (fetch_res or {}).get("yearly_records", {}).get(str(period))
            value = record.get("value") if isinstance(record, dict) else None
            if isinstance(value, (int, float)):
                values.append(float(value))
                used_regions.append(name)

        if not values:
            return {
                "success": False,
                "message": "지역별 값을 하나도 조회하지 못해 전국 값을 계산할 수 없습니다.",
            }

        avg = sum(values) / len(values)
        formula = f"({' + '.join(f'{v:g}' for v in values)}) / {len(values)} = {avg:g}"
        note = (
            f"이 값은 KOSIS가 공식 제공하는 전국 집계가 아니라, '{tbl_nm}' 표의"
            f" 지역별(시도) {len(values)}개 값({', '.join(used_regions)})을"
            f" 단순 산술평균(비가중)으로 재가공한 값입니다. 계산식: {formula}"
            " (지역별 가중치를 반영하지 않은 근사치이므로, 공식 전국값과"
            " 다를 수 있습니다.)"
        )
        return {
            "success": True,
            "orgId": org_id,
            "tblId": tbl_id,
            "tblNm": tbl_nm,
            "value": avg,
            "derivation_used": True,
            "derivation_note": note,
        }

    def _find_alternate_table_match(
        self,
        indicator: str,
        unmatched_claims: List[Dict[str, Any]],
        category_hint: Optional[str],
        exclude_table: "tuple[Optional[str], Optional[str]]",
    ) -> Optional[Dict[str, Any]]:
        """[2026-07 추가/동명이표 대응] 검증된 힌트 표에서 값이 안 맞았을
        때, "이 표 하나만 확인해서 안 맞으니 틀렸다"고 바로 결론 내리는
        대신 - 같은 개념으로 검색되는 다른 통계표(기관이 다르거나, 같은
        기관이라도 조사방법이 다른 표 - 예: 고령인구비율의 주민등록인구
        현황 기준 vs 인구총조사 기준)에 실제로 그 수치가 있는지 넓혀서
        확인한다.

        사용자가 던진 질문 자체가 "KOSIS의 실제 데이터를 썼는가"이지
        "우리가 하드코딩해둔 그 표와 일치하는가"가 아니므로, 힌트 표
        하나만 보고 판정하는 건 과하게 엄격하다는 게 이번 세션에서 나온
        결론이다.

        시점이 없는(period=None) 주장은 애초에 어떤 표에서도 특정 값과
        비교할 수 없으므로 여기서도 건너뛴다. 우연히 숫자 하나만 비슷한
        표를 "동명이표"로 오인하지 않도록, 넘겨받은 unmatched_claims
        전부가 같은 후보 표에서 동시에 맞아야만(다중 주장 동시 대조)
        채택한다 - claim이 1개뿐이면 그 1개만 맞으면 된다.

        반환: {"org_id","tbl_id","tbl_nm","item_info","score"} 또는
        일치하는 대체 표를 못 찾으면 None.
        """
        anchored = [c for c in unmatched_claims if c.get("period")]
        if not anchored:
            return None

        exclude_org, exclude_tbl = exclude_table
        raw_candidates = self._broad_search_table_candidates(indicator)
        raw_candidates = [
            c
            for c in raw_candidates
            if (c.get("ORG_ID"), c.get("TBL_ID")) != (exclude_org, exclude_tbl)
        ]
        if not raw_candidates:
            return None

        verified = self.verify_table_candidates_by_meta(indicator, raw_candidates)
        verified = [
            c
            for c in verified
            if (c.get("ORG_ID"), c.get("TBL_ID")) != (exclude_org, exclude_tbl)
        ]
        if not verified:
            return None

        for cand in verified:
            org_id = cand.get("ORG_ID")
            tbl_id = cand.get("TBL_ID")
            tbl_nm = cand.get("TBL_NM")
            item_info = self.resolve_target_item(org_id, tbl_id, indicator)
            if not item_info.get("matched") or item_info.get("candidates"):
                continue
            score = self.score_candidate_against_claims(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                indicator=indicator,
                item_info=item_info,
                claims=anchored,
                category_hint=category_hint,
            )
            if score["total"] > 0 and score["matched"] == score["total"]:
                logger.info(
                    f"  └─ [동명이표 재검색 - 대체 표 발견] '{indicator}'"
                    f" 기본 힌트 표 대신 [{org_id}_{tbl_id}] '{tbl_nm}'에서"
                    f" 나머지 {score['matched']}개 주장이 일치했습니다."
                )
                return {
                    "org_id": org_id,
                    "tbl_id": tbl_id,
                    "tbl_nm": tbl_nm,
                    "item_info": item_info,
                    "score": score,
                }

        return None

    def _resolve_table_for_claims(
        self,
        indicator: str,
        claims: List[Dict[str, Any]],
        category_hint: Optional[str],
    ) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
        """지표명으로 통계표 하나를 자동으로 확정한다 (자동화 모드 전용).

        process_turn의 표 후보 탐색/랭킹/실메타 검증/다중 주장 대조
        로직(이번 세션에서 만든 부품들)을 그대로 재사용하되, 사용자에게
        되묻는 대신 확신할 수 없으면 곧바로 실패(None)를 반환한다.

        반환: (table_candidate_dict 또는 None, 실패 사유 또는 None)
        table_candidate_dict는 ORG_ID/TBL_ID/TBL_NM 키를 쓰는 원본
        candidate 형태(search_table_candidates가 주는 형태)로 통일한다.
        """
        has_hint = self.resolve_hint_key(indicator) is not None
        if has_hint:
            table_info = self.resolve_target_table(indicator, claims=claims)
            if not table_info:
                return None, "지표에 대한 통계표를 찾지 못했습니다."
            return {
                "ORG_ID": table_info["org_id"],
                "TBL_ID": table_info["tbl_id"],
                "TBL_NM": table_info["tbl_nm"],
                "STRT_PRD_DE": table_info.get("period_start"),
                "END_PRD_DE": table_info.get("period_end"),
            }, None

        raw_candidates = self._broad_search_table_candidates(indicator)

        if not raw_candidates:
            return None, "관련 통계표를 찾지 못했습니다."

        if len(raw_candidates) > 1:
            raw_candidates = self._llm_rank_table_candidates(
                indicator, raw_candidates
            )

        if len(raw_candidates) == 1:
            return raw_candidates[0], None

        # 후보가 여럿이면 실제 메타로 검증해 컬럼이 진짜 존재하는 표만
        # 추린다 (verify_table_candidates_by_meta - Task #23과 동일 원리).
        verified = self.verify_table_candidates_by_meta(indicator, raw_candidates)
        if len(verified) == 1:
            return verified[0], None

        if not verified:
            return None, "표 후보들에서 관련 컬럼을 확인하지 못했습니다."

        # 검증 통과 표가 여럿이면, 시점이 명시된 주장이 2개 이상일 때
        # 다중 주장 대조로 자동으로 좁혀본다 - score_candidate_against_claims
        # (Task #26/#27과 동일 원리).
        anchored = [c for c in claims if c.get("period")]
        if len(anchored) >= 2:
            scored = []
            for cand in verified:
                item_info = self.resolve_target_item(
                    cand.get("ORG_ID"), cand.get("TBL_ID"), indicator
                )
                if not item_info.get("matched") or item_info.get("candidates"):
                    continue
                score = self.score_candidate_against_claims(
                    org_id=cand.get("ORG_ID"),
                    tbl_id=cand.get("TBL_ID"),
                    tbl_nm=cand.get("TBL_NM"),
                    indicator=indicator,
                    item_info=item_info,
                    claims=claims,
                    category_hint=category_hint,
                )
                scored.append((score, cand))
            scored.sort(key=lambda s: s[0]["matched"], reverse=True)
            if (
                scored
                and scored[0][0]["matched"] >= 2
                and (
                    len(scored) == 1
                    or scored[0][0]["matched"] > scored[1][0]["matched"]
                )
            ):
                return scored[0][1], None

        return None, "표 후보가 여러 개라 확신할 수 있는 하나를 고르지 못했습니다."

    # [2026-08-10 개편 - 형제 표(원지수/계절조정 등) 대응 + 비용 절감]
    # 표제목 계열 접미사와 같은 어휘가 claim 원문에 이미 적혀 있으면, 그
    # 단어로 형제 표 중 하나를 추측 없이 결정론적으로 고른다.
    _SERIES_QUALIFIER_WORDS = (
        "원지수", "계절조정지수", "계절조정", "추세변동치", "추세변동",
        "추세치", "추세", "원계열", "불변", "경상",
    )

    def _detect_series_qualifier(
        self, claims: List[Dict[str, Any]]
    ) -> Optional[str]:
        """claim 원문(들)에 계열 구분 어휘가 명시돼 있으면 그 단어를 그대로
        반환한다(추측이 아니라 claim이 이미 갖고 있는 정보를 읽는 것). 여러
        claim에 서로 다른 어휘가 섞여 있는 경우(이례적)는 첫 발견만 쓴다."""
        for c in claims:
            text = c.get("claim") or c.get("raw_sentence") or ""
            for word in self._SERIES_QUALIFIER_WORDS:
                if word in text:
                    return word
        return None

    def _keyword_clean_match_candidates(
        self, candidates: List[Dict[str, Any]], keywords: List[str]
    ) -> List[Dict[str, Any]]:
        """[2026-08-10 개편] 후보 표 각각에 resolve_keyword_group_in_table을
        직접 돌려 실제 컬럼/축이 있는지 확인한다 - KOSIS 메타 조회 + 로컬
        문자열 매칭뿐이라 HCX 호출이 전혀 없다. verify_table_candidates_
        by_meta(resolve_target_item 경유, 힌트 없는 지표는 LLM 호출)보다
        먼저 시도해서, 대부분의 경우 여기서 이미 끝나도록 한다.

        반환: [{"candidate": raw_candidate_dict, "item_info": ...}, ...]
        item_info는 resolve_keyword_group_in_table의 결과 그대로라(obj_axes
        까지 이미 확정) 뒤 단계에서 다시 조회할 필요가 없다.
        """
        clean: List[Dict[str, Any]] = []
        for cand in candidates:
            org_id = cand.get("ORG_ID")
            tbl_id = cand.get("TBL_ID")
            if not org_id or not tbl_id:
                continue
            item_info, _fail_reason = self._resolve_item_for_claim_keywords(
                org_id, tbl_id, keywords
            )
            if item_info is not None:
                clean.append({"candidate": cand, "item_info": item_info})
        return clean

    def _expand_clean_matches_with_siblings(
        self,
        clean_matches: List[Dict[str, Any]],
        keywords: List[str],
    ) -> "tuple[List[Dict[str, Any]], Dict[tuple, List[Dict[str, Any]]]]":
        """[2026-08-10 개편, 2.4절 연계] 이미 keyword로 clean하게 resolve된
        후보들 각각에 대해서만 find_sibling_tables로 형제 표(원지수/계절
        조정 등)를 찾아본다 - raw_candidates 전체가 아니라 "이미 유망하다고
        확인된" 후보 주변만 넓히므로 검색 호출이 무한정 늘지 않는다(HCX는
        여전히 0회, KOSIS 검색만 후보 수만큼 추가).

        원래 검색에서 형제 표가 누락될 수 있는 이유(2026-08-05 실측):
        "...(원지수)"/"...(계절조정지수)"는 표제목 문자열 자체가 달라
        키워드 검색이 둘 중 하나만 상위로 올리고 나머지는 후보에서 빠질 수
        있다.

        반환: (확장된 clean_matches, {(org_id,tbl_id): find_sibling_tables
        원본 반환값}) - 두 번째 값은 나중에 resolve_national_value_or_derive
        가 같은 검색을 중복하지 않도록 그대로 넘겨줄 수 있는 형태다(호출부가
        필요할 때 재사용 - 이번 개편은 만들어서 반환해두는 것까지).
        """
        expanded = list(clean_matches)
        seen_ids = {
            (m["candidate"].get("ORG_ID"), m["candidate"].get("TBL_ID"))
            for m in clean_matches
        }
        sibling_lists: Dict[tuple, List[Dict[str, Any]]] = {}

        for m in list(clean_matches):
            cand = m["candidate"]
            org_id, tbl_id, tbl_nm = (
                cand.get("ORG_ID"), cand.get("TBL_ID"), cand.get("TBL_NM"),
            )
            if not org_id or not tbl_id:
                continue
            siblings = self.find_sibling_tables(org_id, tbl_id, tbl_nm)
            sibling_lists[(org_id, tbl_id)] = siblings
            for sib in siblings:
                key = (sib.get("ORG_ID"), sib.get("TBL_ID"))
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                sib_item_info, _fail_reason = self._resolve_item_for_claim_keywords(
                    sib.get("ORG_ID"), sib.get("TBL_ID"), keywords
                )
                if sib_item_info is not None:
                    expanded.append({"candidate": sib, "item_info": sib_item_info})

        return expanded, sibling_lists

    def _score_keyword_group_candidate_against_claims(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        item_info: Dict[str, Any],
        keywords: List[str],
        claims: List[Dict[str, Any]],
    ) -> int:
        """[2026-08-10 개편] score_candidate_against_claims와 목적은 같다
        (여러 claim 수치와 동시에 맞는 후보 찾기)but item_info가
        resolve_keyword_group_in_table 형태(obj_axes 딕셔너리)라 그 함수를
        그대로 재사용할 수 없어 따로 둔다. 시점이 있는 claim만 대상으로,
        그 시점의 실제 값이 claim 값과 일치하는 개수를 센다
        (_claim_value_matches 재사용 - 소수 자릿수 기준 엄격 허용오차)."""
        indicator_concept = " ".join(k for k in keywords if k) or tbl_nm
        matched = 0
        for c in claims:
            period = str(c.get("period") or "")
            if not period or c.get("value") is None:
                continue
            prd_se = self._period_to_prd_se(period)
            fetch_res = self.fetch_kosis_data_range(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                itm_id=item_info.get("itm_id", "all"),
                itm_nm=item_info.get("itm_nm"),
                indicator=indicator_concept,
                start_period=period,
                end_period=period,
                prd_se=prd_se,
                extra_obj_axes=item_info.get("obj_axes") or None,
                extra_obj_axes_fallback=item_info.get("obj_axes_fallback") or None,
            )
            if not fetch_res.get("success"):
                continue
            rec = (fetch_res.get("yearly_records") or {}).get(period)
            if not isinstance(rec, dict) or rec.get("value") is None:
                continue
            m = re.search(r"([-–]?[\d,]+(?:\.\d+)?)", str(rec.get("value")))
            if not m:
                continue
            value_num = float(m.group(1).replace(",", "").replace("–", "-"))
            if self._claim_value_matches(
                value_num, c["value"], c.get("raw_text", ""), c.get("precision")
            ):
                matched += 1
        return matched

    def _disambiguate_table_candidates(
        self,
        clean_matches: List[Dict[str, Any]],
        claims: List[Dict[str, Any]],
        keywords: List[str],
    ) -> "tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]":
        """[2026-08-10 개편] clean_matches(이미 키워드로 실제 컬럼이 확인된
        후보들, 형제 표 확장 포함)가 여럿이면 하나로 좁힌다. 추측이 아니라
        (1) claim 원문에 이미 적힌 계열 명시어, (2) claim에 이미 적힌
        숫자와 실제 조회값의 일치, 이 두 가지 "이미 갖고 있는 근거"만
        순서대로 쓴다. 이 함수 자체는 HCX를 호출하지 않는다 - 둘 다 안
        되면 하나를 임의로 찍지 않고 모호함을 그대로 반환해서, 호출부가
        (원한다면) 좁혀진 후보만 갖고 마지막 수단으로 LLM 랭킹을 쓸지
        판단하게 한다(Decision 003: 확실하지 않으면 추측하지 않는다).

        반환: (table_dict, item_info, fail_reason). 성공 시 fail_reason=None,
        실패 시 앞 두 값이 None이고 fail_reason에 남은 후보 이름을 담는다.
        """
        if len(clean_matches) == 1:
            m = clean_matches[0]
            return m["candidate"], m["item_info"], None

        qualifier = self._detect_series_qualifier(claims)
        if qualifier:
            narrowed = [
                m for m in clean_matches
                if qualifier in (m["candidate"].get("TBL_NM") or "")
            ]
            if len(narrowed) == 1:
                logger.info(
                    "  └─ [형제 표 확정 - claim 원문의 계열 명시어"
                    f" '{qualifier}'] '{narrowed[0]['candidate'].get('TBL_NM')}'"
                    " 채택"
                )
                m = narrowed[0]
                return m["candidate"], m["item_info"], None
            if narrowed:
                clean_matches = narrowed  # 좁혀졌지만 아직 여럿 - 값 대조로 계속

        anchored = [
            c for c in claims if c.get("period") and c.get("value") is not None
        ]
        if anchored:
            scored = []
            for m in clean_matches:
                cand = m["candidate"]
                score = self._score_keyword_group_candidate_against_claims(
                    cand.get("ORG_ID"), cand.get("TBL_ID"), cand.get("TBL_NM"),
                    m["item_info"], keywords, anchored,
                )
                scored.append((score, m))
            scored.sort(key=lambda s: s[0], reverse=True)
            if scored[0][0] > 0 and (
                len(scored) == 1 or scored[0][0] > scored[1][0]
            ):
                logger.info(
                    "  └─ [형제 표 확정 - 실값 대조]"
                    f" '{scored[0][1]['candidate'].get('TBL_NM')}'가 claim"
                    f" 수치와 {scored[0][0]}개 일치"
                )
                m = scored[0][1]
                return m["candidate"], m["item_info"], None

        names = ", ".join(m["candidate"].get("TBL_NM", "") for m in clean_matches)
        return None, None, (
            f"컬럼은 확인됐지만 같은 개념의 표가 여러 개({names})라 claim"
            " 원문이나 실값만으로는 하나로 확정하지 못했습니다."
        )

    def _llm_disambiguate_sibling_candidates(
        self, indicator_concept: str, clean_matches: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """[2026-08-10 개편 - 마지막 수단] claim 원문/실값으로도 못 좁힌
        형제 표 후보에 한해서만, 좁혀진 목록을 그대로 LLM에게 보여주고
        직접 골라달라고 한다.

        기존 _llm_rank_table_candidates(범용 랭킹)를 그대로 재사용하지
        않는 이유: 그 함수는 "관련 없는 잡음 후보를 걸러내는" 용도라 LLM
        응답이 비어 있으면(ranked_numbers=[]) 원래 순서를 그대로 돌려주는
        관대한 폴백을 쓴다. 여기서는 이미 전부 실제로 컬럼이 존재하는
        후보들(clean_matches)이라 "빈 응답 = 판단 못함"과 "1번이 맞음"을
        구분해야 한다 - 구분 안 하면 LLM이 아무 정보도 안 줬는데 1번을
        확신에 차서 채택하는 꼴이 되어 Decision 003(확실하지 않으면
        추측하지 않는다)에 어긋난다. 그래서 이 메서드는 ranked_numbers가
        실제로 비어있지 않을 때만 그 1번째를 채택하고, 비어있거나 호출
        자체가 실패하면 None을 반환해 호출부가 "여전히 모호함"으로
        처리하게 한다.
        """
        candidates = [m["candidate"] for m in clean_matches]
        options_text = "\n".join(
            f"{i + 1}. {c.get('TBL_NM', '')}" for i, c in enumerate(candidates)
        )
        system_instruction = (
            "당신은 국가통계포털(KOSIS) 통계표 후보 중 사용자가 찾는 개념과"
            " 가장 관련 있는 표를 고르는 역할입니다. 아래 후보는 전부 실제로"
            f" '{indicator_concept}' 컬럼이 존재하는 표입니다(제목만 다름 -"
            " 예: 같은 지표의 원지수/계절조정 등 계열 차이일 수 있습니다).\n\n"
            f"{options_text}\n\n"
            "사용자가 찾는 개념에 가장 부합하는 표 하나만 번호로 답하세요."
            " 표 제목만으로는 어느 쪽인지 전혀 판단할 근거가 없으면 억지로"
            ' 고르지 말고 빈 배열로 답하세요. {"ranked_numbers": [번호]}'
            ' 또는 {"ranked_numbers": []} 형태의 순수 JSON으로만 응답하세요.'
        )
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"찾는 개념: \"{indicator_concept}\""},
        ]
        try:
            raw = self.hcx.generate_completion(messages, temperature=0.0)
            clean = re.sub(r"```json|```", "", raw).strip()
            parsed = json.loads(clean)
            numbers = parsed.get("ranked_numbers", [])
            if not numbers:
                logger.info("  └─ [형제 표 LLM 최종 판단 - 근거 없음] 채택하지 않음")
                return None
            idx = int(numbers[0]) - 1
            if 0 <= idx < len(clean_matches):
                return clean_matches[idx]
            return None
        except Exception as e:
            logger.warning(f"⚠️ [형제 표 LLM 최종 판단 예외 - 모호함 유지]: {e}")
            return None

    def _resolve_table_for_claim_keywords(
        self,
        keywords: List[str],
        claims: List[Dict[str, Any]],
        category_hint: Optional[str],
    ) -> "tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]]":
        """[종합 프로젝트 - 검색 단계 확장] _resolve_table_for_claims와
        비슷한 역할이되, 지표명 문자열 하나(indicator: str) 대신 키워드
        그룹(keywords: List[str])을 받는다. 기존 _resolve_table_for_claims는
        건드리지 않았다 - 힌트가 있는 지표는 여전히 그 함수를 쓰면 된다.

        [2026-08-10 개편 - 순서를 바꿈] 예전엔 (표 랭킹 LLM 2회) ->
        (후보마다 resolve_target_item으로 메타 검증, 힌트 없는 키워드면
        후보 수만큼 LLM) 순이었다. 이제는 먼저 후보 전체에
        resolve_keyword_group_in_table(HCX 없음, kosis_resolution.py
        2.6절)을 직접 돌려 실제 컬럼이 있는 후보만 추린다. 이유 두 가지:

        (1) 비용 - 순수 KOSIS 메타 조회 + 로컬 문자열 매칭이라 HCX가 0회.
        예전엔 힌트 없는 키워드("재배면적" 등) 후보마다 resolve_target_item
        의 LLM 분기가 돌아 후보 수만큼 비용이 쌓였다.
        (2) 정확도 - verify_table_candidates_by_meta 자체 docstring에 있는
        "청년 실업률" 실측 사례처럼, 제목 기반 랭킹이 먼저 오면 정답 표가
        낮은 순위로 밀려 후보에서 잘릴 위험이 있다. 컬럼 존재 여부를 먼저
        전수 확인하면 이 위험이 없다.

        여러 후보가 clean하게 resolve되면(형제 표 - 원지수/계절조정 등)
        find_sibling_tables로 후보를 넓힌 뒤(_expand_clean_matches_with_
        siblings), claim 원문의 계열 명시어 -> claim 수치와 실값 대조 순으로
        좁힌다(_disambiguate_table_candidates, 둘 다 HCX 없음). 그래도 못
        좁히면 마지막 수단으로만, 이미 좁혀진 후보에 한해 기존 LLM 랭킹을
        돌린다(verify_table_candidates_by_meta 전체 순회보다 훨씬 저렴).

        순수 키워드 패스가 후보를 하나도 못 찾으면(사전에 없는 표현 등
        완전히 새 개념이라 문자열 매칭으로 원천적으로 못 찾는 경우), 기존
        LLM 기반 경로(_llm_rank_table_candidates + verify_table_candidates_
        by_meta, 절삭 없음)로 그대로 폴백한다 - 안전망은 유지.

        반환: (table_candidate_dict 또는 None, item_info 또는 None, 실패
        사유 또는 None). item_info가 함께 나오면 호출부(resolve_and_fetch_
        for_claim_keywords)는 _resolve_item_for_claim_keywords를 다시 부를
        필요가 없다 - 이미 이 단계에서 확정됐다.
        """
        indicator_concept = " ".join(k for k in keywords if k)
        if not indicator_concept:
            return None, None, "검색할 키워드가 없습니다."

        raw_candidates = self._broad_search_table_candidates_for_keywords(keywords)
        if not raw_candidates:
            return None, None, "관련 통계표를 찾지 못했습니다."

        clean_matches = self._keyword_clean_match_candidates(raw_candidates, keywords)

        if clean_matches:
            clean_matches, _sibling_lists = self._expand_clean_matches_with_siblings(
                clean_matches, keywords
            )
            table, item_info, fail_reason = self._disambiguate_table_candidates(
                clean_matches, claims, keywords
            )
            if table:
                return table, item_info, None

            # 순수 키워드 매칭으로 못 좁힌 경우에만, 이미 좁혀진 후보
            # 안에서 마지막 수단으로 LLM에게 직접 물어본다(raw_candidates
            # 전체가 아니라 clean_matches만 - verify_table_candidates_by_meta
            # 전체 순회보다 훨씬 저렴하다). LLM도 근거 없이 못 고르면(빈
            # 응답) 여기서도 추측하지 않고 그대로 모호함 실패를 반환한다.
            if len(clean_matches) > 1:
                top_match = self._llm_disambiguate_sibling_candidates(
                    indicator_concept, clean_matches
                )
                if top_match:
                    logger.info(
                        "  └─ [형제 표 확정 - 마지막 수단 LLM 판단]"
                        f" '{top_match['candidate'].get('TBL_NM')}' 채택"
                    )
                    return top_match["candidate"], top_match["item_info"], None
            return None, None, fail_reason

        # ---- 폴백: 기존 LLM 기반 경로(순수 키워드 매칭이 아무것도 못
        # 찾았을 때만 - 완전히 새 개념 등) ----
        logger.info(
            "  └─ [순수 키워드 매칭 실패 - 기존 LLM 경로로 폴백] 후보"
            f" {len(raw_candidates)}개 중 키워드로 clean하게 resolve된 표가"
            " 하나도 없음"
        )
        ranked_candidates = raw_candidates
        if len(ranked_candidates) > 1:
            ranked_candidates = self._llm_rank_table_candidates(
                indicator_concept, ranked_candidates
            )

        if len(ranked_candidates) == 1:
            return ranked_candidates[0], None, None

        verified = self.verify_table_candidates_by_meta(
            indicator_concept, ranked_candidates, max_tables=len(ranked_candidates)
        )
        if len(verified) == 1:
            return verified[0], None, None

        if not verified:
            return None, None, "표 후보들에서 관련 컬럼을 확인하지 못했습니다."

        anchored = [c for c in claims if c.get("period")]
        if len(anchored) >= 2:
            scored = []
            for cand in verified:
                item_info = self.resolve_target_item(
                    cand.get("ORG_ID"), cand.get("TBL_ID"), indicator_concept
                )
                if not item_info.get("matched") or item_info.get("candidates"):
                    continue
                score = self.score_candidate_against_claims(
                    org_id=cand.get("ORG_ID"),
                    tbl_id=cand.get("TBL_ID"),
                    tbl_nm=cand.get("TBL_NM"),
                    indicator=indicator_concept,
                    item_info=item_info,
                    claims=claims,
                    category_hint=category_hint,
                )
                scored.append((score, cand))
            scored.sort(key=lambda s: s[0]["matched"], reverse=True)
            if (
                scored
                and scored[0][0]["matched"] >= 2
                and (
                    len(scored) == 1
                    or scored[0][0]["matched"] > scored[1][0]["matched"]
                )
            ):
                return scored[0][1], None, None

        return None, None, "표 후보가 여러 개라 확신할 수 있는 하나를 고르지 못했습니다."

    def _resolve_item_for_claim_keywords(
        self,
        org_id: str,
        tbl_id: str,
        keywords: List[str],
    ) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
        """[종합 프로젝트 - 검색 단계 확장] _resolve_item_for_claims와 같은
        역할(표 안에서 컬럼/축 값 확정)을 하되, indicator 문자열 하나 대신
        키워드 그룹을 받는다.

        내부적으로 resolve_keyword_group_in_table(kosis_resolution.py,
        2.6절)을 그대로 쓴다 - phrase별 item/축값 판별, 동명이의 breadcrumb
        좁히기, 넓은 phrase의 subsumption 제거는 전부 거기서 이미 처리되기
        때문에, 이 함수는 그 결과를 기존 파이프라인이 기대하는
        (item_info, 실패사유) 튜플 형태로 감싸주기만 한다.

        resolve_target_item(_resolve_item_for_claims가 쓰는 함수)과 달리
        candidates를 남겨서 되묻지 않는다 - resolve_keyword_group_in_table
        이 이미 breadcrumb/포함관계로 자체적으로 하나를 소거해내려 시도하고,
        그래도 못 정한 phrase는 "unresolved" 목록으로 떨어진다. 이 목록이
        하나라도 있으면 "일부 키워드를 이 표에서 못 찾았다"는 사유와 함께
        실패로 처리한다 - 조용히 무시하고 넘어가면 사용자가 준 키워드 일부를
        버린 채 판정하는 셈이라 위험하다(Decision 003 원칙 - 확실하지 않으면
        추측하지 않는다).
        """
        result = self.resolve_keyword_group_in_table(org_id, tbl_id, keywords)
        if result.get("unresolved"):
            return None, (
                "다음 키워드를 이 표에서 찾지 못했습니다: "
                + ", ".join(result["unresolved"])
            )
        if result.get("itm_id") == "all" and not result.get("obj_axes"):
            return None, "이 표에서 관련 컬럼/분류값을 하나도 확인하지 못했습니다."
        return result, None

    def resolve_and_fetch_for_claim_keywords(
        self,
        keywords: List[str],
        claims: List[Dict[str, Any]],
        category_hint: Optional[Union[str, List[str]]],
        start_period: str,
        end_period: str,
        prd_se: str = "Y",
    ) -> Dict[str, Any]:
        """[종합 프로젝트 - 검색 단계 확장, entrypoint] 키워드 그룹 하나로
        표 확정 -> 컬럼/축 확정 -> 실제 값 조회까지 한 번에 잇는다.

        _resolve_table_for_claim_keywords(표 확정) -> _resolve_item_for_
        claim_keywords(컬럼/축 확정) -> fetch_kosis_data_range(실제 값
        조회) 세 함수를 순서대로 호출하는 얇은 오케스트레이션이다 - 각
        단계 자체의 로직은 이미 검증된 함수에 그대로 있고, 여기서는 실패
        시 각 단계의 사유를 그대로 전달하는 것과 단계 간 데이터 배선만
        담당한다.

        fetch_kosis_data_range는 itm_id를 서버 필터로 신뢰하지 않고
        항상 itmId="all"로 받은 뒤 itm_nm/keywords로 클라이언트단에서
        행을 고르는 방식이라(그 함수 자체의 기존 설계), 여기서 확정한
        obj_axes 딕셔너리만 extra_obj_axes로 그대로 넘기면 된다 -
        obj_axis/obj_code(단일 축)는 안 쓴다. 여러 phrase가 이미 각자
        축을 확정해놨으므로 "주 축 하나 + 나머지 축들"을 구분할 필요가
        없기 때문이다.

        raw_sentence(claim 원문장)는 이 함수도 여전히 쓰지 않는다 - 검색/
        조회는 순수 키워드+시점 기반이고, 원문장은 이 결과를 받아 판정할
        때(judgment.py)만 필요하다. 호출부가 claim 객체에 원문장을 그대로
        들고 있다가 판정 단계에 넘기면 된다. (예외: raw_sentence의 계열
        명시어("원지수"/"계절조정" 등)만은 형제 표 disambiguation에
        _resolve_table_for_claim_keywords 내부에서 참조한다 - claims 딕셔너리
        의 "claim" 필드로 이미 들고 있으므로 이 함수의 시그니처는 안 바뀐다.)

        [2026-08-10 개편] _resolve_table_for_claim_keywords가 순수 키워드
        경로를 탔으면 item_info까지 이미 확정해서 함께 반환한다 - 그 경우
        _resolve_item_for_claim_keywords를 다시 부르지 않는다(같은 메타
        조회를 중복하지 않기 위함). 기존 LLM 폴백 경로를 탄 경우(item_info
        가 None)만 여기서 별도로 컬럼을 확정한다.

        [2026-08-10 추가 - NOT_FOUND/UNRESOLVED 정밀도 손실 수정] 실패
        딕셔너리에 "stage"를 함께 남긴다("table"/"item"/미지정=fetch 단계
        실패) - `_fetch_result_to_evidence`가 이 정보로 query_status를
        "not_found"(후보 표 자체가 없음)/"unresolved"(표는 있었지만 컬럼을
        못 정함)/"error"(표·컬럼은 확정했지만 실제 조회 자체가 실패, 예:
        주기 불일치)로 세분화하는 데 쓴다. 예전엔 이 셋이 전부 "error"
        하나로 뭉개져서, 최종 판정 단계의 설명 문구가 실제 실패 사유와
        다르게 나올 수 있었다(README 7장 "다음에 정할 것" 항목).
        """
        table, item_info, table_fail_reason = self._resolve_table_for_claim_keywords(
            keywords, claims, category_hint
        )
        if not table:
            return {"success": False, "message": table_fail_reason, "stage": "table"}

        org_id = table.get("ORG_ID")
        tbl_id = table.get("TBL_ID")
        tbl_nm = table.get("TBL_NM")

        if item_info is None:
            item_info, item_fail_reason = self._resolve_item_for_claim_keywords(
                org_id, tbl_id, keywords
            )
            if not item_info:
                return {"success": False, "message": item_fail_reason, "stage": "item"}

        indicator_concept = " ".join(k for k in keywords if k)
        fetch_res = self.fetch_kosis_data_range(
            org_id=org_id,
            tbl_id=tbl_id,
            tbl_nm=tbl_nm,
            itm_id=item_info.get("itm_id", "all"),
            itm_nm=item_info.get("itm_nm"),
            indicator=indicator_concept,
            start_period=start_period,
            end_period=end_period,
            prd_se=prd_se,
            category_hint=category_hint,
            extra_obj_axes=item_info.get("obj_axes") or None,
            extra_obj_axes_fallback=item_info.get("obj_axes_fallback") or None,
        )
        if not fetch_res.get("success"):
            fetch_res.setdefault("stage", "fetch")
        return fetch_res

    @staticmethod
    def _period_to_prd_se(period: Optional[str]) -> str:
        """claim의 period 표기 길이로 주기를 결정론적으로 판별한다
        (4자리=연간/Y, 5자리=분기/Q, 6자리=월/M) - _required_periodicity_
        from_claims(kosis_resolution.py)와 같은 원칙: 추측이 아니라 이미
        추출된 표기 형식 자체로 판별한다."""
        length = len(str(period or ""))
        return {4: "Y", 5: "Q", 6: "M"}.get(length, "Y")

    @staticmethod
    def _fetch_result_to_evidence(
        fetch_res: Optional[Dict[str, Any]], period: str
    ) -> Dict[str, Any]:
        """resolve_and_fetch_for_claim_keywords(또는 fetch_kosis_data_range)
        의 반환 형태를 adapter.py가 그대로 파싱할 수 있는 evidence 딕셔너리
        (parse_evidence_and_log가 기대하는 필드명)로 변환한다.

        [2026-08-10 수정] 실패 시 query_status를 "error" 하나로 뭉개지
        않고, resolve_and_fetch_for_claim_keywords가 남긴 "stage"와 실패
        사유 문자열로 세분화한다:
          - stage="table"이고 사유가 "후보 자체를 못 찾음"류면 "not_found"
            (리콜 실패 - 검색에 아무것도 안 걸림)
          - stage="table"(다른 사유)/"item"이면 "unresolved"(표 후보는
            있었지만 컬럼/축을 확정 못함 - 해석 실패)
          - stage="fetch" 또는 stage 미지정(fetch_kosis_data_range 자체
            실패, 예: 주기 불일치·KOSIS err코드)이면 "error"(표·컬럼은
            확정했지만 실제 조회가 실패 - 기술적 실패)
        adapter.py의 상태 매핑(parse_evidence_and_log)이 이 세 값을 각각
        NOT_FOUND/UNRESOLVED/UNRESOLVED로 구분해서 최종 설명 문구에
        반영한다.
        """
        if not fetch_res or not fetch_res.get("success"):
            fetch_res = fetch_res or {}
            stage = fetch_res.get("stage")
            message = fetch_res.get("message") or ""
            if stage == "table" and ("찾지 못했습니다" in message or "키워드가 없습니다" in message):
                status = "not_found"
            elif stage in ("table", "item"):
                status = "unresolved"
            else:
                status = "error"
            return {
                "query_status": status,
                "error_message": fetch_res.get("message"),
            }
        record = (fetch_res.get("yearly_records") or {}).get(str(period))
        if not isinstance(record, dict) or record.get("value") is None:
            return {"query_status": "no_data"}
        return {
            "org_id": fetch_res.get("orgId"),
            "table_id": fetch_res.get("tblId"),
            "table_name": fetch_res.get("tblNm"),
            "normalized_value": record.get("value"),
            "normalized_unit": record.get("unit") or record.get("UNIT_NM"),
            "query_status": "success",
            "derivation": {
                "used": bool(fetch_res.get("derivation_used")),
                "note": fetch_res.get("derivation_note"),
            },
        }

    def process_claim_group_keywords(
        self,
        claims: List[Dict[str, Any]],
        keywords_by_claim_id: Dict[str, List[str]],
        category_hint: Optional[Union[str, List[str]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """[종합 프로젝트 - entrypoint, 2.2~2.5 최종 배선] claim 목록(1번
        Task 출력)과 claim_id별 키워드 그룹을 받아, claim_id별 evidence를
        만든다.

        keywords_by_claim_id는 이 모듈 밖에서 만들어져 들어오는 입력이다
        - 원문장에서 어떻게 키워드를 뽑는지는 이 모듈이 알 필요가 없다
        (README 2.7절/Decision Log 006: 키워드 추출 단계가 KOSIS 축
        구조를 알 필요는 없지만, "검색해볼 phrase 후보"를 주는 역할
        자체는 여전히 이 모듈 밖의 몫이다).

        처리 순서:
        1) route_claim_group(2.5절, adapter.py)로 direct/derived_
           comparison/excluded를 나눈다.
        2) excluded는 evidence 없이 바로 상태만 남긴다(검색 자체를
           시도하지 않음 - 1번 Task가 이미 판단한 결과를 재추측하지
           않는다).
        3) direct는 각각 resolve_and_fetch_for_claim_keywords(표 확정 ->
           컬럼/축 확정 -> 값 조회, 2.2/2.4절이 적용된 경로)로 값을 찾는다.
        4) derived_comparison은 검색을 시도하지 않고, 3번에서 이미 찾은
           소스 claim들의 값을 EvidencePoint 2개로 묶어 is_comparison=True
           evidence를 만든다 - 소스 중 하나라도 값을 못 찾았으면(3번
           단계가 실패했으면) 이 파생값도 "error"로 남긴다(Decision 003:
           확실하지 않으면 추측하지 않는다 - 소스가 불완전한데 억지로
           비교값을 만들어내지 않음).

        반환: {claim_id: evidence_dict, ...} - 각 evidence_dict는
        adapter.py의 parse_evidence_and_log가 그대로 소비할 수 있는 형태.
        이 함수는 evidence를 준비하는 데까지만 책임진다 - 실제 judgment.py
        호출(최종 일치/불일치/판단불가 판정)은 호출부가 이 결과와 claim을
        adapter.build_inputs에 넘겨서 별도로 수행한다.

        [경계 - 2.3절] "전국 집계가 없어 자체 파생 계산이 필요한" 경우
        (resolve_national_value_or_derive)는 여기서 자동으로 트리거하지
        않는다. 어떤 claim이 "전국 단위 값"을 요구하는지를 claim 스키마
        (metric/value/unit/period)만으로 결정론적으로 판별할 방법이
        아직 없어서(추측이 될 위험), 이 경로가 필요하다고 이미 알고 있는
        호출부가 resolve_national_value_or_derive를 직접 불러 쓰는 걸
        전제로 한다 - 잘못 추측해서 자동으로 재가공값을 끼워넣는 것보다
        안전하다.
        """
        routing = route_claim_group(claims)
        results: Dict[str, Dict[str, Any]] = {}

        for c in routing["excluded"]:
            results[c["claim_id"]] = {"query_status": "not_eligible"}

        for c in routing["direct"]:
            keywords = keywords_by_claim_id.get(c["claim_id"], [])
            period = str(c.get("period") or "")
            prd_se = self._period_to_prd_se(period)
            fetch_res = self.resolve_and_fetch_for_claim_keywords(
                keywords,
                claims=[c],
                category_hint=category_hint,
                start_period=period,
                end_period=period,
                prd_se=prd_se,
            )
            results[c["claim_id"]] = self._fetch_result_to_evidence(
                fetch_res, period
            )

        for item in routing["derived_comparison"]:
            c = item["claim"]
            sources = item["sources"]
            points = []
            table_ref: Optional[Dict[str, Any]] = None
            for s in sources:
                s_result = results.get(s["claim_id"])
                if not s_result or s_result.get("query_status") != "success":
                    points = None
                    break
                points.append(
                    {
                        "period": s.get("period"),
                        "value": s_result.get("normalized_value"),
                        "unit": s_result.get("normalized_unit"),
                    }
                )
                if table_ref is None:
                    table_ref = s_result

            if points:
                results[c["claim_id"]] = {
                    "org_id": (table_ref or {}).get("org_id"),
                    "table_id": (table_ref or {}).get("table_id"),
                    "table_name": (table_ref or {}).get("table_name"),
                    "is_comparison": True,
                    "values": points,
                    "query_status": "success",
                }
            else:
                results[c["claim_id"]] = {
                    "query_status": "error",
                    "error_message": (
                        "파생 비교값의 소스 claim 중 일부를 KOSIS에서"
                        " 확인하지 못해 비교값을 만들 수 없습니다."
                    ),
                }

        return results

    def _resolve_item_for_claims(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        indicator: str,
        claims: List[Dict[str, Any]],
        category_hint: Optional[str],
    ) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
        """확정된 표 안에서 컬럼(항목)을 자동으로 확정한다 (자동화 모드 전용).

        process_turn의 [다중 주장 대조 - 항목 자동 확정] 분기(Task #29)와
        동일한 원리를 대화 없는 버전으로 옮긴 것이다.
        """
        item_info = self.resolve_target_item(org_id, tbl_id, indicator, claims=claims)
        if not item_info.get("matched"):
            return None, "이 표에서 관련 컬럼을 찾지 못했습니다."

        candidates = item_info.get("candidates") or []
        if not candidates:
            return item_info, None

        anchored = [c for c in claims if c.get("period")]
        if len(anchored) < 2:
            return None, "항목이 여러 개로 모호한데, 구분할 시점 정보가 부족합니다."

        scored = []
        for cand in candidates:
            if not cand.get("itm_id"):
                continue
            score = self.score_candidate_against_claims(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                indicator=indicator,
                item_info=cand,
                claims=claims,
                category_hint=category_hint,
            )
            scored.append((score, cand))
        scored.sort(key=lambda s: s[0]["matched"], reverse=True)
        if (
            scored
            and scored[0][0]["matched"] >= 2
            and (
                len(scored) == 1
                or scored[0][0]["matched"] > scored[1][0]["matched"]
            )
        ):
            winner = dict(scored[0][1])
            winner["candidates"] = []
            return winner, None

        return None, "항목이 여러 개로 모호해 자동으로 확정하지 못했습니다."

    def _fact_check_single_indicator(
        self,
        indicator: str,
        claims: List[Dict[str, Any]],
        category_hint: Optional[str],
        text: Optional[str] = None,
        reference_date: "Optional[Union[str, date]]" = None,
    ) -> Dict[str, Any]:
        """지표 하나 + 그 지표에 속한 숫자 주장들을 표 확정 -> 컬럼 확정 ->
        대조까지 끝까지 돌린다 (기존 fact_check_text의 3~5단계를 그대로
        옮긴 것). fact_check_text가 지표 하나짜리 문단이면 이걸 한 번만,
        여러 지표가 섞인 문단이면 지표별로 여러 번 호출한다.

        text: 원본 문단(선택) - 넘기면 classify_request로 rate_preference
        (등락률 우선 여부)를 판단해 self.slots에 세팅한다(대화형 process_turn
        경로에만 있던 기존 기능을 완전자동화 경로에도 연결). 없으면 이
        판단 자체를 생략한다(하위호환 - 기존 동작 유지).

        반환 형태는 fact_check_text의 반환값에서 "indicator"/"claims_total"
        을 포함한 서브셋과 동일하다(그대로 fact_check_text 최상위 결과나
        sub_results 원소로 쓸 수 있게).
        """
        # [2026-07 추가] 전망/예측치 주장(예: "1300조원을 넘어설 전망")은
        # KOSIS 확정치와 성격이 달라 애초에 비교 대상이 될 수 없다 - 먼저
        # 걸러낸다(extract_all_claims가 표시해둔 is_forecast 플래그 사용).
        forecast_claims = [c for c in claims if c.get("is_forecast")]
        claims = [c for c in claims if not c.get("is_forecast")]

        # [2026-07 추가 - 골든셋 실측] 한 문장에 서로 다른 주제의 숫자가
        # 섞여 있을 때(예: "성장률이 -0.2%로... 정부안(12조2000억원)보다
        # 더 늘려야 한다는..."), 지금까지는 추출된 숫자 주장을 전부 이
        # 지표의 표와 비교했다. "12조2000억원"(정부 예산 규모, 단위=원)은
        # 경제성장률 표(단위=%)에는 애초에 있을 수 없는 값인데도 claims_total에
        # 끼어들어 claims_matched를 항상 깎았고, 그 결과 성장률 자체는
        # 정확히 맞았는데도 status가 VERIFIED에서 PARTIALLY_VERIFIED로
        # 떨어져 골든셋 "일치" 라벨과 어긋나는 사례가 실측됐다
        # (gdp성장률_1분기, #52). kosis_config.py 힌트의 unit_cat(이
        # 지표가 원래 어떤 단위 종류인지)과, 이미 있는 _unit_compatible
        # (단위 카테고리 비교, kosis_text_utils.py - 컬럼 후보 필터링에
        # 쓰던 것과 동일 로직)을 재사용해, 단위 종류 자체가 명백히 다른
        # 주장은 이 지표와 무관한 것으로 보고 전망치와 같은 방식으로
        # 비교 대상에서 미리 제외한다. unit_cat이 설정 안 된 지표는
        # 기존 동작 그대로(과잉 필터링 방지).
        early_hint_key = self.resolve_hint_key(indicator) or indicator
        early_meta = DEFAULT_INDICATOR_METADATA.get(early_hint_key, {})
        expected_unit_cat = early_meta.get("unit_cat")
        unit_mismatch_claims = []
        if expected_unit_cat:
            kept_claims = []
            for c in claims:
                if self._unit_compatible(c.get("unit"), expected_unit_cat):
                    kept_claims.append(c)
                else:
                    unit_mismatch_claims.append(c)
            claims = kept_claims

        sub: Dict[str, Any] = {
            "indicator": indicator,
            "status": "UNVERIFIED",
            "reason": None,
            "table": None,
            "item": None,
            "claims_matched": 0,
            "claims_total": len(claims),
            "details": [],
            "message": None,
        }
        if forecast_claims:
            sub["forecast_claims_excluded"] = [
                c.get("raw_text") for c in forecast_claims
            ]
            logger.info(
                f"  └─ [전망치 제외] {[c.get('raw_text') for c in forecast_claims]}"
                " - KOSIS 확정치와 비교 대상이 아니라 대조에서 제외합니다."
            )
        if unit_mismatch_claims:
            sub["unit_mismatch_claims_excluded"] = [
                c.get("raw_text") for c in unit_mismatch_claims
            ]
            logger.info(
                f"  └─ [단위 불일치 제외] {[c.get('raw_text') for c in unit_mismatch_claims]}"
                f" - '{indicator}'의 단위({expected_unit_cat})와 종류가 달라"
                " 이 지표와 무관한 주장으로 보고 대조에서 제외합니다."
            )
        if not claims:
            sub["reason"] = (
                "전망/예측치 주장만 있어 KOSIS 확정치와 비교할 수 없습니다."
                if forecast_claims
                else "단위가 이 지표와 맞지 않는 주장만 있어 비교할 수 없습니다."
                if unit_mismatch_claims
                else "검증할 수치 주장이 없습니다."
            )
            sub["message"] = sub["reason"]
            return sub

        # [2026-07 추가] 완전자동화 경로에도 rate_preference 판단을 연결한다
        # (process_turn 대화형 경로에만 있던 기존 인프라 - classify_request/
        # resolve_target_item의 등락률 컬럼 우선 탐색 로직을 그대로 재사용).
        # 원본 문단(text)을 못 받으면(하위호환 호출 등) 그냥 생략한다.
        if text and hasattr(self, "slots"):
            try:
                rule_hints = {
                    "indicator": indicator,
                    "claims": [c.get("raw_text") for c in claims],
                }
                classification = self.classify_request(text, rule_hints)
                self.slots["rate_preference"] = classification.get(
                    "rate_preference"
                )
            except Exception as e:
                logger.warning(f"⚠️ [rate_preference 분류 예외 - 생략]: {e}")
                self.slots["rate_preference"] = None

        # [2026-07 추가] "항목(ITM)축이 사실은 월을 나타내는" 표(예: 화재
        # 발생현황 DT_15601N_001)는 표/컬럼 확정 절차(resolve_target_item
        # 등) 자체가 안 맞는 구조라(itmId가 "지표"가 아니라 "몇 월인지"를
        # 나타냄) 일반 흐름을 타면 안 된다. month_as_item이 설정된
        # 지표는 여기서 곧바로 전용 로직(score_month_as_item_claims)으로
        # 분기한다.
        hint_key_early = self.resolve_hint_key(indicator) or indicator
        meta_info_early = DEFAULT_INDICATOR_METADATA.get(hint_key_early, {})
        month_cfg = meta_info_early.get("month_as_item")
        if month_cfg:
            return self._fact_check_month_as_item_indicator(
                indicator, claims, meta_info_early, month_cfg
            )

        # [2026-07 실측 버그 수정] kosis_config.py의 DEFAULT_INDICATOR_METADATA
        # 항목마다 정성껏 넣어둔 default_category_hint(예: "전국"/"전체"/
        # "대한민국"/"국내총생산(명목 원화표시)")가, 사실 이 fact_check_text
        # 자동화 경로에서는 한 번도 안 읽혔다 - category_hint는 오직
        # LLM이 문장에서 직접 뽑아낸 extracted_category_hint 하나로만
        # 채워졌다(구버전 대화형 process_turn 경로에만 이 병합 로직이
        # 있었고, 새 자동화 경로로 옮겨오지 못했다). 그래서 "국내총생산"처럼
        # 카테고리축 자체가 모호해서 default_category_hint 없이는 절대
        # 못 고르는 지표는, 문장에 그 카테고리명이 그대로 안 적혀 있는 한
        # (거의 항상 그렇다) 카테고리 필터가 하나도 안 걸린 채로 조회돼
        # closest_value조차 못 잡는 실사용 버그로 실측됐다(GDP 스트레스
        # 테스트로 발견). 문장에서 직접 뽑힌 힌트를 우선하고, 없으면
        # 설정된 기본값으로 보완한다.
        if not category_hint:
            hint_key = self.resolve_hint_key(indicator) or indicator
            meta_hint = DEFAULT_INDICATOR_METADATA.get(hint_key, {})
            default_hint = meta_hint.get("default_category_hint")
            if default_hint:
                logger.info(
                    f"  └─ [카테고리 힌트 보완 - 설정 기본값] '{indicator}' ->"
                    f" '{default_hint}' (문장에 직접 언급된 카테고리 없음)"
                )
                category_hint = default_hint

        table_cand, table_reason = self._resolve_table_for_claims(
            indicator, claims, category_hint
        )
        if not table_cand:
            sub["reason"] = table_reason or "통계표를 확정하지 못했습니다."
            sub["message"] = sub["reason"]
            return sub

        org_id = table_cand.get("ORG_ID")
        tbl_id = table_cand.get("TBL_ID")
        tbl_nm = table_cand.get("TBL_NM")
        sub["table"] = {"org_id": org_id, "tbl_id": tbl_id, "tbl_nm": tbl_nm}

        item_info, item_reason = self._resolve_item_for_claims(
            org_id, tbl_id, tbl_nm, indicator, claims, category_hint
        )
        if not item_info:
            sub["reason"] = item_reason or "항목을 확정하지 못했습니다."
            sub["message"] = sub["reason"]
            return sub

        sub["item"] = {
            "itm_id": item_info.get("itm_id"),
            "itm_nm": item_info.get("itm_nm"),
            "breadcrumb": item_info.get("breadcrumb"),
        }

        # [2026-07-24 추가 - #54 두 시점 diff claim] "1년 새 7만명 늘었다"류
        # claim은 단일 시점값 비교(score_candidate_against_claims)로는 애초에
        # 평가가 안 된다 - 별도 경로(score_diff_claim)로 각각 평가하고
        # 일반 claim들의 결과와 합친다. Decision 003(manual_diff 미채택)과
        # 다른 문제라는 점은 score_diff_claim 독스트링 참고.
        diff_claims = [c for c in claims if c.get("is_diff_claim")]
        regular_claims = [c for c in claims if not c.get("is_diff_claim")]

        final_score = self.score_candidate_against_claims(
            org_id=org_id,
            tbl_id=tbl_id,
            tbl_nm=tbl_nm,
            indicator=indicator,
            item_info=item_info,
            claims=regular_claims,
            category_hint=category_hint,
        )
        for dc in diff_claims:
            diff_result = self.score_diff_claim(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                indicator=indicator,
                item_info=item_info,
                claim=dc,
                category_hint=category_hint,
                reference_date=reference_date,
            )
            if diff_result["matched"]:
                final_score["matched"] += 1
            final_score["details"].extend(diff_result["details"])
        final_score["total"] = len(claims)

        sub["claims_matched"] = final_score["matched"]
        sub["details"] = final_score["details"]

        # [2026-07 추가/동명이표 대응] 힌트 표에서 안 맞은 주장이 남아있으면
        # - "우리 기본 표랑 다르다"가 아니라 "KOSIS 어딘가에 실제로 이
        # 수치가 있는가"를 마저 확인한다. 같은 개념으로 검색되는 다른
        # 표(기관/조사방법이 다를 수 있음)에서 나머지 주장이 전부(다중
        # 주장 동시 대조) 맞으면, 그 표의 값을 가져와 병합한다. 어느
        # 표에서 실제로 맞았는지는 반드시 결과에 남긴다(사용자 확인 완료
        # - "VERIFIED로 보고하되 어느 표인지 명시").
        alt_table_used = None
        unmatched_claims = [
            d["claim"] for d in final_score["details"] if not d["matched"]
        ]
        if unmatched_claims:
            alt = self._find_alternate_table_match(
                indicator, unmatched_claims, category_hint, (org_id, tbl_id)
            )
            if alt:
                alt_item = alt["item_info"]
                alt_matched_label = alt_item.get("breadcrumb") or alt_item.get(
                    "itm_nm"
                )
                alt_score_by_raw = {
                    d["claim"]["raw_text"]: d for d in alt["score"]["details"]
                }
                for d in sub["details"]:
                    if d["matched"]:
                        continue
                    alt_d = alt_score_by_raw.get(d["claim"]["raw_text"])
                    if alt_d and alt_d["matched"]:
                        d["matched"] = True
                        d["found_value"] = alt_d["found_value"]
                        d["found_period"] = alt_d["found_period"]
                        d["matched_via_alt_table"] = {
                            "org_id": alt["org_id"],
                            "tbl_id": alt["tbl_id"],
                            "tbl_nm": alt["tbl_nm"],
                            "item_nm": alt_matched_label,
                        }
                sub["claims_matched"] = sum(1 for d in sub["details"] if d["matched"])
                alt_table_used = {
                    "org_id": alt["org_id"],
                    "tbl_id": alt["tbl_id"],
                    "tbl_nm": alt["tbl_nm"],
                    "item_nm": alt_matched_label,
                }
                sub["alt_table_used"] = alt_table_used

        matched = sub["claims_matched"]
        total = final_score["total"]
        matched_label = item_info.get("breadcrumb") or item_info.get("itm_nm")

        alt_note = ""
        if alt_table_used:
            alt_note = (
                f" (이 중 일부는 기본 표 대신 실제로는 '{alt_table_used['tbl_nm']}'"
                f"({alt_table_used['org_id']}) 표의 '{alt_table_used['item_nm']}'"
                " 자료와 일치)"
            )

        if matched == 0:
            sub["status"] = "UNVERIFIED"
            sub["reason"] = "표/항목은 확정했지만 주장과 일치하는 수치를 찾지 못했습니다."
            sub["message"] = (
                f"'{tbl_nm}'/'{matched_label}' 표에서 조회했지만, 문단의 수치"
                f" {total}개 중 일치하는 게 없었습니다."
            )
        elif matched == total:
            sub["status"] = "VERIFIED"
            sub["message"] = (
                f"'{tbl_nm}'/'{matched_label}' 표에서 문단의 수치"
                f" {total}개가 모두 일치해 검증됐습니다.{alt_note}"
            )
        else:
            sub["status"] = "PARTIALLY_VERIFIED"
            sub["message"] = (
                f"'{tbl_nm}'/'{matched_label}' 표에서 문단의 수치"
                f" {total}개 중 {matched}개만 일치했습니다.{alt_note}"
            )

        logger.info(
            f"[완전 자동화 - 팩트체크 결과] status={sub['status']}"
            f" indicator='{indicator}' table='{tbl_nm}'"
            f" matched={matched}/{total}"
            + (f" alt_table={alt_table_used['tbl_id']}" if alt_table_used else "")
        )
        return sub

    def _fact_check_month_as_item_indicator(
        self,
        indicator: str,
        claims: List[Dict[str, Any]],
        meta_info: Dict[str, Any],
        month_cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        """"항목(ITM)축이 사실은 월을 나타내는" 표(예: 화재발생현황
        DT_15601N_001 - 시점은 연도, "항목"이 1월~12월+합계) 전용 처리.

        일반 경로(_resolve_table_for_claims -> _resolve_item_for_claims ->
        score_candidate_against_claims)는 "표 하나를 정하고 그 표 안에서
        지표에 맞는 컬럼(ITM) 하나를 확정한 뒤 기간 범위로 값을 쭉
        가져오는" 구조를 전제하는데, 이 표는 조회할 ITM 자체가 주장마다
        (그 주장이 몇 월 값인지에 따라) 달라져야 해서 그 전제가 안
        맞는다. fallback_tbl_id/org_id(이미 표를 사람이 확인해 확정해둔
        상태)를 그대로 쓰고, score_month_as_item_claims로 주장마다 개별
        조회해 비교한다. 검색/후보 비교(Step 1~2) 없이 바로 표를 확정해서
        쓰는 건, 이런 특수 구조 표는 애초에 제목 검색으로는 다시 찾기
        어렵고(#5와 같은 한계) 사람이 이미 MCP로 검증해둔 표라는 전제가
        있기 때문이다.
        """
        sub: Dict[str, Any] = {
            "indicator": indicator,
            "status": "UNVERIFIED",
            "reason": None,
            "table": None,
            "item": None,
            "claims_matched": 0,
            "claims_total": len(claims),
            "details": [],
            "message": None,
        }

        org_id = meta_info.get("org_id")
        tbl_id = meta_info.get("fallback_tbl_id")
        if not org_id or not tbl_id:
            sub["reason"] = "month_as_item 지표에 org_id/fallback_tbl_id가 설정되지 않았습니다."
            sub["message"] = sub["reason"]
            return sub

        tbl_nm = meta_info.get("tbl_nm") or tbl_id
        month_item_names = month_cfg.get("item_names", {})
        cause_category_hint = month_cfg.get("category_hint")

        sub["table"] = {"org_id": org_id, "tbl_id": tbl_id, "tbl_nm": tbl_nm}
        sub["item"] = {"itm_id": None, "itm_nm": "(월별 항목축)", "breadcrumb": None}

        score = self.score_month_as_item_claims(
            org_id=org_id,
            tbl_id=tbl_id,
            tbl_nm=tbl_nm,
            claims=claims,
            month_item_names=month_item_names,
            cause_category_hint=cause_category_hint,
        )
        sub["claims_matched"] = score["matched"]
        sub["details"] = score["details"]

        matched, total = score["matched"], score["total"]
        if total == 0:
            sub["reason"] = "대조할 주장이 없습니다."
            sub["message"] = sub["reason"]
        elif matched == 0:
            sub["status"] = "UNVERIFIED"
            sub["reason"] = "표는 확정했지만 주장과 일치하는 수치를 찾지 못했습니다."
            sub["message"] = (
                f"'{tbl_nm}' 표에서 조회했지만, 문단의 수치 {total}개 중"
                " 일치하는 게 없었습니다."
            )
        elif matched == total:
            sub["status"] = "VERIFIED"
            sub["message"] = (
                f"'{tbl_nm}' 표에서 문단의 수치 {total}개가 모두 일치해"
                " 검증됐습니다."
            )
        else:
            sub["status"] = "PARTIALLY_VERIFIED"
            sub["message"] = (
                f"'{tbl_nm}' 표에서 문단의 수치 {total}개 중 {matched}개만"
                " 일치했습니다."
            )

        logger.info(
            f"[완전 자동화 - 팩트체크 결과/월=항목축] status={sub['status']}"
            f" indicator='{indicator}' table='{tbl_nm}' matched={matched}/{total}"
        )
        return sub

    def fact_check_text(
        self,
        text: str,
        reference_date: "Optional[Union[str, date]]" = None,
    ) -> Dict[str, Any]:
        """[완전 자동화 진입점] 대화(슬롯 채우기) 없이 기사/문단 텍스트
        하나를 통째로 넣으면, 그 안의 숫자 주장들을 KOSIS 실데이터와
        교차 검증해서 바로 결과를 반환한다.

        reference_date: 기사 게재일("YYYY-MM-DD" 또는 date, 예:
        final_news.csv의 작성일). 넘기면 "지난달"/"작년"/"이달"처럼 절대
        연도 표기가 없는 상대 시점 주장도 게재일 기준으로 해석한다
        (relative_date_edge_cases.md). 안 넘기면 기존과 동일하게 절대
        "YYYY년" 표기가 있는 주장만 시점이 채워진다 - 하위호환 유지.

        process_turn(챗봇 모드)은 지표/시점을 한 번에 하나씩 확인해가며
        슬롯을 채우는 방식이라, 시점이 여러 개 섞인 기사 문단을 검증할
        때는 오히려 대화가 번거로워진다(이번 세션에서 "청년 실업률" 기사로
        실측). 여기서는 사람이 MCP로 직접 검증할 때 거친 5단계 루프(주장
        추출 -> 표 검색 -> 컬럼 확정 -> 실데이터 조회 -> 교차 대조)를
        한 번에 자동으로 돈다.

        자동으로 확신할 수 없는 단계가 있으면(표/항목이 모호함, 못 찾음
        등) 그 시점에서 바로 "UNVERIFIED"로 반환한다 - 애매한 후보를
        참고용으로라도 들이밀며 답한 척하는 것보다, 팩트체크에서는
        "확인 불가"가 항상 더 안전하다.

        [2026-07 추가] 한 문단에 서로 다른 지표가 섞여 있으면(예: "혼인·
        이혼 2025" 기사에 혼인건수/조혼인율/이혼건수/조이혼율이 동시에
        나옴) _assign_claims_to_indicator_groups로 숫자 주장을 지표별로
        묶어서 지표마다 표/컬럼을 따로 확정하고 따로 대조한다. 지표가
        하나뿐인(대다수) 문단은 이전과 완전히 동일하게 동작한다(최상위
        "indicator"/"table"/"item"이 그 하나의 결과를 그대로 담음).
        지표가 여러 개면 "sub_results"에 지표별 결과가 각각 담기고,
        최상위 필드들은 그중 첫 번째(대표) 결과로 채워진다 - 기존
        단일-지표 스키마를 쓰던 코드가 깨지지 않게 하기 위한 하위호환
        처리다.

        반환 형태:
        {
          "status": "VERIFIED" | "PARTIALLY_VERIFIED" | "UNVERIFIED",
          "reason": str 또는 None (UNVERIFIED일 때만),
          "indicator": str 또는 None,
          "table": {"org_id","tbl_id","tbl_nm"} 또는 None,
          "item": {"itm_id","itm_nm","breadcrumb"} 또는 None,
          "claims_matched": int, "claims_total": int,
          "details": [{"claim","matched","found_value","found_period"}, ...],
          "message": str (사람이 읽을 자연어 요약),
          "sub_results": [{"indicator", "status", "table", "item",
                           "claims_matched", "claims_total", "details",
                           "message"}, ...] 또는 None (지표가 하나뿐이면
                           안 채워짐 - 기존 코드 호환용 필드라 항상 있진
                           않다),
        }
        """
        result: Dict[str, Any] = {
            "status": "UNVERIFIED",
            "reason": None,
            "indicator": None,
            "table": None,
            "item": None,
            "claims_matched": 0,
            "claims_total": 0,
            "details": [],
            "message": None,
        }

        # 1) 문단 안의 모든 숫자 주장을 뽑는다.
        claims = self.extract_all_claims(text, reference_date=reference_date)
        result["claims_total"] = len(claims)
        if not claims:
            result["reason"] = "검증할 수치 주장을 찾지 못했습니다."
            result["message"] = result["reason"]
            return result

        # 2) 지표 개념을 뽑는다 (기존 HCX+규칙 하이브리드 추출기 재사용).
        entities = self.extract_delta_entities(text)
        indicator = entities.get("extracted_indicator")
        if not indicator:
            result["reason"] = "확인하려는 지표(통계 항목)를 특정하지 못했습니다."
            result["message"] = result["reason"]
            return result
        result["indicator"] = indicator
        category_hint = entities.get("extracted_category_hint")

        # 2.5) 문단에 서로 다른 지표 키워드가 여러 개 섞여 있는지 근접
        # 매칭으로 확인한다. 배정된 그룹이 2개 미만이면(=지표가 하나뿐이면)
        # 기존 단일-지표 경로를 그대로 탄다 - 동작 변경 없음.
        groups = self._assign_claims_to_indicator_groups(text, claims)
        distinct_indicators = set(groups.keys())

        if len(distinct_indicators) <= 1:
            single = self._fact_check_single_indicator(
                indicator, claims, category_hint, text=text,
                reference_date=reference_date,
            )
            result["status"] = single["status"]
            result["reason"] = single["reason"]
            result["table"] = single["table"]
            result["item"] = single["item"]
            result["claims_matched"] = single["claims_matched"]
            result["details"] = single["details"]
            result["message"] = single["message"]
            return result

        # 3~5) 다중 지표 경로 - 지표별로 표/컬럼 확정 + 대조를 따로 돈다.
        logger.info(
            f"  └─ [다중 지표 문단 감지] {len(distinct_indicators)}개 지표"
            f" 그룹으로 나눠서 각각 대조합니다: {sorted(distinct_indicators)}"
        )
        sub_results: List[Dict[str, Any]] = []
        for alias_value, group_claims in groups.items():
            sub_results.append(
                self._fact_check_single_indicator(
                    alias_value, group_claims, category_hint, text=text,
                    reference_date=reference_date,
                )
            )

        # 어떤 키워드에도 안 걸린(=근접 매칭 실패) 숫자는 HCX가 뽑은 대표
        # 지표에 묶어서 마저 대조한다 - 이게 "모호할 때 LLM(이미 뽑아둔
        # 대표 지표)으로 폴백"에 해당한다(새 LLM 호출을 추가하지 않음).
        grouped_ids = {id(c) for cs in groups.values() for c in cs}
        leftover = [c for c in claims if id(c) not in grouped_ids]
        if leftover:
            sub_results.append(
                self._fact_check_single_indicator(
                    indicator, leftover, category_hint, text=text,
                    reference_date=reference_date,
                )
            )

        result["sub_results"] = sub_results
        result["claims_matched"] = sum(s["claims_matched"] for s in sub_results)
        result["claims_total"] = sum(s["claims_total"] for s in sub_results)
        result["details"] = [d for s in sub_results for d in s["details"]]

        primary = sub_results[0]
        result["table"] = primary["table"]
        result["item"] = primary["item"]

        statuses = {s["status"] for s in sub_results}
        if statuses == {"VERIFIED"}:
            result["status"] = "VERIFIED"
        elif statuses == {"UNVERIFIED"}:
            result["status"] = "UNVERIFIED"
            result["reason"] = "여러 지표를 확인했지만 일치하는 게 하나도 없었습니다."
        else:
            result["status"] = "PARTIALLY_VERIFIED"

        result["message"] = " / ".join(
            f"[{s['indicator']}] {s['message']}" for s in sub_results
        )

        logger.info(
            f"[완전 자동화 - 팩트체크 결과/다중지표] status={result['status']}"
            f" 지표 {len(sub_results)}개 matched={result['claims_matched']}"
            f"/{result['claims_total']}"
        )
        return result


# ====================================================================
# 🧪 [Main Execution]
# ====================================================================
# [2026-07-25 변경] 대화형 REPL(process_turn 기반)은 chatbot.py로 옮겼다.
# 이 파일은 이제 완전자동화 팩트체크 파이프라인(fact_check_text)만 담당
# 한다. 간단한 동작 확인용 스모크 테스트만 남겨둔다.
if __name__ == "__main__":
    agent = KosisInteractiveAgent()

    sample_text = (
        "올해 1분기(1~3월) 성장률이 -0.2%로 마이너스(-) 성장을 했다."
    )
    result = agent.fact_check_text(sample_text, reference_date="2025-04-28")
    print(json.dumps(result, ensure_ascii=False, indent=2))