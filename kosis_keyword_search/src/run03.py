"""
KOSIS keyword search MVP.

역할:
2번 모듈이 만든 확장 키워드를 KOSIS 통합검색 OpenAPI에 넣고,
각 키워드가 KOSIS에서 검색 결과를 가지는지 True/False로 반환한다.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KOSIS_SEARCH_API_URL = "https://kosis.kr/openapi/statisticsSearch.do"

def load_json(file_path: Path) -> Any:
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_role2_data(data: Any) -> list[dict[str, Any]]:
    """
    Role2 데이터가 list여도, dict여도 claim 리스트로 맞춘다.
    """
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if "claims" in data and isinstance(data["claims"], list):
            return data["claims"]

        return [data]

    raise ValueError("입력 JSON은 dict 또는 list 형태여야 합니다.")


def convert_claim(item: dict[str, Any]) -> dict[str, Any]:
    """
    Role2 데이터에서 claim_id, metric, keywords만 뽑아
    검색 로직 입력 형식인 expanded_keywords로 변환한다.
    """
    claim_id = item.get("claim_id", "claim_unknown")
    metric = item.get("metric", "")

    result = item.get("result", {})
    keywords = item.get("keywords") or result.get("keywords", [])

    if not isinstance(keywords, list):
        raise ValueError(f"{claim_id}의 keywords는 list 형태여야 합니다.")

    cleaned_keywords = []
    seen = set()

    for keyword in keywords:
        keyword = str(keyword).strip()

        if not keyword:
            continue

        if keyword in seen:
            continue

        seen.add(keyword)
        cleaned_keywords.append(keyword)

    return {
        "claim_id": claim_id,
        "metric": metric,
        "expanded_keywords": cleaned_keywords,
    }

def load_api_key() -> str:
    """
    .env 파일에서 KOSIS_API_KEY를 읽어온다.
    python-dotenv를 설치하지 않아도 실행되도록 직접 읽는 방식이다.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"

    if os.getenv("KOSIS_API_KEY"):
        return os.getenv("KOSIS_API_KEY", "").strip()

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

def build_search_url(
    keyword: str,
    api_key: str,
    sort: str = "RANK",
    start_count: int = 1,
    result_count: int = 10,
    org_id: str | None = None,
) -> str:
    """
    KOSIS 통합검색 API 요청 URL을 만든다.

    sort:
    - RANK: 정확도순
    - DATE: 최신순
    """
    params = {
        "method": "getList",
        "apiKey": api_key,
        "format": "json",
        "searchNm": keyword,
        "sort": sort,
        "startCount": str(start_count),
        "resultCount": str(result_count),
    }

    if org_id:
        params["orgId"] = org_id

    return f"{KOSIS_SEARCH_API_URL}?{urlencode(params)}"


def fetch_json(url: str) -> Any:
    """KOSIS API 응답을 읽고, KOSIS식 응답을 Python dict/list로 변환한다."""
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=15) as response:
        raw_text = response.read().decode("utf-8")

    if not raw_text.strip():
        raise ValueError("KOSIS API 응답이 비어 있습니다.")

    try:
        return json.loads(raw_text)     # 이 부분이 불필요한 부분을 제거하고 쌍따옴표를 붙여서
    except json.JSONDecodeError:        # JSON으로 변환하는 작업을 수행한다.
        fixed_text = re.sub(
            r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
            r'\1"\2"\3',
            raw_text,
        )
        return json.loads(fixed_text)


def normalize_response_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        if data.get("err") == "30":
            return []

        for key in ("result", "data", "list", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    return []


def search_keyword(
    keyword: str,
    api_key: str,
    result_count: int = 10,
) -> dict[str, Any]:
    """
    키워드 1개를 KOSIS 통합검색에서 검색한다.
    결과가 1개 이상 있으면 exists=True로 판단한다.
    """
    url = build_search_url(
        keyword=keyword,
        api_key=api_key,
        result_count=result_count,
    )

    try:
        data = fetch_json(url)
        items = normalize_response_items(data)

        return {
            "keyword": keyword,
            "exists": len(items) > 0,
            "status": "success",
            "result_count": len(items),
            "top_results": simplify_top_results(items)
        }

    except Exception as error:
        return {
            "keyword": keyword,
            "exists": False,
            "status": "error",
            "result_count": 0,
            "top_results": [],
            "error_reason": str(error),
        }


def simplify_top_results(items: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """
    검색 결과 전체를 다 넘기면 너무 길어질 수 있어서,
    상위 몇 개만 보기 쉬운 형태로 정리한다.
    """
    simplified = []

    for item in items[:limit]:
        simplified.append(
            {
                "org_id": pick_first(item, ["ORG_ID", "orgId", "org_id"]),
                "tbl_id": pick_first(item, ["TBL_ID", "tblId", "tbl_id"]),
                "title": pick_first(
                    item,
                    ["TBL_NM", "tblNm", "title", "TITLE", "STAT_NM", "statNm"],
                ),
            }
        )

    return simplified


def pick_first(data: dict[str, Any], keys: list[str]) -> Any:
    """여러 후보 key 중 실제로 존재하는 값을 하나 고른다."""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def search_keywords(claim_id: str, keywords: list[str]) -> dict[str, Any]:
    """
    키워드 여러 개를 순서대로 검색하고 전체 결과 JSON을 만든다.
    """
    api_key = load_api_key()
    results = []

    for keyword in keywords:
        clean_keyword = keyword.strip()
        if not clean_keyword:
            continue

        results.append(search_keyword(clean_keyword, api_key))

    matched_keywords = [
        result["keyword"]
        for result in results
        if result["exists"] is True
    ]

    return {
        "claim_id": claim_id,
        "module": "kosis_keyword_search",
        "status": "success",
        "searched_count": len(results),
        "matched_count": len(matched_keywords),
        "results": results,
        "matched_keywords": matched_keywords,
    }

def search_claims(claims: list[dict[str, Any]]) -> dict[str, Any]:
    """
    여러 claim을 받아서 claim별로 키워드 검색을 실행한다.
    """
    claim_results = []
    start_time = time.perf_counter()

    for claim in claims:
        claim_id = claim["claim_id"]
        metric = claim.get("metric", "")
        keywords = claim.get("expanded_keywords", [])

        result = search_keywords(claim_id, keywords)

        claim_results.append(
            {
                "claim_id": result["claim_id"],
                "metric": metric,
                "module": result["module"],
                "status": result["status"],
                "searched_count": result["searched_count"],
                "matched_count": result["matched_count"],
                "results": result["results"],
                "matched_keywords": result["matched_keywords"],
            }
        )

    total_search_count = sum(
        claim["searched_count"]
           for claim in claim_results
    )

    elapsed_seconds = round(time.perf_counter() - start_time, 3)

    return {
        "module": "kosis_keyword_search",
        "status": "success",
        "claim_count": len(claim_results),
        "total_search_count": total_search_count,
        "elapsed_seconds": elapsed_seconds,
        "claims": claim_results,
    }


def load_keywords_from_json(file_path: str) -> list[dict[str, Any]]:
    """
    입력 JSON에서 claim별 검색 키워드를 읽는다.

    지원 형식:
    [
      {
        "claim_id": "claim_001",
        "metric": "...",
        "expanded_keywords": [...]
      },
      {
        "claim_id": "claim_002",
        "metric": "...",
        "expanded_keywords": [...]
      }
    ]

    단일 claim dict가 들어와도 list로 감싸서 처리한다.
    """
    path = Path(file_path)

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        raise ValueError("입력 JSON은 dict 또는 list 형태여야 합니다.")

    claims = []

    for item in data:
        if not isinstance(item, dict):
            continue

        claim_id = item.get("claim_id", "claim_unknown")
        metric = item.get("metric", "")
        keywords = item.get("expanded_keywords", [])

        if not isinstance(keywords, list):
            raise ValueError(f"{claim_id}의 expanded_keywords는 리스트 형태여야 합니다.")

        cleaned_keywords = []
        seen = set()

        for keyword in keywords:
            keyword = str(keyword).strip()

            if not keyword:
                continue

            if keyword in seen:
                continue

            seen.add(keyword)
            cleaned_keywords.append(keyword)

        claims.append(
            {
                "claim_id": claim_id,
                "metric": metric,
                "expanded_keywords": cleaned_keywords,
            }
        )

    return claims

def save_result_json(result: dict[str, Any], file_path: str) -> None:
    """검색 결과를 JSON 파일로 저장한다."""
    path = Path(file_path)

    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def main() -> None:
    """
    Role2 원본 키워드 데이터를 읽고,
    내부에서 검색용 형식으로 변환한 뒤,
    바로 KOSIS 키워드 검색을 실행한다.
    """
    base_dir = Path(__file__).resolve().parent

    input_path = base_dir / "role2_sample_keywords3.json"
    output_path = base_dir / "run03_result.json"

    role2_data = load_json(input_path)
    raw_claims = normalize_role2_data(role2_data)

    converted_claims = [
        convert_claim(claim)
        for claim in raw_claims
    ]

    result = search_claims(converted_claims)

    save_result_json(result, str(output_path))

    print("KOSIS 키워드 검색 결과")
    print("-" * 40)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("-" * 40)
    print(f"입력 파일: {input_path}")
    print(f"결과 저장 위치: {output_path}")

if __name__ == "__main__":
    main()
