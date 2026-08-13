# -*- coding: utf-8 -*-
"""공용 설정 — `.env` 기반 비밀키·경로 관리.

**팀 통합 기준(2026-08-13 결정)**: 비밀키는 OS 영구 환경변수가 아니라 저장소 루트의
`.env` 파일로 관리한다. 변수명은 `.env.example`이 정본이다.

    NCP_CLOVASTUDIO_API_KEY   CLOVA Studio(HCX) API 키   — 1번(Stage B) 사용
    KOSIS_API_KEY             KOSIS Open API 키          — 2~5번 사용

설계 메모
- **외부 의존성 없음**: `python-dotenv`를 쓰지 않고 직접 파싱한다. 팀원이 dotenv를
  써도 같은 파일을 읽으므로 충돌하지 않는다.
- **`.env`가 OS 환경변수를 덮어쓴다**(override=True 기본). "이제 .env로 관리한다"는
  합의를 코드가 그대로 따르게 하기 위함이다 — 남아 있는 옛 OS 변수가 조용히 이기면
  "왜 .env를 고쳤는데 안 바뀌지"라는 디버깅 불가능한 상태가 된다.
- 키는 **절대 로그·예외 메시지에 값이 실리지 않는다**(§7 — 키 외부 공유 금지).
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- 정본 변수명 (.env.example과 반드시 일치) --------------------------------
ENV_HCX_KEY = "NCP_CLOVASTUDIO_API_KEY"
ENV_KOSIS_KEY = "KOSIS_API_KEY"
ENV_HCX_ENDPOINT = "HCX_ENDPOINT"        # 선택 — 기본 CLOVA Studio v3
ENV_HCX_MODEL = "HCX_MODEL"              # 선택 — 기본 HCX-005
ENV_PART1_DIR = "PART1_DIR"              # 선택 — 골든셋·원본 데이터 폴더(평가 전용)

# 구 변수명 — 팀 통합 전 개인 환경에서 쓰던 이름. 정본이 없을 때만 폴백한다.
LEGACY_HCX_KEYS = ("NCP_API_KEY", "HCX_API_KEY")

_loaded: set[Path] = set()
_warned: set[str] = set()


def _parse_env_file(path: Path) -> dict[str, str]:
    """최소 dotenv 파서. `KEY=VALUE`, `export KEY=VALUE`, `#` 주석, 따옴표를 지원."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]            # 따옴표 안은 주석 제거를 하지 않는다
        else:
            val = val.split(" #", 1)[0].rstrip()
        out[key] = val
    return out


def load_env(path: str | Path | None = None, override: bool = True) -> dict[str, str]:
    """`.env`를 읽어 os.environ에 반영. 반환: 파일에서 읽은 항목(값 포함, 로그 금지).

    path 생략 시 `REPO_ROOT/.env` → 현재 작업 디렉터리의 `.env` 순으로 찾는다.
    파일이 없으면 조용히 빈 dict — CI처럼 OS 환경변수만 있는 환경도 그대로 동작한다.
    같은 파일은 프로세스당 한 번만 읽는다(반복 호출 비용 0).
    """
    candidates = [Path(path)] if path else [REPO_ROOT / ".env", Path.cwd() / ".env"]
    for p in candidates:
        p = p.resolve()
        if not p.is_file():
            continue
        if p in _loaded and not path:
            return {}
        values = _parse_env_file(p)
        for k, v in values.items():
            if override or k not in os.environ:
                os.environ[k] = v
        _loaded.add(p)
        return values
    return {}


def get_env(name: str, default: str | None = None) -> str | None:
    """`.env` 자동 로드 후 환경변수 조회."""
    load_env()
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def get_secret(name: str, *fallbacks: str, required: bool = True) -> str:
    """비밀키 조회. 값은 절대 예외 메시지에 넣지 않는다.

    fallbacks는 구 변수명 호환용 — 정본이 비어 있을 때만 쓰이며, 쓰이면 1회 안내한다.
    """
    load_env()
    val = os.environ.get(name, "").strip()
    if val:
        return val
    for alt in fallbacks:
        alt_val = os.environ.get(alt, "").strip()
        if alt_val:
            if alt not in _warned:
                _warned.add(alt)
                print(f"[config] 구 변수명 {alt} 사용 중 — .env에 {name} 로 옮기세요 "
                      f"(.env.example 참조)")
            return alt_val
    if not required:
        return ""
    raise RuntimeError(
        f"환경변수 {name} 가 필요합니다. 저장소 루트에 .env 를 만들고 값을 넣으세요 "
        f"(.env.example 복사 → 값 채우기). 키는 커밋 금지(§7)."
    )


def get_hcx_api_key(required: bool = True) -> str:
    """CLOVA Studio(HCX) API 키 — Stage B 전용."""
    return get_secret(ENV_HCX_KEY, *LEGACY_HCX_KEYS, required=required)


def get_kosis_api_key(required: bool = True) -> str:
    """KOSIS Open API 키 — 2~5번 Task용. 1번 파이프라인은 사용하지 않는다."""
    return get_secret(ENV_KOSIS_KEY, required=required)


# --- 경로 ---------------------------------------------------------------------
def data_dir() -> Path:
    """산출물 기본 디렉터리(`data/`). 저장소 루트 기준 — 실행 위치에 의존하지 않는다."""
    return Path(get_env("DATA_DIR") or (REPO_ROOT / "data"))


def cache_dir() -> Path:
    """record-replay 캐시 디렉터리(`cache/`)."""
    return Path(get_env("CACHE_DIR") or (REPO_ROOT / "cache"))


def part1_dir() -> Path:
    """골든셋·원본 데이터 폴더. **평가/개발 전용**이며 운영 파이프라인은 참조하지 않는다.

    저장소에 포함되지 않는 자료(기사 원문·골든셋)라 기본값은 개인 경로일 수밖에 없다.
    다른 사람은 `.env`의 PART1_DIR로 자기 경로를 지정한다.
    """
    return Path(get_env(ENV_PART1_DIR) or "D:/part1")
