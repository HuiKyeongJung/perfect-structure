# KOSIS Keyword Search

2번 모듈이 만든 확장 키워드를 KOSIS 통합검색 OpenAPI에 넣고, 검색 결과가 존재하는지 `true` 또는 `false`로 반환하는 모듈이다.

## 실행 전 준비

`kosis_keyword_search` 폴더 안에 `.env` 파일을 만들고 아래처럼 API key를 넣는다.

```text
KOSIS_API_KEY=발급받은_API_KEY
```

## 입력 예시

`sample_keywords.json`

```json
{
  "claim_id": "claim_001",
  "claim_text": "2025년 1월 소비자물가지수는 115.71이다.",
  "expanded_keywords": [
    "소비자물가지수",
    "소비자 물가 지수",
    "CPI"
  ]
}
```

## 실행 방법

```bash
python keyword_search.py
```

또는 프로젝트 루트에서 실행한다면:

```bash
python .\kosis_keyword_search\keyword_search.py
```

## 출력

실행하면 키워드별 검색 결과 존재 여부가 출력되고, 같은 내용이 `sample_result.json`에 저장된다.

```json
{
  "keyword": "소비자물가지수",
  "exists": true,
  "status": "success",
  "result_count": 10
}
```

현재 단계에서는 수치 검증, 통계표 항목 매칭, 유사도 계산은 하지 않는다.  
목표는 키워드가 KOSIS에서 검색 가능한지 1차로 확인하는 것이다.
