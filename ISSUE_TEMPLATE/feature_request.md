---
name: "🚀 Feature Request (뉴스 사실 검증)"
description: "AI 뉴스 사실 검증 시스템의 신규 기능, 파이프라인, UI 개발 작업을 정의합니다."
title: "[Feat] 작업 요약 (예: GPT-4o 기반 팩트체크 프롬프트 엔지니어링)"
labels: ["feature"]
assignees: ""
---

## 📌 개요
> 구현하고자 하는 AI 알고리즘, 파이프라인 모델, 또는 기능의 목적을 설명해주세요.

- 예시: 뉴스 본문과 신뢰성 있는 출처(검증 데이터셋)를 비교하여 교차 검증하는 Few-shot 프롬프트를 설계하고 API를 구축합니다.

## ⚙️ 요구사항 (Todo List)
> 이 이슈를 완료하기 위해 필요한 세부 작업 아키텍처 및 태스크를 체크리스트로 작성해주세요.

- [ ] [AI/Data] 사실 검증(Fact-checking)을 위한 3-레이어 프롬프트 템플릿 설계
- [ ] [Backend] 뉴스 URL 입력 시 본문 텍스트를 추출하는 파서(Scraper) 모듈 개발
- [ ] [Backend] AI 연동 API 엔드포인트 구현 및 에러 핸들링 (Rate Limit 대응)
- [ ] [Frontend] 검증 진행률(Progress Bar) 및 신뢰도 점수(0~100%) 시각화 UI 구현

## 📊 AI & 데이터 명세 (선택)
> AI 모델, 데이터 소스, 프롬프트 아키텍처 등 특이사항을 적어주세요.

- **대상 모델**: (예: GPT-4o, Claude 3.5 Sonnet, 로컬 Llama-3 등)
- **외부 API/데이터**: (예: 네이버 뉴스 API, Google Search API, 전용 팩트체크 DB)
- **평가 지표 Target**: (예: 정밀도(Precision) 85% 이상 검증 목적)

## 🔗 관련 이슈 / 마일스톤
- 관련 Milestone: `v0.1.0-Prototype`
- 의존성이 있는 다른 이슈: #