# KOSIS 검색 키워드 확장 Task

## Task 방향 변경

초기에는 뉴스 원문에서 수치 문장을 찾고, 후보를 점수화해 검토 대상을 고르는 실험을 진행했습니다. 이후 역할을 다시 정리하면서 해당 과정은 이 Task의 범위에서 제외했습니다.

현재 담당은 앞 단계가 만든 입력 객체에서 통계 지표명인 `metric`을 받아 KOSIS 검색용 키워드 목록으로 확장하는 것입니다.

| 이전에 시도한 범위 | 현재 Task 범위 |
|---|---|
| 뉴스 원문에서 수치 문장 추출 | 상위 단계가 전달한 입력 객체 사용 |
| Claim 후보 점수화·후보군 선정 | `metric` 원키워드 추출·검증 |
| 사람 검토용 후보 CSV 생성 | 관련 검색 키워드 확장 |
| 문장 내부 명사구 추출 | KOSIS 통합검색에 사용할 키워드 리스트 생성 |

## 현재 집중 파이프라인

```text
상위 단계 Claim 객체
        ↓
metric 추출 및 검증
        ↓
원키워드(original_keyword) 보존
        ↓
관련 키워드 확장(keywords)
        ↓
KOSIS 통합검색
        ↓
검색 결과를 다음 단계에 전달
```

현재는 **metric 추출·원키워드 보존 단계**까지 구현했습니다. 관련 키워드 확장과 KOSIS 통합검색은 다음 단계입니다.

## 입력 기준

상위 단계는 아래처럼 Claim 문장과 구조화된 값을 전달합니다.

```json
{
  "claim_id": "Ae4300e50-C001",
  "claim": "재배면적이 10만4943㏊로 작년보다 1.0% 감소했다.",
  "metric": "재배면적",
  "value": "10만4943",
  "unit": "㏊",
  "period": "2025",
  "kosis_eligible": true
}
```

이 Task의 키워드 기준값은 `metric`입니다. `claim`, `value`, `unit`, `period`에서 새 metric을 추론하지 않습니다.

`metric`이 없을 때만 레거시·오타 호환 필드인 `metric:`을 사용합니다.

## 현재 구현 상태

| 항목 | 상태 | 내용 |
|---|---|---|
| metric 검증·정규화 | 완료 | 문자열 검증, 빈 값 오류, 앞뒤·연속 공백 정리 |
| `metric:` 호환 | 완료 | `metric`이 없을 때만 사용 |
| 검색 대상 판정 | 완료 | `kosis_eligible is True`일 때만 키워드 목록 생성 |
| 원키워드 보존 | 완료 | `original_keyword`와 `keywords`에 metric 저장 |
| 관련 키워드 확장 | 예정 | 원키워드 기반 확장 규칙과 테스트 추가 |
| KOSIS 통합검색 | 예정 | 확장된 키워드 목록으로 검색 수행 |

현재 결과는 원키워드 하나만 담습니다.

```json
{
  "claim_id": "Ae4300e50-C001",
  "metric": "재배면적",
  "original_keyword": "재배면적",
  "keywords": ["재배면적"],
  "status": "success",
  "error_message": ""
}
```

`kosis_eligible`이 `False`이면 metric은 보존하지만 검색용 키워드 목록은 비웁니다.

## 다음 구현 순서

1. 원키워드에서 관련 키워드를 확장하는 규칙 정의
2. 확장 결과를 검증하는 테스트 데이터와 테스트 코드 작성
3. 실패 사례를 기준으로 확장 규칙 보완
4. 확장 키워드 목록을 KOSIS 통합검색에 연결
5. 검색 결과를 정리해 다음 단계에 전달

## 파일 구성

| 파일 | 역할 |
|---|---|
| `src/keyword_generator.py` | metric 검증·정규화 및 키워드 목록 생성 |
| `tests/test_keyword_generator.py` | metric 처리와 원키워드 보존 테스트 |
| `docs/daily_logs/` | 날짜별 작업 기록 |

## 테스트

```bash
./.venv/bin/python -B -m pytest tests -q
```

뉴스 원문과 파생 데이터는 저작권 문제로 GitHub에 포함하지 않습니다.
