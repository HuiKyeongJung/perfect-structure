# 04-keyword_mapping

## 역할

`04-keyword_mapping`은 `03-kosis-search`에서 전달받은 KOSIS 검색 가능 키워드 중에서 원문 `metric`과 의미적으로 관련성이 높은 키워드만 선별하여 `05-claim-verification`에 전달한다.

## 파이프라인 위치

```text
01-news-parser
02-keyword-extraction
03-kosis-search
04-keyword_mapping
05-claim-verification
```

## 입력

`03-kosis-search`의 검색 검증 결과를 입력으로 받는다.

```json
{
  "claim_id": "claim_001",
  "status": "success",
  "searched_count": 5,
  "results": [
    {
      "keyword": "소비자물가지수",
      "exists": true,
      "status": "success"
    },
    {
      "keyword": "소비자 물가 지수",
      "exists": true,
      "status": "success"
    },
    {
      "keyword": "없는키워드테스트",
      "exists": false,
      "status": "success"
    }
  ]
}
```

추가로 원문 Claim에서 추출된 `metric`을 함께 사용한다.

```json
{
  "metric": "쉬었음 청년"
}
```

## 처리 로직

1. `exists: true`인 키워드만 후보로 사용한다.
2. 후보 키워드가 `metric`과 동일하면 유사도 계산 없이 즉시 최종 키워드 리스트에 추가한다.
3. 나머지 후보는 `metric`과 임베딩한 뒤 코사인 유사도를 계산한다.
4. 설정한 임계값 미만의 키워드는 제거한다.
5. 임계값 이상인 키워드만 최종 키워드 리스트로 반환한다.

검색 관련 임베딩 모델과 유사어 관련 임베딩 모델 중 어떤 모델을 사용할지는 추가 실험 후 결정한다.

## 처리 예시

### 입력 흐름

```text
metric
"쉬었음 청년"

연관 키워드 확장
["쉬었음 청년", "쉬었음", "청년", "비경제활동인구", "백수", "무직자"]

KOSIS 검색 검증 결과
["쉬었음", "청년", "비경제활동인구"]
```

`백수`, `무직자`는 `exists: false`이므로 제거한다.

### 04-keyword_mapping 처리

- `"쉬었음 청년"`과 `"쉬었음"`, `"청년"`, `"비경제활동인구"`의 코사인 유사도를 계산한다.
- 임계값 미만의 키워드는 제거한다.
- `metric`과 완전히 동일한 키워드는 유사도 계산 없이 포함한다.

## 출력

`05-claim-verification`에는 최종 키워드 리스트만 전달한다.

```json
{
  "keywords": [
    "쉬었음",
    "청년",
    "비경제활동인구"
  ]
}
```

`metric`, `keyword`, `similarity` 값은 추후 캐싱하여 재사용할 수 있지만, 다음 단계에는 전달하지 않는다.

## 캐시 예정 구조

```json
{
  "metric": "쉬었음 청년",
  "keyword": "비경제활동인구",
  "similarity": 0.82
}
```

## 향후 계획

- 정답 Table ID 그룹을 모두 포함하는 유사도 임계값을 Recall 기준으로 탐색한다.
- 정답 세부 차원 ID 그룹을 모두 포함하는 유사도 임계값을 Recall 기준으로 탐색한다.
- 관련 용어 매핑 결과를 Recall 기준으로 검증한다.
- KOSIS 키워드 간 관련성을 측정하고 실제 관련 여부를 Recall 기준으로 검증한다.
