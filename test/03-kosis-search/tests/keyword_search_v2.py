"""
KOSIS keyword search v2.

목표:
- 기존 keyword_search.py는 건드리지 않는다.
- 검색 키워드를 KOSIS 통합검색 API에 넣는다.
- 04번 task 팀원이 요청한 search_results 형태로만 출력한다.
"""

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KOSIS_SEARCH_API_URL = "https://kosis.kr/openapi/statisticsSearch.do"


def load_api_key() -> str:
    """kosis_keyword_search/.env 파일에서 KOSIS_API_KEY를 읽는다."""
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
) -> str:
    """KOSIS 통합검색 API 요청 URL을 만든다."""
    params = {
        "method": "getList",
        "apiKey": api_key,
        "format": "json",
        "jsonVD": "Y",
        "searchNm": keyword,
        "sort": sort,
        "startCount": str(start_count),
        "resultCount": str(result_count),
    }

    return f"{KOSIS_SEARCH_API_URL}?{urlencode(params)}"


def fetch_json(url: str) -> Any:
    """
    KOSIS API 응답을 Python 객체로 변환한다.
    KOSIS 응답은 key에 따옴표가 빠진 유사 JSON 형태로 올 수 있어서 보정한다.
    """
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
        return json.loads(raw_text)
    except json.JSONDecodeError:
        fixed_text = re.sub(
            r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
            r'\1"\2"\3',
            raw_text,
        )
        return json.loads(fixed_text)


def normalize_response_items(data: Any) -> list[dict[str, Any]]:
    """KOSIS 응답을 검색 결과 리스트로 변환한다."""
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


def to_list(value: Any) -> list[str]:
    """값을 항상 리스트 형태로 맞춘다."""
    if value in (None, ""):
        return []

    return [str(value).strip()]

def split_path(value: Any) -> list[str]:
    """
    MT_ATITLE 같은 경로 문자열을 리스트로 나눈다.
    예: '국내통계 > 고용·노동 > 고용'
    """
    if value in (None, ""):
        return []

    text = str(value)
    parts = re.split(r"\s*>\s*", text)

    return [part.strip() for part in parts if part.strip()]

def build_contents_list(item: dict[str, Any], query: str) -> list[str]:
    contents = []

    for key in ("CONTENTS", "ITEM03", "STAT_NM", "TBL_NM"):
        value = item.get(key)
        if value not in (None, ""):
            contents.append(str(value).strip())

    if query:
        contents.append(query)

    return contents

def convert_to_v2_result(item: dict[str, Any], query: str) -> dict[str, Any]:
    return {
        "ORG_NM": item.get("ORG_NM"),
        "TBL_NM": item.get("TBL_NM"),
        "in_MT_ATITLE": split_path(item.get("MT_ATITLE")),
        "in_CONTENTS": build_contents_list(item, query),
        "QUERY": query,
    }


def search_keyword_v2(
    keyword: str,
    api_key: str,
    result_count: int = 10,
) -> list[dict[str, Any]]:
    """키워드 1개를 검색하고, v2 search_results 형식으로 변환한다."""
    url = build_search_url(
        keyword=keyword,
        api_key=api_key,
        result_count=result_count,
    )

    data = fetch_json(url)
    items = normalize_response_items(data)

    return [convert_to_v2_result(item, keyword) for item in items[:3]]


def load_search_keywords(file_path: str) -> list[str]:
    """
    검색 키워드 입력 파일을 읽는다.

    지원 형태 1:
    {
      "search_keywords": ["취업자", "취업자 수"]
    }

    지원 형태 2:
    ["취업자", "취업자 수"]
    """
    path = Path(file_path)

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return [str(keyword).strip() for keyword in data if str(keyword).strip()]

    if isinstance(data, dict):
        keywords = data.get("search_keywords", [])

        if not isinstance(keywords, list):
            raise ValueError("search_keywords는 리스트 형태여야 합니다.")

        return [str(keyword).strip() for keyword in keywords if str(keyword).strip()]

    raise ValueError("입력 파일은 리스트 또는 search_keywords를 가진 JSON 객체여야 합니다.")


def search_keywords_v2(keywords: list[str]) -> dict[str, Any]:
    """
    여러 키워드를 검색하고 search_results만 담은 최종 결과를 만든다.
    """
    api_key = load_api_key()
    search_results = []

    for keyword in keywords:
        try:
            search_results.extend(search_keyword_v2(keyword, api_key))
        except Exception as error:
            search_results.append(
                {
                    "in_ORG_NM": [],
                    "in_TBL_NM": [],
                    "in_MT_ATITLE": [],
                    "in_CONTENTS": [],
                    "QUERY": keyword,
                    "error": str(error),
                }
            )

    return {
        "search_results": search_results
    }


def save_result_json(result: dict[str, Any], file_path: str) -> None:
    """검색 결과를 JSON 파일로 저장한다."""
    path = Path(file_path)

    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def main() -> None:
    base_dir = Path(__file__).resolve().parent

    input_path = base_dir / "v2_sample_keywords.json"
    output_path = base_dir / "v2_sample_result.json"

    keywords = load_search_keywords(str(input_path))
    result = search_keywords_v2(keywords)

    save_result_json(result, str(output_path))

    print("KOSIS 키워드 검색 v2 결과")
    print("-" * 40)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("-" * 40)
    print(f"결과 저장 위치: {output_path}")


if __name__ == "__main__":
    main()