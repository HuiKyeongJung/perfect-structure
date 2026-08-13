"""자동 분리된 모듈 (kosis_agent.py 리팩터링) - 동작은 기존과 동일합니다."""

import re
from typing import Any, Dict, List, Optional


class TextUtilsMixin:
    """문자열/메타 로우 파싱 관련 순수 유틸리티 (self.slots/self.hcx 등
    인스턴스 상태에 의존하지 않는 static/classmethod 모음).
    """

    @staticmethod
    def _split_meta_rows(
        raw_list: List[Dict[str, Any]]
    ) -> "tuple[List[Dict[str, Any]], List[Dict[str, Any]]]":
        """getMeta(type=ITM) 응답 한 번에 실제로는 두 종류가 섞여서 온다.

        실제 KOSIS 응답을 찍어보면(2026-07 실측), OBJ_ID="ITEM"인 행이
        진짜 항목(컬럼/측정값, 예: "종사자 현황")이고, 그 외 OBJ_ID="A"/"B"...
        인 행들은 사실 분류축(OBJ, 예: "특성별")의 코드값 트리다
        (UP_ITM_ID로 계층 구조, OBJ_ID_SN이 objL 몇 번째 축인지를 나타내는
        것으로 보임). 즉 "정비사"처럼 사람이 "컬럼"이라고 생각하는 개념이
        실제로는 ITM이 아니라 OBJ 분류값으로 존재하는 표가 많다 - 그래서
        컬럼 매칭을 item 행에서만 하면 놓친다.

        반환값: (item_rows, category_rows)
        """
        item_rows = [r for r in raw_list if r.get("OBJ_ID") == "ITEM"]
        category_rows = [r for r in raw_list if r.get("OBJ_ID") != "ITEM"]
        return item_rows, category_rows


    @staticmethod
    def _row_id(item: Dict[str, Any]) -> Optional[str]:
        return item.get("ITM_ID") or item.get("itmId")


    @staticmethod
    def _row_name(item: Dict[str, Any]) -> str:
        return item.get("ITM_NM") or item.get("itmNm") or ""


    @staticmethod
    def _fmt_table_option(
        idx: int, cand: Dict[str, Any], top_pick: bool = False
    ) -> str:
        """통계표 후보 하나를 번호 + 수록기간과 함께 사람이 읽기 좋게 포맷한다.

        수록기간(STRT_PRD_DE~END_PRD_DE)을 안 보여주면, 이미 폐기돼서 옛날
        자료까지만 있는 통계표(예: 1992~1996년까지만 수록된 표)를 사용자가
        모르고 골랐다가 "데이터가 존재하지 않습니다" 에러만 받는 일이 생긴다.

        top_pick=True면 랭킹 1위 후보에 배지를 붙인다. 후보 목록이 20~30개씩
        나올 때 사용자가 순서와 무관하게 아무거나 고르는 경우가 실측됐는데,
        그러면 이미 두 단계 랭킹(제목 매칭 + 상세설명 재랭킹)을 거쳐 가장
        관련성 높다고 판단된 표를 놔두고 관련 없는 표로 빠지는 일이 생긴다
        - 배지로 "이미 랭킹된 결과 중 1위"임을 눈에 띄게 알려서, 특별한
        이유가 없다면 이 표를 고르도록 유도한다.
        """
        tbl_nm = cand.get("TBL_NM", "")
        org_tbl = f"{cand.get('ORG_ID')}_{cand.get('TBL_ID')}"
        start = cand.get("STRT_PRD_DE")
        end = cand.get("END_PRD_DE")
        period = f", 수록기간: {start}~{end}" if start or end else ""
        badge = " ⭐ 가장 관련성 높음" if top_pick else ""
        return f"  {idx}. {tbl_nm} ({org_tbl}{period}){badge}"


    _KOREAN_PARTICLES = (
        "으로서", "이라서", "이지만", "에서는", "에게는", "이다가",
        "까지", "부터", "보다", "이다", "에서", "에게", "로서", "으로",
        "이라", "이고", "이며",
        "은", "는", "이", "가", "을", "를", "의", "에", "로", "와",
        "과", "도", "만",
    )

    # "4,248명이다"에서 숫자를 떼어내면 "명이다"만 토큰으로 남는다. 이건
    # 명사가 아니라 단위(명)+서술격 조사/어미(이다)일 뿐이라 통째로 버려야
    # 하는데, _KOREAN_PARTICLES의 일반 조사 스트리핑은 "잘라내고 남는 부분이
    # 최소 2글자는 돼야 한다"는 조건(len(t) > len(p)+1) 때문에 "명이다"(3자)
    # 에서 "이다"(2자)를 못 뗀다(3 > 3이 거짓이라서) - 이 서술어 어미들은
    # 문장 끝에서 통째로 떨어져 나가도 되는 것들이라 별도로, 남는 길이
    # 제한 없이 먼저 떼어낸다.
    _KOREAN_COPULA_ENDINGS = ("이었다", "였다", "입니다", "한다", "했다", "이다")

    @classmethod
    def _extract_keywords_from_sentence(cls, text: str) -> List[str]:
        """긴 원문 문장(지표 추출 실패 폴백)에서 컬럼 매칭에 쓸 만한 명사
        키워드 후보만 추려낸다. 숫자/쉼표/기호와 흔한 조사/서술어 어미를
        떼어낸다.

        예: "2023년 대형 항공사 소속 정비사는 4,248명이다."
            -> ["대형", "항공사", "소속", "정비사"]  ("명이다"는 통째로 제거)
        """
        cleaned = re.sub(r"[0-9,\.%]+", " ", text)
        cleaned = re.sub(r"[^\w\s가-힣]", " ", cleaned)
        tokens = cleaned.split()
        keywords: List[str] = []
        particles = sorted(cls._KOREAN_PARTICLES, key=len, reverse=True)
        copulas = sorted(cls._KOREAN_COPULA_ENDINGS, key=len, reverse=True)
        for tok in tokens:
            t = tok
            for c in copulas:
                if t.endswith(c) and len(t) > len(c):
                    t = t[: -len(c)]
                    break
            for p in particles:
                if t.endswith(p) and len(t) > len(p) + 1:
                    t = t[: -len(p)]
                    break
            if len(t) >= 2:
                keywords.append(t)
        return keywords


    # 단위 문자열을 대략적인 "종류"로 묶는다. 정확한 KOSIS 단위 사전을
    # 다 아는 건 아니라서 완벽하진 않지만, "사람 수를 물었는데 개수/금액/
    # 비율 단위 후보가 나온다"처럼 명백히 종류가 다른 오탐을 걸러내는
    # 데는 이 정도 substring 판별로 충분하다.
    _UNIT_CATEGORY_PATTERNS = (
        ("person", ("명", "인")),
        ("money", ("원", "달러", "$", "€", "USD", "KRW")),
        ("percent", ("%", "％", "퍼센트")),
        ("count", ("개", "건", "곳", "대", "동", "실")),
    )

    # [2026-07-24 추가 - unit_cat 오분류 방지] kosis_config.py의 unit_cat은
    # 원래 KOSIS UNIT_NM 그대로("%"/"명"/"건"/"천명"/"2020=100" 등)를
    # 넣는 게 원칙이지만, 실측 없이 "이 지표가 뭘 나타내는지" 설명하는
    # 한글 문구(예: "전년동월대비증감률", "원지수")를 잘못 넣은 사례가
    # 있었다(#57 회귀로 실측: 소비자물가지수_10월_일치/생산자물가_전월비
    # 골든셋 케이스에서 "대비"의 "대", "동월"의 "동"이 count 카테고리
    # 마커와 우연히 겹쳐 정상 %) claim이 단위 불일치로 오배제됨).
    # 근본 수정은 kosis_config.py 값 자체를 실제 UNIT_NM으로 바로잡는
    # 것이지만(소비자물가지수/전산업생산지수 완료), 앞으로 또 비슷한
    # 설명형 문구가 unit_cat에 들어가도 최소한 흔한 한국어 비교 접미사
    # ("~대비"/"~동월"/"~동기")에서는 오탐이 안 나게 스캔 전에 제거한다.
    _UNIT_FALSE_POSITIVE_STRIP_RE = re.compile(r"(대비|동월|동기)")

    @classmethod
    def _unit_categories(cls, unit: Optional[str]) -> set:
        """단위 문자열에 해당하는 "종류"를 전부 모아 집합으로 반환한다.

        [2026-07 변경 - 복합 단위 버그 수정] KOSIS는 한 컬럼(예:
        DT_1B8000G의 '출생사망혼인이혼' 항목)이 여러 하위 행마다 서로
        다른 단위를 쓸 때, UNIT_NM을 "명 건"처럼 여러 단위를 공백으로
        이어붙인 하나의 문자열로 내려주는 경우가 있다(실측: 혼인건수/
        이혼건수 값의 UNIT_NM이 "명 건"으로 와서, 예전엔 첫 매치("명"
        -> person)만 채택하는 바람에 단위 '건'인 주장이 전부 "단위 불일치"
        로 걸러져 애초에 값 비교조차 안 됐다). 이제는 문자열 안에 매칭
        되는 카테고리를 전부 모아서, 하나라도 겹치면 호환으로 본다.
        """
        if not unit:
            return set()
        scan_unit = cls._UNIT_FALSE_POSITIVE_STRIP_RE.sub("", unit)
        cats = {
            category
            for category, markers in cls._UNIT_CATEGORY_PATTERNS
            if any(m in scan_unit for m in markers)
        }
        return cats or {"other"}

    # [2026-07 추가] KOSIS는 표에 따라 원본 값을 절대 단위(명/원/건)가
    # 아니라 "천명"/"십억원"처럼 축척(scale)이 붙은 단위로 내려줄 때가
    # 있다(예: GDP 표 DT_200Y101의 UNIT_NM="십억원"은 raw 값이 이미
    # "십억원 단위 숫자"라는 뜻 - 실제 절대 원 값을 얻으려면 raw*10^9를
    # 해야 한다). 기사 주장(claim)은 항상 절대값으로 추출되므로(예:
    # "2401조원" -> 2.401e15), 후보 값도 같은 절대 단위로 맞춰야 비교가
    # 성립한다. 이 배율을 안 곱하면 GDP처럼 축척 있는 표는 원천적으로
    # 값이 안 맞아 항상 불일치로 나온다(실측 - "십억원"/"천명" 단위표 대상
    # 스트레스 테스트로 발견).
    #
    # 주의: "천명당"(조혼인율/조사망률처럼 "인구 천 명당" 비율 단위)은
    # 축척이 아니라 비율의 분모를 나타내는 표현이라 배율을 곱하면 안 된다
    # (곱하면 4.7 -> 4700으로 완전히 틀어짐). 그래서 전체 문자열이
    # "[배율 접두어]?[절대 기본단위]"와 정확히 일치할 때만("당" 같은 꼬리가
    # 붙으면 매치 실패) 배율을 적용한다 - 부분 문자열 검사가 아니라 완전
    # 일치라 "천명당"처럼 접미사가 붙은 경우는 안전하게 배율 없이(1배)
    # 지나간다.
    # [2026-07 추가] 위 순수-한글 패턴과 별개로, KOSIS는 "100만 USD"처럼
    # 아라비아 숫자 계수 + 한글 배율 접두어 + 공백 + 영문 기본단위를 섞어
    # 내려줄 때도 있다(실측: DT_2KAA809 외환보유액 표의 UNIT_NM="100만
    # USD" - "100" x "만"(10^4) = 10^6배, 기본단위는 "달러"가 아니라
    # 영문 "USD"). 기존 _UNIT_SCALE_RE는 전체 문자열이 순수 한글
    # "[배율]?[기본단위]"여야만 매치되므로 이런 혼합 표기는 그냥
    # 통과(배율 1배)해버려 실제 값이 100만분의 1로 축소돼 항상 불일치가
    # 났다. 숫자 계수/영문 기본단위까지 포괄하는 패턴으로 확장한다.
    _UNIT_SCALE_RE = re.compile(
        r"^(\d+)?\s*(조|십억|억|백만|만|천)?\s*(원|달러|불|명|건|USD|KRW)$"
    )
    _UNIT_SCALE_MULTIPLIERS = {
        "조": 1_000_000_000_000,
        "십억": 1_000_000_000,
        "억": 100_000_000,
        "백만": 1_000_000,
        "만": 10_000,
        "천": 1_000,
    }

    @classmethod
    def _unit_scale_multiplier(cls, unit: Optional[str]) -> float:
        if not unit:
            return 1.0
        m = cls._UNIT_SCALE_RE.match(unit.strip())
        if not m:
            return 1.0
        numeral_str, scale_word, _base = m.groups()
        if not numeral_str and not scale_word:
            return 1.0
        multiplier = 1.0
        if numeral_str:
            multiplier *= float(numeral_str)
        if scale_word:
            multiplier *= float(cls._UNIT_SCALE_MULTIPLIERS[scale_word])
        return multiplier

    @classmethod
    def _unit_compatible(
        cls, claimed_unit: Optional[str], candidate_unit: Optional[str]
    ) -> bool:
        """claimed_unit(주장 문장에서 뽑은 단위, 예: "명")과 candidate_unit
        (실제 조회된 표 값의 단위, 예: "개")이 종류가 다르면 False.

        실측 사례: "정비사는 4,248명이다"를 확인하려는데, 값 비교 구제가
        단위가 "개"인 표(인력변동 현황의 사업체 수 등)를 후보로 잡아
        숫자만 보고 비교해버린 적이 있다. 애초에 사람 수를 묻는데 단위가
        "개"/"원"/"%"인 값은 숫자가 우연히 가까워도 같은 개념일 수 없다.
        둘 중 하나라도 단위 정보가 없으면(파싱 실패 등) 과잉 필터링을
        피하기 위해 호환된다고(True) 본다 - 이 가드는 "명백히 다른 게
        확실할 때만" 걸러내는 보수적인 안전장치다.

        candidate_unit이 "명 건"처럼 여러 단위가 섞인 복합 문자열일 수
        있으므로, 카테고리를 집합으로 비교해 하나라도 겹치면 호환으로
        판단한다(둘 다 단일 카테고리였던 예전 동작과 100% 호환).
        """
        c1 = cls._unit_categories(claimed_unit)
        c2 = cls._unit_categories(candidate_unit)
        if not c1 or not c2 or "other" in c1 or "other" in c2:
            return True
        return bool(c1 & c2)

    @staticmethod
    def _fuzzy_contains(nm: str, keyword: str) -> bool:
        """keyword가 nm(항목/분류명) 안에 (부분적으로라도) 들어있는지 판단.

        단순 부분 문자열 매칭만으로는 "정비사"가 "항공기 정비"에 안 걸린다
        (마지막 "사" 때문). 흔한 직업/개념 접미사를 뗀 core도 함께 비교한다.
        """
        if not nm or not keyword:
            return False
        if keyword in nm or nm in keyword:
            return True
        for suf in ("사", "직", "원", "공", "가", "인", "자"):
            if keyword.endswith(suf) and len(keyword) > 1:
                core = keyword[:-1]
                if not core:
                    continue
                # 실측 사례: "정비사"에서 뗀 core "정비"가 "항공산업 관련
                # 정비업"(산업분류)에도 걸려버린다. "업"은 사람/직무가 아니라
                # 업종을 뜻하는 접미사라서, keyword 자체가 "업"으로 끝나는
                # 게 아닌데 nm만 "업"으로 끝나면 카테고리 종류가 다른
                # 오탐으로 보고 건너뛴다("정비사"(사람) != "정비업"(산업)).
                if nm.endswith("업") and not keyword.endswith("업"):
                    continue
                if core in nm or nm in core:
                    return True
        return False

    # ------------------------------------------------------------------
    # 사용자가 통계표 후보 중 하나를 고르도록 답한 경우 매칭
    # ------------------------------------------------------------------

    @staticmethod
    def _prev_month(yyyymm: str) -> str:
        """YYYYMM의 전월을 YYYYMM으로 반환 (연도 넘어가는 경우 처리)."""
        y, m = int(yyyymm[:4]), int(yyyymm[4:6])
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        return f"{y}{m:02d}"


    @staticmethod
    def _period_range(start_period: str, end_period: str, prd_se: str) -> List[str]:
        """start_period~end_period 사이의 시점 목록을 생성한다.

        연간(prd_se="Y")은 int(YYYY) range로 충분하지만, 월간(prd_se="M")은
        12월->다음해 1월처럼 자릿수가 안 이어지므로 int range를 그대로 쓰면
        안 되고 달력 계산을 직접 해야 한다.

        [2026-07 추가] 분기(prd_se="Q")도 KOSIS 실제 코드 포맷이 "YYYYN"
        (연도 4자리 + 분기 1자리, 예: "20244"=2024년 4분기 - kosis_table_info
        메타데이터로 실측 확인)이라 4분기->다음해 1분기 경계에서 월간과
        똑같은 문제가 생긴다("20244" 다음 정수는 "20245"인데 그런 분기는
        없음). int range를 그대로 쓰면 안 되고 분기 롤오버를 직접 계산한다.
        """
        if prd_se == "M":
            y, m = int(start_period[:4]), int(start_period[4:6])
            end_y, end_m = int(end_period[:4]), int(end_period[4:6])
            periods = []
            while (y, m) <= (end_y, end_m):
                periods.append(f"{y}{m:02d}")
                m += 1
                if m == 13:
                    m, y = 1, y + 1
            return periods
        if prd_se == "Q":
            y, q = int(start_period[:4]), int(start_period[4:5])
            end_y, end_q = int(end_period[:4]), int(end_period[4:5])
            periods = []
            while (y, q) <= (end_y, end_q):
                periods.append(f"{y}{q}")
                q += 1
                if q == 5:
                    q, y = 1, y + 1
            return periods
        return [str(y) for y in range(int(start_period), int(end_period) + 1)]

    @staticmethod
    def _quarter_period_to_kosis_code(period: str) -> str:
        """내부 표현(5자리 "YYYYN")을 실제 KOSIS Open API가 요구하는
        데이터 조회용 포맷(6자리 "YYYY0N", 월과 같은 자리수에 분기번호를
        0-padding)으로 변환한다.

        [2026-07 실측 확인] kosis_table_info(getMeta, type=PRD) 메타데이터
        응답은 분기 포맷을 5자리("20244")로 알려주는데, 실제 데이터 조회
        (getList)에 그 포맷을 그대로 쓰면 err30이 난다 - 메타 조회용
        표기와 실제 조회용 표기가 다르다. 여러 포맷 후보를 직접 API에
        찔러본 결과 6자리 zero-padded("202404")만 성공했고, 반환된 행의
        PRD_DE 값도 "202404"로 왔다(둘 다 실측 확인, DT_1G18007/2024년
        4분기). 내부 claim 표현은 월(6자리, "MM"=01~12)과 겹치는 걸 막기
        위해 5자리를 유지하고, 이 변환은 실제 API를 호출/응답을 읽는
        경계 지점에서만 적용한다.
        """
        return f"{period[:4]}0{period[4:5]}"

    # ------------------------------------------------------------------
    # KOSIS 실제 값 조회 (org_id/tbl_id는 확정된 상태에서 호출)
    # ------------------------------------------------------------------
