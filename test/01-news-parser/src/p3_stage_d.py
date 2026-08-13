# -*- coding: utf-8 -*-
"""P3 Stage D — metric_normalized (§5.6 · v0.4 계약 필드, 50차 승격 / 55차 정책 전환).

★ 55차 결정(사용자): **골든 유래 시드 사전을 폐지한다.**
골든의 metric_normalized는 44차에 에이전트가 합성한 값이고 **3번의 KOSIS 검색 검증을
거치지 않았다.** 검증 안 된 동의어를 사전으로 굳히면 오류가 영속화된다 —
실제로 `우럭 1kg당 도매 가격`→`조피볼락 도매가격`(동의어 치환), `가계 대출
잔액/증가액/하루평균증가액`→전부 `가계대출`(정보 손실) 같은 훼손이 시드에 실재했다.

그래서 현행 동작은 단순하다:
  metric_normalized = verbatim metric   (그대로 복사, 의미 훼손 0)
사전은 비어 있는 채로 유지되며, **3번이 KOSIS 검색으로 통과시킨 표준명만**
`status=approved`로 등재되면 그때부터 적용된다(검증된 것만 치환한다는 원칙).
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from src import config

DICTIONARY_DEFAULT = config.data_dir() / "metric_dictionary.jsonl"
KNOWN_STATUSES = frozenset({"seed", "seed_ambiguous", "llm_unverified", "approved"})
# 55차: seed 계열은 로드해도 적용하지 않는다(미검증). approved만 활성.
ACTIVE_STATUSES = frozenset({"approved"})


def build_seed_entries(golden) -> list[dict]:
    """골든 (metric → metric_normalized) 매핑을 **참고 자료로만** 덤프한다.

    55차부터 이 결과는 사전 적용 대상이 아니다(status=llm_unverified로 기록).
    3번의 KOSIS 검색 결과를 받으면 통과분만 approved로 승격해 재사용한다.
    """
    mapping: dict[str, set[str]] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    for c in golden.claims:
        if c.metric and c.metric_normalized and c.metric.strip() != c.metric_normalized.strip():
            mapping[c.metric.strip()].add(c.metric_normalized.strip())
            counts[c.metric.strip()] += 1
    entries = []
    for metric in sorted(mapping):
        norms = sorted(mapping[metric])
        entries.append({
            "metric": metric,
            "normalized": None,                    # 미검증 — 적용 금지
            "candidates": norms,
            "count": counts[metric],
            "status": "llm_unverified",
            "source": f"golden:{golden.version} (KOSIS 미검증 — 3번 통과 시 approved 승격)",
        })
    return entries


def save_dictionary(entries: list[dict], path: Path | str = DICTIONARY_DEFAULT) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def load_dictionary(path: Path | str = DICTIONARY_DEFAULT) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class MetricNormalizer:
    """검증된 표준명만 치환하고, 나머지는 verbatim metric을 그대로 쓴다.

    status 처리: approved = 활성 / seed·seed_ambiguous·llm_unverified = 미적용(참고용).
    미지 status는 조용히 무시하지 않고 예외(사전 오손 신호).
    """

    def __init__(self, entries: list[dict] | None = None):
        self._exact: dict[str, str] = {}
        for e in entries or []:
            status = e.get("status", "")
            if status not in KNOWN_STATUSES:
                raise ValueError(f"사전 status 이탈: {status!r} (metric={e.get('metric')!r})")
            if status in ACTIVE_STATUSES and e.get("normalized"):
                self._exact[e["metric"]] = e["normalized"]

    def normalize(self, metric: str) -> tuple[str | None, str]:
        """→ (표준명 | None, method: approved_hit | unverified)."""
        m = (metric or "").strip()
        if m in self._exact:
            return self._exact[m], "approved_hit"
        return None, "unverified"

    def apply(self, claims, traces=None, fallback_to_metric: bool = True) -> dict:
        """metric_normalized가 빈 Claim을 채운다(추출기 제공값 우선).

        검증된 표준명이 있으면 치환, 없으면 verbatim metric 복사(기본).
        """
        stats = {"filled": 0, "already": 0, "fallback": 0, "unverified": 0}
        tr_by_claim = {}
        if traces:
            tr_by_claim = {t.get("claim_id"): t for t in traces if t.get("claim_id")}
        for c in claims:
            if c.metric_normalized:
                stats["already"] += 1
                t = tr_by_claim.get(c.claim_id)
                if t is not None:
                    t["normalized_method"] = "extractor"
                continue
            norm, method = self.normalize(c.metric)
            if norm:
                c.metric_normalized = norm
                stats["filled"] += 1
            else:
                stats["unverified"] += 1
                if fallback_to_metric and c.metric:
                    c.metric_normalized = c.metric      # 검증 전에는 바꾸지 않는다
                    stats["fallback"] += 1
                    method = "verbatim"
            t = tr_by_claim.get(c.claim_id)
            if t is not None:
                t["normalized_method"] = method
        return stats


def main() -> None:
    ap = argparse.ArgumentParser(
        description="metric_dictionary 관리 — 골든 매핑 덤프(미검증 참고용)")
    ap.add_argument("--dump-candidates", action="store_true",
                    help="골든의 (metric→normalized) 후보를 llm_unverified로 덤프")
    ap.add_argument("--golden", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=DICTIONARY_DEFAULT)
    args = ap.parse_args()
    if not args.dump_candidates:
        ap.error("--dump-candidates 를 지정하세요")
    from src.p3_golden import load_golden, GOLDEN_DEFAULT
    gold = load_golden(args.golden or GOLDEN_DEFAULT)
    entries = build_seed_entries(gold)
    save_dictionary(entries, args.out)
    print(f"후보 {len(entries)}종 덤프(전부 llm_unverified — 적용 안 됨) → {args.out}")
    print("3번의 KOSIS 검색 통과분만 status=approved로 승격해 활성화하세요.")


if __name__ == "__main__":
    main()
