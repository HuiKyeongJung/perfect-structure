import atexit
import json
import logging
import re
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from config import config

# 로거 설정
logger = logging.getLogger("FactCheckPipeline.Client")


# ---------------------------------------------------------------------
# [2026-07 추가] HCX/KOSIS API 호출 횟수 누적 기록 (비용 추적용)
# ---------------------------------------------------------------------
# 실행(프로세스)마다 몇 번씩 호출했는지 로컬 파일(api_usage_log.jsonl)에
# 한 줄씩(JSON Lines) 남긴다. kosis_factcheck.log(logging용)와 같은
# 방식으로 cwd 기준 상대경로에 쌓인다 - test_*.py를 ~/test에서 실행하면
# ~/test/api_usage_log.jsonl에 기록됨.
#
# 카운터는 클라이언트 인스턴스가 아니라 모듈(프로세스) 전역이다. 테스트
# 스크립트들이 케이스마다 KosisInteractiveAgent()를 새로 만들어서(즉
# HCXClient/KosisApiClient도 매번 새로 생성됨) 인스턴스 속성으로 두면
# 케이스마다 카운터가 0으로 리셋돼버린다 - 한 번의 스크립트 실행(프로세스)
# 전체를 "1회 실행 단위"로 누적하려면 프로세스 전역이어야 한다.
#
# 나중에 파서로 여러 줄을 모아 합산할 수 있도록, 파일 형식은 한 줄에
# JSON 객체 하나(JSON Lines)로 고정한다 - 매 실행이 끝날 때(atexit) 그
# 실행분만큼의 카운트를 담은 줄 하나를 append한다.
#
# [2026-08-11 추가] 왕복시간(elapsed_sec)도 같은 per-call 딕셔너리에 함께
# 담는다 - 예전엔 probe_hcx_latency_cost.py처럼 별도 스크립트를 일부러
# 돌려야만 지연시간을 잴 수 있었는데, 토큰 수와 지연시간은 "같은 호출 한
# 건"의 서로 다른 측정값일 뿐이라 별도 로그 파일로 분리하면 나중에 (1)
# 두 파일을 타임스탬프/순서로 다시 맞춰야 하고 (2) 동시에 여러 프로세스가
# 돌면 그 맞춤조차 어긋날 수 있다. 그래서 실제 요청을 보내는 이 지점
# (generate_completion) 한 곳에서 시간까지 같이 재서 넣으면, 이후 어떤
# 스크립트가 HCXClient를 쓰든 별도 계측 코드 없이 자동으로 시간+토큰이
# 같은 곳(api_usage_log.jsonl)에 쌓인다 - 나중에 관리자 대시보드를 만들
# 때도 로그 소스가 하나면 충분하다.
_USAGE_LOG_FILE = "api_usage_log.jsonl"

_usage_counters: Dict[str, Any] = {
    "hcx_calls": 0,
    # 호출 1건당 하나씩: {promptTokens, completionTokens, totalTokens,
    # elapsed_sec, (실패 시) error} - usage 필드가 없어도(예: 응답 파싱은
    # 됐지만 usage가 없는 경우) elapsed_sec는 항상 채워진다.
    "hcx_usage_tokens": [],
    "kosis_calls_by_endpoint": defaultdict(int),
}


def _record_hcx_call(usage: Optional[Dict[str, Any]] = None) -> None:
    _usage_counters["hcx_calls"] += 1
    if usage:
        _usage_counters["hcx_usage_tokens"].append(usage)


def _record_kosis_call(endpoint: str) -> None:
    _usage_counters["kosis_calls_by_endpoint"][endpoint] += 1


def _flush_usage_log() -> None:
    """프로세스 종료 시 이번 실행에서 쌓인 호출 횟수를 한 줄로 기록한다.

    호출이 아예 한 번도 없었으면(예: import만 하고 API 호출 없이 끝난
    스크립트) 빈 줄을 남기지 않는다.
    """
    kosis_total = sum(_usage_counters["kosis_calls_by_endpoint"].values())
    hcx_total = _usage_counters["hcx_calls"]
    if kosis_total == 0 and hcx_total == 0:
        return

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "script": sys.argv[0] if sys.argv else None,
        "argv": sys.argv[1:],
        "hcx_calls": hcx_total,
        "hcx_usage_tokens": _usage_counters["hcx_usage_tokens"],
        "kosis_calls_total": kosis_total,
        "kosis_calls_by_endpoint": dict(_usage_counters["kosis_calls_by_endpoint"]),
    }
    try:
        with open(_USAGE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(
            f"[API 사용량 기록] hcx={hcx_total}건, kosis={kosis_total}건 ->"
            f" {_USAGE_LOG_FILE}"
        )
    except Exception as e:
        logger.error(f"[API 사용량 기록 실패]: {e}")


atexit.register(_flush_usage_log)


def fix_and_parse_kosis_json(raw_text: str) -> Any:
    """KOSIS 특유의 비표준 JSON 포맷을 보정 후 파싱합니다."""
    if not raw_text or not raw_text.strip():
        return []

    # {key: "val"} 형태에서 key에 큰따옴표가 없는 KOSIS 비표준 구문 보정
    fixed_text = re.sub(r'([{,]\s*)([a-zA-Z0-9_]+)\s*:', r'\1"\2":', raw_text)
    try:
        return json.loads(fixed_text)
    except json.JSONDecodeError:
        try:
            return json.loads(raw_text)
        except Exception:
            return []


class HCXClient:
    """HyperCLOVA X (CLOVA Studio) API 연결 및 요청 관리"""

    def __init__(self):
        self.api_key = config.NCP_CLOVASTUDIO_API_KEY
        self.base_url = config.HCX_BASE_URL
        self.generation_model = config.HCX_GENERATION_MODEL

        # 기본 Request Header 설정
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        logger.info(f"HyperCLOVA X Client 초기화 완료 (생성 모델: {self.generation_model})")

    def generate_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_completion_tokens: int = 1024,
        thinking_effort: str = "none",
    ) -> str:
        """HyperCLOVA X Chat Completion v3 (추론) 호출 메서드

        HCX-007은 Chat Completions v3 API(/v3/chat-completions/{modelName})에서만
        동작하며, maxTokens/stop/repeatPenalty 등 v1 필드는 사용할 수 없다.
        """
        url = f"{self.base_url}/v3/chat-completions/{self.generation_model}"

        payload = {
            "messages": messages,
            "thinking": {"effort": thinking_effort},  # none|low|medium|high
            "topP": 0.8,
            "topK": 0,
            "maxCompletionTokens": max_completion_tokens,
            "temperature": temperature,
            "repetitionPenalty": 1.1,  # 0.0 < x <= 2.0 (5.0은 유효 범위 밖)
            "includeAiFilters": True,
        }

        # 요청 추적용 고유 ID (요청마다 다르게)
        request_headers = {
            **self.headers,
            "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid.uuid4()),
        }

        t0 = time.perf_counter()
        try:
            res = requests.post(url, headers=request_headers, json=payload, timeout=30)
            elapsed_sec = round(time.perf_counter() - t0, 3)
            res.raise_for_status()
            result = res.json()
            # [2026-07 추가, 2026-08-11 확장] 호출 자체는 성공/실패와
            # 무관하게(과금은 요청이 나간 시점에 이미 발생) 여기서
            # 카운트한다. usage 필드가 있으면(NCP CLOVA Studio 응답에 토큰
            # 사용량이 담겨 오는 경우) 같이 보관해 나중에 더 정확한 비용
            # 추정에 쓸 수 있게 한다. 왕복시간(elapsed_sec)도 usage와 같은
            # 딕셔너리에 넣어 같은 호출 한 건의 기록으로 함께 남긴다(위
            # _usage_counters 주석 참고 - 별도 로그로 쪼개지 않는 이유).
            usage = dict(result.get("result", {}).get("usage") or result.get("usage") or {})
            usage["elapsed_sec"] = elapsed_sec
            _record_hcx_call(usage)
            # 추론 모델 응답은 message.content(최종 답변)와 message.thinkingContent(추론 과정)로
            # 분리됨. 멀티턴 대화에 이어 붙일 때는 content만 사용해야 함.
            return result.get("result", {}).get("message", {}).get("content", "")
        except Exception as e:
            elapsed_sec = round(time.perf_counter() - t0, 3)
            _record_hcx_call({"elapsed_sec": elapsed_sec, "error": str(e)[:200]})
            logger.error(f"[HCX Completion 예외 발생]: {e}")
            return ""


class KosisApiClient:

    def __init__(self):
        self.api_key = config.KOSIS_API_KEY
        self.search_url = config.KOSIS_SEARCH_URL
        self.data_url = config.KOSIS_DATA_URL
        self.meta_url = "https://kosis.kr/openapi/statisticsMeta.do"
        logger.info("KOSIS API Client 초기화 및 엔드포인트 바인딩 완료")

    def search_metadata(
        self, keyword: str, result_count: int = 20
    ) -> List[Dict[str, Any]]:
        """[Step 1] 통계표 검색 (DT_2OEEM1012 최우선 타겟팅 보장)

        result_count: 기본은 20이지만, "실업률"처럼 일부러 넓게 검색하는
        수식어 제거 검색어("청년 실업률" -> "실업률")는 워낙 흔한 단어라
        KOSIS 자체 기본 정렬에서 상위 20위 안에도 정작 찾는 표가 안 들 수
        있다(2026-07 실측). 이런 경우 호출부가 더 큰 값을 넘겨서 검색
        범위를 넓힐 수 있게 파라미터화한다.
        """
        logger.info(f"[Step 1. 통계표 검색] 검색어: '{keyword}'")
        params = {
            "method": "getList",
            "apiKey": self.api_key,
            "searchNm": keyword,
            "format": "json",
            "resultCount": str(result_count),
        }
        try:
            res = requests.get(self.search_url, params=params, timeout=10)
            _record_kosis_call("search_metadata")
            result = fix_and_parse_kosis_json(res.text.strip())

            if isinstance(result, list) and len(result) > 0:
                target_table = None
                for cand in result:
                    tbl_id = cand.get("TBL_ID", "")
                    tbl_nm = cand.get("TBL_NM", "")

                    if tbl_id == "DT_2OEEM1012" or "국가 통화" in tbl_nm:
                        target_table = cand
                        break

                if target_table:
                    result.remove(target_table)
                    result.insert(0, target_table)
                else:
                    if "최저임금" in keyword:
                        fallback_table = {
                            "ORG_ID": "101",
                            "TBL_ID": "DT_2OEEM1012",
                            "TBL_NM": "국가 통화 단위(NCU)로 표시된 경상 가격의 최저임금",
                        }
                        result.insert(0, fallback_table)

                top = result[0]
                logger.info(
                    f"  └─ [Step 1 성공] 타겟 통계표 확정: [{top.get('ORG_ID')}_{top.get('TBL_ID')}] '{top.get('TBL_NM')}'"
                )
                return result
            else:
                if "최저임금" in keyword:
                    return [{
                        "ORG_ID": "101",
                        "TBL_ID": "DT_2OEEM1012",
                        "TBL_NM": "국가 통화 단위(NCU)로 표시된 경상 가격의 최저임금",
                    }]
                return []
        except Exception as e:
            logger.error(f"  └─ [Step 1 예외 발생]: {e}")
            return []

    def get_itm_meta_list(self, org_id: str, tbl_id: str) -> List[Dict[str, Any]]:
        """[컬럼 후보 조회] 통계표의 전체 ITM(항목) 메타 목록을 반환합니다.
        get_initial_dimension_count는 대표 1개만 반환하므로, 여러 후보 중
        정확한 컬럼을 골라야 하는 대화형 시나리오에서는 이 메서드를 사용합니다.
        """
        try:
            res = requests.get(
                self.meta_url,
                params={
                    "method": "getMeta",
                    "apiKey": self.api_key,
                    "orgId": org_id,
                    "tblId": tbl_id,
                    "type": "ITM",
                    "format": "json",
                },
                timeout=5,
            )
            _record_kosis_call("get_itm_meta_list")
            data = fix_and_parse_kosis_json(res.text.strip())
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"[ITM 메타 목록 조회 예외]: {e}")
            return []

    def get_obj_meta_list(self, org_id: str, tbl_id: str) -> List[Dict[str, Any]]:
        """[분류값 후보 조회] 통계표의 OBJ(분류) 메타 전체 목록을 반환합니다.

        get_initial_dimension_count는 OBJ_ID만 모아 차원 개수를 세는 데
        쓰지만, 여기서는 분류값 이름(예: "항공기 정비", "1-4인")까지 필요한
        컬럼/분류 이름 기반 검색(딥서치)에 쓴다.

        주의: KOSIS 공식 문서에 이 응답의 필드명이 명확히 나와 있지 않아
        (OBJ_NM/NM 등 후보로 방어적으로 파싱한다), 실제 API 키로 한 번
        검증해보는 것을 권장한다.
        """
        try:
            res = requests.get(
                self.meta_url,
                params={
                    "method": "getMeta",
                    "apiKey": self.api_key,
                    "orgId": org_id,
                    "tblId": tbl_id,
                    "type": "OBJ",
                    "format": "json",
                },
                timeout=5,
            )
            _record_kosis_call("get_obj_meta_list")
            data = fix_and_parse_kosis_json(res.text.strip())
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"[OBJ 메타 목록 조회 예외]: {e}")
            return []

    def get_period_meta(self, org_id: str, tbl_id: str) -> List[Dict[str, Any]]:
        """[수록정보] 이 통계표가 실제로 어떤 주기(연/분기/월 등)로 데이터를
        제공하는지 조회합니다 (getMeta type=PRD).

        2026-07 실측: "청년 실업률"을 물었는데 코드가 사용자 발화의
        "3월"만 보고 무조건 prdSe="M"으로 요청했다가, 정작 그 표
        (청년실업률(시도))는 분기/연간 데이터만 있고 월간 자체가 없어서
        objL/itmId를 다 맞게 넣어도 항상 err:30("데이터가 존재하지
        않습니다")만 났다. MCP로 직접 확인할 때는 kosis_table_info
        (type=PRD)로 이 표가 지원하는 주기를 먼저 확인한 뒤에 조회했다 -
        이 메서드는 그 확인 과정을 코드로 옮긴 것으로, fetch 전에 먼저
        불러서 사용자가 요청한 주기가 실제로 있는지 검증하는 데 쓴다.

        반환값 예: [{"PRD_SE": "Q", ...}, {"PRD_SE": "Y", ...}] (필드명이
        공식 문서에 명확히 없어 방어적으로 파싱한다).
        """
        try:
            res = requests.get(
                self.meta_url,
                params={
                    "method": "getMeta",
                    "apiKey": self.api_key,
                    "orgId": org_id,
                    "tblId": tbl_id,
                    "type": "PRD",
                    "format": "json",
                },
                timeout=5,
            )
            _record_kosis_call("get_period_meta")
            data = fix_and_parse_kosis_json(res.text.strip())
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"[수록정보 조회 예외]: {e}")
            return []

    def get_table_description(self, org_id: str, tbl_id: str) -> Dict[str, Any]:
        """[통계표설명] 통계표명/기관명/수록정보/분류·항목/주석/단위/출처/
        가중치/자료갱신일을 사람이 읽을 수 있는 형태로 제공하는 공식
        메타 API (https://kosis.kr/openapi/devGuide/devGuide_060101List.do).

        get_itm_meta_list/get_obj_meta_list보다 훨씬 풍부한 설명(특히
        주석)을 담고 있어서, 표를 잘못 골랐는지/이 표에 찾는 개념이 있을
        법한지 LLM이 판단할 때 추가 근거로 쓸 수 있다. 아직 에이전트
        로직에는 배선돼 있지 않고, 필요할 때 호출해서 쓰는 용도로
        추가해둔다.
        """
        # 가이드에 명시된 정확한 엔드포인트: statisticsData.do (자료 조회용
        # self.data_url/self.meta_url과는 별개 경로일 수 있어 하드코딩한다.
        table_desc_url = "https://kosis.kr/openapi/statisticsData.do"
        try:
            res = requests.get(
                table_desc_url,
                params={
                    "method": "getMeta",
                    "apiKey": self.api_key,
                    "type": "TBL",
                    "orgId": org_id,
                    "tblId": tbl_id,
                    "format": "json",
                    "content": "json",
                },
                timeout=5,
            )
            _record_kosis_call("get_table_description")
            data = fix_and_parse_kosis_json(res.text.strip())
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                return data
            return {}
        except Exception as e:
            logger.error(f"[통계표설명 조회 예외]: {e}")
            return {}

    def get_stat_explanation(
        self, org_id: str, tbl_id: str
    ) -> Dict[str, Any]:
        """[통계설명자료] 작성목적/조사대상/조사방법/조사항목 등 이 통계
        (표가 아니라 그 표가 속한 조사 자체)를 훨씬 풍부하게 설명하는
        공식 메타 API (https://kosis.kr/openapi/statisticsExplData.do).

        get_table_description(statisticsData.do?type=TBL)이 "이 표 안에
        어떤 컬럼/분류가 있는지"를 설명한다면, 이건 "이 조사가 애초에
        무엇을 왜 조사했는지"를 설명한다 - 예를 들어 "항공산업실태조사"는
        작성목적에 "항공산업 및 항공연관산업에 대한 구조, 분포 및
        산업활동 실태를 파악..."이라고 나오고, examinObjrange(조사대상
        범위)에는 "항공, 비행, 조종사, 승무원, 정비" 등 실제 업종
        키워드가 길게 나열돼 있다 - 표 제목(TBL_NM)이나 짧은 CONTENTS만
        보고는 알 수 없는, "이 조사 안에 찾는 개념이 있을 법한지"를
        판단하는 데 결정적인 근거가 된다. 2026-07 MCP(kosis_meta)로 실측
        검증 후 추가.

        파라미터 형식은 공개 KOSIS R 클라이언트(seokhoonj/kosis)의
        getStatExpl 구현을 참고해 맞췄다: method=getList, jsonVD/jsonMVD=Y,
        metaItm=ALL.
        """
        stat_expl_url = "https://kosis.kr/openapi/statisticsExplData.do"
        try:
            res = requests.get(
                stat_expl_url,
                params={
                    "method": "getList",
                    "apiKey": self.api_key,
                    "orgId": org_id,
                    "tblId": tbl_id,
                    "format": "json",
                    "jsonVD": "Y",
                    "jsonMVD": "Y",
                    "metaItm": "ALL",
                },
                timeout=5,
            )
            _record_kosis_call("get_stat_explanation")
            data = fix_and_parse_kosis_json(res.text.strip())
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and "err" not in data:
                return data
            return {}
        except Exception as e:
            logger.error(f"[통계설명자료 조회 예외]: {e}")
            return {}

    def verify_table_columns(
        self,
        org_id: str,
        tbl_id: str,
        itm_ids: Optional[List[str]] = None,
        start_year: Optional[str] = None,
        end_year: Optional[str] = None,
        prd_se: str = "Y",
    ) -> List[Dict[str, Any]]:
        """[검증용 Full Scan] 통계표의 실제 컬럼(항목/분류) 구성을 사람이 읽을 수
        있는 이름과 함께 그대로 확인합니다.

        KOSIS 공식 가이드(통계자료 > 통계표선택 방법,
        https://kosis.kr/openapi/devGuide/devGuide_0201List.do)의
        outputFields 파라미터를 사용해 ORG_ID/TBL_ID/TBL_NM/OBJ_ID/OBJ_NM/NM/
        ITM_ID/ITM_NM/UNIT_NM/PRD_SE/PRD_DE/LST_CHN_DE 등 응답필드를 전부
        받아옵니다. itmId/itmNm 매핑이 맞는지, 특정 연도에 실제 데이터가
        존재하는지를 재귀적 차원 추론 없이 눈으로 바로 확인할 때 씁니다.

        objL1~objL8: KOSIS는 파라미터가 "존재"하기만 하면 값이 비어 있어도
        필수 조건을 만족시키는 경우가 있어(예: 차원 없는 테이블), 차원 수를
        몰라도 8개를 전부 빈 값으로 채워 한 번에 요청합니다.
        """
        itm_param = "+".join(itm_ids) if itm_ids else "all"

        output_fields = " ".join([
            "ORG_ID", "TBL_ID", "TBL_NM",
            "OBJ_ID", "OBJ_NM", "OBJ_NM_ENG", "NM", "NM_ENG",
            "ITM_ID", "ITM_NM", "ITM_NM_ENG",
            "UNIT_NM", "UNIT_NM_ENG",
            "PRD_SE", "PRD_DE", "LST_CHN_DE",
        ])

        params: Dict[str, Any] = {
            "method": "getList",
            "apiKey": self.api_key,
            "orgId": org_id,
            "tblId": tbl_id,
            "itmId": itm_param,
            "format": "json",
            "jsonVD": "Y",
            "prdSe": prd_se,
            "outputFields": output_fields,
        }
        for i in range(1, 9):
            params[f"objL{i}"] = ""

        if start_year or end_year:
            params["startPrdDe"] = start_year or end_year
            params["endPrdDe"] = end_year or start_year
        else:
            params["newEstPrdCnt"] = "5"

        try:
            res = requests.get(self.data_url, params=params, timeout=10)
            _record_kosis_call("verify_table_columns")
            data = fix_and_parse_kosis_json(res.text.strip())
            if isinstance(data, list):
                logger.info(
                    f"[Full Scan 검증] {org_id}_{tbl_id} -> {len(data)}건 컬럼/값 확인"
                )
                return data
            logger.warning(f"[Full Scan 검증] 예상치 못한 응답 형식: {data}")
            return []
        except Exception as e:
            logger.error(f"[Full Scan 검증 예외]: {e}")
            return []

    def get_initial_dimension_count(
        self, org_id: str, tbl_id: str
    ) -> Tuple[str, int]:
        """[Step 2] 메타 정보 분석 (ITM 및 OBJ 차원 수 감지)

        OBJ 메타가 없는 테이블(예: DT_2OEEM1012)이 실제로 존재하므로, 기본
        차원 수는 1이 아니라 0에서 시작한다. 실제로 차원이 더 필요한 경우는
        Step 3의 bounded retry(err 20/21)가 늘려가며 찾아낸다.
        """
        logger.debug(
            f"[Step 2. 메타 정보 분석 개시] ORG_ID: '{org_id}', TBL_ID: '{tbl_id}'"
        )
        itm_id = "all"
        dim_count = 0  # 차원이 없는 테이블을 기본으로 가정 (기존: 1 -> 잘못된 objL 파라미터 유발)

        try:
            res = requests.get(
                self.meta_url,
                params={
                    "method": "getMeta",
                    "apiKey": self.api_key,
                    "orgId": org_id,
                    "tblId": tbl_id,
                    "type": "ITM",
                    "format": "json",
                },
                timeout=5,
            )
            _record_kosis_call("get_initial_dimension_count.itm")
            itm_data = fix_and_parse_kosis_json(res.text.strip())
            if isinstance(itm_data, list) and len(itm_data) > 0:
                itm_id = (
                    itm_data[0].get("ITM_ID")
                    or itm_data[0].get("itmId")
                    or "all"
                )
                logger.debug(
                    f"  ├─ [ITM 메타] 대표 itmId: '{itm_id}' ({itm_data[0].get('ITM_NM', '명칭없음')})"
                )
            else:
                logger.warning(
                    f"  ├─ [ITM 메타] 조회 실패/빈값, 기본값 'all' 적용. Raw: {res.text[:100]}"
                )
        except Exception as e:
            logger.error(f"  ├─ [ITM 메타 예외]: {e}")

        try:
            res = requests.get(
                self.meta_url,
                params={
                    "method": "getMeta",
                    "apiKey": self.api_key,
                    "orgId": org_id,
                    "tblId": tbl_id,
                    "type": "OBJ",
                    "format": "json",
                },
                timeout=5,
            )
            _record_kosis_call("get_initial_dimension_count.obj")
            obj_data = fix_and_parse_kosis_json(res.text.strip())
            if isinstance(obj_data, list) and len(obj_data) > 0:
                unique_objs = {
                    item.get("OBJ_ID")
                    for item in obj_data
                    if item.get("OBJ_ID")
                }
                if len(unique_objs) > 0:
                    dim_count = len(unique_objs)
                logger.debug(
                    f"  └─ [OBJ 메타] 감지된 차원 목록: {sorted(list(unique_objs))} | 감지된 차원수: {dim_count}개"
                )
            else:
                logger.debug(
                    f"  └─ [OBJ 메타] 차원 정보 없음 -> 차원 없는 테이블로 간주하고 {dim_count}개로 시작합니다. Raw: {res.text[:100]}"
                )
        except Exception as e:
            logger.error(f"  └─ [OBJ 메타 예외]: {e}")

        logger.debug(
            f"[Step 2. 최종 추론 결과] 설정된 itmId: '{itm_id}', 시작 차원수: {dim_count}개"
        )
        return itm_id, dim_count

    def fetch_actual_statistics_bounded_retry(
        self,
        org_id: str,
        tbl_id: str,
        start_year: str,
        end_year: str,
        itm_id: str = "all",
        current_dim: int = 0,
        max_dim: int = 8,
        prd_se: str = "Y",
        objl_fixed: Optional[Dict[int, str]] = None,
    ) -> List[Dict[str, Any]]:
        """[Step 3] Bounded Retry 기반 실데이터 수집

        err 20(필수 차원 부족)뿐 아니라 err 21(잘못된 요청 변수)도 차원 수
        불일치로 인한 것일 수 있으므로 함께 재시도 대상에 포함한다.

        objl_fixed: {축 번호: 코드값} - 특정 objL 위치에 "all" 대신 정확한
        분류 코드를 강제로 넣고 싶을 때 사용한다(예: {1: "A0201"}이면
        objL1="A0201"). resolve_target_item이 컬럼이 아니라 OBJ 분류값을
        찾아낸 경우, 그 축만 서버에서 미리 필터링해서 응답 크기를 줄이고
        결과를 정확하게 만드는 데 쓴다. 지정 안 된 축은 기존처럼 "all".
        """
        valid_itm_id = itm_id if itm_id else "all"

        params = {
            "method": "getList",
            "apiKey": self.api_key,
            "orgId": org_id,
            "tblId": tbl_id,
            "format": "json",
            "jsonVD": "Y",
            "prdSe": prd_se,
            "startPrdDe": start_year,
            "endPrdDe": end_year,
            "itmId": valid_itm_id,
        }

        objl_fixed = objl_fixed or {}
        for i in range(1, current_dim + 1):
            params[f"objL{i}"] = objl_fixed.get(i, "all")

        log_params = {
            k: ("***" if k == "apiKey" else v) for k, v in params.items()
        }
        logger.debug(
            f"[Step 3. 데이터 요청 #{current_dim}] 차원수 {current_dim}개 | Params: {log_params}"
        )

        try:
            res = requests.get(self.data_url, params=params, timeout=10)
            _record_kosis_call("fetch_actual_statistics_bounded_retry")
            raw_text = res.text.strip()

            if "err" in raw_text and "errMsg" in raw_text:
                # [2026-07 변경] err 20/21은 "차원 수를 하나 늘려서 다시
                # 시도해보는" 정상적인 탐색 과정에서 거의 매번 뜨는 예상된
                # 신호라 WARNING이 아니라 DEBUG로 낮췄다 - 실제 문제 신호는
                # 아래 "[최종 실패]"(모든 재시도 소진 후)만으로 충분하다.
                logger.debug(f"  └─ [KOSIS 응답 에러] Raw Response: {raw_text}")
                err_data = json.loads(raw_text)
                err_code = str(err_data.get("err"))

                # 20: 필수 차원 부족 / 21: 잘못된 요청 변수(차원 수 불일치로도 발생)
                # 두 경우 모두 차원을 하나 늘려서 재시도한다.
                if err_code in ("20", "21") and current_dim < max_dim:
                    logger.debug(
                        f"  └─ [에러 {err_code} 감지] 차원 불일치 추정 -> 차원 확장 재시도 ({current_dim}개 -> {current_dim + 1}개)"
                    )
                    return self.fetch_actual_statistics_bounded_retry(
                        org_id,
                        tbl_id,
                        start_year,
                        end_year,
                        itm_id=valid_itm_id,
                        current_dim=current_dim + 1,
                        max_dim=max_dim,
                        prd_se=prd_se,
                        objl_fixed=objl_fixed,
                    )

                logger.error(
                    f"  └─ [최종 실패] KOSIS 에러코드 [{err_code}]: {err_data.get('errMsg')}"
                )
                return []

            raw_list = fix_and_parse_kosis_json(raw_text)
            if not isinstance(raw_list, list):
                logger.error(
                    f"  └─ [파싱 실패] 응답 데이터가 배열 형식이 아닙니다. Raw: {raw_text[:200]}"
                )
                return []

            refined = []
            for item in raw_list:
                categories = [
                    item.get(f"C{i}_NM")
                    for i in range(1, 9)
                    if item.get(f"C{i}_NM") and item.get(f"C{i}_NM") != "전체"
                ]
                refined.append({
                    "date": item.get("PRD_DE"),
                    "indicator": item.get("ITM_NM"),
                    "value": item.get("DT"),
                    "unit": item.get("UNIT_NM"),
                    "category_path": (
                        " > ".join(categories) if categories else "전체"
                    ),
                    "raw_dict": item,
                })
            logger.debug(
                f"  └─ [수집 성공] {len(refined)}건 레코드 정제 완료 (최종 차원수: {current_dim}개)"
            )
            return refined

        except Exception as e:
            logger.error(f"  └─ [Step 3 통신 예외]: {e}")
            return []