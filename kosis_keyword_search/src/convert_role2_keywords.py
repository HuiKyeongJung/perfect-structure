import json
from pathlib import Path
from typing import Any


def load_json(file_path: Path) -> Any:
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: Any, file_path: Path) -> None:
    with file_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_role2_data(data: Any) -> list[dict[str, Any]]:
    """
    2번이 준 데이터가 단일 dict여도, list여도 처리할 수 있게 맞춘다.
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
    2번 데이터에서 claim_id, metric, keywords만 뽑고,
    keywords는 keyword_search.py 입력 형식에 맞게 expanded_keywords로 이름을 바꾼다.
    """
    claim_id = item.get("claim_id", "claim_unknown")
    metric = item.get("metric", "")
    keywords = item.get("keywords", [])

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


def main() -> None:
    base_dir = Path(__file__).resolve().parent

    input_path = base_dir / "role2_sample_keywords.json"
    output_path = base_dir / "role2_convert_keywords.json"

    role2_data = load_json(input_path)
    claims = normalize_role2_data(role2_data)

    converted_claims = [
        convert_claim(claim)
        for claim in claims
    ]

    output_data = converted_claims

    save_json(output_data, output_path)

    print("2번 키워드 데이터 변환 완료")
    print("-" * 40)
    print(f"입력 파일: {input_path}")
    print(f"출력 파일: {output_path}")
    print(f"클레임 수: {len(converted_claims)}")
    total_keywords = sum(len(claim["expanded_keywords"]) for claim in converted_claims)
    print(f"검색 키워드 수: {total_keywords}")


if __name__ == "__main__":
    main()