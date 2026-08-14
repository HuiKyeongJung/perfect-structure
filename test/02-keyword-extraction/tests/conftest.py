# 테스트 실행 시 프로젝트의 src 패키지를 불러올 수 있게 경로를 설정합니다.
"""테스트에서 프로젝트 루트의 ``src`` 패키지를 찾도록 설정한다."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
