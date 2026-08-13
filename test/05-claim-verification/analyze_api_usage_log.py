# -*- coding: utf-8 -*-
"""api_usage_log.jsonl(client.py `_flush_usage_log`가 실행마다 남기는 실제
HCX/KOSIS 호출 기록) 집계 - README "HCX 호출이 실제로 얼마나 걸리고 비용이
드는지" 항목의 비용 쪽을 이 로그 기반으로 정리한다 (2026-08-11).

[probe_hcx_latency_cost.py와의 관계 - 2026-08-11 갱신] 처음엔 "토큰/비용은
이 로그, 시간은 probe_hcx_latency_cost.py"로 역할을 나눴는데, 그날 바로
client.py의 `generate_completion` 자체에 왕복시간(elapsed_sec)을 재서 같은
usage 딕셔너리에 합쳐 넣도록 고쳤다(별도 로그 파일로 쪼개지 않은 이유는
client.py의 `_usage_counters` 위 주석 참고 - 같은 호출 한 건의 기록을
나중에 타임스탬프로 다시 맞추는 것보다, 애초에 한 곳에서 같이 재는 게 더
안전하다는 판단). 그 결과 이 로그(api_usage_log.jsonl)는 2026-08-11 이후
기록된 줄부터는 토큰 수와 왕복시간을 모두 담고 있고, 이 스크립트도 그
elapsed_sec을 같이 집계한다. 그 이전에 쌓인 줄에는 elapsed_sec이 없을 수
있으므로(구버전 client.py로 기록됨) 그런 줄은 시간 집계에서 자동으로
제외된다 - 토큰 집계에는 영향 없음.

[가격을 하드코딩하지 않는 이유] probe_hcx_latency_cost.py와 같은 이유 -
NCP CLOVA Studio 단가가 바뀌면 조용히 틀린 원화 금액을 보여주게 된다.
아래 PRICE_PER_1K_PROMPT_TOKENS_WON / PRICE_PER_1K_COMPLETION_TOKENS_WON을
사용자가 NCP 콘솔에서 확인한 현재 단가로 직접 채우면(기본값 None) 그때만
원화 비용을 같이 계산해서 보여준다 - 안 채우면 토큰 수까지만 보여준다.

[실행] python3 analyze_api_usage_log.py
  - 네트워크/API 키 불필요 - 이미 쌓인 api_usage_log.jsonl 파일만 읽는다.
  - 특정 스크립트만 보고 싶으면: python3 analyze_api_usage_log.py <스크립트명 일부>
    (예: python3 analyze_api_usage_log.py judgment 처럼 부분 문자열로 필터)
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

_LOG_FILE = "api_usage_log.jsonl"

# NCP 콘솔에서 확인한 현재 HCX-007 단가(원/1000토큰)를 직접 채우면 원화
# 비용까지 계산합니다. 모르면 None으로 두세요 - 토큰 수까지만 보여줍니다.
PRICE_PER_1K_PROMPT_TOKENS_WON: Optional[float] = 1.25
PRICE_PER_1K_COMPLETION_TOKENS_WON: Optional[float] = 5


def load_records(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        print(f"[안내] {path} 파일이 없습니다 - 아직 실제 API를 호출한 실행이 없는 것 같습니다.")
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def script_label(rec: Dict[str, Any]) -> str:
    raw = rec.get("script") or "?"
    return os.path.basename(raw)


def main() -> int:
    name_filter = sys.argv[1] if len(sys.argv) > 1 else None
    records = load_records(_LOG_FILE)
    if not records:
        return 1

    if name_filter:
        records = [r for r in records if name_filter in script_label(r)]
        print(f"[필터] 스크립트명에 '{name_filter}' 포함된 실행만: {len(records)}건\n")

    per_script: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "hcx_calls": 0, "prompt": 0, "completion": 0,
                 "total": 0, "kosis_calls": 0, "elapsed_list": []}
    )

    grand = {"runs": 0, "hcx_calls": 0, "prompt": 0, "completion": 0,
             "total": 0, "kosis_calls": 0, "elapsed_list": []}

    for rec in records:
        label = script_label(rec)
        bucket = per_script[label]
        bucket["runs"] += 1
        grand["runs"] += 1

        hcx_calls = rec.get("hcx_calls", 0) or 0
        bucket["hcx_calls"] += hcx_calls
        grand["hcx_calls"] += hcx_calls

        kosis_calls = rec.get("kosis_calls_total", 0) or 0
        bucket["kosis_calls"] += kosis_calls
        grand["kosis_calls"] += kosis_calls

        for usage in rec.get("hcx_usage_tokens") or []:
            p = usage.get("promptTokens", 0) or 0
            c = usage.get("completionTokens", 0) or 0
            t = usage.get("totalTokens", p + c) or (p + c)
            bucket["prompt"] += p
            bucket["completion"] += c
            bucket["total"] += t
            grand["prompt"] += p
            grand["completion"] += c
            grand["total"] += t
            elapsed = usage.get("elapsed_sec")
            if isinstance(elapsed, (int, float)):
                bucket["elapsed_list"].append(elapsed)
                grand["elapsed_list"].append(elapsed)

    def fmt_cost(prompt_tok: int, completion_tok: int) -> str:
        if PRICE_PER_1K_PROMPT_TOKENS_WON is None or PRICE_PER_1K_COMPLETION_TOKENS_WON is None:
            return ""
        won = (
            prompt_tok / 1000 * PRICE_PER_1K_PROMPT_TOKENS_WON
            + completion_tok / 1000 * PRICE_PER_1K_COMPLETION_TOKENS_WON
        )
        return f", 약 {won:,.1f}원"

    def fmt_elapsed(elapsed_list: List[float]) -> str:
        if not elapsed_list:
            return f"{'-':>18s}"
        return f"{min(elapsed_list):5.2f}/{sum(elapsed_list)/len(elapsed_list):5.2f}/{max(elapsed_list):5.2f}"

    print("=" * 110)
    print(f"{'스크립트':38s} {'실행':>4s} {'HCX':>6s} {'promptTok':>9s} {'complTok':>8s} {'totalTok':>8s}"
          f" {'KOSIS':>6s}  {'왕복초(min/avg/max)':>20s}")
    print("=" * 110)
    for label, b in sorted(per_script.items(), key=lambda kv: -kv[1]["total"]):
        print(
            f"{label:38s} {b['runs']:>4d} {b['hcx_calls']:>6d} {b['prompt']:>9d}"
            f" {b['completion']:>8d} {b['total']:>8d} {b['kosis_calls']:>6d}"
            f"  {fmt_elapsed(b['elapsed_list']):>20s}"
            f"{fmt_cost(b['prompt'], b['completion'])}"
        )
    print("-" * 110)
    print(
        f"{'전체 합계':38s} {grand['runs']:>4d} {grand['hcx_calls']:>6d} {grand['prompt']:>9d}"
        f" {grand['completion']:>8d} {grand['total']:>8d} {grand['kosis_calls']:>6d}"
        f"  {fmt_elapsed(grand['elapsed_list']):>20s}"
        f"{fmt_cost(grand['prompt'], grand['completion'])}"
    )
    print("=" * 110)

    if grand["hcx_calls"]:
        print(f"\nHCX 호출 1건당 평균 토큰: prompt {grand['prompt']/grand['hcx_calls']:.1f} / "
              f"completion {grand['completion']/grand['hcx_calls']:.1f} / "
              f"total {grand['total']/grand['hcx_calls']:.1f}")
    if grand["elapsed_list"]:
        el = grand["elapsed_list"]
        print(f"HCX 호출 1건당 왕복시간(elapsed_sec 기록된 {len(el)}건 기준):"
              f" 최소 {min(el):.2f}초 / 평균 {sum(el)/len(el):.2f}초 / 최대 {max(el):.2f}초")
    else:
        print("\n[참고] elapsed_sec이 기록된 호출이 아직 없습니다 - 2026-08-11 이전에"
              " 쌓인 구버전 로그만 있는 것 같습니다. client.py를 새로 실행하면"
              " 그때부터 왕복시간도 함께 쌓입니다.")

    if PRICE_PER_1K_PROMPT_TOKENS_WON is None:
        print("\n[참고] 원화 비용을 같이 보고 싶으면 이 파일 상단의"
              " PRICE_PER_1K_PROMPT_TOKENS_WON / PRICE_PER_1K_COMPLETION_TOKENS_WON을"
              " NCP 콘솔에서 확인한 현재 단가로 채운 뒤 다시 실행하세요.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
