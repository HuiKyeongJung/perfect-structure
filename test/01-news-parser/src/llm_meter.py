# -*- coding: utf-8 -*-
"""LLM 사용량 계측 — 토큰·호출 수·요금·응답 속도 기록과 집계 (서버/서비스 리소스 산정용).

**왜 필요한가**: 서비스로 올리려면 "기사 1건을 처리하는 데 얼마가 들고 몇 초가 걸리는가"를
숫자로 알아야 한다. 이 모듈은 HCX 호출마다 한 줄씩 기록하고, 그 로그를 집계해 리포트를 낸다.

기록 단위는 **호출 1회**다(문장 1개가 아니다) — 한 문장이 수리·재샘플로 최대 3콜까지
쓰므로, 재시도가 비용에서 차지하는 비중이 보여야 튜닝의 손익을 판단할 수 있다.

요금표 (`API/CLOVA_요금.pdf`, 1,000 토큰당 KRW, **VAT 별도**)
    기본 HCX-005 / HCX-007  입력 1.25원 · 출력 5원
    기본 HCX-DASH-002       입력 0.25원 · 출력 1원
    기본 HCX-003            입출력 통합 5원
    기본 HCX-DASH-001       입출력 통합 1원

토큰 수는 추정하지 않는다 — CLOVA 응답의 `result.inputLength`·`result.outputLength`를
그대로 쓴다(과금 기준과 같은 값). 응답에 없으면 `null`로 남기고 집계에서 제외한다.

사용:
    python -m src.llm_meter --report                  # 기록된 로그 집계
    python -m src.llm_meter --report --md usage.md    # 마크다운 리포트로 저장
    python -m src.llm_meter --report --articles 2695  # 전량 처리 비용 추정
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

from src import config

# 1,000 토큰당 원화. (입력 단가, 출력 단가) — 입출력 통합 과금 모델은 같은 값을 둔다.
PRICING_PER_1K: dict[str, tuple[float, float]] = {
    "HCX-007": (1.25, 5.0),
    "HCX-005": (1.25, 5.0),
    "HCX-DASH-002": (0.25, 1.0),
    "HCX-003": (5.0, 5.0),
    "HCX-DASH-001": (1.0, 1.0),
}
VAT_RATE = 0.10
USAGE_LOG_DEFAULT = "llm_usage.jsonl"


def cost_krw(model: str, input_tokens: int | None, output_tokens: int | None,
             vat: bool = False) -> float | None:
    """호출 1회의 원화 비용. 단가를 모르는 모델이거나 토큰이 없으면 None."""
    price = PRICING_PER_1K.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    krw = (input_tokens * price[0] + output_tokens * price[1]) / 1000.0
    return krw * (1 + VAT_RATE) if vat else krw


@dataclass
class CallRecord:
    """호출 1회의 기록. 캐시 재생도 한 줄로 남긴다(실호출 0원이지만 '했을 비용'을 안다)."""
    ts: float
    model: str
    prompt_version: str = ""
    stage: str = "stage_b"
    attempt: str = "initial"          # initial · repair · resample · stage_c_repair
    article_id: str = ""
    sent_id: str = ""
    cached: bool = False              # True = 캐시 재생(API 미호출)
    ok: bool = True
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None   # 요청 → 응답 (백오프 대기 제외)
    wall_ms: float | None = None      # 재시도 대기까지 포함한 실제 소요
    http_retries: int = 0
    http_status: int | None = None
    error: str = ""
    cost_krw: float | None = None
    extra: dict = field(default_factory=dict)


class UsageMeter:
    """호출 기록을 JSONL로 append. 파이프라인 성능에 영향이 없도록 즉시 flush만 한다."""

    def __init__(self, path: str | Path | None = None, model: str = "",
                 prompt_version: str = "", enabled: bool = True):
        self.path = Path(path) if path else (config.data_dir() / USAGE_LOG_DEFAULT)
        self.model = model
        self.prompt_version = prompt_version
        self.enabled = enabled
        self.records: list[CallRecord] = []
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **kw) -> CallRecord:
        kw.setdefault("ts", time.time())
        kw.setdefault("model", self.model)
        kw.setdefault("prompt_version", self.prompt_version)
        rec = CallRecord(**kw)
        if rec.cost_krw is None and not rec.cached:
            rec.cost_krw = cost_krw(rec.model, rec.input_tokens, rec.output_tokens)
        self.records.append(rec)
        if self.enabled:
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        return rec

    # 세션 중 즉석 요약(파이프라인 종료 로그용)
    def summary(self) -> dict:
        live = [r for r in self.records if not r.cached]
        toks_in = sum(r.input_tokens or 0 for r in live)
        toks_out = sum(r.output_tokens or 0 for r in live)
        return {
            "calls_total": len(self.records),
            "calls_api": len(live),
            "calls_cached": len(self.records) - len(live),
            "input_tokens": toks_in,
            "output_tokens": toks_out,
            "cost_krw": round(sum(r.cost_krw or 0 for r in live), 2),
        }


class HCXTokenizer:
    """토큰 계산기(챗 v3) — `POST /v3/api-tools/chat-tokenize/{model}`.

    인퍼런스가 아니라 **과금 없이** 토큰 수만 세는 API다(요금표에 항목 없음).
    "입력 토큰 중 시스템 프롬프트가 몇 %인가" 같은 비용 구조 분석에 쓴다 —
    추정 대신 실제 토크나이저를 쓰므로 인퍼런스 청구값과 어긋나지 않는다.
    """

    ENDPOINT = "https://clovastudio.stream.ntruss.com/v3/api-tools/chat-tokenize/{model}"

    def __init__(self, model: str = "HCX-005", api_key: str | None = None, timeout: int = 30):
        self.model = model
        self.api_key = api_key or config.get_hcx_api_key()
        self.timeout = timeout

    def count(self, messages: list[dict]) -> list[int]:
        """messages와 같은 길이의 토큰 수 목록."""
        import urllib.request

        req = urllib.request.Request(
            self.ENDPOINT.format(model=self.model),
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for m in data.get("result", {}).get("messages", []):
            out.append(sum(c.get("count", 0) for c in (m.get("content") or [])))
        return out


def load_records(path: str | Path | None = None) -> list[dict]:
    path = Path(path) if path else (config.data_dir() / USAGE_LOG_DEFAULT)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _pct(values: list[float], q: float) -> float:
    """백분위(선형 보간 없음 — 표본이 적을 때 과장되지 않게 최근접 순위법)."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def aggregate(records: list[dict]) -> dict:
    """호출 로그 → 집계 지표. 캐시 재생은 요금·속도 통계에서 제외한다."""
    live = [r for r in records if not r.get("cached")]
    cached = [r for r in records if r.get("cached")]
    ok = [r for r in live if r.get("ok")]
    failed = [r for r in live if not r.get("ok")]

    lat = [r["latency_ms"] for r in ok if r.get("latency_ms") is not None]
    tin = [r["input_tokens"] for r in live if r.get("input_tokens") is not None]
    tout = [r["output_tokens"] for r in live if r.get("output_tokens") is not None]
    costs = [r["cost_krw"] for r in live if r.get("cost_krw") is not None]

    by_attempt: dict[str, dict] = {}
    for r in live:
        a = by_attempt.setdefault(r.get("attempt", "?"),
                                  {"calls": 0, "in": 0, "out": 0, "krw": 0.0})
        a["calls"] += 1
        a["in"] += r.get("input_tokens") or 0
        a["out"] += r.get("output_tokens") or 0
        a["krw"] += r.get("cost_krw") or 0.0

    models = sorted({r.get("model", "") for r in live})
    sentences = {(r.get("article_id"), r.get("sent_id")) for r in records
                 if r.get("sent_id")}
    articles = {r.get("article_id") for r in records if r.get("article_id")}

    krw = sum(costs)
    # 요금 구성 — 입력·출력 중 무엇이 비용을 지배하는지. 단가(출력이 4배 비쌈)만 보고
    # 판단하면 틀린다: 물량이 압도적이면 싼 쪽이 요금을 지배한다(실측: 입력이 92%).
    price = PRICING_PER_1K.get(models[0]) if len(models) == 1 else None
    krw_in = (sum(tin) * price[0] / 1000.0) if price else 0.0
    krw_out = (sum(tout) * price[1] / 1000.0) if price else 0.0

    return {
        "models": models,
        "cost_krw_input": krw_in,
        "cost_krw_output": krw_out,
        "calls_total": len(records),
        "calls_api": len(live),
        "calls_cached": len(cached),
        "calls_failed": len(failed),
        "http_retries": sum(r.get("http_retries") or 0 for r in live),
        "sentences": len(sentences),
        "articles": len(articles),
        "calls_per_sentence": (len(live) / len(sentences)) if sentences else 0.0,
        "input_tokens": sum(tin),
        "output_tokens": sum(tout),
        "total_tokens": sum(tin) + sum(tout),
        "avg_input_tokens": (sum(tin) / len(tin)) if tin else 0.0,
        "avg_output_tokens": (sum(tout) / len(tout)) if tout else 0.0,
        "cost_krw": krw,
        "cost_krw_vat": krw * (1 + VAT_RATE),
        "cost_per_call": (krw / len(costs)) if costs else 0.0,
        "cost_per_sentence": (krw / len(sentences)) if sentences else 0.0,
        "cost_per_article": (krw / len(articles)) if articles else 0.0,
        "latency": {
            "n": len(lat),
            "mean": (sum(lat) / len(lat)) if lat else 0.0,
            "median": statistics.median(lat) if lat else 0.0,
            "p90": _pct(lat, 0.90),
            "p95": _pct(lat, 0.95),
            "max": max(lat) if lat else 0.0,
            "min": min(lat) if lat else 0.0,
        },
        "output_tps": (sum(tout) / (sum(lat) / 1000.0)) if lat and sum(lat) else 0.0,
        "by_attempt": by_attempt,
    }


def _fmt_krw(v: float) -> str:
    return f"{v:,.2f}원"


def render_report(agg: dict, target_articles: int | None = None,
                  concurrency: int = 1) -> str:
    """사람이 읽는 리포트(마크다운). 숫자는 실측만 쓰고, 추정에는 '추정' 꼬리표를 붙인다."""
    L = agg["latency"]
    lines: list[str] = []
    add = lines.append

    add("# LLM 사용량 리포트 (HCX)")
    add("")
    if agg["calls_api"] == 0:
        add("> ⚠ 실호출 기록이 없습니다 — 전부 캐시 재생이거나 로그가 비어 있습니다.")
        add("> 실측치를 얻으려면 캐시를 우회한 실행이 필요합니다"
            "(`--spike N --fresh` 또는 프롬프트 버전 상향).")
        add("")
    add(f"- 모델: **{', '.join(agg['models']) or '—'}**")
    add(f"- 대상: 기사 {agg['articles']}건 · 문장 {agg['sentences']}개")
    add("")

    add("## ⑴ 호출 수")
    add("")
    add("| 항목 | 값 |")
    add("|---|---|")
    add(f"| 총 기록 | {agg['calls_total']:,} |")
    add(f"| **실 API 호출** | **{agg['calls_api']:,}** |")
    add(f"| 캐시 재생(과금 0) | {agg['calls_cached']:,} |")
    add(f"| 실패 호출 | {agg['calls_failed']:,} |")
    add(f"| HTTP 재시도(429·5xx) | {agg['http_retries']:,} |")
    add(f"| 문장당 평균 호출 | {agg['calls_per_sentence']:.2f} |")
    add("")

    add("## ⑵ 토큰")
    add("")
    add("| 항목 | 합계 | 호출당 평균 |")
    add("|---|---|---|")
    add(f"| 입력 | {agg['input_tokens']:,} | {agg['avg_input_tokens']:,.0f} |")
    add(f"| 출력 | {agg['output_tokens']:,} | {agg['avg_output_tokens']:,.0f} |")
    add(f"| **합계** | **{agg['total_tokens']:,}** | "
        f"{agg['avg_input_tokens'] + agg['avg_output_tokens']:,.0f} |")
    add("")

    add("## ⑶ 요금")
    add("")
    add("| 항목 | 금액 |")
    add("|---|---|")
    add(f"| 실측 합계 (VAT 별도) | **{_fmt_krw(agg['cost_krw'])}** |")
    add(f"| 실측 합계 (VAT 10% 포함) | {_fmt_krw(agg['cost_krw_vat'])} |")
    add(f"| 호출 1회당 | {_fmt_krw(agg['cost_per_call'])} |")
    add(f"| 문장 1개당 | {_fmt_krw(agg['cost_per_sentence'])} |")
    add(f"| 기사 1건당 | {_fmt_krw(agg['cost_per_article'])} |")
    add("")

    ci, co = agg.get("cost_krw_input", 0.0), agg.get("cost_krw_output", 0.0)
    if ci or co:
        tot = ci + co
        add("| 구성 | 토큰 | 요금 | 비중 |")
        add("|---|---|---|---|")
        add(f"| 입력 | {agg['input_tokens']:,} | {_fmt_krw(ci)} | {ci/tot*100:.1f}% |")
        add(f"| 출력 | {agg['output_tokens']:,} | {_fmt_krw(co)} | {co/tot*100:.1f}% |")
        add("")
        ratio = (agg["input_tokens"] / agg["output_tokens"]) if agg["output_tokens"] else 0
        if ci > co:
            add(f"> **비용을 지배하는 것은 입력이다** — 단가는 출력이 4배 비싸지만 "
                f"물량이 입력:출력 = {ratio:.0f}:1이라 요금의 {ci/tot*100:.0f}%가 입력에서 나온다. "
                "줄일 대상 1순위는 **매 호출 반복되는 시스템 프롬프트**다 "
                "(`--breakdown`으로 구성 확인).")
        else:
            add(f"> 출력이 요금의 {co/tot*100:.0f}%를 차지한다 — 출력 길이(항목 수·note 길이)를 먼저 줄인다.")
        add("")
    add("> 단가: HCX-005 기본 — 입력 1,000토큰 1.25원 · 출력 1,000토큰 5원 "
        "(`API/CLOVA_요금.pdf`, VAT 별도).")
    add("")

    add("## ⑷ 응답 속도")
    add("")
    add("| 지표 | 값 |")
    add("|---|---|")
    add(f"| 표본 | {L['n']:,}콜 |")
    add(f"| 평균 | {L['mean']/1000:.2f}초 |")
    add(f"| 중앙값 | {L['median']/1000:.2f}초 |")
    add(f"| p90 | {L['p90']/1000:.2f}초 |")
    add(f"| p95 | {L['p95']/1000:.2f}초 |")
    add(f"| 최소 / 최대 | {L['min']/1000:.2f}초 / {L['max']/1000:.2f}초 |")
    add(f"| 출력 처리량 | {agg['output_tps']:,.1f} tok/s |")
    add("")

    if agg["by_attempt"]:
        add("## ⑸ 시도 유형별 — 재시도가 비용에서 차지하는 몫")
        add("")
        add("| 유형 | 호출 | 입력 | 출력 | 요금 | 비중 |")
        add("|---|---|---|---|---|---|")
        total = agg["cost_krw"] or 1.0
        order = {"initial": 0, "repair": 1, "resample": 2, "stage_c_repair": 3}
        for name in sorted(agg["by_attempt"], key=lambda k: order.get(k, 9)):
            a = agg["by_attempt"][name]
            add(f"| `{name}` | {a['calls']:,} | {a['in']:,} | {a['out']:,} | "
                f"{_fmt_krw(a['krw'])} | {a['krw']/total*100:.1f}% |")
        add("")

    if target_articles and agg["cost_per_article"] > 0:
        per_art_krw = agg["cost_per_article"]
        per_art_calls = agg["calls_api"] / max(agg["articles"], 1)
        per_art_sec = per_art_calls * (L["mean"] / 1000.0)
        total_krw = per_art_krw * target_articles
        total_sec = per_art_sec * target_articles / max(concurrency, 1)
        add(f"## ⑹ 규모 추정 — 기사 {target_articles:,}건 (동시성 {concurrency})")
        add("")
        add("| 항목 | 추정치 |")
        add("|---|---|")
        add(f"| API 호출 | 약 {per_art_calls * target_articles:,.0f}회 |")
        add(f"| 토큰 | 약 {(agg['total_tokens']/max(agg['articles'],1))*target_articles:,.0f} |")
        add(f"| 요금 (VAT 별도) | **약 {_fmt_krw(total_krw)}** |")
        add(f"| 요금 (VAT 포함) | 약 {_fmt_krw(total_krw * (1 + VAT_RATE))} |")
        add(f"| 소요 시간 | 약 {total_sec/60:.1f}분 ({total_sec/3600:.2f}시간) |")
        add("")
        add(f"> 기사당 실측 평균(호출 {per_art_calls:.1f}회 · {_fmt_krw(per_art_krw)} · "
            f"{per_art_sec:.1f}초)을 선형 확장한 **추정치**다. 기사 길이 분포가 표본과 다르면 "
            "그만큼 어긋난다 — 표본이 작을수록 참고용으로만 볼 것.")
        add("")

    return "\n".join(lines)


def input_breakdown(n_samples: int = 10, model: str = "HCX-005") -> str:
    """입력 토큰의 구성(시스템 프롬프트 vs 문장별 사용자 메시지)을 실측해 절감 여지를 낸다.

    시스템 프롬프트는 **모든 호출에 통째로 반복**되므로, 여기가 크면 문장 수에 비례해
    비용이 곱해진다. 토큰 계산기 API를 쓰므로 인퍼런스 과금은 발생하지 않는다.
    """
    from src.p3_stage_a import collect_candidates
    from src.p3_stage_b import PROMPT_V1, build_sentence_index, build_user_message

    tok = HCXTokenizer(model)
    system = PROMPT_V1.read_text(encoding="utf-8")
    sent_index = build_sentence_index()
    cands, _, _ = collect_candidates()
    sample = cands[:n_samples]

    sys_tokens = tok.count([{"role": "system", "content": system}])[0]
    user_tokens = []
    for c in sample:
        msg = build_user_message(c, sent_index)
        user_tokens.append(tok.count([{"role": "user", "content": msg}])[0])

    avg_user = sum(user_tokens) / len(user_tokens) if user_tokens else 0
    total = sys_tokens + avg_user
    price_in = PRICING_PER_1K.get(model, (1.25, 5.0))[0]

    lines = ["# 입력 토큰 구성 (호출 1회 기준)", "",
             f"- 모델: **{model}** · 표본 {len(sample)}문장 · 토큰 계산기 API 실측(과금 없음)", "",
             "| 구성 | 토큰 | 비중 | 성격 |", "|---|---|---|---|",
             f"| 시스템 프롬프트 | {sys_tokens:,} | {sys_tokens/total*100:.1f}% | "
             "**매 호출 반복** — 문장 수만큼 곱해진다 |",
             f"| 사용자 메시지(평균) | {avg_user:,.0f} | {avg_user/total*100:.1f}% | "
             "문장·맥락·앵커 — 문장마다 다름 |",
             f"| **합계** | **{total:,.0f}** | 100% | |", "",
             f"- 사용자 메시지 범위: {min(user_tokens):,} ~ {max(user_tokens):,} 토큰", ""]

    if sys_tokens > avg_user:
        for cut in (0.3, 0.5):
            saved = sys_tokens * cut * price_in / 1000
            lines.append(f"- 시스템 프롬프트를 **{cut:.0%} 줄이면** 호출당 "
                         f"{sys_tokens*cut:,.0f}토큰 · {saved:.2f}원 절감 "
                         f"(1,000콜당 {saved*1000:,.0f}원)")
        lines += ["", "> 다만 few-shot을 걷어내면 추출 품질이 떨어질 수 있다 — "
                  "**축약판 프롬프트는 dev 채점으로 품질 손실을 확인한 뒤** 채택할 것(§5.6)."]
    return "\n".join(lines)


def main(argv=None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="LLM 사용량 집계 리포트")
    ap.add_argument("--report", action="store_true", help="집계 리포트 출력")
    ap.add_argument("--breakdown", type=int, nargs="?", const=10, default=0,
                    metavar="N", help="입력 토큰 구성 분석 (표본 N문장, 기본 10). 과금 없음")
    ap.add_argument("--input", type=Path, default=None,
                    help=f"사용량 로그 (기본: data/{USAGE_LOG_DEFAULT})")
    ap.add_argument("--md", type=Path, default=None, help="마크다운 리포트 저장 경로")
    ap.add_argument("--articles", type=int, default=0,
                    help="이 기사 수로 규모 추정 (예: 2695 = news.csv 전량)")
    ap.add_argument("--concurrency", type=int, default=1, help="동시 호출 수(소요 시간 추정용)")
    args = ap.parse_args(argv)

    if args.breakdown:
        print(input_breakdown(args.breakdown))
        if not args.report:
            return
        print()

    if not args.report:
        ap.error("--report 또는 --breakdown 을 지정하세요")

    records = load_records(args.input)
    if not records:
        path = args.input or (config.data_dir() / USAGE_LOG_DEFAULT)
        print(f"사용량 로그가 비어 있습니다: {path}")
        print("먼저 계측이 켜진 상태로 실행하세요 — 예: python -m src.p3_stage_b --spike 20 --fresh")
        return
    report = render_report(aggregate(records), args.articles or None, args.concurrency)
    print(report)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(report, encoding="utf-8")
        print(f"\n리포트 저장: {args.md}")


if __name__ == "__main__":
    main()
