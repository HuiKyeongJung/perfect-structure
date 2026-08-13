import os
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

class Config:
    # === HyperCLOVA X (CLOVA Studio) API 설정 ===
    # 네이버 클라우드 플랫폼 콘솔 > AI Services > CLOVA Studio > API 키 메뉴에서 발급
    # 신규 API 키 체계는 Authorization: Bearer 헤더 하나로 인증하므로 APP_ID는 불필요
    NCP_CLOVASTUDIO_API_KEY = os.getenv("NCP_CLOVASTUDIO_API_KEY")

    # 목적별 모델 정의
    # - 메인 생성/추론 모델: HCX-007 (Chat Completions v3 API 전용, 고성능 지시 이행 및 추론)
    # - 임베딩: 별도 모델명이 아닌 임베딩 v2 API 엔드포인트를 사용
    HCX_GENERATION_MODEL = "HCX-007"
    HCX_EMBEDDING_API_VERSION = "v2"  # 임베딩 v2 API 사용

    # CLOVA Studio API 엔드포인트 Base URL
    # 신규 URL(stream.ntruss.com) 사용 권장. 구버전 apigw.ntruss.com은 지원 중단 예정이며
    # 신규 API 키로는 인증 및 스트리밍 응답을 이용할 수 없음
    HCX_BASE_URL = "https://clovastudio.stream.ntruss.com"

    # 요청 헤더 관련 (선택 사항)
    HCX_REQUEST_ID_HEADER = "X-NCP-CLOVASTUDIO-REQUEST-ID"

    @classmethod
    def hcx_headers(cls, request_id: str | None = None, stream: bool = False) -> dict:
        """CLOVA Studio API 호출용 공통 요청 헤더 생성"""
        headers = {
            "Authorization": f"Bearer {cls.NCP_CLOVASTUDIO_API_KEY}",
            "Content-Type": "application/json",
        }
        if request_id:
            headers[cls.HCX_REQUEST_ID_HEADER] = request_id
        if stream:
            headers["Accept"] = "text/event-stream"
        return headers

    # === KOSIS API 설정 ===
    KOSIS_API_KEY = os.getenv("KOSIS_API_KEY")

    # 통계 설명 및 목록 검색용 통합검색 API URL
    KOSIS_SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"

    # 실제 세부 통계값(DT) 조회용 파라미터 API URL
    KOSIS_DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

# 싱글톤 인스턴스 생성
config = Config()