# -*- coding: utf-8 -*-
"""P3 Stage E — 산출 + 전수 회계 (§5.6).

산출 4+1종:
- claims_full.jsonl : 17필드(골든 스키마) — 내부 표준
- claims.jsonl      : 8필드 사영(v0.4) — 공식 인수인계(§4.1). to_handoff()가 계약 위반 조합을 차단
- excluded.jsonl    : 제외 대장 — §5.3 계약 코드만 허용
- errors.jsonl      : 내부 실패(EXTRACTION_ERROR 등) — 계약 파일과 분리, 사람 검토 큐
- claims_trace.jsonl: 계보(5번 요청 §4.2) — 오프셋·period 해소 방법·감사 플래그·사영 시 소실 정보

전수 회계(§5.6 3항): 모든 숫자 문장 키는 claims ∪ excluded ∪ errors 중 최소 한 곳에 있어야
하고, 숫자 아닌 문장이 산출물에 나타나면 안 된다. 위반 = 파이프라인 실패(예외).
"""
from __future__ import annotations

import json
from pathlib import Path

from src.p3_schemas import (ClaimRecord, ExcludedRecord, DocumentSet, PIPELINE_VERSION,
                            CONTRACT_EXCLUSION_CODES)
from src.p3_stage_a import SentenceCandidate


class AccountingError(RuntimeError):
    """전수 회계 인바리언트 위반 — 산출을 중단시킨다(§5.1 원칙 ④)."""


def assign_claim_ids(claims: list[ClaimRecord]) -> None:
    """{article_id}-C{일련}(§4.1) — 입력 순서(문장 순 → item 순)를 존중해 기사별 부여."""
    seq: dict[str, int] = {}
    for c in claims:
        seq[c.article_id] = seq.get(c.article_id, 0) + 1
        c.claim_id = f"{c.article_id}-C{seq[c.article_id]:03d}"


def accounting_check(candidates: list[SentenceCandidate], claims: list[ClaimRecord],
                     excluded: list[ExcludedRecord], errors: list[dict]) -> dict:
    """집합 커버리지(§5.6 — 혼합 문장 실재로 등식이 아닌 커버리지) + 비대상 유출 검사."""
    numeric_keys = {c.key for c in candidates}
    if len(numeric_keys) != len(candidates):   # 집합 회계의 중복 무감 방지(이중 방어)
        raise AccountingError(f"후보 문장 키 중복 — {len(candidates) - len(numeric_keys)}건")
    covered = ({(c.article_id, c.sent_id) for c in claims}
               | {(e.article_id, e.sent_id) for e in excluded}
               | {(e["article_id"], e["sent_id"]) for e in errors})
    missing = sorted(numeric_keys - covered)
    leaked = sorted(covered - numeric_keys)   # 숫자 아닌 문장(또는 미지 문장)에서 산출 발생
    if missing or leaked:
        raise AccountingError(f"전수 회계 위반 — 미회계 {len(missing)}건 {missing[:5]} / "
                              f"비대상 유출 {len(leaked)}건 {leaked[:5]}")
    return {"numeric_sentences": len(numeric_keys), "claims": len(claims),
            "excluded": len(excluded), "errors": len(errors)}


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    """tmp에 쓰고 os.replace — 부분 파일이 번들에 남지 않게(원자 교체)."""
    import os
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def emit_all(outdir: Path | str, candidates: list[SentenceCandidate],
             claims: list[ClaimRecord], excluded: list[ExcludedRecord],
             errors: list[dict], traces: list[dict]) -> dict[str, Path]:
    """검증(회계·코드) → 파일 5종 산출. 반환: 파일 경로 dict.

    원자성: 5종의 행 리스트를 전부 선평가(여기서 예외가 나면 디스크는 무변경)한 뒤에야
    쓰기 시작하고, 각 파일은 tmp→replace로 교체 — '새 claims_full + 이전 실행 excluded'
    같은 혼합 번들(§4.2는 통째 인수인계 단위)을 만들지 않는다.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for c in claims:
        c.finalize()   # 미파생(None) 상태로 full=null/handoff=true 파일 간 불일치 방지
    bad_codes = [e for e in excluded if e.exclusion_code not in CONTRACT_EXCLUSION_CODES]
    if bad_codes:
        raise AccountingError(f"excluded.jsonl에 계약 밖 코드 {len(bad_codes)}건 — "
                              f"{[e.exclusion_code for e in bad_codes[:5]]} (내부 코드는 errors.jsonl로)")
    summary = accounting_check(candidates, claims, excluded, errors)

    # 선평가 — to_handoff의 계약 위반 예외까지 여기서 전부 소진
    rows_full = [c.to_dict() for c in claims]
    rows_handoff = [c.to_handoff() for c in claims]
    rows_excluded = [e.to_dict() for e in excluded]

    paths = {
        "claims_full": outdir / "claims_full.jsonl",
        "claims": outdir / "claims.jsonl",
        "excluded": outdir / "excluded.jsonl",
        "errors": outdir / "errors.jsonl",
        "trace": outdir / "claims_trace.jsonl",
    }
    _write_jsonl_atomic(paths["claims_full"], rows_full)
    _write_jsonl_atomic(paths["claims"], rows_handoff)
    _write_jsonl_atomic(paths["excluded"], rows_excluded)
    _write_jsonl_atomic(paths["errors"], errors)
    _write_jsonl_atomic(paths["trace"], traces)
    summary["eligible_true"] = sum(1 for c in claims if c.kosis_eligible)
    summary["paths"] = {k: str(v) for k, v in paths.items()}
    return summary


def load_documents_jsonl(claims_full_path: Path | str, excluded_path: Path | str,
                         version: str = PIPELINE_VERSION) -> DocumentSet:
    """산출물 재적재(채점·재분석용) — claims_full은 17필드 그대로 왕복된다."""
    ds = DocumentSet(version=version)
    with open(claims_full_path, encoding="utf-8") as f:
        for line in f:
            ds.claims.append(ClaimRecord(**json.loads(line)))
    with open(excluded_path, encoding="utf-8") as f:
        for line in f:
            ds.excluded.append(ExcludedRecord(**json.loads(line)))
    return ds
