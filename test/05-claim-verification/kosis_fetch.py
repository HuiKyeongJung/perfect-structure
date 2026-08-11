"""자동 분리된 모듈 (kosis_agent.py 리팩터링) - 동작은 기존과 동일합니다."""

import logging
import re
from typing import Any, Dict, List, Optional, Union

from kosis_config import DEFAULT_INDICATOR_METADATA

logger = logging.getLogger("Task2.KosisChatAgent")


class FetchMixin:
    """확정된 org_id/tbl_id/itm_id(/obj_axis,obj_code)로 KOSIS 실데이터를
    조회하고, 여러 후보 행 중 정확한 하나를 고르는 역할. TextUtilsMixin의
    _period_range를 self.을 통해 쓴다.
"""

    def _select_target_row(
        self,
        year_rows: List[Dict[str, Any]],
        itm_nm: Optional[str],
        keywords: List[str],
        category_hint: Optional[Union[str, List[str]]],
    ) -> Optional[Dict[str, Any]]:
        """우선순위:
        1) itm_nm과 정확히 일치하는 행들로 후보를 좁힌다.
        2) 후보가 여럿이면(같은 항목 안에서 C1 등 카테고리가 여러 개인 경우,
           예: "전산업생산지수(농림어업 포함/제외)") category_hint로 다시
           좁힌다. category_hint가 리스트면(여러 축을 동시에 지정, 예:
           ["전국", "종합"]) 모든 힌트가 다 포함된 행만 남긴다 - 이 폴백은
           extra_obj_axes 서버 필터가 실패했을 때(예: 메타 이름 불일치)의
           안전망이라 리스트 전체를 AND로 취급한다.
        3) 그래도 여럿이면 keywords(AND) fuzzy 매칭.
        4) 그래도 여럿이면 "포함"이 들어간 카테고리를 기본 선호한다
           (일부를 제외한 부분지표보다 전체를 포괄하는 지표가 헤드라인
           수치인 경우가 많기 때문). 다르게 쓰고 싶으면 category_hint를
           명시하면 된다.
        5) 그래도 못 정하면 첫 번째 행.
        """

        def cat_str(row: Dict[str, Any]) -> str:
            raw_dict = row.get("raw_dict", {})
            return " ".join([
                str(v)
                for k, v in raw_dict.items()
                if k.startswith("C") and k.endswith("_NM") and v
            ])

        def row_itm_nm(row: Dict[str, Any]) -> str:
            return str(row.get("raw_dict", {}).get("ITM_NM", ""))

        candidates = year_rows
        if itm_nm:
            exact = [r for r in year_rows if row_itm_nm(r) == itm_nm]
            if exact:
                candidates = exact

        if len(candidates) <= 1:
            return candidates[0] if candidates else None

        if category_hint:
            hints = (
                [category_hint]
                if isinstance(category_hint, str)
                else [h for h in category_hint if h]
            )
            narrowed = [
                r for r in candidates if all(h in cat_str(r) for h in hints)
            ]
            if narrowed:
                candidates = narrowed

        if len(candidates) <= 1:
            return candidates[0] if candidates else None

        # keywords가 비어 있으면 all([])==True라 첫 후보를 그냥 골라버리는
        # 의미 없는 매칭이 되므로, 실제 키워드가 있을 때만 이 단계를 탄다.
        real_keywords = [kw for kw in keywords if kw]
        if real_keywords:
            for r in candidates:
                full_meta_str = f"{cat_str(r)} {row_itm_nm(r)}"
                if all(kw in full_meta_str for kw in real_keywords):
                    return r

        # "포함" 우선(농림어업 포함/제외처럼 지표 자체에 있는 케이스)에 더해,
        # 성별(남성/여성)처럼 컬럼 밑에 다시 나뉜 하위분류가 있는 표에서는
        # 사용자가 성별을 특정하지 않았으면 "합계/소계/전체" 행을 우선한다
        # (예: "항공기 정비" 컬럼 아래 소계/남성/여성 중 소계를 우선 선택).
        for label in ("포함", "합계", "소계", "전체"):
            preferred = next((r for r in candidates if label in cat_str(r)), None)
            if preferred:
                return preferred
        return candidates[0]

    # ------------------------------------------------------------------
    # 표가 실제로 지원하는 주기(연/분기/월) 확인
    # ------------------------------------------------------------------

    _PRD_SE_LABELS = {"Y": "연간", "Q": "분기", "M": "월간", "F": "다년", "IR": "부정기"}

    @classmethod
    def _prd_se_label(cls, prd_se: Optional[str]) -> str:
        return cls._PRD_SE_LABELS.get(prd_se, str(prd_se))

    def _get_supported_prd_se(
        self, org_id: str, tbl_id: str
    ) -> Optional[set]:
        """이 표가 실제로 지원하는 주기 코드 집합을 조회한다(예: {"Q","Y"}).

        client.get_period_meta가 없거나(구버전 스텁/테스트) 호출이
        실패하면 None을 반환한다 - 호출부는 None이면 "확인할 수 없음"으로
        보고 기존처럼 요청한 주기 그대로 진행한다(과잉 차단 방지 - 이
        가드는 "확실히 안 되는 걸 미리 아는" 용도지, "확실하지 않으면
        일단 막는" 용도가 아니다).
        """
        get_period_meta = getattr(self.kosis, "get_period_meta", None)
        if get_period_meta is None:
            return None
        try:
            raw = get_period_meta(org_id, tbl_id)
        except Exception as e:
            logger.warning(f"⚠️ [수록정보 조회 실패 - 주기 검증 생략]: {e}")
            return None
        if not raw:
            return None

        # [실측 확인 완료] KOSIS getMeta(type=PRD)의 실제 원본 응답은
        # 다음과 같다(2026-07, orgId=101/tblId=DT_2OEEM1012 직접 조회):
        #   [{"PRD_SE": "년", "STRT_PRD_DE": "1960", "END_PRD_DE": "2025"}]
        # 즉 "PRD_SE" 필드 자체에 코드("Y")가 아니라 한글 라벨("년")이
        # 그대로 들어있고, PRD_SE_NM이라는 별도 필드는 애초에 없다. 이전
        # 수정은 "PRD_SE에 진짜 코드가 오고, 라벨은 PRD_SE_NM에만 온다"고
        # 잘못 가정해서 raw_code(="년")를 그냥 upper()만 하고 그대로
        # 써버렸다(한글은 upper()해도 안 바뀌므로 여전히 "년") - 그래서
        # "Y"와 비교할 때 계속 불일치로 오판했다("최저임금" 표가 연간
        # 데이터를 지원하는데도 "지원 안 됨"으로 막힌 사고). 이제 어느
        # 필드에서 왔든 값 자체를 항상 라벨→코드 매핑에 먼저 넣어보고,
        # 매핑에 없을 때만(이미 "Y" 같은 진짜 코드로 온 경우) 그대로 쓴다.
        label_to_code = {
            "연간": "Y", "년": "Y", "년간": "Y", "연": "Y",
            "분기": "Q", "분기간": "Q",
            "월간": "M", "월": "M",
            "다년": "F", "다년간": "F",
            "부정기": "IR",
        }
        codes = set()
        for row in raw:
            raw_val = (
                row.get("PRD_SE")
                or row.get("prdSe")
                or row.get("PRD_SE_NM")
            )
            if not raw_val:
                continue
            raw_val = str(raw_val).strip()
            normalized = label_to_code.get(raw_val)
            if normalized:
                codes.add(normalized)
            else:
                # 매핑에 없으면 이미 "Y"/"Q" 같은 진짜 코드로 온 경우일
                # 가능성이 높으니 그대로 사용한다(대소문자만 정규화).
                codes.add(raw_val.upper())
        return codes or None

    # ------------------------------------------------------------------
    # 표 하나를 골랐는데 그 표에 원하는 컬럼이 아예 없는 경우의 자동 전환
    # ------------------------------------------------------------------

    def fetch_kosis_data_range(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        itm_id: str,
        itm_nm: Optional[str],
        indicator: str,
        start_period: str,
        end_period: str,
        prd_se: str = "Y",
        category_hint: Optional[Union[str, List[str]]] = None,
        obj_axis: Optional[int] = None,
        obj_code: Optional[str] = None,
        extra_obj_axes: Optional[Dict[int, str]] = None,
        extra_obj_axes_fallback: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Any]:
        """[KOSIS Tool] 확정된 org_id/tbl_id로 실데이터 수집

        resolve_target_item이 고른 itm_id는 검색용 키워드 매칭 결과일 뿐,
        실제 이 통계표에서 유효한 코드인지는 보장되지 않는다(코드 체계가
        다르면 KOSIS가 err 21로 거부한다). 그래서 서버 필터링에는 itm_id를
        직접 쓰지 않고 itmId="all"로 전체를 받아온 뒤, 이미 구현된 행(row)
        선택 로직에서 itm_nm과 keywords를 함께 대조해 정확한 값을 골라낸다.

        obj_axis/obj_code: resolve_target_item이 찾아낸 컬럼이 사실 ITM이
        아니라 OBJ 분류값이었던 경우(예: "정비사"가 "직무별" 분류축의 코드
        값), 그 축 번호(objL 몇 번째)와 코드값이다.

        extra_obj_axes: category_hint("대형 항공사"->"300인 이상" 같은
        규모 수식어 등, 혹은 여러 축을 동시에 지정하는 리스트)가
        resolve_category_hints_axes로 실제 메타에서 별도 축(들)임이
        확인된 경우 {축 번호: 코드값} 딕셔너리다. 예를 들어 "대형 항공사
        소속 정비사"는 "정비사"=직무별(objL2) 축 코드값이면서 "대형"=
        특성별(objL1) 축 코드값이라, 두 축을 동시에 서버에서 필터링해야
        정확하다(2026-07 KOSIS 실 API 검증: 300인 이상+항공기 정비 합계로
        objL1=A0306&objL2=B0603 조회 시 정확한 값이 나옴). 표에 카테고리
        축이 두 개 이상인 경우(예: 주택매매가격지수의 행정구역별+주택
        유형별)도 마찬가지로 여러 축을 동시에 pin해야 하므로, 이 딕셔너리는
        한 축이 아니라 여러 축을 동시에 담을 수 있다.

        obj_axis와 extra_obj_axes가 있으면 그 축들만 "all" 대신 정확한
        코드로 서버에 필터를 걸어서 응답을 훨씬 작고 정확하게 받는다 -
        그 외 축은 여전히 "all"로 받아서 기존 행 선택 로직(category_hint
        문자열 매칭, 합계 우선 등)으로 나머지를 고른다 (extra_obj_axes
        해석이 실패했을 때의 폴백이기도 하다).

        extra_obj_axes_fallback: [2026-08-10 추가] resolve_keyword_group_
        in_table이 "부모/자식 카테고리 이름이 똑같아 정적으로는 어느 쪽이
        진짜 leaf인지 판별 불가"라고 표시한 축에 한해 {축 번호: 대체
        코드} - 실측(문화산업 임금동향, DT_113_STBL_1031340)에서 부모
        코드로 조회하면 err:30(데이터 0건)인데 이름이 같은 자식 코드가
        진짜 leaf였던 사례가 나왔다(반대로 부모가 맞고 자식이 중복
        placeholder인 사례도 이미 있었음 - _resolve_leaf_row 주석 참고).
        메타 이름만으로는 결정할 수 없으므로 추측하지 않고, extra_obj_axes
        로 1차 조회했는데 결과가 0건이면 이 대체 코드로 딱 한 번만 더
        시도한다 - 실제 KOSIS 응답을 근거로 판단을 미루는 것뿐, 새로
        추측을 얹는 게 아니다.

        prd_se="Y"면 start_period/end_period는 YYYY, "M"이면 YYYYMM이다.
        """
        trail = getattr(self, "_queried_table_trail", None)
        if trail is not None:
            key = f"{org_id}_{tbl_id}"
            if key not in trail:
                trail.append(key)

        logger.debug(
            f"⚡ [KOSIS 데이터 범위 조회] 지표: '{indicator}', 주기: '{prd_se}',"
            f" 기간: '{start_period}~{end_period}', 참고 itmId: '{itm_id}'"
            f" ({itm_nm}), 카테고리 힌트: '{category_hint}',"
            f" obj_axis={obj_axis} obj_code={obj_code}"
            f" extra_obj_axes={extra_obj_axes}"
        )

        meta_info = DEFAULT_INDICATOR_METADATA.get(indicator, {})
        # 행 선택용 키워드 (itm_nm이 없을 때의 fuzzy 폴백 매칭에만 사용)
        keywords = list(meta_info.get("keywords", []))

        # [주기 지원 확인] 실제 조회 전에 이 표가 요청한 주기(연/분기/월)를
        # 정말 지원하는지 먼저 확인한다. 2026-07 실측: "청년 실업률"에서
        # 사용자가 "3월"이라고 해서 prdSe="M"으로 요청했는데, 정작 그 표는
        # 분기/연간 데이터만 있어서(월간 자체가 없음) objL/itmId를 다
        # 맞게 넣어도 항상 err:30만 났다 - 표 구조(축/컬럼)만 메타로
        # 검증하고 "이 표가 애초에 이 형식의 데이터를 주는지"는 확인 없이
        # 바로 조회부터 시도했던 게 문제였다. 이제 연간(Y)으로는 안전하게
        # (표기만 잘라내면 되므로 포맷 추측 리스크가 없다) 자동 대체하고,
        # 그마저도 안 되면(분기만 있는 등, 정확한 분기 코드 포맷을
        # 추측해서 조회하는 건 검증 안 된 채로 값을 만들어내는 위험이
        # 있으므로) 추측 대신 사용자에게 이 표가 실제로 어떤 주기를
        # 지원하는지 명확히 알리고 되묻는다.
        # 원래 요청 주기/기간을 남겨둔다 - 연간으로 자동 대체하더라도,
        # 최종 결과는 호출부(kosis_agent.py)가 base_period(예: "202503")로
        # 그대로 조회할 수 있도록 원래 키 형식을 유지해야 한다(아래 결과
        # 조립 단계에서 사용).
        original_start_period, original_end_period, original_prd_se = (
            str(start_period),
            str(end_period),
            prd_se,
        )
        period_note = None
        # [2026-08-10 추가] 분기가 objL 분류축인 표에서, 단일 시점이 아니라
        # 여러 분기에 걸친 범위 요청(예: "1~4분기 전체", 연간 총계를
        # 분기별로 다 확인하는 claim)일 때 쓸 축 번호. 단일 시점은 정확한
        # 코드를 바로 objl_fixed에 pin하면 되지만(아래), 범위는 분기마다
        # 다른 코드가 필요해서 서버 필터 대신 "이 축은 all로 받아온 뒤
        # period_records 조립 단계에서 행마다 분기 이름을 직접 대조"하는
        # 방식으로 처리한다 - 그러려면 그 조립 루프도 이 축 번호를 알아야
        # 하므로 함수 스코프에 남겨둔다.
        quarter_axis_for_matching: Optional[int] = None
        supported_prd_se = self._get_supported_prd_se(org_id, tbl_id)
        if supported_prd_se and prd_se not in supported_prd_se:
            if "Y" in supported_prd_se:
                requested_label = self._prd_se_label(prd_se)
                # [2026-08-10 추가 - 분기가 "주기"가 아니라 분류축인 표]
                # 표가 prd_se="Q" 자체는 지원 안 해도, 실제로는 "1분기"~
                # "4분기"라는 이름의 objL 분류축으로 같은 정보를 갖고 있는
                # 표가 실측으로 확인됐다(문화체육관광일자리현황조사,
                # DT_113_STBL_1031340 - 표 메타상 주기는 "년"뿐이지만,
                # 실제 조회하면 연도 하나에 "1분기"~"4분기" x 산업분류
                # 조합으로 16개 행이 나온다). 이걸 그냥 연간으로 잘라
                # 버리면 4개 분기 값 중 어느 게 맞는지 모른 채
                # 응답이 여러 후보로 섞여버린다("확실하지 않으면 추측하지
                # 않는다", Decision 003) - 잘라내기 전에 먼저 "N분기"가
                # 이 표의 분류축 값으로 존재하는지 시도해본다.
                is_quarter_request = (
                    prd_se == "Q"
                    and len(str(start_period)) == 5
                    and len(str(end_period)) == 5
                    and str(start_period)[-1] in ("1", "2", "3", "4")
                )
                quarter_axis_hint = None
                if is_quarter_request:
                    quarter_axis_hint = self.resolve_category_hint_axis(
                        org_id, tbl_id, f"{str(start_period)[-1]}분기"
                    )

                is_single_period = str(start_period) == str(end_period)

                if quarter_axis_hint and is_single_period:
                    # 단일 시점 - 정확한 분기 코드를 바로 pin한다(기존
                    # 동작, 2026-08-10 실측 확인 완료).
                    axis_n = quarter_axis_hint["obj_axis"]
                    code_n = quarter_axis_hint["obj_code"]
                    logger.debug(
                        f"  └─ [주기 자동 대체 - 분기 분류축으로 보정] '{tbl_nm}'"
                        f" 표는 '{requested_label}' 주기 파라미터 자체는 없지만,"
                        f" 같은 정보를 가진 분류축을 찾아(axis={axis_n},"
                        f" code={code_n}) 값을 뭉뚱그리지 않고 정확한 분기"
                        " 값 그대로 조회합니다."
                    )
                    period_note = (
                        f"이 표는 '{requested_label}' 주기 파라미터는 없지만,"
                        " 분기가 별도 분류축으로 존재해 그 축으로 정확히"
                        " 조회했습니다(연간 평균/합계가 아니라 해당 분기"
                        " 값입니다)."
                    )
                    start_period, end_period, prd_se = (
                        str(start_period)[:4],
                        str(end_period)[:4],
                        "Y",
                    )
                    extra_obj_axes = dict(extra_obj_axes or {})
                    extra_obj_axes.setdefault(axis_n, code_n)
                elif quarter_axis_hint:
                    # [2026-08-10 추가] 범위 요청("1~4분기 전체" 등) - 분기
                    # 마다 코드가 다르므로 서버 필터로 하나만 pin할 수 없다.
                    # 대신 이 축은 필터 없이("all") 받아오고, 아래 조립
                    # 루프에서 각 목표 분기(period)마다 그 행의 분류값
                    # 이름("N분기")을 직접 대조해서 정확히 하나씩 골라낸다.
                    axis_n = quarter_axis_hint["obj_axis"]
                    logger.debug(
                        f"  └─ [주기 자동 대체 - 분기 분류축(범위)으로 보정]"
                        f" '{tbl_nm}' 표는 '{requested_label}' 주기 파라미터"
                        f" 자체는 없지만, 분류축(axis={axis_n})으로 존재해"
                        " 여러 분기를 한 번에 받아온 뒤 각 분기 이름을 직접"
                        " 대조해서 조회합니다."
                    )
                    period_note = (
                        f"이 표는 '{requested_label}' 주기 파라미터는 없지만,"
                        " 분기가 별도 분류축으로 존재해 그 축의 각 분기 이름을"
                        " 직접 대조해 조회했습니다(연간 평균/합계가 아니라"
                        " 분기별 실제 값입니다)."
                    )
                    start_period, end_period, prd_se = (
                        str(start_period)[:4],
                        str(end_period)[:4],
                        "Y",
                    )
                    quarter_axis_for_matching = axis_n
                else:
                    logger.debug(
                        f"  └─ [주기 자동 대체] '{tbl_nm}' 표는 '{requested_label}'"
                        " 데이터가 없어(지원 주기:"
                        f" {sorted(supported_prd_se)}) 연간 자료로 대신"
                        " 조회합니다."
                    )
                    period_note = (
                        f"이 표는 {requested_label} 데이터가 없어 연간 자료로"
                        " 대신 조회했습니다"
                    )
                    start_period, end_period, prd_se = (
                        str(start_period)[:4],
                        str(end_period)[:4],
                        "Y",
                    )
            else:
                requested_label = self._prd_se_label(prd_se)
                supported_label = ", ".join(
                    self._prd_se_label(p) for p in sorted(supported_prd_se)
                )
                logger.warning(
                    f"  └─ [주기 불일치] '{tbl_nm}' 표는 '{requested_label}'"
                    f" 데이터가 없습니다(지원 주기: {supported_label}) -"
                    " 추측 대신 사용자에게 안내합니다."
                )
                return {
                    "success": False,
                    "message": (
                        f"'{tbl_nm}' 표는 요청하신 주기({requested_label})"
                        f" 데이터가 없습니다. 이 표가 실제로 지원하는 주기는"
                        f" {supported_label}입니다. 다른 주기로 다시"
                        " 물어보시거나 다른 표를 찾아볼까요?"
                    ),
                }

        _, init_dim = self.kosis.get_initial_dimension_count(org_id, tbl_id)

        # 서버에 직접 필터를 걸 축들을 모은다. 두 축이 서로 다르면(보통의
        # 케이스: obj_axis=직무별, extra_obj_axis=특성별) 둘 다 적용하고,
        # 혹시 같은 축을 가리키면(드묾, 힌트 해석이 원래 컬럼과 겹친 경우)
        # 더 구체적인 원래 obj_axis/obj_code를 우선하고 extra는 무시한다.
        objl_fixed: Dict[int, str] = {}
        if obj_axis and obj_code:
            objl_fixed[obj_axis] = obj_code
        if extra_obj_axes:
            for axis, code in extra_obj_axes.items():
                if axis and code and axis not in objl_fixed:
                    objl_fixed[axis] = code
        objl_fixed = objl_fixed or None

        # OBJ 분류값으로 확정된 축이 있으면 그 축 번호까지는 최소한 차원이
        # 존재한다고 봐야 한다 (메타 추론이 그 축을 놓쳤을 수 있으므로).
        if objl_fixed:
            max_fixed_axis = max(objl_fixed.keys())
            if max_fixed_axis > init_dim:
                init_dim = max_fixed_axis

        # [2026-07 추가 - 분기 포맷 변환] 내부 표현(5자리 "YYYYN")은 실제
        # KOSIS Open API 데이터 조회(getList)가 요구하는 포맷이 아니다 -
        # 실측 확인된 실제 포맷(6자리 "YYYY0N")으로 API 호출 시점에만
        # 변환한다(_quarter_period_to_kosis_code 주석 참고). 내부/응답
        # 비교용 값은 그대로 5자리를 유지해야 하므로 별도 변수를 쓴다.
        api_start_period = (
            self._quarter_period_to_kosis_code(str(start_period))
            if prd_se == "Q"
            else str(start_period)
        )
        api_end_period = (
            self._quarter_period_to_kosis_code(str(end_period))
            if prd_se == "Q"
            else str(end_period)
        )

        raw_data = self.kosis.fetch_actual_statistics_bounded_retry(
            org_id=org_id,
            tbl_id=tbl_id,
            start_year=api_start_period,
            end_year=api_end_period,
            itm_id="all",  # 신뢰도 낮은 itmId를 서버 필터로 보내지 않는다
            current_dim=init_dim,
            max_dim=8,
            prd_se=prd_se,
            objl_fixed=objl_fixed,
        )

        # [2026-08-10 추가 - 문화산업 임금동향 실측으로 발견] objl_fixed로
        # 찍은 축 중, "부모/자식 이름이 같아 정적으로 판별 불가"였던 축이
        # 있고(extra_obj_axes_fallback) 1차 조회가 0건이면, 그 축만 이름이
        # 같은 자식 코드로 바꿔 딱 한 번 더 시도한다. 부모가 진짜 맞는
        # 표라면(관광객입국자수 실측 사례) 애초에 raw_data가 비지 않으므로
        # 이 분기 자체가 안 타서 회귀 위험이 없다.
        if not raw_data and extra_obj_axes_fallback and objl_fixed:
            fallback_objl = dict(objl_fixed)
            changed = False
            for axis, fallback_code in extra_obj_axes_fallback.items():
                if axis in fallback_objl and fallback_objl[axis] != fallback_code:
                    fallback_objl[axis] = fallback_code
                    changed = True
            if changed:
                logger.info(
                    f"  └─ [1차 조회 0건 - 동일 이름 자식 코드로 재시도]"
                    f" objl_fixed={objl_fixed} -> {fallback_objl}"
                )
                raw_data = self.kosis.fetch_actual_statistics_bounded_retry(
                    org_id=org_id,
                    tbl_id=tbl_id,
                    start_year=api_start_period,
                    end_year=api_end_period,
                    itm_id="all",
                    current_dim=init_dim,
                    max_dim=8,
                    prd_se=prd_se,
                    objl_fixed=fallback_objl,
                )
                if raw_data:
                    logger.info("  └─ [동일 이름 자식 코드 재시도 성공]")

        if not raw_data:
            return {
                "success": False,
                "message": (
                    f"{start_period}~{end_period}({prd_se}) '{indicator}' 데이터를"
                    " 조회하지 못했습니다."
                ),
            }

        period_records: Dict[str, Dict[str, Any]] = {}

        # 주기가 자동 대체됐으면(예: M -> Y), 실제 조회는 대체된 주기로
        # 하지만 결과는 원래 요청 주기의 기간 목록으로 순회하면서, 각
        # 원래 기간에 대응하는 대체 주기 코드(연도로 자름)로 데이터를
        # 찾아 원래 기간 키(예: "202503")로 저장한다 - 그래야 호출부가
        # base_period/compare_period로 그대로 조회할 수 있다.
        substituted = prd_se != original_prd_se
        for period in self._period_range(
            original_start_period, original_end_period, original_prd_se
        ):
            if substituted:
                lookup_code = period[:4]
            elif original_prd_se == "Q":
                # KOSIS 응답의 PRD_DE도 6자리 zero-padded로 온다(실측
                # 확인) - 내부 5자리 표현을 그대로 비교하면 안 맞는다.
                lookup_code = self._quarter_period_to_kosis_code(period)
            else:
                lookup_code = period
            period_rows = [
                r
                for r in raw_data
                if str(r.get("raw_dict", {}).get("PRD_DE", "")) == lookup_code
            ]

            # [2026-08-10 추가] 분기가 분류축인 표를 범위로 조회할 때
            # (quarter_axis_for_matching이 설정된 경우) - PRD_DE만으로는
            # 그 연도의 4개 분기 행이 전부 걸리므로, 이 목표 기간(period,
            # 예: "20243")의 분기 숫자와 실제로 이름이 일치하는 행만 다시
            # 좁힌다. 이름이 안 찍힌 행(다른 표 구조)은 안전하게 걸러내지
            # 않고 그대로 둔다 - 몰라서 놓치는 것보다는 기존 폴백(itm_nm/
            # category_hint 매칭)에 맡기는 편이 낫다.
            if quarter_axis_for_matching and original_prd_se == "Q" and len(period) == 5:
                quarter_digit = period[-1]
                quarter_label = f"{quarter_digit}분기"
                name_key = f"C{quarter_axis_for_matching}_NM"
                narrowed = [
                    r
                    for r in period_rows
                    if r.get("raw_dict", {}).get(name_key) == quarter_label
                ]
                if narrowed:
                    period_rows = narrowed
                else:
                    logger.warning(
                        f"  └─ [{period} 분기 이름 대조 실패] '{name_key}'가"
                        f" '{quarter_label}'인 행을 못 찾음 - 기존 방식(전체"
                        f" {len(period_rows)}개 후보 중 itm_nm/category_hint"
                        " 매칭)으로 폴백합니다."
                    )

            target_row = self._select_target_row(
                period_rows, itm_nm, keywords, category_hint
            )

            if target_row:
                period_records[period] = target_row
                logger.debug(
                    f"  └─ [{period} 행 선택] 카테고리="
                    f"'{target_row.get('raw_dict', {}).get('C1_NM', '-')}'"
                    f" 값={target_row.get('value')}"
                    f" ({len(period_rows)}개 후보 중 선택)"
                )
            else:
                logger.warning(f"  └─ [{period} 행 없음] 후보 {len(period_rows)}개")

        return {
            "success": True,
            "orgId": org_id,
            "tblId": tbl_id,
            "tblNm": tbl_nm,
            "yearly_records": period_records,
            "period_note": period_note,
        }

    # ------------------------------------------------------------------
    # 대화 컨트롤러
    # ------------------------------------------------------------------
