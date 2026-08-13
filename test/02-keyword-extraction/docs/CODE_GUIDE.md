# 코드 파일 가이드

## src

- `test/02-keyword-extraction/src/hcx_client.py` — HCX-005·HCX-007 모델에 공통 API 요청을 보내고 응답을 처리하는 클라이언트입니다.
- `test/02-keyword-extraction/src/embedding_client.py` — 키워드 문자열을 CLOVA Studio Embedding v2 벡터로 변환하는 클라이언트입니다.
- `test/02-keyword-extraction/src/embedding_ranker.py` — 원키워드와 후보 키워드의 Embedding 유사도를 계산해 최종 키워드 순서를 정렬합니다.
- `test/02-keyword-extraction/src/metric_selector.py` — 원문 metric과 정규화 metric을 비교해 키워드 생성에 사용할 하나를 선택합니다.
- `test/02-keyword-extraction/src/keyword_generator.py` — 선택된 metric을 원키워드로 보존하고 Seed·사전·HCX 확장 후보를 병합한 뒤 Embedding 순으로 정렬합니다.

## scripts

- `test/02-keyword-extraction/scripts/build_claim_golden_comparison_inputs.py` — `claim_golden.xlsx`에서 유효한 claim·원문 metric·정규화 metric 테스트 입력을 생성합니다.
- `test/02-keyword-extraction/scripts/run_claim_golden_comparison.py` — 원문 metric과 정규화 metric을 각각 전체 파이프라인에 실행해 두 결과를 비교합니다.
- `test/02-keyword-extraction/scripts/run_selected_metric_test.py` — 두 metric 중 하나를 선택하고 선택된 metric만 키워드 파이프라인에 실행해 결과를 저장합니다.

## tests

- `test/02-keyword-extraction/tests/test_hcx_client.py` — HCX 모델별 요청 설정과 통합된 API 환경변수 사용을 검증합니다.
- `test/02-keyword-extraction/tests/test_embedding_client.py` — Embedding API 요청·응답 파싱·입력 검증·오류 처리를 검증합니다.
- `test/02-keyword-extraction/tests/test_embedding_ranker.py` — Cosine similarity 정렬과 Embedding 실패 시 키워드 보존 정책을 검증합니다.
- `test/02-keyword-extraction/tests/test_metric_selector.py` — HCX index 선택, 단일 후보 처리, 실패 fallback 등 metric 선택 규칙을 검증합니다.
- `test/02-keyword-extraction/tests/test_run_claim_golden_comparison.py` — 원문·정규화 metric의 독립 실행, 비교 저장, 중복·누적 처리와 출력 요약을 검증합니다.
- `test/02-keyword-extraction/tests/test_run_selected_metric_test.py` — 선택된 metric만 generator에 한 번 전달되고 입력·출력·실패 정보가 한 객체에 저장되는지 검증합니다.

## 기타

- `test/02-keyword-extraction/.env.example` — HyperCLOVA X·KOSIS API 키와 로그 레벨에 필요한 환경변수 이름을 실제 값 없이 안내합니다.
