# -*- coding: utf-8 -*-
"""P3 record-replay 캐시 (§5.6) — LLM 호출의 녹화·재생.

키 = (prompt_version, model, params 해시, payload 해시) — 프롬프트 버전이 키에 승격돼
버전별 replay 코퍼스가 분리 보관된다(리뷰: 전문 해시는 공백 변경에도 전부 무효).
수리(repair) 시퀀스는 원 payload 키 아래 대화 체인째 저장해 재생 시 통째로 재현한다.

용도 ①중단-재개: 실행이 끊겨도 성공분은 캐시에서 재생돼 재실행이 사실상 재개가 된다
    ②회귀 테스트: 동결된 LLM 원출력을 입력으로 Stage C 이후 룰만 재검증(HCX 0콜)
    ③비용: dev 튜닝 반복에서 동일 프롬프트 재호출 차단.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class ReplayCache:
    def __init__(self, path: Path | str, prompt_version: str, model: str,
                 params: dict | None = None):
        self.path = Path(path)
        self.meta = {"prompt_version": prompt_version, "model": model,
                     "params": params or {}}
        self._index: dict[str, dict] = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    if row.get("meta") == self.meta:   # 다른 버전 행은 무시(파일 공유 허용)
                        self._index[row["key"]] = row

    def key(self, payload: str) -> str:
        blob = json.dumps({"meta": self.meta, "payload": payload},
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def get(self, payload: str) -> dict | None:
        """→ {response, chain} | None. response = 최종(파싱 대상) 응답 텍스트."""
        return self._index.get(self.key(payload))

    def put(self, payload: str, response: str, chain: list | None = None) -> None:
        row = {"key": self.key(payload), "meta": self.meta,
               "response": response, "chain": chain or []}
        self._index[row["key"]] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def __len__(self) -> int:
        return len(self._index)
