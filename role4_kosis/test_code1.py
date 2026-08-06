"""
test_code1.py

이 파일은 Role4 Lookup 모듈의 첫 번째 API 연결 확인용 실험 코드이다.
목표는 KOSIS API에서 실제 통계값을 가져오고,
그 값을 최소 Evidence JSON 형태로 출력할 수 있는지 확인하는 것이다.

아직 자동 차원 매칭, 후보 여러 개 탐색, 계산형 Claim 처리는 하지 않는다.
"""

import json
import urllib.parse
import urllib.request

import os
from pathlib import Path


def load_env():
    """
    .env 파일에서 KOSIS_API_KEY를 읽어오는 간단한 함수.
    python-dotenv 없이도 동작하게 직접 구현한다.
    """
    env_path = Path(__file__).resolve().parent / ".env"

    if not env_path.exists():
        raise FileNotFoundError(f".env 파일을 찾을 수 없습니다: {env_path}")

    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            key, _, value = line.partition("=")

            if key.strip() == "KOSIS_API_KEY":
                return value.strip().strip('"').strip("'")

    raise KeyError(".env 파일에 KOSIS_API_KEY가 없습니다.")

def mask_api_key(url):
    """
    URL에서 apiKey 파라미터 값만 안전하게 숨긴다.
    URL 인코딩 여부와 상관없이 동작한다.
    """
    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed.query)

    if "apiKey" in query_params:
        query_params["apiKey"] = ["***"]

    masked_query = urllib.parse.urlencode(query_params, doseq=True)

    return urllib.parse.urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            masked_query,
            parsed.fragment,
        )
    )

def build_kosis_params( #입력들을 받아서 KOSIS API용 파라미터로 포장해주는 함수. 
    org_id,
    tbl_id,
    itm_id,
    obj_l1,
    obj_l2,
    prd_se,
    start_period,
    end_period,
    obj_l3=None,
    obj_l4=None,
):
    """
    KOSIS API 요청에 필요한 파라미터를 만든다.
    obj_l3, obj_l4는 없는 통계표도 많으므로 선택값으로 둔다.
    """

    params = {
        "method": "getList",
        "apiKey": load_env(),
        "format": "json",
        "jsonVD": "Y",
        "userStatsId": f"test/{org_id}/{tbl_id}/2/1/{start_period}/{end_period}",
        "prdSe": prd_se,
        "startPrdDe": start_period,
        "endPrdDe": end_period,
        "orgId": org_id,
        "tblId": tbl_id,
        "itmId": itm_id,
        "objL1": obj_l1,
        "objL2": obj_l2,
    }

    if obj_l3 is not None:
        params["objL3"] = obj_l3

    if obj_l4 is not None:
        params["objL4"] = obj_l4

    return params

def fetch_kosis_data( #특정 통계표 전용이 아닌, 입력값을 받아 조회하는 함수
    org_id,
    tbl_id,
    itm_id,
    obj_l1,
    obj_l2,
    prd_se,
    start_period,
    end_period,
    obj_l3=None,
    obj_l4=None,
):
    """
    인자로 받은 KOSIS 조회 조건을 이용해 실제 KOSIS API를 호출한다.
    """

    base_url = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

    params = build_kosis_params(
        org_id=org_id,
        tbl_id=tbl_id,
        itm_id=itm_id,
        obj_l1=obj_l1,
        obj_l2=obj_l2,
        prd_se=prd_se,
        start_period=start_period,
        end_period=end_period,
        obj_l3=obj_l3,
        obj_l4=obj_l4,
    )

    query_string = urllib.parse.urlencode(params)
    url = f"{base_url}?{query_string}"

    print("KOSIS API 호출 URL:")
    print(mask_api_key(url))
    print()

    with urllib.request.urlopen(url, timeout=15) as response:
        raw_data = response.read().decode("utf-8")

    data = json.loads(raw_data)

    return data, params, url


def extract_first_row(data):
    """
    KOSIS 응답에서 첫 번째 통계값 row를 꺼낸다.
    보통 KOSIS API 응답은 list 형태로 온다.
    """

    if not data:
        raise ValueError("KOSIS 응답 데이터가 비어 있습니다.")

    if isinstance(data, dict) and "err" in data:
        raise ValueError(f"KOSIS API 오류: {data}")

    if not isinstance(data, list):
        raise TypeError(f"예상하지 못한 응답 형식입니다: {type(data)}")

    return data[0]


def build_evidence(row, params, url):
    """
    5번 모듈에 넘길 수 있는 최소 Evidence JSON을 만든다.
    """

    value = row.get("DT")
    unit = row.get("UNIT_NM")
    period = row.get("PRD_DE")
    table_name = row.get("TBL_NM")
    item_name = row.get("ITM_NM")
    region_name = row.get("C1_NM")
    category_name = row.get("C2_NM")
    news_value = 115.71
    kosis_value = float(value)

    evidence = {
        "claim_id": "claim_001",
        "lookup_status": "success",
        "claim_text": "2025년 1월 소비자물가지수는 115.71이다.",
        "selected_candidate": {
            "org_id": params["orgId"],
            "tbl_id": params["tblId"],
            "table_name": table_name,
            "stat_name": "소비자물가조사",
            "selection_reason": "소비자물가지수 총지수, 전국, 2025년 1월 조건으로 KOSIS 값을 조회함",
        },
        "claim_items": [
            {
                "claim_item_id": "claim_001_1",
                "target_metric": "소비자물가지수 총지수",
                "news_value": news_value,
                "news_unit": "2020=100",
                "evidence_type": "single_value",
                "evidence_values": [
                    {
                        "name": "consumer_price_index",
                        "label": "2025년 1월 전국 소비자물가지수 총지수",
                        "metric": item_name,
                        "category": category_name,
                        "region": region_name,
                        "period": period,
                        "value": kosis_value,
                        "unit": unit,
                        "query_params": {
                            "orgId": params["orgId"],
                            "tblId": params["tblId"],
                            "itmId": params["itmId"],
                            "objL1": params["objL1"],
                            "objL2": params["objL2"],
                            "prdSe": params["prdSe"],
                            "startPrdDe": params["startPrdDe"],
                            "endPrdDe": params["endPrdDe"],
                        },
                    }
                ],
                "calculated_values": [],
            }
        ],
        "source_url": mask_api_key(url),
        "warnings": [],
    }

    return evidence


def main():
    data, params, url = fetch_kosis_data(
    org_id="101",
    tbl_id="DT_1J22112",
    itm_id="T",
    obj_l1="T10",
    obj_l2="0",
    prd_se="M",
    start_period="202501",
    end_period="202501",
    )
    row = extract_first_row(data)
    evidence = build_evidence(row, params, url)
    news_value = 115.71
    kosis_value = float(row.get("DT"))

    print("조회 결과 요약")
    print("-" * 40)
    print(f"통계표명: {row.get('TBL_NM')}")
    print(f"항목명: {row.get('ITM_NM')}")
    print(f"분류1: {row.get('C1_NM')}")
    print(f"분류2/지역: {row.get('C2_NM')}")
    print(f"시점: {row.get('PRD_DE')}")
    print(f"값: {row.get('DT')}")
    print(f"단위: {row.get('UNIT_NM')}")
    print(f"뉴스 수치: {news_value}")
    print(f"KOSIS 수치: {kosis_value}")
    print(f"일치 여부: {news_value == kosis_value}")
    print()

    print("최소 Evidence JSON")
    print("-" * 40)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()