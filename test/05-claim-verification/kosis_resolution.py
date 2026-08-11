"""자동 분리된 모듈 (kosis_agent.py 리팩터링) - 동작은 기존과 동일합니다."""

import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Union

from kosis_config import (
    DEFAULT_INDICATOR_METADATA,
    INDICATOR_ALIAS_MAP,
    QUALIFIER_KEYWORDS,
)

logger = logging.getLogger("Task2.KosisChatAgent")


class ResolutionMixin:
    """통계표(TBL)/컬럼(ITM)/분류값(OBJ) 후보를 찾고 확정하는 역할.
    TextUtilsMixin의 메서드(_fuzzy_contains 등)를 self.을 통해 쓰므로,
    실제로는 TextUtilsMixin과 함께 상속돼야 한다 (KosisInteractiveAgent
    에서 함께 상속).
"""

    def resolve_target_table(
        self,
        indicator: str,
        claims: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """search_metadata로 통계표(ORG_ID/TBL_ID)를 동적으로 특정합니다.

        fallback_tbl_id가 있는 지표는 검색 결과와 무관하게 그 표를
        최우선으로 채택합니다.

        [2026-07 실측 버그 수정] 예전에는 "검색이 완전히 실패했을 때만"
        하드코딩 힌트를 썼는데(검색 결과가 하나라도 있으면 그중 fallback_
        tbl_id가 있는지만 찾아보고, 없으면 조용히 candidates[0](검색 1위)를
        채택), 실제로 "실업률"을 검색하면 결과 자체는 여러 개 나오지만
        우리가 사람이 직접 확인해서 검증해둔 정답 표(DT_1DA7002S)는 그
        결과에 아예 없는 경우가 실측됐다. 이러면 힌트를 정성껏 만들어놔도
        조용히 무시되고 검색 1위(엉뚱한 표일 수 있음)로 새버린다. 검증된
        힌트가 있다는 것 자체가 "이미 확인 끝난 정답"이라는 뜻이므로,
        검색 결과가 있든 없든 힌트를 최우선으로 신뢰한다 - 최저임금이
        기존에 사실상 이렇게 동작했던 것(client.py의 search_metadata가
        DT_2OEEM1012를 결과 1위로 강제 재정렬)을 모든 힌트 지표에 일반화한
        것이다.
        """
        # "청년 실업률"처럼 수식어가 붙어 있어도 기본형("실업률") 힌트를
        # 찾아 쓴다 - resolve_hint_key 참고(그렇지 않으면 힌트가 있어도
        # 매번 조용히 검색 경로로 새버리는 사고가 난다).
        hint_key = self.resolve_hint_key(indicator, claims=claims) or indicator
        meta_hint = DEFAULT_INDICATOR_METADATA.get(hint_key, {})
        fallback_tbl_id = meta_hint.get("fallback_tbl_id")
        fallback_org_id = meta_hint.get("org_id", "101")

        if fallback_tbl_id:
            logger.info(
                f"  └─ [통계표 확정 - 검증된 힌트] '{indicator}' ->"
                f" [{fallback_org_id}_{fallback_tbl_id}] (검색 결과와 무관하게"
                " 우선 채택)"
            )
            result = {
                "org_id": fallback_org_id,
                "tbl_id": fallback_tbl_id,
                "tbl_nm": indicator,
                "period_start": None,
                "period_end": None,
            }
            swap = self._find_same_name_periodicity_match(
                result["org_id"], result["tbl_id"], result["tbl_nm"],
                indicator, claims,
            )
            return swap or result

        candidates = self.kosis.search_metadata(indicator)

        chosen = candidates[0] if candidates else None

        if chosen:
            logger.info(
                f"  └─ [통계표 확정] '{indicator}' -> "
                f"[{chosen.get('ORG_ID')}_{chosen.get('TBL_ID')}] "
                f"'{chosen.get('TBL_NM')}'"
            )
            result = {
                "org_id": chosen.get("ORG_ID", fallback_org_id),
                "tbl_id": chosen.get("TBL_ID"),
                "tbl_nm": chosen.get("TBL_NM", indicator),
                "period_start": chosen.get("STRT_PRD_DE"),
                "period_end": chosen.get("END_PRD_DE"),
            }
            swap = self._find_same_name_periodicity_match(
                result["org_id"], result["tbl_id"], result["tbl_nm"],
                indicator, claims, search_hits=candidates,
            )
            return swap or result

        # 힌트가 없는 지표는 검색이 완전히 실패하면 빈 결과를 그대로 반환
        # 한다(호출부가 "못 찾음"으로 처리) - fallback_tbl_id 폴백은 위에서
        # 이미 처리했으므로 여기서는 더 시도할 게 없다.
        return {}

    @staticmethod
    def _required_periodicity_from_claims(
        claims: Optional[List[Dict[str, Any]]],
    ) -> Optional[str]:
        """claims에 이미 붙어 있는 period 표기 길이로 필요한 주기를
        결정론적으로 판별한다(4자리=연간/Y, 5자리=분기/Q, 6자리=월/M).
        추측이 아니라 claim 추출 단계에서 이미 확정된 시점 표기를 그대로
        재활용하는 것이므로 quarterly_hint_key와 같은 원칙을 따른다.
        """
        if not claims:
            return None
        lens = {
            len(str(c["period"])) for c in claims if c.get("period")
        }
        if 6 in lens:
            return "M"
        if 5 in lens:
            return "Q"
        if 4 in lens:
            return "Y"
        return None

    def _find_same_name_periodicity_match(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        indicator: str,
        claims: Optional[List[Dict[str, Any]]],
        search_hits: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """[2026-07 추가 - #46 표 버전 다중화 일반화] quarterly_hint_key는
        GDP처럼 사람이 미리 등록해둔 지표에만 동작한다. 이 함수는 등록이
        안 돼 있는 지표에 대한 일반 안전망으로, 확정된 표가 claims가
        요구하는 주기(연/분기/월)를 지원하지 않을 때만 동작한다:

        1) 확정된 표의 실제 지원 주기(getMeta type=PRD)를 조회해 claims가
           요구하는 주기가 없으면
        2) 그 표의 진짜 표제명(TBL_NM)으로 KOSIS를 재검색해 "완전히 같은
           이름"을 가진 다른 (ORG_ID, TBL_ID)를 찾고
        3) 그 동명 후보들 중 필요한 주기를 실제로 지원하는 표가 있으면
           그걸로 교체한다.

        표 ID 접미사(_XX/06->07 등)는 정형화돼 있지 않아 패턴으로 추측하지
        않고, 항상 실제 검색 결과에 동명 표가 존재하는지로만 판단한다.
        필요 주기를 특정할 수 없거나(claims에 period 없음) 이미 지원되면
        조용히 None을 반환해 원래 표를 그대로 쓴다 - 불필요한 API 호출도
        피한다.
        """
        required = self._required_periodicity_from_claims(claims)
        if not required:
            return None

        try:
            prd_meta = self.kosis.get_period_meta(org_id, tbl_id)
        except Exception:
            return None
        supported = {
            p.get("PRD_SE") for p in prd_meta if p.get("PRD_SE")
        }
        if not supported or required in supported:
            # 메타 조회 실패(빈 값)는 판단 불가로 보고 원래 표를 유지한다 -
            # 잘못된 정보로 섣불리 표를 바꾸는 것보다 안전하다.
            return None

        real_tbl_nm = tbl_nm
        hits = search_hits
        if hits is None:
            hits = self.kosis.search_metadata(indicator)
        for cand in hits:
            if (
                cand.get("ORG_ID") == org_id
                and cand.get("TBL_ID") == tbl_id
            ):
                real_tbl_nm = cand.get("TBL_NM") or real_tbl_nm
                break

        same_name = [
            cand for cand in hits
            if cand.get("TBL_NM") == real_tbl_nm
            and (cand.get("ORG_ID"), cand.get("TBL_ID"))
            != (org_id, tbl_id)
        ]
        if not same_name:
            return None

        for cand in same_name:
            c_org, c_tbl = cand.get("ORG_ID"), cand.get("TBL_ID")
            if not c_org or not c_tbl:
                continue
            try:
                c_prd_meta = self.kosis.get_period_meta(c_org, c_tbl)
            except Exception:
                continue
            c_supported = {
                p.get("PRD_SE") for p in c_prd_meta if p.get("PRD_SE")
            }
            if required in c_supported:
                logger.info(
                    f"  └─ [동명이표 주기 매칭] '{real_tbl_nm}' "
                    f"[{org_id}_{tbl_id}](주기:{supported})가 요구 주기"
                    f"'{required}'를 지원 안 함 -> 동일 표제명의 다른 표 "
                    f"[{c_org}_{c_tbl}](주기:{c_supported})로 교체"
                )
                return {
                    "org_id": c_org,
                    "tbl_id": c_tbl,
                    "tbl_nm": cand.get("TBL_NM", real_tbl_nm),
                    "period_start": cand.get("STRT_PRD_DE"),
                    "period_end": cand.get("END_PRD_DE"),
                }
        return None

    # ------------------------------------------------------------------
    # [Step B] 통계표 내 정확한 컬럼(ITM) 하나를 특정
    # ------------------------------------------------------------------

    def _match_meta_rows(
        self, rows: List[Dict[str, Any]], keywords: List[str], use_fuzzy: bool
    ) -> List[Dict[str, Any]]:
        """rows(ITM 또는 OBJ 분류값 행) 중 keywords와 일치하는 행만 골라낸다.

        실측 사례: "정비사"가 접미사 제거로 "정비"가 되듯, "항공사"도
        "사"가 접미사 취급돼 "항공"으로 줄어드는데, 항공산업 관련 표는
        거의 모든 분류값이 "항공..."으로 시작해서 "항공"이 표 안 거의
        전부와 매칭돼버린다. 이러면 실제로는 원하는 개념이 그 표에 아예
        없는데도 "21개 후보 발견"처럼 매칭에 성공한 것처럼 보여서, 다른
        표로 자동 전환해야 할 상황(_resolve_item_with_table_fallback)을
        놓치게 된다. 그래서 후보군의 상당수(기준: 3개 초과 & 40% 초과)에
        걸리는 키워드는 "이 표 안에서는 식별력이 없다"고 보고 무시한다.
        """
        matched: Dict[str, Dict[str, Any]] = {}
        for kw in keywords:
            # genericness는 "업" 예외 없는 순수 core 매칭 기준으로 판단한다.
            # (실제 채택 여부를 결정하는 _fuzzy_contains는 "정비사" vs
            # "정비업" 같은 산업분류 오탐을 막느라 "업"으로 끝나는 행을
            # 걸러내는데, 그 필터링 후 남은 개수만 보면 "항공사"->"항공"
            # 처럼 원래 거의 모든 행에 걸리던 범용 단어가 우연히 소수만
            # 남아 "식별력 있는 키워드"로 착각될 수 있다. 그래서 genericness
            # 판단은 "업" 예외를 적용하기 전 순수 core 매칭 개수로 한다.)
            core = None
            if use_fuzzy:
                for suf in ("사", "직", "원", "공", "가", "인", "자"):
                    if kw.endswith(suf) and len(kw) > 1:
                        core = kw[:-1]
                        break
            generic_hits = 0
            for row in rows:
                nm = self._row_name(row)
                if core:
                    if core in nm or nm in core:
                        generic_hits += 1
                elif kw in nm or nm in kw:
                    generic_hits += 1

            if rows and generic_hits > max(3, len(rows) * 0.4):
                logger.info(
                    f"  └─ [키워드 제외] '{kw}'가 {generic_hits}/{len(rows)}개"
                    " 행에 매칭돼 너무 범용적이라 이 표에서는 제외합니다."
                )
                continue

            kw_hits = []
            for row in rows:
                nm = self._row_name(row)
                is_match = (
                    self._fuzzy_contains(nm, kw) if use_fuzzy else (kw and kw in nm)
                )
                if is_match:
                    kw_hits.append(row)

            for row in kw_hits:
                rid = self._row_id(row)
                if rid:
                    matched.setdefault(rid, row)
        return list(matched.values())


    def _llm_select_meta_rows(
        self,
        indicator: str,
        item_rows: List[Dict[str, Any]],
        category_rows: List[Dict[str, Any]],
        loose: bool = False,
        top_k: int = 5,
    ) -> Optional[List[Dict[str, Any]]]:
        """실제로 스캔해서 받아온 이 표의 항목/분류값 후보 안에서, LLM이
        사용자가 찾는 개념과 일치하는 것(들)을 고르게 한다.

        문자열 유사도 휴리스틱("정비사"->접미사 제거->"정비" 식 fuzzy
        매칭)은 실측 과정에서 "항공사"->"항공"이 항공 관련 표 전체에
        걸리거나, "정비"가 "정비업"(산업분류)과 "항공기 정비"(직무)를
        구분 못 하는 등 오탐이 계속 나왔다. LLM이 KOSIS 통계표 자체를
        학습한 건 아니라서 후보를 새로 "생성"하는 건 못 미덥지만, 이미
        API로 실제로 받아온 짧은 후보 목록 중에서 "어느 게 질문과 의미가
        같은지" 고르는 건 일반적인 독해/선택 과제라 문자열 휴리스틱보다
        훨씬 안정적이다.

        loose=True(top_k개까지 느슨하게 허용)는 값 비교 기반 구제용
        2차 시도 전용이다: 기본(strict) 모드는 "의미가 정확히 같지
        않으면 포함하지 마세요"라고 엄격하게 지시하는데, 이게 같은
        temperature=0 호출인데도 실측에서 매번 다른 결과를 내는 경계
        사례가 있었다("항공기 정비"를 어떤 실행에서는 맞다고, 어떤
        실행에서는 "없음"이라고 판단). strict가 "없음"이라고 답해도 그
        표에 정말 관련 개념이 없다는 보장은 없으므로, loose 모드로 "완전히
        확신은 안 서도 후보가 될 만한 것들"까지 여러 개 받아서, 호출부가
        실제 값을 다 조회해 주장 수치와 비교해보고 최종 판단하게 한다
        (LLM의 개념 판단만으로 확정하지 않고, 실제 데이터 값이라는 더
        확실한 근거로 검증한다).

        반환값:
            None -> LLM 호출 자체가 실패함(네트워크/파싱 오류 등). 호출부는
                    이 경우 기존 fuzzy 매칭으로 폴백해야 한다.
            [] -> LLM이 정상 응답했고, 일치하는 항목이 없다고 판단함.
            [row, ...] -> 일치한다고(혹은 loose 모드면 후보로) 고른 실제
                    메타 행(들).
        """
        candidates: List[Dict[str, Any]] = []
        seen = set()
        for row in item_rows:
            key = ("실제 컬럼(항목)", self._row_name(row))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {"row": row, "nm": key[1], "axis": key[0], "breadcrumb": key[1]}
            )
        for row in category_rows:
            axis_nm = row.get("OBJ_NM") or "분류"
            key = (axis_nm, self._row_name(row))
            if key in seen:
                continue
            seen.add(key)
            # 리프 이름만 보여주면 "300인 이상"이 종사자규모 그룹인지
            # 산업분류 그룹인지 LLM이 알 수 없다 - 같은 축(axis_nm) 안에
            # 서로 다른 개념 그룹이 섞여 있는 경우(특성별 축에 항공산업
            # 분류/종사자규모/매출액규모가 함께 있는 실측 사례)를 구분할
            # 수 있도록, 최상위 그룹명까지 포함한 breadcrumb을 보여준다.
            breadcrumb = self._row_breadcrumb(category_rows, row)
            candidates.append(
                {"row": row, "nm": key[1], "axis": key[0], "breadcrumb": breadcrumb}
            )

        if not candidates:
            return []

        options_text = "\n".join(
            f"{i + 1}. [{c['axis']}] {c['breadcrumb']}"
            for i, c in enumerate(candidates)
        )

        rate_note = ""
        if self.slots.get("rate_preference") == "official_rate":
            rate_note = (
                "\n\n참고: 사용자는 KOSIS가 공식 제공하는 등락률/증감률"
                " 수치를 찾고 있을 가능성이 높습니다. 그런 항목(등락률,"
                " 증감률, 전년동월비 등)이 후보에 있다면 우선 고려하세요."
            )

        breadcrumb_note = (
            "\n\n목록의 각 항목은 \"그룹명 > 세부값\" 형태로 돼 있을 수"
            " 있습니다(예: \"종사자규모 > 300인 이상\", \"항공산업 관련"
            " 제조 및 수리업 > 항공산업 관련 정비업\"). 이때 \">\" 앞부분이"
            " 그 값이 실제로 속한 상위 개념(같은 축 번호라도 규모/매출액/"
            "산업분류/직무처럼 서로 완전히 다른 그룹일 수 있음)입니다."
            " 세부값의 글자만 보고 판단하지 말고, 그 그룹 자체가 찾는"
            " 개념과 맞는 종류인지(예: 직무/직업을 찾는데 그룹이 종사자"
            " 규모나 매출액 구간이면 다른 종류이므로 제외) 반드시 함께"
            " 판단하세요."
        )
        if loose:
            system_instruction = (
                "당신은 국가통계포털(KOSIS) 통계표 안에서 사용자가 찾는"
                " 개념과 관련 있을 만한 항목(들)을 고르는 역할입니다. 아래"
                " 목록은 이 통계표에 실제로 존재하는 항목/분류값입니다"
                " (당신이 사전에 학습한 지식이 아니라 방금 API로 받아온"
                " 실제 데이터입니다) - 목록에 없는 답은 만들어내면 안"
                " 됩니다.\n\n"
                f"{options_text}{rate_note}{breadcrumb_note}\n\n"
                "완전히 확신이 서지 않아도, 사용자가 찾는 개념과 조금이라도"
                f" 관련 있어 보이는 항목을 최대 {top_k}개까지 번호로"
                ' 나열하세요 (실제 값을 비교해서 최종 판단은 나중에 별도로'
                ' 합니다). 다만 그룹 자체가 종류가 다르면(직무를 찾는데'
                " 규모/매출액 구간인 경우 등) 값이 숫자로 그럴듯해 보여도"
                ' 후보에 넣지 마세요. {"matched_numbers": [번호, ...]} 형태의'
                " 순수 JSON으로만 응답하세요. 정말 아무 관련도 없으면"
                ' {"matched_numbers": []}로 응답하세요.'
            )
        else:
            system_instruction = (
                "당신은 국가통계포털(KOSIS) 통계표 안에서 사용자가 찾는 개념과"
                " 정확히 일치하는 항목(들)을 고르는 역할입니다. 아래 목록은 이"
                " 통계표에 실제로 존재하는 항목/분류값입니다 (당신이 사전에"
                " 학습한 지식이 아니라 방금 API로 받아온 실제 데이터입니다) -"
                " 목록에 없는 답은 만들어내면 안 됩니다.\n\n"
                f"{options_text}{rate_note}{breadcrumb_note}\n\n"
                "사용자가 찾는 개념과 의미가 정확히 같은 항목의 번호만 골라"
                ' {"matched_numbers": [번호, ...]} 형태의 순수 JSON으로'
                " 응답하세요. 정확히 일치하는 게 없으면"
                ' {"matched_numbers": []}로 응답하세요. 비슷해 보여도 의미가'
                " 다르면(예: 사람/직무를 뜻하는 개념인데 산업·업종 분류인"
                " 경우, 성별처럼 질문과 무관한 하위분류인 경우, 그룹 자체가"
                " 규모·매출액 구간처럼 종류가 다른 경우) 포함하지 마세요."
            )
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f'찾는 개념: "{indicator}"'},
        ]
        try:
            raw = self.hcx.generate_completion(messages, temperature=0.0)
            clean = re.sub(r"```json|```", "", raw).strip()
            parsed = json.loads(clean)
            numbers = parsed.get("matched_numbers", [])
            picked = []
            for n in numbers:
                try:
                    idx = int(n) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(candidates):
                    picked.append(candidates[idx]["row"])
            if loose:
                picked = picked[:top_k]
            logger.info(
                f"  └─ [LLM 항목 선택{'(loose)' if loose else ''}]"
                f" '{indicator}' -> "
                f"{[self._row_name(r) for r in picked] or '없음'}"
            )
            return picked
        except Exception as e:
            logger.warning(f"⚠️ [LLM 항목 선택 예외 - fuzzy 폴백]: {e}")
            return None


    def resolve_target_item(
        self,
        org_id: str,
        tbl_id: str,
        indicator: str,
        claims: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """ITM 메타 전체를 조회해 키워드로 컬럼 후보를 좁힙니다.

        반환값:
            {"itm_id": "T002", "itm_nm": "수출금액", "candidates": [], "matched": True}
              -> ITM(진짜 컬럼)에서 1개로 확정된 경우
            {"itm_id": "all", "itm_nm": "항공기 정비", "obj_axis": 1,
             "obj_code": "B0301", "candidates": [], "matched": True}
              -> 사실 OBJ 분류값이었던 경우 (itm_id는 "all"로 두고 obj_axis/
                 obj_code로 objL{obj_axis}에 정확한 코드를 넣어 서버 필터링)
            {"itm_id": None, "itm_nm": None, "candidates": [...], "matched": True}
              -> 여러 개로 모호해 사용자 확인이 필요한 경우 (candidates 각
                 항목에도 obj_axis/obj_code가 있으면 OBJ 분류값 후보)
        matched=False는 근거 없이 "일단 첫 항목"으로 찍었거나 아예 못 찾은
        경우다.
        """
        # resolve_target_table과 동일한 이유로, "청년 실업률"처럼 수식어가
        # 붙어도 기본형("실업률") 힌트의 item_keywords를 그대로 쓴다.
        item_hint_key = self.resolve_hint_key(indicator, claims=claims) or indicator
        meta_hint = DEFAULT_INDICATOR_METADATA.get(item_hint_key, {})
        item_keywords = meta_hint.get("item_keywords") or meta_hint.get(
            "keywords", []
        )
        use_fuzzy = not item_keywords
        if use_fuzzy:
            if len(indicator) > 12:
                extracted_keywords = self._extract_keywords_from_sentence(
                    indicator
                )
                item_keywords = extracted_keywords or [indicator]
            else:
                item_keywords = [indicator]

        if self.slots.get("rate_preference") == "official_rate":
            rate_keywords = ["등락률", "증감률", "전년동월비", "전월비", "증감"]
            item_keywords = list(item_keywords) + [
                kw for kw in rate_keywords if kw not in item_keywords
            ]

        raw_list = self.kosis.get_itm_meta_list(org_id, tbl_id)
        item_rows, category_rows = self._split_meta_rows(raw_list)

        if not raw_list:
            logger.warning(
                f"  └─ [메타 조회 실패] '{indicator}' 하드코딩 폴백"
                f" itmId 사용: {meta_hint.get('target_itm_id', 'all')}"
            )
            return {
                "itm_id": meta_hint.get("target_itm_id", "all"),
                "itm_nm": None,
                "candidates": [],
                "matched": bool(meta_hint.get("target_itm_id")),
            }

        def to_candidate(row: Dict[str, Any]) -> Dict[str, Any]:
            if row.get("OBJ_ID") != "ITEM":
                row = self._resolve_leaf_row(category_rows, row)
            cand = {
                "itm_id": self._row_id(row),
                "itm_nm": self._row_name(row),
                "breadcrumb": self._row_breadcrumb(category_rows, row),
            }
            axis_sn = row.get("OBJ_ID_SN")
            if row.get("OBJ_ID") != "ITEM" and axis_sn is not None:
                try:
                    cand["obj_axis"] = int(axis_sn)
                    cand["obj_code"] = self._row_id(row)
                    cand["itm_id"] = "all"  # 진짜 itmId 파라미터는 그대로 두고 objL로 필터
                except (TypeError, ValueError):
                    pass
            return cand

        # DEFAULT_INDICATOR_METADATA에 커스텀 힌트가 있는 지표(최저임금 등)는
        # 이미 검증된 결정적 키워드가 있으니 기존 fuzzy 매칭을 그대로 쓴다.
        # 힌트가 없는 지표(내용 기반 검색으로 막 확정된 새 지표)는, 문자열
        # 유사도 휴리스틱이 계속 오탐을 내서(예: "항공사"->"항공"이 항공
        # 관련 표 전체에 걸림) LLM에게 실제로 스캔한 후보 목록을 그대로
        # 보여주고 고르게 한다 - LLM이 KOSIS 자체를 학습한 건 아니지만,
        # 이미 API로 받아온 짧은 후보 중 "의미가 같은 것 고르기"는 잘한다.
        matched_source = "item"
        if use_fuzzy:
            # LLM에게 보여줄 "찾는 개념"은 원본 indicator가 아니라 정제된
            # item_keywords를 우선한다. 지표 추출이 완전히 실패해서
            # indicator 자체가 "2023년 대형 항공사 소속 정비사 4,248명"처럼
            # 숫자/조사가 섞인 문장 전체인 경우, 이걸 그대로 LLM 프롬프트에
            # "찾는 개념: ..."으로 넣으면 LLM이 문장 전체를 문자 그대로
            # 매칭하려다 실제로 존재하는 후보(예: "항공기 정비")도 "없음"으로
            # 오판하는 사례가 실측됐다. item_keywords는 위에서 이미
            # _extract_keywords_from_sentence로 숫자/날짜/조사를 뗀
            # 상태이므로 이걸 합쳐서 더 짧고 명확한 개념으로 물어본다.
            llm_concept = indicator
            if len(indicator) > 12 and item_keywords:
                llm_concept = " ".join(item_keywords)
            llm_result = self._llm_select_meta_rows(
                llm_concept, item_rows, category_rows
            )
            if llm_result is not None:
                matched_items = llm_result
                matched_source = "llm"
            else:
                matched_items = self._match_meta_rows(
                    item_rows, item_keywords, use_fuzzy
                )
                matched_source = "item(fuzzy 폴백)"
                if not matched_items and category_rows:
                    matched_items = self._match_meta_rows(
                        category_rows, item_keywords, use_fuzzy
                    )
                    matched_source = "category(fuzzy 폴백)"
        else:
            matched_items = self._match_meta_rows(
                item_rows, item_keywords, use_fuzzy
            )
            if not matched_items and category_rows:
                matched_items = self._match_meta_rows(
                    category_rows, item_keywords, use_fuzzy
                )
                matched_source = "category"

        if len(matched_items) == 1:
            only = matched_items[0]
            cand = to_candidate(only)
            logger.info(
                f"  └─ [컬럼 확정 - {matched_source}] itmId='{cand['itm_id']}'"
                f" ({cand['itm_nm']})"
                + (
                    f" obj_axis={cand['obj_axis']} obj_code={cand['obj_code']}"
                    if "obj_axis" in cand
                    else ""
                )
            )
            return {**cand, "candidates": [], "matched": True}

        if len(matched_items) > 1:
            # [2026-07 추가/외환보유액 힌트 검증 중 발견] 키워드 매칭이
            # 여러 개로 걸려도, DEFAULT_INDICATOR_METADATA에 이미 사람이
            # MCP로 실측 확인해둔 target_itm_id가 있고 그게 후보 중 하나면
            # 그걸 곧바로 채택한다. 실측 사례(경제활동인구, DT_1DA7002S):
            # item_keywords=["경제활동인구"]가 정확한 항목(T20 "경제활동인구")
            # 뿐 아니라 그 항목명을 부분 문자열로 포함하는 다른 항목(T50
            # "비경제활동인구")에도 매칭돼 "2개 후보 발견"으로 모호 처리
            # 됐었다 - 이런 접두사 포함 케이스는 문자열 매칭만으로는 원천적
            # 으로 구분 불가능하지만, target_itm_id는 이미 사람이 검증해둔
            # 정답이므로 후보 중에 있으면 추측이 아니라 확정으로 봐도 된다.
            target_itm_id = meta_hint.get("target_itm_id")
            if target_itm_id:
                exact = [
                    row for row in matched_items
                    if self._row_id(row) == target_itm_id
                ]
                if len(exact) == 1:
                    cand = to_candidate(exact[0])
                    logger.info(
                        f"  └─ [컬럼 확정 - {matched_source}+target_itm_id 우선]"
                        f" itmId='{cand['itm_id']}' ({cand['itm_nm']}) -"
                        f" 나머지 {len(matched_items) - 1}개 후보(부분 문자열"
                        " 포함 등)는 배제"
                    )
                    return {**cand, "candidates": [], "matched": True}

            logger.info(
                f"  └─ [컬럼 모호 - {matched_source}] {len(matched_items)}개"
                " 후보 발견 -> 사용자 확인 필요"
            )
            return {
                "itm_id": None,
                "itm_nm": None,
                "candidates": [to_candidate(i) for i in matched_items],
                "matched": True,
            }

        # 키워드 매칭 실패 -> 하드코딩 target_itm_id, 그마저 없으면 첫 항목
        if meta_hint.get("target_itm_id"):
            return {
                "itm_id": meta_hint["target_itm_id"],
                "itm_nm": None,
                "candidates": [],
                "matched": True,
            }

        # 키워드 매칭이 완전히 실패한 경우: "일단 첫 항목"으로 조용히
        # 폴백하면 엉뚱한 컬럼을 확신에 찬 것처럼 대답하는 사고로 이어지니,
        # ITM 축(진짜 컬럼)만 후보로 되묻는다 - 분류값(category_rows)까지
        # 섞어서 다 보여주면 후보가 수백 개로 폭발할 수 있다.
        MAX_CANDIDATES = 20
        logger.warning(
            f"  └─ [컬럼 키워드 매칭 실패] '{indicator}' 관련 항목을 못 찾음"
            f" (ITM {len(item_rows)}개 / 분류값 {len(category_rows)}개 중)"
            " -> 사용자 확인 요청"
        )
        if item_rows and len(item_rows) <= MAX_CANDIDATES:
            return {
                "itm_id": None,
                "itm_nm": None,
                "candidates": [to_candidate(i) for i in item_rows],
                "matched": False,
            }

        if item_rows:
            first = item_rows[0]
            logger.warning(
                f"  └─ [항목이 {len(item_rows)}개로 너무 많아 되묻기 생략] 첫"
                f" 번째 항목으로 폴백: itmId='{self._row_id(first)}'"
                f" ({self._row_name(first)})"
            )
            return {
                "itm_id": self._row_id(first) or "all",
                "itm_nm": self._row_name(first),
                "candidates": [],
                "matched": False,
            }

        return {
            "itm_id": "all",
            "itm_nm": None,
            "candidates": [],
            "matched": False,
        }

    # ------------------------------------------------------------------
    # [내용 기반 검색] 커스텀 힌트가 없는 지표의 통계표 후보 찾기
    # ------------------------------------------------------------------
    # KOSIS statisticsSearch.do(=self.kosis.search_metadata)는 통계표
    # 제목/설명(TBL_NM, CONTENTS)만 검색 대상이고, 테이블 내부의 항목(ITM)/
    # 분류(OBJ) 값 - 예: "항공기 정비" 같은 컬럼명 - 은 KOSIS에 애초에 그런
    # 검색 API가 없다. 그래서 "정비사"로 제목 검색을 아무리 시도해도, 제목이
    # "항공산업 인력현황조사"인 테이블은 잘 안 걸린다.
    #
    # 과거 버전은 이걸 보완하려고 후보 테이블 전부의 ITM/OBJ 메타를 훑는
    # "딥서치"를 자동으로 돌렸는데, 후보가 늘어날수록 API 호출이 배로
    # 늘어나는 문제가 있었다. 대신 여기서는: (1) 검색어를 한 번만 넓혀서
    # 후보를 모으고, (2) 후보가 여럿이면 미리 다 스캔하지 않고 사용자에게
    # 직접 골라달라고 물어본다 - 컬럼(ITM) 스캔은 사용자가 확정한 "그
    # 한 테이블"에 대해서만 수행한다.


    def search_table_candidates(
        self, query: str, top_n: int = 5, result_count: int = 20
    ) -> List[Dict[str, Any]]:
        """검색어로 통계표 후보를 찾아 상위 top_n개만 반환한다 (스캔 없이 검색만).

        result_count: KOSIS에 실제로 요청할 원본 검색 결과 개수(기본 20).
        top_n으로 자르기 전에 원본 자체를 더 크게 받아야 하는 경우(예:
        "실업률"처럼 일부러 넓게 잡는 검색어)를 위해 별도로 조절 가능하게
        열어둔다 - top_n만 키워봐야 애초에 원본이 20개뿐이면 의미가 없다.
        """
        results = self.kosis.search_metadata(query, result_count=result_count)
        return results[:top_n]

    @staticmethod
    def _qualifier_stripped_search_term(indicator: str) -> Optional[str]:
        """지표명이 "청년 실업률"처럼 인구통계 수식어로 시작하면, 수식어를
        뗀 기본 지표명("실업률")도 검색어 후보로 준다.

        실측(2026-07, "청년 실업률" 기사 자동 검증): "청년 실업률"로만
        검색하면 이미 그 이름 그대로 파생된 통계표("청년실업률(시도)" -
        분기/연간만 있어 기사의 월별 수치와 안 맞음)나 국제(ILO) 비교
        통계표만 걸리고, 정작 원본 데이터가 있는 일반 표("연령별
        경제활동인구총괄" - 여기선 "청년"이 표 제목이 아니라 표 안의
        연령대 행(15~29세)으로만 존재)는 검색에 아예 안 걸렸다. 인구통계
        수식어는 보통 표 제목이 아니라 표 안의 분류 행으로 존재하므로,
        수식어를 뗀 기본 지표명도 같이 검색해봐야 이런 표를 놓치지 않는다.
        """
        for qualifier in QUALIFIER_KEYWORDS:
            prefix = f"{qualifier} "
            if indicator.startswith(prefix):
                stripped = indicator[len(prefix):].strip()
                if stripped:
                    return stripped
        return None

    @classmethod
    def resolve_hint_key(
        cls,
        indicator: str,
        claims: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[str]:
        """이 지표명으로 DEFAULT_INDICATOR_METADATA에서 실제로 찾아야 할
        키를 결정한다("실업률"이면 그대로, "청년 실업률"이면 "실업률").

        [2026-07 추가 - 표 버전 다중화(#46) 대응] "경제성장률"처럼 같은
        개념이 실제로는 주기(연간/분기)에 따라 완전히 다른 KOSIS 표에
        나뉘어 있는 지표가 있다("올해 1분기 성장률 -0.2%"는 분기 국민계정
        표(DT_200Y102)에만 있고, 힌트로 등록된 연간 국제비교 표
        (DT_2KAA905)에는 아예 없다 - 팀원 골든셋 재검증 중 실측). 어느
        버전인지 "추측"하는 게 아니라, 문장에서 이미 추출된 claim의 시점
        표기 자체가 분기(YYYYN, 5자리)인지 연도(YYYY, 4자리)인지로
        결정론적으로 판별 가능하므로 Decision Log 003의 "추측 금지"
        원칙에 어긋나지 않는다. claims를 넘기면, 매핑된 힌트 항목에
        "quarterly_hint_key"가 있고 분기 형식 시점이 하나라도 있을 때
        그 대체 키로 바꿔 반환한다(claims를 안 넘기면 기존과 동일하게
        동작 - 하위호환).
        """
        if indicator is None:
            return None
        mapped = INDICATOR_ALIAS_MAP.get(indicator, indicator)
        key = None
        if mapped in DEFAULT_INDICATOR_METADATA:
            key = mapped
        else:
            stripped = cls._qualifier_stripped_search_term(indicator)
            if stripped:
                stripped_mapped = INDICATOR_ALIAS_MAP.get(stripped, stripped)
                if stripped_mapped in DEFAULT_INDICATOR_METADATA:
                    key = stripped_mapped
        if key is None:
            return None
        if claims:
            quarterly_key = DEFAULT_INDICATOR_METADATA.get(key, {}).get(
                "quarterly_hint_key"
            )
            if quarterly_key and quarterly_key in DEFAULT_INDICATOR_METADATA:
                has_quarter_period = any(
                    c.get("period") and len(str(c["period"])) == 5
                    for c in claims
                )
                if has_quarter_period:
                    return quarterly_key

            # [2026-07-24 추가/MCP 실측 - 소비자물가지수_10월 대응] "물가는
            # 원지수(수준값) 표 하나에만 힌트가 걸려 있는데, claim이
            # 등락률(%)인 경우가 있다(예: "1년 전과 비교해 2.4% 상승").
            # 원지수 표에는 등락률 항목 자체가 없어서(MCP 실측: DT_1J22001
            # ITM 목록에 %가 아예 없음) 이 표로 조회하면 단위가 안 맞는
            # 값과 비교하게 된다. quarterly_hint_key와 같은 원리 - 이미
            # 추출된 claim의 unit이라는 결정론적 근거로만 판별하고, 표
            # 안에 실제 그 unit이 있는지는 추측하지 않고 rate_hint_key로
            # 등록된 별도 표를 그대로 신뢰한다.
            rate_key = DEFAULT_INDICATOR_METADATA.get(key, {}).get(
                "rate_hint_key"
            )
            if rate_key and rate_key in DEFAULT_INDICATOR_METADATA:
                cur_unit_cat = DEFAULT_INDICATOR_METADATA.get(key, {}).get(
                    "unit_cat"
                )
                rate_unit_cat = DEFAULT_INDICATOR_METADATA.get(
                    rate_key, {}
                ).get("unit_cat")
                # 주의: 여기서는 일반적인 _unit_compatible(관대한 판정 -
                # "other" 카테고리면 무조건 호환으로 봐서 과잉 필터링을
                # 피하는 용도)을 그대로 쓰면 안 된다. "2020=100"처럼 실제
                # 단위 카테고리 패턴에 안 걸리는 문자열은 항상 {"other"}로
                # 분류되고, _unit_compatible은 "other"가 끼면 무조건
                # True를 반환하므로 "%"와도 항상 호환된다고 나와 스왑
                # 조건이 절대 안 걸린다(실측으로 확인). 여기서는 반대로
                # "claim이 진짜 rate_key 쪽 카테고리와 겹치고, 현재 key
                # 쪽 카테고리와는 안 겹치는" 엄격한 판정이 필요하므로
                # _unit_categories를 직접 비교한다.
                cur_cats = cls._unit_categories(cur_unit_cat)
                rate_cats = cls._unit_categories(rate_unit_cat)
                has_rate_claim = any(
                    c.get("unit")
                    and (cls._unit_categories(c["unit"]) & rate_cats)
                    and not (cls._unit_categories(c["unit"]) & cur_cats)
                    for c in claims
                )
                if has_rate_claim:
                    return rate_key
        return key


    def _rank_candidates_by_text(
        self,
        indicator: str,
        candidates: List[Dict[str, Any]],
        text_fn,
    ) -> List[Dict[str, Any]]:
        """candidates를 text_fn(cand)이 만든 설명 텍스트 기반으로 LLM에게
        관련도 순으로 정렬시키는 공용 로직. _llm_rank_table_candidates의
        1차(짧은 CONTENTS)/2차(풍부한 통계설명자료) 패스가 공유해서 쓴다.
        """
        if len(candidates) <= 1:
            return candidates

        options_text = "\n".join(
            f"{i + 1}. {text_fn(c)}" for i, c in enumerate(candidates)
        )
        system_instruction = (
            "당신은 국가통계포털(KOSIS) 통계표 후보 중 사용자가 찾는 개념과"
            " 가장 관련 있는 표를 고르는 역할입니다. 아래는 실제 검색된"
            " 통계표 후보 목록입니다 (번호. 제목 - 설명).\n\n"
            f"{options_text}\n\n"
            "사용자가 찾는 개념이 실제 컬럼/분류값으로 들어있을 가능성이"
            " 높은 표부터 순서대로 번호를 나열하세요. 관련 있는 표만"
            ' 나열하면 되고, 전부 나열할 필요는 없습니다. {"ranked_numbers":'
            ' [번호, ...]} 형태의 순수 JSON으로만 응답하세요.'
        )
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f'찾는 개념: "{indicator}"'},
        ]
        try:
            raw = self.hcx.generate_completion(messages, temperature=0.0)
            clean = re.sub(r"```json|```", "", raw).strip()
            parsed = json.loads(clean)
            numbers = parsed.get("ranked_numbers", [])
            ranked: List[Dict[str, Any]] = []
            used = set()
            for n in numbers:
                try:
                    idx = int(n) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(candidates) and idx not in used:
                    ranked.append(candidates[idx])
                    used.add(idx)
            # 랭킹에서 언급 안 된 나머지는 원래 순서 그대로 뒤에 붙인다
            # (LLM이 놓쳤을 경우를 대비해 후보를 아예 잃어버리지 않는다).
            for i, c in enumerate(candidates):
                if i not in used:
                    ranked.append(c)
            return ranked
        except Exception as e:
            logger.warning(f"⚠️ [통계표 재정렬 예외 - 원래 순서 유지]: {e}")
            return candidates

    @staticmethod
    def _first_present(d: Dict[str, Any], keys: "tuple") -> str:
        """KOSIS 통계설명자료 응답의 필드명이 실제 API 키/응답 버전에 따라
        다를 수 있어(예: WRITING_PURPS vs writingPurps vs 작성목적) 여러
        후보 키를 순서대로 시도해 처음 값이 있는 걸 쓴다."""
        for k in keys:
            v = d.get(k)
            if v:
                return str(v)
        return ""

    def _fetch_stat_explanation(
        self, org_id: Optional[str], tbl_id: Optional[str]
    ) -> Dict[str, Any]:
        """통계설명자료(작성목적/조사대상/조사항목 등) raw 조회.

        방어적 처리: get_stat_explanation은 이번에 새로 추가된 API라
        (구버전 KosisApiClient나 테스트용 stub에는 없을 수 있음), 메서드이
        없거나 호출 중 어떤 예외가 나도 랭킹 전체를 실패시키지 않고 빈
        dict로 조용히 폴백한다 - 이건 랭킹 품질을 높이는 보조 신호이지,
        없으면 안 되는 필수 경로가 아니다.
        """
        try:
            return self.kosis.get_stat_explanation(org_id, tbl_id) or {}
        except Exception:
            return {}

    def _stat_explanation_text(
        self, cand: Dict[str, Any], expl: Dict[str, Any]
    ) -> str:
        """이미 조회해둔 통계설명자료(expl)와 후보 표 정보(cand)를 합쳐,
        랭킹 프롬프트에 넣을 짧은 설명 텍스트를 만든다. expl은 같은
        조사(STAT_ID)에 속한 표끼리 공유되는 survey-level 정보라 호출부가
        캐싱해서 넘겨준다 - 여기서는 표 제목(TBL_NM, 표마다 다름)과 합쳐
        매번 새로 문자열을 만든다.
        """
        if not expl:
            return f"{cand.get('TBL_NM', '')} - {str(cand.get('CONTENTS', ''))[:200]}"
        purpose = self._first_present(
            expl, ("WRITING_PURPS", "writingPurps", "작성목적")
        )[:150]
        scope = self._first_present(
            expl, ("EXAMIN_OBJRANGE", "examinObjrange", "조사대상")
        )[:200]
        items = self._first_present(
            expl, ("JOSA_ITM", "josaItm", "조사항목")
        )[:100]
        parts = [p for p in (purpose, scope, items) if p]
        if not parts:
            return f"{cand.get('TBL_NM', '')} - {str(cand.get('CONTENTS', ''))[:200]}"
        return f"{cand.get('TBL_NM', '')} - " + " / ".join(parts)

    def _llm_rank_table_candidates(
        self,
        indicator: str,
        candidates: List[Dict[str, Any]],
        explain_top_n: int = 8,
    ) -> List[Dict[str, Any]]:
        """통계표 후보들을 검색 API가 준 순서 대신, 실제 내용 기반 관련도
        순으로 다시 정렬한다. 2단계로 진행한다.

        1차: search_metadata가 이미 공짜로 주는 짧은 CONTENTS로 빠르게
        1차 정렬한다 (추가 API 호출 없음).

        2차: 1차 결과 상위 explain_top_n개에 한해서만, 훨씬 풍부한
        통계설명자료(작성목적/조사대상범위/조사항목 - get_stat_explanation,
        2026-07 MCP로 실측 검증)를 실제로 가져와서 그 텍스트로 다시 한 번
        재정렬한다. 표 제목이나 짧은 CONTENTS만 봐서는 "이 조사 안에 찾는
        개념이 있을 법한지" 판단이 잘 안 서는 경우가 많다 - 예를 들어
        "정비사"는 "항공산업실태조사"라는 표 제목 어디에도 없지만, 그
        조사의 조사대상범위 설명에는 "조종사, 승무원, 정비" 등이 명시돼
        있다. 다만 이 조회는 표마다 API 호출이 하나씩 늘어나므로, 후보
        전체가 아니라 1차에서 이미 유망하다고 판단된 상위 몇 개로 제한한다
        (같은 조사(STAT_ID)에 속한 표는 통계설명자료가 동일하므로
        STAT_ID 단위로 캐싱해 중복 호출도 피한다).
        """
        if len(candidates) <= 1:
            return candidates

        quick_ranked = self._rank_candidates_by_text(
            indicator,
            candidates,
            lambda c: f"{c.get('TBL_NM', '')} - {str(c.get('CONTENTS', ''))[:200]}",
        )
        logger.info(
            "  └─ [통계표 관련도 재정렬 - 1차/CONTENTS] "
            + " -> ".join(c.get("TBL_NM", "") for c in quick_ranked[:5])
        )

        top = quick_ranked[:explain_top_n]
        rest = quick_ranked[explain_top_n:]
        if len(top) <= 1:
            return quick_ranked

        # expl(raw 통계설명자료 dict)만 STAT_ID 단위로 캐싱한다 - 이건
        # 같은 조사에 속한 표끼리 진짜로 동일한 내용이다. 최종 텍스트는
        # 표마다 TBL_NM이 다르므로 캐시된 expl + 표 정보를 매번 새로
        # 조합해서 만든다 (캐시 키가 겹친다고 다른 표의 제목까지 재사용
        # 하는 사고를 방지).
        expl_cache: Dict[str, Dict[str, Any]] = {}

        def cached_text(c: Dict[str, Any]) -> str:
            key = c.get("STAT_ID") or f"{c.get('ORG_ID')}_{c.get('TBL_ID')}"
            if key not in expl_cache:
                expl_cache[key] = self._fetch_stat_explanation(
                    c.get("ORG_ID"), c.get("TBL_ID")
                )
            return self._stat_explanation_text(c, expl_cache[key])

        rich_ranked = self._rank_candidates_by_text(indicator, top, cached_text)
        final_ranked = rich_ranked + rest
        logger.info(
            "  └─ [통계표 관련도 재정렬 - 2차/통계설명자료] "
            + " -> ".join(c.get("TBL_NM", "") for c in final_ranked[:5])
        )
        return final_ranked


    def verify_table_candidates_by_meta(
        self,
        indicator: str,
        table_candidates: List[Dict[str, Any]],
        max_tables: int = 8,
    ) -> List[Dict[str, Any]]:
        """랭킹된 통계표 후보 상위 max_tables개에 대해, 제목/설명 텍스트가
        아니라 실제 ITM/OBJ 메타를 조회해서 찾는 개념이 진짜 컬럼(또는
        분류값)으로 존재하는지 하나하나 확인한다.

        이 메서드가 생긴 배경: "청년 실업률" 실측 사례에서, 정답 표
        (DT_1DA7002S "연령별 경제활동인구 총괄")는 제목에 "청년"도
        "실업률"도 글자 그대로 없어서 텍스트 기반 랭킹에서 낮은 순위로
        밀렸고, 대신 제목에 "청년실업률"이 그대로 박힌 다른 표(시도별
        분기 통계, 월간 데이터 자체가 없음)가 1위로 잘못 뽑혔다. 반면
        MCP로 직접 확인할 때는 표 제목만 보고 판단한 게 아니라, 후보
        표들의 실제 type=ITM 메타를 하나씩 열어봐서 "실업률" 항목과
        "15~29세" 분류값이 실제로 존재하는 표를 찾아냈다 - 이 메서드는
        그 수동 확인 과정을 코드로 옮긴 것이다.

        값(kosis_get_data)까지는 조회하지 않는다 - 시점(연/월)을 아직
        모르는 이른 대화 단계(예: "청년 실업률"이라고만 말하고 아직
        언제인지 안 말한 경우)에도 쓸 수 있어야 하기 때문이다. 시점과
        주장 수치까지 이미 알고 있는 경우는 이보다 더 강한 증거인
        gather_candidate_values(실제 값까지 비교)를 우선 쓴다 - 이
        메서드는 그게 불가능할 때의 차선책이다.

        반환값: matched=True이고 candidates가 비어있는(모호하지 않은)
        원본 후보 dict 리스트. 순서는 입력 순서(이미 랭킹된 순서)를
        유지한다.
        """
        verified: List[Dict[str, Any]] = []
        for cand in table_candidates[:max_tables]:
            org_id = cand.get("ORG_ID")
            tbl_id = cand.get("TBL_ID")
            if not org_id or not tbl_id:
                continue
            item_info = self.resolve_target_item(org_id, tbl_id, indicator)
            if item_info.get("matched") and not item_info.get("candidates"):
                verified.append(cand)
                logger.debug(
                    "  └─ [실제 메타 검증 성공] "
                    f"'{cand.get('TBL_NM')}' 표에 '{indicator}' 관련 컬럼"
                    f" 확인됨: '{item_info.get('breadcrumb') or item_info.get('itm_nm')}'"
                )
        return verified

    def gather_candidate_values(
        self,
        indicator: str,
        table_candidates: List[Dict[str, Any]],
        start_period: str,
        end_period: str,
        prd_se: str,
        category_hint: Optional[str],
        target_period: str,
        max_tables: int = 5,
    ) -> List[Dict[str, Any]]:
        """랭킹된 통계표 후보 상위 max_tables개에 대해 실제로 컬럼을
        확정하고 값까지 조회해서 (표, 항목, 값) 후보 리스트를 만든다.

        기존에는 "가장 관련도 높은 첫 번째 표"에서 컬럼 매칭에 성공하면
        바로 그 표로 확정했다. 그런데 "정비사"처럼 같은 개념이 여러 표에
        서로 다른 형태로 걸쳐 있을 수 있다(산업분류 "정비업" vs 직무
        "항공기 정비" 등, 2026-07 실측 사례) - 이 경우 후보 표 각각의 실제
        수치를 다 조회해봐야 어느 해석이 맞는지 판단할 수 있다. 이 메서드는
        MCP로 여러 표를 직접 조회하며 비교했던 수동 과정을 그대로 코드로
        옮긴 것이다. 후보 표 개수만큼 API 호출이 늘어나므로 상위
        max_tables개로 제한한다. 컬럼 매칭에 실패했거나(matched=False)
        모호한(candidates가 남은) 표는 자동 비교 대상에서 제외한다 - 이건
        사람에게 되물어야 할 케이스지, 자동으로 넘겨짚을 케이스가 아니다.
        """
        results: List[Dict[str, Any]] = []
        for cand in table_candidates[:max_tables]:
            org_id = cand.get("ORG_ID")
            tbl_id = cand.get("TBL_ID")
            tbl_nm = cand.get("TBL_NM")
            if not org_id or not tbl_id:
                continue

            item_info = self.resolve_target_item(org_id, tbl_id, indicator)
            if not item_info.get("matched") or item_info.get("candidates"):
                continue

            # category_hint("300인 이상" 등, 혹은 여러 축을 동시에 지정하는
            # 리스트)도 이 표 기준으로 축을 확인해서 같이 걸어준다 - 그래야
            # "정비업 300인 이상" 같은 조건도, "전국+종합"처럼 축이 여러
            # 개인 표도 표마다 정확하게 비교된다.
            extra_obj_axes = self.resolve_category_hints_axes(
                org_id, tbl_id, category_hint, exclude_axis=item_info.get("obj_axis")
            )

            fetch_res = self.fetch_kosis_data_range(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                itm_id=item_info["itm_id"],
                itm_nm=item_info.get("itm_nm"),
                indicator=indicator,
                start_period=start_period,
                end_period=end_period,
                prd_se=prd_se,
                category_hint=category_hint,
                obj_axis=item_info.get("obj_axis"),
                obj_code=item_info.get("obj_code"),
                extra_obj_axes=extra_obj_axes,
            )
            if not fetch_res.get("success"):
                continue

            rec = fetch_res.get("yearly_records", {}).get(target_period)
            if not rec:
                continue

            value_raw = rec.get("value")
            m = re.search(r"([-–]?[\d,]+(?:\.\d+)?)", str(value_raw))
            value_num = (
                float(m.group(1).replace(",", "").replace("–", "-"))
                if m else None
            )
            cand_unit = rec.get("unit")

            # [단위 불일치 가드] gather_item_candidate_values_in_table과
            # 동일한 이유 - 표 단위가 주장 문장의 단위와 종류가 다르면(예:
            # 사람 수를 물었는데 "개"/"원") 숫자가 가까워도 비교 대상에서
            # 제외한다.
            claimed_unit = self.slots.get("claimed_unit")
            if not self._unit_compatible(claimed_unit, cand_unit):
                logger.debug(
                    f"  └─ [단위 불일치 제외] '{tbl_nm}'/"
                    f"'{item_info.get('itm_nm')}'(단위: '{cand_unit}') - 주장"
                    f" 단위 '{claimed_unit}'와 종류가 달라 비교 대상에서 제외"
                )
                continue

            results.append(
                {
                    "org_id": org_id,
                    "tbl_id": tbl_id,
                    "tbl_nm": tbl_nm,
                    "itm_id": item_info["itm_id"],
                    "itm_nm": item_info.get("itm_nm"),
                    "breadcrumb": item_info.get("breadcrumb"),
                    "obj_axis": item_info.get("obj_axis"),
                    "obj_code": item_info.get("obj_code"),
                    "extra_obj_axes": extra_obj_axes,
                    "value_raw": value_raw,
                    "value_num": value_num,
                    "unit": cand_unit,
                }
            )
        logger.info(
            f"  └─ [다중 표 비교] '{indicator}' 상위 {max_tables}개 표 중"
            f" {len(results)}개에서 값 조회 성공: "
            + ", ".join(
                f"{r['tbl_nm']}/{r['itm_nm']}={r['value_raw']}" for r in results
            )
        )
        return results

    @staticmethod
    def _claim_value_matches(
        value_num: Optional[float],
        claimed_value: float,
        raw_text: str = "",
        precision: Optional[float] = None,
    ) -> bool:
        """조회값과 주장값이 "같은 수치"인지 엄격하게 판단한다.

        [2026-07 변경 - 엄격 검증으로 전환] 예전엔 1%(상대오차)/0.15(절대오차)
        중 더 관대한 쪽을 허용오차로 썼는데, 이러면 실제로는 다른 값인데도
        "대충 비슷하다"는 이유로 VERIFIED가 나와버리는 문제가 있었다(실측:
        기사가 "1만9449건"이라고 썼는데 KOSIS 실제값은 19442건으로 서로
        다른 수치인데도 옛 허용오차(1%=194)로는 통과했을 것; "0.77명"과
        실제 "0.78명"도 마찬가지). 사실확인 도구는 "비슷하면 맞다"가 아니라
        "그 값 그대로다"를 검증해야 하므로, 이제는 기사 원문에 적힌 소수
        자릿수 기준 반올림 오차(그 자리에서 반올림/버림 차이 정도)만
        허용한다 - 예: "0.77명"이면 ±0.005까지만, "1만9449건"처럼 정수면
        ±0.5까지만 허용. 자릿수 다른 실제 값 차이는 이제 불일치로 잡힌다.

        [2026-07 추가 - 팀원 골든셋 재실행 중 발견한 회귀 수정] 위 로직은
        raw_text를 다시 정규식으로 파싱해 "소수점 자릿수"만 봤는데, 이건
        "4156억"/"2,916만" 처럼 조/억/만 배율이 붙은 큰수 표기에서는 완전히
        틀린 정밀도를 계산한다 - raw_text의 표면 자릿수("4156")는 소수점이
        0자리라 허용오차가 ±0.5로 계산되지만, 실제 비교 대상은 절대값
        415,600,000,000이라 KOSIS 원본과는 억 단위 반올림 오차(수백만~수천만
        차이)가 정상적으로 존재한다 - 그 결과 외환보유액/수출액처럼 정확히
        맞는 값들까지 전부 불일치로 나오는 실사용 회귀가 있었다. 이제
        extract_all_claims가 자릿수 배율을 알고 있는 시점에 정밀한 허용오차
        (precision)를 직접 계산해 claim에 실어보내고, 여기서는 그 값이
        있으면 그대로 쓴다(raw_text 재파싱은 precision이 없는 옛 claim과의
        하위호환용 폴백으로만 남긴다).
        """
        if value_num is None:
            return False
        if precision is not None:
            tolerance = precision
        else:
            m = re.search(r"\d+(?:\.(\d+))?", raw_text or "")
            decimals = len(m.group(1)) if (m and m.group(1)) else 0
            tolerance = 0.5 * (10 ** -decimals)
        diff = abs(value_num - claimed_value)
        return diff <= tolerance

    def score_candidate_against_claims(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        indicator: str,
        item_info: Dict[str, Any],
        claims: List[Dict[str, Any]],
        category_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """이 표/컬럼이 문장 안의 여러 주장 수치와 동시에 얼마나 맞아떨어지는지
        점수를 매긴다.

        배경: 이번 세션에서 실제로 MCP를 손으로 써서 "청년 실업률" 기사를
        검증할 때 썼던 방법 - 숫자 하나만 맞춰보는 게 아니라, 문단에 있는
        여러 시점의 숫자(7.5%, 2021년 6월 8.9%, 2021년 3월 10%, 전체
        실업률 3.1% 등)가 같은 표/같은 컬럼에서 동시에 다 맞아떨어지는지
        확인했다. 우연히 숫자 하나가 비슷할 확률보다 여러 개가 한꺼번에
        맞을 확률이 훨씬 낮으므로, 이 매칭 개수를 "이 표가 진짜 맞는
        표"라는 확신의 근거로 쓸 수 있다 (기존 pick_best_matching_candidate는
        주장 값이 정확히 하나뿐일 때만 쓸 수 있었던 것의 확장판).

        claims는 extract_all_claims()가 반환한 형식 - period가 있는
        주장(예: "2021년 6월(8.9%)")은 그 시점 값과 정확히 대조하고,
        period가 없는 주장(예: 문장 어순상 시점을 특정하지 못한 "7.5%")은
        조회 범위 전체에서 값이 일치하는 시점이 하나라도 있는지로 대조한다
        (완전한 검증은 아니지만, 시점 앵커가 없어도 최소한의 신호는 준다).

        반환: {"matched": int, "total": int, "details": [...],
               "coverage_prd_se": "M"|"Y"|None}
        period 정보가 있는 주장이 하나도 없으면 조회 범위를 정할 수 없어
        바로 matched=0으로 반환한다(추측으로 임의 범위를 조회하지 않음).
        """
        result: Dict[str, Any] = {
            "matched": 0,
            "total": len(claims),
            "details": [],
            "coverage_prd_se": None,
        }
        if not claims:
            return result

        periods_with_date = [c["period"] for c in claims if c.get("period")]
        if not periods_with_date:
            result["details"] = [
                {"claim": c, "matched": False, "reason": "기간 정보 없음"}
                for c in claims
            ]
            return result

        # 주기 판단: YYYYMM(6자리)이 하나라도 있으면 월간, 없고 YYYYN(5자리,
        # 분기 - kosis_table_info 실측 포맷)이 있으면 분기, 나머지는 연간.
        if any(len(p) == 6 for p in periods_with_date):
            prd_se = "M"
        elif any(len(p) == 5 for p in periods_with_date):
            prd_se = "Q"
        else:
            prd_se = "Y"
        start_period = min(periods_with_date)
        end_period = max(periods_with_date)
        if prd_se == "M":
            start_period = start_period if len(start_period) == 6 else start_period + "01"
            end_period = end_period if len(end_period) == 6 else end_period + "12"
        elif prd_se == "Q":
            # 연 단위(YYYY)로만 채워진 주장이 분기 주장과 섞여 있으면
            # 그 해의 1~4분기 전체를 범위로 잡는다("작년"만 있고 몇 분기
            # 인지 모르는 주장도 최소한 그 해 범위 안에서는 대조 시도).
            start_period = start_period if len(start_period) == 5 else start_period + "1"
            end_period = end_period if len(end_period) == 5 else end_period + "4"
        result["coverage_prd_se"] = prd_se

        extra_obj_axes = self.resolve_category_hints_axes(
            org_id, tbl_id, category_hint, exclude_axis=item_info.get("obj_axis")
        )

        fetch_res = self.fetch_kosis_data_range(
            org_id=org_id,
            tbl_id=tbl_id,
            tbl_nm=tbl_nm,
            itm_id=item_info.get("itm_id"),
            itm_nm=item_info.get("itm_nm"),
            indicator=indicator,
            start_period=start_period,
            end_period=end_period,
            prd_se=prd_se,
            category_hint=category_hint,
            obj_axis=item_info.get("obj_axis"),
            obj_code=item_info.get("obj_code"),
            extra_obj_axes=extra_obj_axes,
        )
        if not fetch_res.get("success"):
            result["details"] = [
                {"claim": c, "matched": False, "reason": "조회 실패"} for c in claims
            ]
            return result

        records = fetch_res.get("yearly_records", {})

        def _value_num(rec: Dict[str, Any]) -> Optional[float]:
            # [2026-07-24] 음수 부호(-/–)를 못 잡는 정규식이었던 버그 수정 -
            # KOSIS API가 "-0.2" 같은 음수 성장률을 반환해도 부호 없는
            # [\d,]+ 클래스로는 "0.2"만 매치되어 부호가 사라졌다.
            m = re.search(r"([-–]?[\d,]+(?:\.\d+)?)", str(rec.get("value")))
            if not m:
                return None
            raw = float(m.group(1).replace(",", "").replace("–", "-"))
            # [2026-07 추가] "십억원"/"천명"처럼 축척 붙은 단위는 raw 값을
            # 절대값으로 환산해야 (항상 절대값으로 추출되는) claim과 비교가
            # 성립한다 - 자세한 배경은 _unit_scale_multiplier 주석 참고.
            return raw * self._unit_scale_multiplier(rec.get("unit"))

        claimed_unit_global = self.slots.get("claimed_unit") if hasattr(self, "slots") else None

        for c in claims:
            claim_unit = c.get("unit") or claimed_unit_global
            period = c.get("period")
            matched = False
            found_value, found_period = None, None
            # [2026-07 추가 - 엄격 검증용 관측값] 엄격한 허용오차 때문에
            # matched=False가 나와도, 사용자가 "얼마나 차이가 나서 불일치로
            # 판정됐는지" 눈으로 확인할 수 있어야 한다(반올림/차수 오탐인지
            # 진짜 다른 값인지 구분하는 데 필요). matched 여부와 무관하게
            # 그 시점에 실제로 조회된 값을 별도로 남긴다.
            closest_value, closest_period = None, None

            if period and len(period) in (5, 6):
                # 6자리(YYYYMM, 월간) 또는 5자리(YYYYN, 분기 - kosis_table_info
                # 실측 포맷) 둘 다 records 딕셔너리 키와 정확히 일치하는
                # 단일 시점 조회라 같은 분기(elif가 아니라 하나로 묶음)로
                # 처리한다.
                rec = records.get(period)
                if rec is not None:
                    cand_unit = rec.get("unit")
                    if self._unit_compatible(claim_unit, cand_unit):
                        v = _value_num(rec)
                        closest_value, closest_period = v, period
                        if self._claim_value_matches(v, c["value"], c.get("raw_text", ""), c.get("precision")):
                            matched, found_value, found_period = True, v, period
            elif period and len(period) == 4:
                # 연도만 있는 주장(예: 조회가 연간 표) - 해당 연도로 시작하는
                # 아무 조회 시점이나 값이 맞으면 매칭으로 인정한다.
                # [2026-07 추가] 월간(len==6) 분기에만 있던 closest_value
                # 추적이 이 분기엔 없어서, GDP(연간 표)처럼 이 분기를 타는
                # 지표는 matched=False일 때 "그 시점에 실제로 뭐가 조회
                # 됐는지"를 사람이 전혀 볼 수 없는 사각지대가 있었다(스트레스
                # 테스트로 발견 - closest_value가 항상 None으로만 나옴).
                # 단위가 안 맞아 continue로 건너뛴 것과, 단위는 맞는데
                # 값만 다른 것을 구분할 수 있게 첫 번째로 발견되는 "단위
                # 호환되는" 후보를 closest로 남긴다.
                for p, rec in records.items():
                    if not str(p).startswith(period):
                        continue
                    cand_unit = rec.get("unit")
                    if not self._unit_compatible(claim_unit, cand_unit):
                        continue
                    v = _value_num(rec)
                    if closest_value is None:
                        closest_value, closest_period = v, p
                    if self._claim_value_matches(v, c["value"], c.get("raw_text", ""), c.get("precision")):
                        matched, found_value, found_period = True, v, p
                        break
            else:
                # 시점 앵커가 없는 주장 - 조회된 범위 전체에서 값이 맞는
                # 시점이 하나라도 있는지 본다. (위와 동일한 이유로
                # closest_value를 함께 남긴다.)
                for p, rec in records.items():
                    cand_unit = rec.get("unit")
                    if not self._unit_compatible(claim_unit, cand_unit):
                        continue
                    v = _value_num(rec)
                    if closest_value is None:
                        closest_value, closest_period = v, p
                    if self._claim_value_matches(v, c["value"], c.get("raw_text", ""), c.get("precision")):
                        matched, found_value, found_period = True, v, p
                        break

            if matched:
                result["matched"] += 1
            result["details"].append(
                {
                    "claim": c,
                    "matched": matched,
                    "found_value": found_value,
                    "found_period": found_period,
                    # matched=False여도 그 시점에 실제로 조회된 값(있다면) -
                    # 반올림/차수 정도 차이인지 완전히 다른 값인지 사람이
                    # 눈으로 바로 판단할 수 있게 한다.
                    "closest_value": closest_value,
                    "closest_period": closest_period,
                }
            )

        logger.info(
            f"  └─ [다중 주장 대조] '{tbl_nm}'/'{item_info.get('itm_nm')}':"
            f" {result['matched']}/{result['total']}개 주장 일치"
        )
        return result

    def score_diff_claim(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        indicator: str,
        item_info: Dict[str, Any],
        claim: Dict[str, Any],
        category_hint: Optional[Union[str, List[str]]],
        reference_date: "Optional[Union[str, Any]]" = None,
    ) -> Dict[str, Any]:
        """[2026-07-24 추가 - #54 두 시점 diff claim 평가] "쉬었음 인구가
        1년 새 7만명 넘게 늘어난 것으로 나타났다"류 claim을 평가한다.

        Decision 003(manual_diff 미채택)에서 금지한 것과는 다른 문제다.
        Decision 003은 "공식 등락률 컬럼이 없을 때 우리가 임의로 계산법을
        추측해서 VERIFIED로 취급"하는 걸 막은 것이었다(어떤 시리즈/계산법이
        기자 의도와 맞는지 확신할 수 없어서). 여기서는 claim 문장 자체가
        이미 "N년 전 대비 이만큼 변화"라는 두 시점과 연산(뺄셈)을 명시하고
        있다 - 그 두 시점 각각의 공식 확정값을 그대로 가져와서 claim이
        말한 조건이 맞는지 그대로 검증할 뿐, 계산법을 추측하지 않는다.

        "쉬었음" 같은 특수 조사는 매달이 아니라 특정월(5월/8월 등)에만
        공표되므로, 정확한 월을 추측하지 않고 넓은 범위(diff_years+1년
        여유)를 조회한 뒤 실제로 데이터가 존재하는 시점들 중
        diff_years년 차이가 나는 쌍을 찾는다 - 가장 최근에 공표된 쌍을
        우선 시도한다.
        """
        result: Dict[str, Any] = {"matched": False, "total": 1, "details": []}
        diff_years = claim.get("diff_years")
        direction = claim.get("diff_direction")
        if not diff_years or not reference_date:
            result["details"] = [{
                "claim": claim, "matched": False,
                "found_value": None, "found_period": None,
                "closest_value": None, "closest_period": None,
                "reason": "diff 평가에 필요한 기준일/기간 정보가 없습니다",
            }]
            return result

        if isinstance(reference_date, str):
            try:
                ref = date.fromisoformat(str(reference_date)[:10])
            except ValueError:
                result["details"] = [{
                    "claim": claim, "matched": False,
                    "found_value": None, "found_period": None,
                    "closest_value": None, "closest_period": None,
                    "reason": "reference_date 형식을 해석할 수 없습니다",
                }]
                return result
        else:
            ref = reference_date

        # [2026-07-24 버그 수정] end_period를 무조건 "{ref.year}12"로 잡으면
        # KOSIS는 라이브 DB라 실행 시점(오늘 날짜) 기준으로 기사 게재일
        # 이후의 미래 데이터까지 내려줄 수 있다 - 기사가 발표 당시 알 수
        # 없었던 미래 시점과 비교하면 인과관계가 깨진다(실측: reference_date
        # 2025-11-05인데 202512 시점이 "later"로 채택된 사고). 조회 범위와
        # 후보 선정 둘 다 reference_date(YYYYMM)를 넘지 않도록 못박는다.
        ref_period = f"{ref.year}{ref.month:02d}"
        start_year = ref.year - diff_years - 1
        end_year = ref.year

        extra_obj_axes = self.resolve_category_hints_axes(
            org_id, tbl_id, category_hint, exclude_axis=item_info.get("obj_axis")
        )
        fetch_res = self.fetch_kosis_data_range(
            org_id=org_id,
            tbl_id=tbl_id,
            tbl_nm=tbl_nm,
            itm_id=item_info.get("itm_id"),
            itm_nm=item_info.get("itm_nm"),
            indicator=indicator,
            start_period=f"{start_year}01",
            end_period=min(f"{end_year}12", ref_period),
            prd_se="M",
            category_hint=category_hint,
            obj_axis=item_info.get("obj_axis"),
            obj_code=item_info.get("obj_code"),
            extra_obj_axes=extra_obj_axes,
        )
        if not fetch_res.get("success"):
            result["details"] = [{
                "claim": claim, "matched": False,
                "found_value": None, "found_period": None,
                "closest_value": None, "closest_period": None,
                "reason": "조회 실패",
            }]
            return result

        records = fetch_res.get("yearly_records", {})

        def _value_num(rec: Dict[str, Any]) -> Optional[float]:
            m = re.search(r"([-–]?[\d,]+(?:\.\d+)?)", str(rec.get("value")))
            if not m:
                return None
            raw = float(m.group(1).replace(",", "").replace("–", "-"))
            return raw * self._unit_scale_multiplier(rec.get("unit"))

        # 실제로 공표된(=records에 값이 존재하는) 시점만 최신순으로 훑어,
        # 정확히 diff_years년 전(같은 달)에도 데이터가 있는 쌍을 찾는다 -
        # 특수 조사(예: 5월/8월만 공표)의 실제 공표 스케줄을 추측하지 않고
        # 조회된 사실 그대로 판단한다.
        candidate_pairs = []
        for later in sorted(records.keys(), reverse=True):
            if len(later) != 6:
                continue
            # [2026-07-24] fetch_kosis_data_range가 요청 범위를 넘는 값을
            # 돌려줄 가능성에 대비한 이중 방어 - 기사 게재일 이후 시점은
            # 절대 "later" 후보로 쓰지 않는다(인과관계 보호).
            if later > ref_period:
                continue
            later_year, later_month = int(later[:4]), later[4:6]
            earlier = f"{later_year - diff_years}{later_month}"
            if earlier not in records:
                continue
            later_rec, earlier_rec = records[later], records[earlier]
            claim_unit = claim.get("unit")
            if not self._unit_compatible(
                claim_unit, later_rec.get("unit")
            ) or not self._unit_compatible(claim_unit, earlier_rec.get("unit")):
                continue
            later_v, earlier_v = _value_num(later_rec), _value_num(earlier_rec)
            if later_v is None or earlier_v is None:
                continue
            diff = later_v - earlier_v
            if direction == "decrease":
                diff = -diff
            candidate_pairs.append((later, earlier, diff))

        for later, earlier, diff in candidate_pairs:
            if self._claim_value_matches(
                diff, claim["value"], claim.get("raw_text", ""),
                claim.get("precision"),
            ):
                result["matched"] = True
                result["details"] = [{
                    "claim": claim, "matched": True,
                    "found_value": diff, "found_period": f"{earlier}~{later}",
                    "closest_value": diff, "closest_period": f"{earlier}~{later}",
                }]
                logger.info(
                    f"  └─ [diff 주장 대조] '{tbl_nm}'/'{item_info.get('itm_nm')}':"
                    f" {earlier}->{later} 차이={diff} claim={claim['value']} 일치"
                )
                return result

        if candidate_pairs:
            later, earlier, diff = candidate_pairs[0]
            result["details"] = [{
                "claim": claim, "matched": False,
                "found_value": None, "found_period": None,
                "closest_value": diff, "closest_period": f"{earlier}~{later}",
            }]
        else:
            result["details"] = [{
                "claim": claim, "matched": False,
                "found_value": None, "found_period": None,
                "closest_value": None, "closest_period": None,
                "reason": f"{diff_years}년 차이가 나는 공표 시점 쌍을 찾지 못했습니다",
            }]
        logger.info(
            f"  └─ [diff 주장 대조] '{tbl_nm}'/'{item_info.get('itm_nm')}':"
            f" claim={claim.get('raw_text')} 불일치"
        )
        return result

    def score_month_as_item_claims(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        claims: List[Dict[str, Any]],
        month_item_names: Dict[str, str],
        cause_category_hint: Optional[Union[str, List[str]]] = None,
    ) -> Dict[str, Any]:
        """"항목(ITM)축이 사실은 '월'을 나타내는" 표 전용 대조 로직
        (예: 화재발생현황 DT_15601N_001 - 시점(PRD_DE)은 연도 단위이고,
        그 대신 "항목" 축이 1월~12월+합계로 나뉘어 있다).

        일반 score_candidate_against_claims는 "표 하나를 기간 범위로 한
        번에 조회 -> 여러 시점 값을 한 번에 비교"하는 구조인데, 이 표는
        반대로 "연도(시점)마다 조회할 itmId(월)가 매번 달라야" 그 달의
        값이 나온다 - 주장 하나당 (연도, itmId) 조합이 다 다를 수 있어서
        범위 조회 한 번으로 묶을 수 없다. 그래서 이런 표는 지표별로
        DEFAULT_INDICATOR_METADATA에 month_as_item(월->항목명 매핑)을
        선언해두고, 이 메서드가 주장마다 개별적으로 그 달에 해당하는
        itm_nm으로 따로 조회해서 비교한다.

        month_item_names: {"01": "1월", ..., "12": "12월", "total": "합계"}
        형태 - 실제 ITM_NM 문자열 그대로 줘야 _select_target_row의 itm_nm
        정확 매칭에 그대로 먹힌다.
        cause_category_hint: "계"처럼 항목축과 별개인 다른 분류축(예:
        발화요인별)에서 총계 행을 pin해야 할 때 쓴다 - resolve_category_hints_axes로
        정확한 이름을 exact match해서 서버 필터로 건다(부분 문자열 매칭은
        "기계적요인"처럼 힌트 글자가 우연히 다른 카테고리명에 섞여 있을
        위험이 있어 피한다).
        """
        result: Dict[str, Any] = {
            "matched": 0,
            "total": len(claims),
            "details": [],
            "coverage_prd_se": "Y",
        }
        if not claims:
            return result

        extra_obj_axes: Dict[int, str] = {}
        if cause_category_hint:
            extra_obj_axes = self.resolve_category_hints_axes(
                org_id, tbl_id, cause_category_hint
            )

        for c in claims:
            period = c.get("period")
            detail: Dict[str, Any] = {
                "claim": c,
                "matched": False,
                "found_value": None,
                "found_period": None,
                "closest_value": None,
                "closest_period": None,
            }
            if not period:
                detail["reason"] = "기간 정보 없음"
                result["details"].append(detail)
                continue

            year = period[:4]
            month_key = period[4:6] if len(period) == 6 else "total"
            itm_nm = month_item_names.get(month_key)
            if not itm_nm:
                detail["reason"] = f"'{month_key}'에 대응하는 항목명 매핑 없음"
                result["details"].append(detail)
                continue

            fetch_res = self.fetch_kosis_data_range(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                itm_id="all",
                itm_nm=itm_nm,
                indicator="__month_as_item__",
                start_period=year,
                end_period=year,
                prd_se="Y",
                category_hint=cause_category_hint,
                extra_obj_axes=extra_obj_axes,
            )
            if fetch_res.get("success"):
                rec = fetch_res.get("yearly_records", {}).get(year)
                if rec is not None:
                    m = re.search(
                        r"([-–]?[\d,]+(?:\.\d+)?)", str(rec.get("value"))
                    )
                    if m:
                        v = float(
                            m.group(1).replace(",", "").replace("–", "-")
                        ) * (
                            self._unit_scale_multiplier(rec.get("unit"))
                        )
                        detail["closest_value"] = v
                        detail["closest_period"] = period
                        if self._claim_value_matches(v, c["value"], c.get("raw_text", ""), c.get("precision")):
                            detail["matched"] = True
                            detail["found_value"] = v
                            detail["found_period"] = period

            if detail["matched"]:
                result["matched"] += 1
            result["details"].append(detail)

        logger.info(
            f"  └─ [월=항목축 다중 주장 대조] '{tbl_nm}':"
            f" {result['matched']}/{result['total']}개 주장 일치"
        )
        return result

    @staticmethod
    def pick_best_matching_candidate(
        candidates: List[Dict[str, Any]], claimed_value: Optional[float]
    ) -> Optional[Dict[str, Any]]:
        """claimed_value(기사/주장이 인용한 수치)와 가장 가까운 후보를 고른다.

        이 비교는 "여러 표 중 어느 게 주장과 그나마 가장 가까운가"를 골라
        보여주는 참고용 최종 판단일 뿐이다 - 일치하지 않더라도 자동으로
        "거짓"이라고 확정하지 않는다. 그 해석/판단은 여전히 대화형으로
        사용자에게 값 차이와 함께 안내된다 (process_turn의 최종 응답 생성
        단계에서).
        """
        if not candidates:
            return None
        if claimed_value is None:
            return candidates[0]  # 비교 기준이 없으면 관련도 랭킹 1위 채택

        def _diff(c: Dict[str, Any]) -> float:
            if c.get("value_num") is None:
                return float("inf")
            return abs(c["value_num"] - claimed_value)

        return min(candidates, key=_diff)


    def _resolve_leaf_row(
        self, category_rows: List[Dict[str, Any]], row: Dict[str, Any]
    ) -> Dict[str, Any]:
        """row가 하위 분류값(UP_ITM_ID로 연결된 자식)이 있는 헤더/그룹
        노드면, 실제 조회 가능한 leaf(말단) 코드로 바꿔서 반환한다.

        실측 사례(DT_444002_N2023A006): "항공기 정비"(B06) 자체는 헤더
        노드일 뿐이고, 실제 조회 가능한 값은 그 자식인 "합계"(B0603,
        소계에 해당)에만 있다. objL2=B06으로 그대로 조회하면 KOSIS가
        err:30("데이터가 존재하지 않습니다")을 돌려준다 - LLM/fuzzy
        매칭은 "정비사"와 의미가 가장 가까운 헤더 이름("항공기 정비")을
        먼저 고르기 마련이라, 이 보정 없이는 항상 빈 응답만 받게 된다.
        자식 중 "합계"/"소계"/"전체"를 우선하고(성별처럼 하위분류가
        갈릴 때 헤드라인 수치인 경우가 많다는 게 _select_target_row의
        기존 "합계 우선" 로직과 같은 이유), 그런 이름의 자식이 없거나
        자식이 아예 없으면(이미 leaf) 원래 행을 그대로 쓴다(2026-08-10
        변경 - 아래 참고, 예전엔 이 경우 "첫 자식"으로 추측해서 대체했다).
        resolve_target_item의 컬럼 확정(to_candidate)과 resolve_category_
        hint_axis의 규모/분류 힌트 해석 양쪽에서 공통으로 쓴다.

        [2026-07 실측 수정] 관광객입국자수(DT_TRD_TGT_ENT_AGG_MONTH)에서
        이 가정이 틀린 사례가 나왔다: "국적별" 축의 "총계" 행(직접 조회
        가능한 진짜 값, MCP로 실측 확인)이 "UP_ITM_ID로 연결된 자식이
        있다"는 이유만으로 헤더로 오판됐는데, 그 유일한 자식도 이름이
        똑같이 "총계"였다(부모와 동일 이름의 중복 노드 - 실제 조회하면
        err:30). "항공기 정비"(부모) -> "합계"(자식, 이름이 다름) 같은
        진짜 헤더/leaf 관계와 달리, 자식 이름이 부모와 완전히 같으면
        그 자식은 진짜 하위 분류가 아니라 메타 데이터의 중복 표기일
        가능성이 높다 - 이 경우는 leaf 대체를 하지 않고 원래 행(부모)을
        그대로 쓴다.

        [2026-08-10 실측 수정] 전산업생산지수(DT_1JH20202)에서 반대 방향의
        오류가 발견됐다: "전산업생산지수"(헤더, 산업별 자식 = 농림어업/
        광공업/건설업/서비스업/공공행정)는 "합계"류 이름의 자식이 없어서
        예전 로직이 "첫 자식"(농림어업)으로 조용히 대체했는데, 실측 결과
        헤더 코드 자체("1")가 전체 산업 총지수 값을 직접 갖고 있어 원래
        조회가 가능했다(게다가 농림어업은 그 시점 자료가 아직 없어 이중
        으로 틀렸다 - 대체된 값도 못 가져오고, 가져왔어도 전산업이 아닌
        농림어업 하나였을 것). "첫 자식" 무조건 대체는 근거 없는 추측
        이었다고 판단해 제거했다 - 이제는 "합계/소계/전체" 라벨이 있을
        때만 명시적으로 대체하고, 없으면 원래 행(헤더 자체)을 그대로
        쓴다. 헤더가 정말 조회 불가능한 표라면(항공기 정비 사례처럼) 그건
        보통 "합계"류 자식이 있는 케이스라 위 분기에서 이미 잡히고, 그런
        자식도 없이 헤더 자체도 조회 불가능한 진짜 예외 상황이면 이후
        fetch 단계에서 명시적으로 err:30 실패가 나는 게 - 틀린 하위
        카테고리를 확신에 차서 반환하는 것보다 - 안전하다.
        """
        row_id = self._row_id(row)
        axis_sn = row.get("OBJ_ID_SN")
        children = [
            r
            for r in category_rows
            if r.get("UP_ITM_ID") == row_id and r.get("OBJ_ID_SN") == axis_sn
        ]
        if not children:
            return row
        row_name = self._row_name(row)
        if all(self._row_name(c) == row_name for c in children):
            logger.debug(
                f"  └─ [헤더->leaf 보정 생략] '{row_name}'의 자식이 전부"
                " 이름이 똑같은 중복 노드라 원래 행을 그대로 씁니다."
            )
            # [2026-08-10 실측 수정 - 문화산업 임금동향(DT_113_STBL_
            # 1031340) 사례] "부모/자식 이름이 같으면 무조건 부모(원래
            # 행)가 맞다"는 이 분기의 가정은 관광객입국자수("총계"/"총계")
            # 실측 사례에서 나왔는데, 같은 세션에서 정반대인 실측 사례가
            # 나왔다: "문화산업"(헤더, 코드 ...002) 자체는 실제로 조회하면
            # KOSIS err:30("데이터가 존재하지 않습니다")이고, 이름이 똑같은
            # 자식 "문화산업"(리프, 코드 ...002001)이 진짜 조회 가능한
            # 데이터를 갖고 있었다(H-4/H-5/분기축 실측으로 이미 검증된
            # 코드와 정확히 일치). 즉 "부모/자식 이름 동일"이라는 메타
            # 정보만으로는 이 표 하나로는 어느 쪽이 진짜 leaf인지 정적으로
            # 판별할 수 없다(두 실측 사례가 서로 반대 답을 요구함) -
            # 그래서 추측 대신, 원래 행(부모)을 그대로 쓰되 자식 하나를
            # "동일 이름 폴백"으로 함께 반환해둔다. 실제 조회 단계
            # (fetch_kosis_data_range)가 이 코드로 정말 err:30(데이터 0건)을
            # 받으면 그때 이 폴백으로 딱 한 번 다시 시도한다 - 추측이
            # 아니라 KOSIS의 실제 응답을 근거로 판단을 미루는 것이다
            # (Decision 003과 같은 원칙: 확실하지 않으면 정적으로 찍지
            # 않고, 확인 가능한 실제 신호가 생길 때까지 결정을 미룬다).
            if len(children) == 1:
                row = dict(row)
                row["_leaf_fallback_id"] = self._row_id(children[0])
            return row
        for label in ("합계", "소계", "전체"):
            preferred = next(
                (c for c in children if self._row_name(c) == label), None
            )
            if preferred:
                logger.info(
                    f"  └─ [헤더->leaf 보정] '{self._row_name(row)}'는 조회"
                    f" 불가능한 그룹 노드라 자식 '{label}'"
                    f"({self._row_id(preferred)})로 대체합니다."
                )
                return preferred
        # [2026-08-10 수정 - 실측(DT_1JH20202/전산업생산지수)으로 발견된
        # 오류] 예전엔 여기서 "첫 자식"으로 무조건 대체했다. 그런데
        # "합계"/"소계"/"전체"라는 이름의 자식이 없다고 해서 헤더 자체가
        # 항상 조회 불가능한 건 아니다 - 실측 결과, 이 표의 "전산업생산
        # 지수"(헤더, ITM_ID=1)는 산업별(농림어업/광공업/건설업/서비스업/
        # 공공행정) 자식들과 별개로 헤더 코드 "1" 자체가 전체 산업 총지수
        # 값을 갖고 있어 직접 조회가 됐다. 이 상태에서 예전 로직처럼 "첫
        # 자식"(농림어업)으로 조용히 대체하면, 사용자가 찾는 "전산업"(전체)
        # 대신 농림어업 하나만 가리키는 값을 확신에 차서 돌려주는 사고가
        # 난다(게다가 농림어업은 공표 시차 때문에 최신 월 자료가 아예 없어
        # err:30까지 났다 - 이중으로 틀린 값이 될 뻔했다).
        #
        # 그래서 이제는 "합계/소계/전체" 라벨이 있는 경우만 명시적으로
        # 대체하고, 없으면 추측해서 자식 하나를 찍지 않고 원래 행(헤더
        # 자체의 코드)을 그대로 쓴다(Decision 003: 확실하지 않으면
        # 추측하지 않는다). 헤더 자체가 정말로 조회 불가능한 표라면 이후
        # fetch 단계에서 err:30으로 명시적으로 실패하는 게, 틀린 하위
        # 카테고리를 조용히 반환하는 것보다 안전하다 - "항공기 정비"
        # 실측 사례는 애초에 "합계"라는 이름의 자식이 있었으므로 위
        # 분기에서 이미 처리되고, 이 변경의 영향을 받지 않는다.
        logger.debug(
            f"  └─ [헤더->leaf 보정 생략] '{self._row_name(row)}'에 "
            "'합계'/'소계'/'전체' 이름의 자식이 없어 추측하지 않고 원래"
            " 행(헤더 자체 코드)을 그대로 씁니다 - 헤더 자체가 실제로"
            " 조회 불가능하면 이후 fetch 단계에서 명시적으로 실패합니다."
        )
        return row

    def _row_group_root(
        self, category_rows: List[Dict[str, Any]], row: Dict[str, Any]
    ) -> Dict[str, Any]:
        """row(분류값 행)가 속한 axis 안에서 UP_ITM_ID 체인을 타고 최상위
        조상(root, 즉 UP_ITM_ID가 없는 뿌리 노드)까지 거슬러 올라가 그
        root row를 반환한다.

        실측(DT_444002_N2023A005): "특성별" 축(OBJ_ID_SN="1") 하나 안에
        A02(항공산업 분류)/A03(종사자규모)/A04(매출액 규모)라는 서로 완전히
        다른 세 그룹이 UP_ITM_ID 없는 뿌리 노드로 나란히 들어있고,
        "300인 이상"(A0306)은 A03 밑에, "항공산업 관련 정비업"(A020302)은
        A02 밑에 있다 - 같은 축 번호(OBJ_ID_SN)라고 해서 같은 개념 그룹이
        아니라는 뜻이다. 이 루트를 알아야 "정비사"라는 개념이 실제로
        어느 그룹(산업분류? 규모? 매출액?)에 속하는지 판단할 수 있다.
        """
        by_id = {
            self._row_id(r): r
            for r in category_rows
            if r.get("OBJ_ID") != "ITEM"
        }
        current = row
        seen_ids = set()
        while True:
            up_id = current.get("UP_ITM_ID")
            cur_id = self._row_id(current)
            if not up_id or cur_id in seen_ids:
                return current
            seen_ids.add(cur_id)
            parent = by_id.get(up_id)
            if parent is None or parent.get("OBJ_ID_SN") != current.get(
                "OBJ_ID_SN"
            ):
                return current
            current = parent

    def _row_breadcrumb(
        self, category_rows: List[Dict[str, Any]], row: Dict[str, Any]
    ) -> str:
        """row가 분류값(OBJ) 행이면 "루트그룹명 > 리프이름" 형태의, 사람이
        읽고 개념을 바로 판단할 수 있는 경로 문자열을 만든다.

        이걸 안 보여주고 리프 이름만("300인 이상") 노출하면, 사용자도 LLM
        선택 프롬프트도 이게 어느 그룹(종사자규모/산업분류/매출액규모)에
        속하는 값인지 알 길이 없어서, "정비사"라는 직무 개념에 규모
        버킷 값이 후보로 잘못 섞여도 걸러낼 근거가 없다.
        """
        if row.get("OBJ_ID") == "ITEM":
            return self._row_name(row)
        root = self._row_group_root(category_rows, row)
        root_nm = self._row_name(root)
        leaf_nm = self._row_name(row)
        if root_nm == leaf_nm:
            return leaf_nm
        return f"{root_nm} > {leaf_nm}"

    def resolve_category_hint_axis(
        self, org_id: str, tbl_id: str, hint_label: str
    ) -> Optional[Dict[str, Any]]:
        """category_hint(예: "300인 이상"/"포함"/"제외")가 실제로 이 표의 어느
        objL 축(OBJ_ID_SN)의 어떤 코드값(ITM_ID)인지 실제 메타에서 찾는다.

        2026-07 실측(MCP KOSIS 커넥터로 실제 표 DT_444002_N2023A006 검증):
        getMeta(type=ITM) 응답의 OBJ_ID_SN 필드가 그대로 몇 번째 objL 축인지
        를 가리킨다 (예: "특성별"=OBJ_ID_SN:"1"->objL1, "직무별"=
        OBJ_ID_SN:"2"->objL2). 지금까지 category_hint("300인 이상" 같은
        규모 수식어)는 objL을 "all"로 통째로 받아온 뒤 응답 행의 C*_NM
        문자열에 힌트가 우연히 포함되는지만 보는 느슨한 방식이었는데, 이
        메서드는 그 힌트에 해당하는 실제 분류값 행을 미리 찾아서 축 번호+
        코드값을 반환한다 - 호출부가 이걸로 objL{axis}=code 서버 필터를
        걸면(fetch_kosis_data_range의 extra_obj_axis/extra_obj_code), 문자열
        매칭보다 훨씬 정확하고 응답 크기도 줄어든다.

        정확히 이름이 일치하는 분류값을 우선하고, 없으면 부분 일치로 한 번
        더 시도한다. 그래도 못 찾으면 None을 반환해서, 호출부가 기존의
        느슨한 문자열 매칭(_select_target_row의 category_hint 인자)으로
        폴백할 수 있게 한다.

        [2026-07 실측 수정] 경상수지(DT_301Y013)와 소비자심리지수
        (DT_511Y002)에서 _resolve_leaf_row를 무조건 적용하던 게 오히려
        틀린 값을 골랐다: "경상수지"(계정코드별 축의 최상위 합계 행)와
        "전체"(CSI분류코드별 축의 최상위 행)는 둘 다 하위 항목(상품수지/
        서비스수지 등, 성별/연령별 등)이 메타에 걸려 있어서 "자식이 있는
        헤더"로 오판됐지만, 실제로는 두 행 모두 MCP로 직접 조회하면 그
        자체로 값이 나오는 leaf였다(예: 경상수지 자체가 상품수지+서비스
        수지+... 의 합계 값을 이미 갖고 있음). "항공기 정비"(원래
        _resolve_leaf_row를 만든 동기) 같은 진짜 헤더-only 케이스는
        resolve_target_item의 느슨한 fuzzy 매칭에서 나온 것과 달리, 여기
        exact match는 이름이 완전히 일치하는 값을 config가 의도적으로
        정확히 지정한 것이므로(설정 작성자가 이미 그 값을 의도해서 쓴
        것) 그대로 신뢰하고 leaf 보정을 건너뛴다. 이름이 정확히 일치하지
        않아 부분 일치로 폴백한 경우(사용자가 "대형"처럼 느슨한 표현을
        썼을 때)에만 여전히 _resolve_leaf_row로 보정한다 - 그 경우는
        진짜로 어느 행을 의미하는지 불확실하기 때문이다.
        """
        raw_list = self.kosis.get_itm_meta_list(org_id, tbl_id)
        if not raw_list:
            return None
        _, category_rows = self._split_meta_rows(raw_list)

        exact = [r for r in category_rows if self._row_name(r) == hint_label]
        rows = exact or [
            r for r in category_rows if hint_label in self._row_name(r)
        ]
        if not rows:
            return None

        row = rows[0] if exact else self._resolve_leaf_row(category_rows, rows[0])
        axis_sn = row.get("OBJ_ID_SN")
        code = self._row_id(row)
        if axis_sn is None or not code:
            return None
        try:
            return {"obj_axis": int(axis_sn), "obj_code": code}
        except (TypeError, ValueError):
            return None

    def resolve_category_hints_axes(
        self,
        org_id: str,
        tbl_id: str,
        category_hint: Optional[Union[str, List[str]]],
        exclude_axis: Optional[int] = None,
    ) -> Dict[int, str]:
        """category_hint가 문자열 하나든, 여러 독립된 카테고리축을 동시에
        지정하는 리스트든(예: 주택매매가격지수의 ["전국", "종합"] - "행정
        구역별" 축과 "주택유형별" 축을 한 번에 pin해야 하는 경우) 각 힌트를
        실제 표 메타에서 축 번호/코드로 해석해 {axis: code} 딕셔너리로
        합쳐 돌려준다.

        2026-07 실측(주택매매가격지수 DT_1YL13502E, 임금 DT_118N_MON061):
        일부 표는 카테고리축이 두 개 이상이라(지역별+주택유형별,
        지역별+산업별+규모별 등) 힌트 문자열 하나로는 한 축밖에 못
        고정한다. 나머지 축은 기존 _select_target_row의 "합계/전체 우선"
        문자열 폴백에 맡겨졌는데, 그 폴백은 "포함"/"합계"/"소계"/"전체"
        같은 리터럴만 찾아서 "전국"/"종합" 같은 실제 축 값과는 안 맞는
        경우가 많았다. 이제 default_category_hint에 리스트를 넣으면 각
        문자열이 각자의 축을 찾아 전부 서버 필터(objL{axis}=code)로
        걸린다.

        이미 주 컬럼(item)이 차지한 축(exclude_axis)과 겹치는 힌트는
        무시한다 - 원래 컬럼 선택이 더 구체적이므로 덮어쓰지 않는다.
        """
        if not category_hint:
            return {}
        hints = (
            [category_hint] if isinstance(category_hint, str) else list(category_hint)
        )
        axes: Dict[int, str] = {}
        for hint in hints:
            if not hint:
                continue
            resolved = self.resolve_category_hint_axis(org_id, tbl_id, hint)
            if not resolved:
                continue
            axis, code = resolved["obj_axis"], resolved["obj_code"]
            if axis == exclude_axis:
                continue
            axes[axis] = code
        return axes

    def _match_phrase_in_rows(
        self, rows: List[Dict[str, Any]], phrase: str
    ) -> List[Dict[str, Any]]:
        """phrase가 rows(item_rows 또는 category_rows) 중 이름으로 어디에
        걸리는지 찾는다. 정확일치를 우선하고, 없으면 fuzzy로 한 번 더
        시도한다 - resolve_category_hint_axis의 기존 매칭 순서와 같다.
        self._row_name/_fuzzy_contains는 TextUtilsMixin 소속(클래스
        docstring에 명시된 대로, ResolutionMixin은 항상 TextUtilsMixin과
        함께 상속되는 걸 전제로 self.을 통해 호출한다).

        [2026-08-10 실측 수정 - 문화산업 임금동향(DT_113_STBL_1031340)]
        fuzzy 매칭이 한 phrase에 여러 후보를 걸었는데, 그중 한 후보의
        이름이 다른 후보 이름의 부분 문자열이면(실측 사례: 분류축 상위
        그룹 "산업"과 그 자식 leaf "문화산업" - phrase "문화산업
        임금동향"이 "산업"도 "문화산업"도 둘 다 부분 문자열로 걸어버림)
        더 짧고 포괄적인 쪽은 검색 phrase와의 매칭 정보량이 적다. 이걸
        구분 안 하고 메타 목록 순서상 먼저 오는 후보(주로 상위 그룹
        헤더)를 그대로 candidates[0]으로 채택하면, 조회 불가능한 그룹
        코드(예: "산업")가 진짜 leaf("문화산업") 대신 확정돼 KOSIS
        err:30으로 이어진다(실측 확인). 그래서 다른 매칭 후보 이름에
        완전히 포함되는(부분 문자열인) 후보는 제외하고, 더 구체적인
        이름만 남긴다 - "관광산업"/"스포츠산업"처럼 애초에 이름이
        겹치지 않는 후보는 이 필터의 영향을 받지 않는다."""
        if not phrase:
            return []
        exact = [r for r in rows if self._row_name(r) == phrase]
        if exact:
            return exact
        fuzzy = [
            r for r in rows if self._fuzzy_contains(self._row_name(r), phrase)
        ]
        if len(fuzzy) <= 1:
            return fuzzy
        names = {id(r): self._row_name(r) for r in fuzzy}

        def _subsumed_by_another(r: Dict[str, Any]) -> bool:
            nm = names[id(r)]
            return any(
                id(other) != id(r) and nm != other_nm and nm in other_nm
                for other, other_nm in ((o, names[id(o)]) for o in fuzzy)
            )

        specific = [r for r in fuzzy if not _subsumed_by_another(r)]
        return specific or fuzzy

    def _row_ancestor_ids(
        self, category_rows: List[Dict[str, Any]], row: Dict[str, Any]
    ) -> "set":
        """row 자신을 포함해 UP_ITM_ID 체인을 타고 루트까지 만나는 모든
        조상 id를 모은다(자기 자신 포함). resolve_keyword_group_in_table
        에서 "학교급성별"(넓은 개념)이 "남"(그 밑의 구체적인 값)의 조상인지
        판별해, 더 구체적인 phrase가 있으면 넓은 phrase는 중복이라 건너뛰는
        데 쓴다."""
        by_id = {
            self._row_id(r): r for r in category_rows if r.get("OBJ_ID") != "ITEM"
        }
        ids: set = set()
        current = row
        seen: set = set()
        while current is not None:
            cur_id = self._row_id(current)
            if not cur_id or cur_id in seen:
                break
            seen.add(cur_id)
            ids.add(cur_id)
            up_id = current.get("UP_ITM_ID")
            current = by_id.get(up_id) if up_id else None
        return ids

    def resolve_keyword_group_in_table(
        self, org_id: str, tbl_id: str, phrases: List[str]
    ) -> Dict[str, Any]:
        """[종합 프로젝트 - 2.6/2.7절, Decision Log 006] 표가 이미 확정된
        뒤, 느슨한 phrase 묶음(예: "학교급 성별"/"고등학교"/"남"/"독서한 적
        있음")을 그 표의 실제 메타 구조(ITM/OBJ)에 매칭한다.

        순열로 "이 phrase가 몇 번째 자리인지" 시도하는 대신, 메타가 이미
        갖고 있는 라벨(OBJ_ID=="ITEM" 여부, OBJ_ID_SN 축 번호, UP_ITM_ID
        부모)을 그대로 활용한다:

        1) phrase마다 item_rows에 먼저 매칭을 시도하고, 안 걸리면
           category_rows에 매칭한다 - 이름이 어느 쪽에 걸리는지로 ITM인지
           축값인지가 자동으로 갈린다(_match_phrase_in_rows).
        2) 축값 후보가 정확히 1개(동명이의 없음)인 phrase들의 breadcrumb
           루트(_row_group_root)를 "이미 확정된 컨텍스트"로 모은다.
        3) 축값 후보가 여러 개(동명이의 - 예: "고등학교"가 "학교급성별"과
           "학교급학년" 양쪽에 있음)인 phrase는, 확정된 컨텍스트와 루트가
           같은 후보만 남긴다. 그래도 여러 개면 첫 후보로 폴백한다
           (resolve_category_hint_axis의 기존 rows[0] 동작과 동일한
           최후 수단 - 다만 이제는 "다른 정보가 전혀 없을 때만" 쓰는
           마지막 폴백이라는 게 다르다).

        메타(get_itm_meta_list)는 phrase 개수와 무관하게 딱 한 번만
        조회한다 - phrase마다 API를 새로 부르지 않는다.

        한계: 2단계에서 "확정된 컨텍스트"로 삼는 phrase 자체가 틀리게
        매칭됐으면(극히 드물지만 이론상 가능) 그 오류가 3단계의 동명이의
        판별에 그대로 전파된다. 이건 순열로도 못 막는 근본적 한계라(순열도
        결국 "어느 조합이 맞는지" 판별할 외부 근거가 필요함), 이번 설계
        범위 밖으로 남겨둔다.

        반환:
            {"itm_id", "itm_nm"}   - ITM으로 판정된 phrase(있으면, 첫 번째만)
            "obj_axes": {axis: code, ...}  - 축값으로 판정된 phrase들
                (fetch_kosis_data_range의 extra_obj_axes와 바로 호환)
            "obj_axes_fallback": {axis: code, ...}  - [2026-08-10 추가]
                해당 축이 "부모/자식 이름 동일이라 정적으로 판별 불가"
                상태였을 때, 원래 코드(부모)로 실제 조회했더니 err:30
                (데이터 0건)이면 한 번 더 시도해볼 자식 코드
                (fetch_kosis_data_range의 extra_obj_axes_fallback과 호환).
                이런 축이 없으면 빈 딕셔너리.
            "unresolved": [phrase, ...]    - 어느 쪽에도 안 걸린 phrase
                (사전에 없는 표현 - 3장 C번과 같은 유형, 판정 불가 사유로
                 넘겨야 함)
        """
        raw_list = self.kosis.get_itm_meta_list(org_id, tbl_id)
        item_rows, category_rows = self._split_meta_rows(raw_list)

        per_phrase: Dict[str, Dict[str, Any]] = {}
        for phrase in phrases:
            if not phrase:
                continue
            item_matches = self._match_phrase_in_rows(item_rows, phrase)
            if item_matches:
                per_phrase[phrase] = {"type": "item", "candidates": item_matches}
                continue
            cat_matches = self._match_phrase_in_rows(category_rows, phrase)
            if cat_matches:
                per_phrase[phrase] = {"type": "category", "candidates": cat_matches}
            else:
                per_phrase[phrase] = {"type": None, "candidates": []}

        # 2단계: 축값 후보가 정확히 1개(동명이의 없음)인 phrase의
        # breadcrumb 루트를 "이미 확정된 컨텍스트"로 모은다.
        confirmed_root_ids: set = set()
        for info in per_phrase.values():
            if info["type"] == "category" and len(info["candidates"]) == 1:
                root = self._row_group_root(category_rows, info["candidates"][0])
                root_id = self._row_id(root)
                if root_id:
                    confirmed_root_ids.add(root_id)

        # 3단계: 동명이의 phrase는 confirmed_root_ids와 일치하는 후보로
        # 좁힌다(안 좁혀지면 원래 후보 전체 유지 - 다음 단계에서 첫 후보로
        # 폴백). 각 category phrase의 "anchor"(아직 leaf로 캐스케이드하기
        # 전의 매칭 행)를 여기서 확정해둔다.
        category_anchor: Dict[str, Dict[str, Any]] = {}
        for phrase, info in per_phrase.items():
            if info["type"] != "category":
                continue
            candidates = info["candidates"]
            if len(candidates) > 1 and confirmed_root_ids:
                narrowed = [
                    c for c in candidates
                    if self._row_id(self._row_group_root(category_rows, c))
                    in confirmed_root_ids
                ]
                if narrowed:
                    logger.info(
                        f"  └─ [동명이의 좁히기] '{phrase}' 후보"
                        f" {len(candidates)}개 중, 이미 확정된 부모 그룹과"
                        f" 일치하는 {len(narrowed)}개로 좁힘"
                    )
                    candidates = narrowed
            category_anchor[phrase] = candidates[0]

        # 4단계: "학교급성별"(넓은 개념)이 "남"(그 밑 구체적인 값)의
        # 조상이면, 더 구체적인 phrase가 이미 같은 걸 표현하고 있으므로
        # 넓은 phrase는 중복 - obj_axes에 따로 넣지 않는다. 순열로는 이런
        # "포함 관계"를 아예 못 잡는데, 메타의 UP_ITM_ID 체인이 있어서
        # 가능한 판별이다.
        subsumed_phrases: set = set()
        anchor_ids = {p: self._row_id(r) for p, r in category_anchor.items()}
        for p1, r1 in category_anchor.items():
            id1 = anchor_ids[p1]
            for p2, r2 in category_anchor.items():
                if p1 == p2 or id1 == anchor_ids[p2]:
                    continue
                if id1 in self._row_ancestor_ids(category_rows, r2):
                    subsumed_phrases.add(p1)
                    break

        itm_result: Optional[Dict[str, Any]] = None
        obj_axes: Dict[int, str] = {}
        obj_axes_fallback: Dict[int, str] = {}
        unresolved: List[str] = []

        for phrase, info in per_phrase.items():
            if info["type"] is None:
                unresolved.append(phrase)
                continue
            if info["type"] == "item":
                if itm_result is None:
                    row = info["candidates"][0]
                    itm_result = {
                        "itm_id": self._row_id(row),
                        "itm_nm": self._row_name(row),
                    }
                continue
            # type == "category"
            if phrase in subsumed_phrases:
                logger.info(
                    f"  └─ [중복 phrase 제외] '{phrase}'는 같은 그룹의 더"
                    " 구체적인 다른 phrase에 이미 포함돼 있어 별도 축 값을"
                    " 부여하지 않습니다."
                )
                continue
            leaf_row = self._resolve_leaf_row(category_rows, category_anchor[phrase])
            axis_sn = leaf_row.get("OBJ_ID_SN")
            try:
                axis = int(axis_sn)
            except (TypeError, ValueError):
                unresolved.append(phrase)
                continue
            obj_axes[axis] = self._row_id(leaf_row)
            # [2026-08-10 추가] _resolve_leaf_row가 "부모/자식 이름이 같아
            # 정적으로 판별 불가"라 원래 행(부모)을 쓰기로 하면서 함께
            # 남겨둔 "동일 이름 자식" 폴백 코드가 있으면 그대로 이어받는다
            # - fetch_kosis_data_range가 이 축 코드로 실제 조회했을 때
            # err:30(데이터 0건)이면 이 폴백으로 한 번 더 시도한다.
            fallback_id = leaf_row.get("_leaf_fallback_id")
            if fallback_id:
                obj_axes_fallback[axis] = fallback_id

        # itm_id 기본값은 None이 아니라 "all"로 둔다 - resolve_target_item의
        # to_candidate/힌트 폴백과 같은 관례다. ITM phrase가 하나도 안
        # 걸렸어도(전부 축값이거나 unresolved) fetch_kosis_data_range는
        # itmId 파라미터를 "ALL"로 채워야 정상 호출되므로, 여기서
        # None으로 두면 호출부가 매번 방어 코드를 따로 넣어야 한다.
        result = dict(itm_result or {"itm_id": "all", "itm_nm": None})
        result["obj_axes"] = obj_axes
        result["obj_axes_fallback"] = obj_axes_fallback
        result["unresolved"] = unresolved
        return result

    def _suggest_broader_search_terms(self, keyword: str) -> List[str]:
        """HCX로 keyword가 속할 만한 상위 산업/조사 분야명을 제안받는다.

        KOSIS 통계표 제목은 "정비사"처럼 구체적인 직업/개념보다
        "항공산업 인력현황조사"처럼 상위 조사/산업 단위로 지어지는 경우가
        많아서, 원 키워드만으로는 제목 검색에 안 걸리는 경우가 흔하다.
        """
        system_instruction = (
            "당신은 국가통계포털(KOSIS) 통계표 제목 검색을 돕는 도우미입니다.\n"
            "사용자가 준 구체적인 직업/개념 키워드가 어떤 산업 실태조사나"
            " 통계표 제목에 들어있을 법한지, 상위 산업/조사 분야명을 한국어로"
            " 최대 3개 제안하세요. (예: '항공정비사' -> '항공산업 인력현황',"
            " '항공운송업 실태조사', '항공산업 실태조사')\n"
            '반드시 {"terms": ["...", "...", "..."]} 형태의 순수 JSON으로만'
            " 응답하세요 (마크다운 백틱 사용 금지)."
        )
        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": f"키워드: {keyword}"},
        ]
        try:
            raw = self.hcx.generate_completion(messages, temperature=0.3)
            clean = re.sub(r"```json|```", "", raw).strip()
            parsed = json.loads(clean)
            terms = parsed.get("terms", [])
            terms = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
            logger.info(f"[딥서치] '{keyword}' 상위 검색어 제안: {terms}")
            return terms
        except Exception as e:
            logger.warning(f"⚠️ [딥서치] 상위 검색어 제안 실패: {e}")
            return []


    def _match_pending_table(self, user_input: str) -> Optional[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = self.slots.get(
            "pending_table_candidates", []
        )
        if not candidates:
            return None

        text = user_input.strip()

        # 1) 번호로 답한 경우
        num_match = re.search(r"(\d+)", text)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]

        # 2) 통계표명이 포함된 경우
        for cand in candidates:
            tbl_nm = cand.get("TBL_NM") or ""
            if tbl_nm and tbl_nm in text:
                return cand

        return None

    # ------------------------------------------------------------------
    # 사용자가 컬럼 후보 중 하나를 고르도록 답한 경우 매칭
    # ------------------------------------------------------------------

    def _match_pending_item(self, user_input: str) -> Optional[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = self.slots.get(
            "pending_item_candidates", []
        )
        if not candidates:
            return None

        text = user_input.strip()

        # 1) 번호로 답한 경우 ("1", "2번" 등)
        num_match = re.search(r"(\d+)", text)
        if num_match:
            idx = int(num_match.group(1)) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]

        # 2) 항목명이 포함된 경우
        for cand in candidates:
            itm_nm = cand.get("itm_nm") or ""
            if itm_nm and itm_nm in text:
                return cand

        return None

    # ------------------------------------------------------------------
    # 한 해의 여러 행(row) 중 정확히 하나를 고른다
    # ------------------------------------------------------------------

    def gather_item_candidate_values_in_table(
        self,
        org_id: str,
        tbl_id: str,
        tbl_nm: str,
        indicator: str,
        start_period: str,
        end_period: str,
        prd_se: str,
        category_hint: Optional[str],
        target_period: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """엄격한 컬럼 매칭(resolve_target_item)이 "없음"으로 답한 표에서,
        느슨하게라도 관련 있어 보이는 후보 컬럼 여러 개를 뽑아 실제 값을
        다 조회해본다.

        실측 사례: 같은 표(직무별 종사자 현황)에 대해 "정비사"를 엄격
        매칭시켰을 때, 어떤 실행에서는 "항공기 정비"를 정확히 찾아내고
        어떤 실행에서는 "없음"이라고 답하는 비일관성이 있었다(같은
        temperature=0 호출인데도). "없음" 하나만 믿고 바로 이 표를
        포기하고 전혀 무관한 표로 넘어가면, 사실 이 표가 맞는 표인데도
        놓치는 사고로 이어진다(실제로 그렇게 org 115의 무관한 통계로
        넘어가 "2명"이라는 틀린 값을 낸 사고가 있었다). 그래서 확신이
        덜해도 후보가 될 만한 컬럼들의 실제 값을 다 가져와서 주장
        수치와 비교해보고, 근거 있게 가까운 게 있으면 그걸로 확정한다 -
        LLM의 "이 개념이 맞다/아니다"라는 판단만으로 끝내지 않고, 실제
        데이터 값이라는 더 확실한 근거로 검증하는 것이다. 표 전체가
        아니라 후보 top_k개로 제한해 API 호출을 억제한다.
        """
        raw_list = self.kosis.get_itm_meta_list(org_id, tbl_id)
        if not raw_list:
            return []
        item_rows, category_rows = self._split_meta_rows(raw_list)

        loose_rows = self._llm_select_meta_rows(
            indicator, item_rows, category_rows, loose=True, top_k=top_k
        )
        if not loose_rows:
            return []

        results: List[Dict[str, Any]] = []
        for row in loose_rows:
            leaf = row
            if row.get("OBJ_ID") != "ITEM":
                leaf = self._resolve_leaf_row(category_rows, row)

            cand_itm_id = self._row_id(leaf)
            cand_itm_nm = self._row_name(leaf)
            cand_breadcrumb = self._row_breadcrumb(category_rows, leaf)
            obj_axis = None
            obj_code = None
            axis_sn = leaf.get("OBJ_ID_SN")
            if leaf.get("OBJ_ID") != "ITEM" and axis_sn is not None:
                try:
                    obj_axis = int(axis_sn)
                    obj_code = cand_itm_id
                    cand_itm_id = "all"
                except (TypeError, ValueError):
                    pass

            extra_obj_axes = self.resolve_category_hints_axes(
                org_id, tbl_id, category_hint, exclude_axis=obj_axis
            )

            fetch_res = self.fetch_kosis_data_range(
                org_id=org_id,
                tbl_id=tbl_id,
                tbl_nm=tbl_nm,
                itm_id=cand_itm_id,
                itm_nm=cand_itm_nm,
                indicator=indicator,
                start_period=start_period,
                end_period=end_period,
                prd_se=prd_se,
                category_hint=category_hint,
                obj_axis=obj_axis,
                obj_code=obj_code,
                extra_obj_axes=extra_obj_axes,
            )
            if not fetch_res.get("success"):
                continue
            rec = fetch_res.get("yearly_records", {}).get(target_period)
            if not rec:
                continue
            value_raw = rec.get("value")
            m = re.search(r"([-–]?[\d,]+(?:\.\d+)?)", str(value_raw))
            value_num = (
                float(m.group(1).replace(",", "").replace("–", "-"))
                if m else None
            )
            cand_unit = rec.get("unit")

            # [단위 불일치 가드] "정비사는 4,248명이다"처럼 사람 수를
            # 묻는데, 값 비교 구제가 단위가 "개"/"원"/"%"인 표(예:
            # 인력변동 현황의 사업체 수)를 후보로 잡아 숫자만 보고
            # 비교해버린 실측 사례가 있었다. 숫자가 우연히 비슷해도 단위
            # 종류 자체가 다르면 애초에 같은 개념일 수 없으므로, 다른
            # 후보로 넘어가기 전에 여기서 걸러낸다.
            claimed_unit = self.slots.get("claimed_unit") if hasattr(
                self, "slots"
            ) else None
            if not self._unit_compatible(claimed_unit, cand_unit):
                logger.debug(
                    f"  └─ [단위 불일치 제외] '{cand_itm_nm}'(단위: '{cand_unit}')"
                    f" - 주장 단위 '{claimed_unit}'와 종류가 달라 후보에서 제외"
                )
                continue

            results.append(
                {
                    "itm_id": cand_itm_id,
                    "itm_nm": cand_itm_nm,
                    "breadcrumb": cand_breadcrumb,
                    "obj_axis": obj_axis,
                    "obj_code": obj_code,
                    "value_raw": value_raw,
                    "value_num": value_num,
                    "unit": cand_unit,
                }
            )
        logger.info(
            f"  └─ [표 내 느슨한 후보 값 조회] '{indicator}' @ '{tbl_nm}' -> "
            + ", ".join(f"{r['itm_nm']}={r['value_raw']}" for r in results)
        )
        return results

    def _resolve_item_with_table_fallback(
        self,
        indicator: str,
        table_info: Dict[str, Any],
        remaining_candidates: List[Dict[str, Any]],
        max_tries: int = 5,
        claimed_value: Optional[float] = None,
        period: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """table_info로 컬럼 매칭을 시도하고, 완전히 실패하면(matched=False)
        아직 안 써본 형제 표가 남아있는 한 최대 max_tries까지 자동으로
        다음 후보로 넘어가며 재시도한다.

        예: "정비사"를 찾는데 처음 고른 표가 "경영형태별 종사자현황"처럼
        직종 구분이 아예 없는 표라면, 사용자에게 160개 항목을 통째로
        되묻는 대신 같은 조사 안의 다른 표("직무별 종사자현황" 등)로
        조용히 넘어가 본다. 성공하면 그 표로, 다 실패하면 마지막으로
        시도한 표의 결과를 그대로 반환해 기존처럼 사용자에게 되묻는다.

        반환값: (최종 table_info, item_info, 남은 미시도 후보 리스트, 시도한
        표 이름 리스트)

        마지막 요소(tried_tables)는 호출부가 "matched=False로 끝났을 때"를
        구분하는 데 쓴다 - resolve_target_item은 매칭에 완전히 실패해도
        (그 표의 컬럼이 20개 이하면) "혹시 이 중에 있나요?"로 그 표의 전체
        항목 목록을 candidates로 돌려주는데, 이미 여러 표를 거쳐 실패한
        상태에서 마지막으로 시도한(보통 전혀 무관한) 표의 항목 목록을 그대로
        사용자에게 "어떤 항목이세요?"라고 되물으면 "학력별 종사 표에서 어떤
        항목을 확인하고 싶으신가요?"처럼 맥락 없는 질문이 나가버린다.
        tried_tables가 있으면 호출부가 "matched=False + 표를 N개 거침"을
        보고 그 대신 "관련 통계를 못 찾았습니다"로 명확하게 안내할 수 있다.
        """
        tries_left = list(remaining_candidates)
        tried_tables = [table_info.get("tbl_nm")]
        tried = 0
        # 확신은 안 서도(50% 넘게 차이나도) 지금까지 본 것 중 그나마 가장
        # 가까운 후보를 계속 추적해둔다 - 모든 형제 표를 다 시도했는데도
        # 확신 있는 매칭을 하나도 못 찾으면, "아무것도 못 찾았습니다"로
        # 완전히 빈손으로 끝내는 대신 이 best-effort 후보를 반환한다.
        # 실측 사례: "직무별 종사자 현황"에서 이미 "항공기 정비"=6,806을
        # 찾았는데(주장 4,248과는 안 맞지만), 그 뒤 무관한 재무/투자
        # 통계표 4개를 더 뒤지다 결국 "관련 통계 없음"으로 끝나버려서
        # 처음에 찾은 6,806이라는 실제 값 자체가 통째로 버려졌다 - 호출부가
        # 낮은 신뢰도임을 사용자에게 명확히 밝힐 수 있도록(최종 응답의
        # claimed_value 대조 경고), 아예 못 찾은 것과 "찾았지만 확신이
        # 낮음"을 구분해서 최소한 실제 데이터는 넘겨준다.
        best_overall: Optional[Dict[str, Any]] = None
        best_overall_table: Optional[Dict[str, Any]] = None
        best_overall_diff = float("inf")

        while True:
            tried += 1
            item_info = self.resolve_target_item(
                table_info["org_id"], table_info["tbl_id"], indicator
            )
            if item_info.get("matched"):
                return table_info, item_info, tries_left, tried_tables

            # [값 비교 구제] 엄격 매칭이 "없음"이라고 답해도, 주장 수치와
            # 시점을 이미 알고 있으면 이 표를 곧바로 포기하지 않는다. 같은
            # 표에서도 실행마다 판단이 갈리는 경계 사례가 실측됐기 때문에,
            # 느슨한 후보들의 실제 값을 다 가져와 비교해보고 근거 있게
            # 가까운 게 있으면 그걸로 확정한다 - 그래도 없으면 그때
            # 형제 표로 넘어간다(아래).
            if claimed_value is not None and period:
                loose_candidates = self.gather_item_candidate_values_in_table(
                    table_info["org_id"],
                    table_info["tbl_id"],
                    table_info.get("tbl_nm"),
                    indicator,
                    period["start"],
                    period["end"],
                    period["prd_se"],
                    period.get("category_hint"),
                    period["target"],
                )
                best = self.pick_best_matching_candidate(
                    loose_candidates, claimed_value
                )
                if best is not None and best.get("value_num") is not None:
                    rel_diff = abs(best["value_num"] - claimed_value) / max(
                        abs(claimed_value), 1
                    )
                    if rel_diff <= 0.5:
                        logger.info(
                            "  └─ [표 내 값 비교 구제 성공] "
                            f"'{table_info.get('tbl_nm')}'/'{best['itm_nm']}'="
                            f"{best['value_raw']} (주장값={claimed_value},"
                            f" 차이 {rel_diff:.0%}) - 엄격 매칭 실패에도"
                            " 이 표를 그대로 확정합니다."
                        )
                        rescued_item_info = {
                            "itm_id": best["itm_id"],
                            "itm_nm": best.get("itm_nm"),
                            "breadcrumb": best.get("breadcrumb"),
                            "obj_axis": best.get("obj_axis"),
                            "obj_code": best.get("obj_code"),
                            "candidates": [],
                            "matched": True,
                        }
                        return (
                            table_info,
                            rescued_item_info,
                            tries_left,
                            tried_tables,
                        )
                    if rel_diff < best_overall_diff:
                        best_overall_diff = rel_diff
                        best_overall = best
                        best_overall_table = dict(table_info)

            if not tries_left or tried >= max_tries:
                if best_overall is not None:
                    logger.info(
                        "  └─ [모든 표 소진 - 최선의 낮은 신뢰도 후보 채택] "
                        f"'{best_overall_table.get('tbl_nm')}'/"
                        f"'{best_overall['itm_nm']}'={best_overall['value_raw']}"
                        f" (주장값={claimed_value}, 차이 {best_overall_diff:.0%})"
                    )
                    low_conf_item_info = {
                        "itm_id": best_overall["itm_id"],
                        "itm_nm": best_overall.get("itm_nm"),
                        "breadcrumb": best_overall.get("breadcrumb"),
                        "obj_axis": best_overall.get("obj_axis"),
                        "obj_code": best_overall.get("obj_code"),
                        "candidates": [],
                        "matched": True,
                    }
                    return (
                        best_overall_table,
                        low_conf_item_info,
                        tries_left,
                        tried_tables,
                    )
                return table_info, item_info, tries_left, tried_tables

            nxt = tries_left.pop(0)
            logger.info(
                f"  └─ [표 자동 전환] '{table_info.get('tbl_nm')}'에서 '{indicator}'"
                f" 관련 컬럼을 못 찾아 다음 후보 '{nxt.get('TBL_NM')}'로 재시도합니다."
            )
            table_info = {
                "org_id": nxt.get("ORG_ID"),
                "tbl_id": nxt.get("TBL_ID"),
                "tbl_nm": nxt.get("TBL_NM"),
                "period_start": nxt.get("STRT_PRD_DE"),
                "period_end": nxt.get("END_PRD_DE"),
            }
            tried_tables.append(table_info.get("tbl_nm"))

    # ------------------------------------------------------------------
    # 연/월 시점 유틸
    # ------------------------------------------------------------------
