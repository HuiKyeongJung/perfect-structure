# -*- coding: utf-8 -*-
"""P3 평가 하네스 — 골든셋 채점기 (CLAUDE.md §5.6 평가 설계).

매칭(문장 단위 2단계):
  1차: value+unit 정확 일치(공백 제거) — 동일 키 다수면 metric 유사도로 짝 선택
  2차: 잔여 gold×pred를 (수치 코어 일치 + metric 유사도) 점수로 그리디 매칭(임계 미달은 미매칭)
  → 순서 의존 없음(골든에 동일 value+unit 충돌 6건 실재), value 오류 Claim도
    2차에서 매칭되어 '검출 실패'가 아니라 'value 필드 오류'로 계상된다.

지표 3층:
  ⑴ Claim 검출 P/R/F1  ⑵ 매칭 쌍 한정 필드별 정확도(support 병기)
  ⑶ 인수인계 품질 — 7필드 완전 일치율 + 위험 지표(pred eligible=true ∧ 핵심 필드 오류, eligible=true FP)

부수: 제외(excluded) 코드 채점 · 문장 커버리지(전수 회계) 검사.
리포트에 (골든 버전 · 프롬프트 버전 · pipeline_version) 3튜플 태깅.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from src.p3_schemas import ClaimRecord, DocumentSet, PIPELINE_VERSION

# 필드별 정확도 대상(매칭 쌍 한정). claim_id·claim(문장)은 매칭 구조상 제외.
# metric_normalized는 55차부터 채점 대상이 아니다 — 골든의 값은 KOSIS 검증을 거치지
# 않은 합성값이고 파이프라인은 verbatim을 쓰므로, 비교해도 "미검증 합성값과의 합치율"만
# 잴 뿐이다. 3번의 검색 통과분으로 골든이 갱신되면 그때 채점 대상으로 되돌린다.
FIELDS = ["metric", "value", "unit", "period", "forecast",
          "kosis_eligible", "value_type", "direction", "exclusion_code"]
# 인수인계 품질(계약 필드 일치)에서 보는 핵심 필드 — claim_id·claim은 구조상 동일.
# metric_normalized는 위 사유로 제외(파이프라인 값 = metric이라 metric 채점과 중복).
HANDOFF_FIELDS = ["metric", "value", "unit", "period", "kosis_eligible"]
# 1차(정확 키) 매칭에도 최소 metric 유사도를 요구 — 같은 값의 '남의 수치'가 옳은 짝을
# 가로채 오류 귀속을 뒤집는 것 방지(%↔%p 진단 보존). 미달 쌍은 2차로 미루며,
# 2차는 수치 코어 일치로 회수하므로 정당한 짝은 잃지 않는다. 한국어 짧은 지표명은
# 무관해도 sim 0.3대가 흔해(예: '수출 증가율'↔'설비 가동률'=0.33) 0.5로 둔다.
STAGE1_MIN_METRIC_SIM = 0.5
# 2차 통과: 수치 코어 일치 또는 metric 유사도 고임계 — 느슨한 합산 점수는
# '다른 지표·다른 수치'를 TP로 흡수해 검출 지표를 과대평가한다(리뷰 실측 반례).
STAGE2_MIN_METRIC_SIM = 0.75


def _norm(s) -> str:
    if isinstance(s, bool):
        return "TRUE" if s else "FALSE"
    return re.sub(r"\s", "", str(s or ""))


def _num_core(value: str) -> str:
    """value의 수치 코어(숫자·소수점만) — '386억7200만' → '3867200'."""
    return re.sub(r"[^\d.]", "", value or "")


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a or "", b or "").ratio()


def _pair_score(g: ClaimRecord, p: ClaimRecord) -> float:
    core = 1.0 if _num_core(g.value) and _num_core(g.value) == _num_core(p.value) else 0.0
    return 0.4 * core + 0.6 * _sim(g.metric, p.metric)


def match_sentence(gold: list[ClaimRecord], pred: list[ClaimRecord]
                   ) -> tuple[list[tuple[ClaimRecord, ClaimRecord]], list[ClaimRecord], list[ClaimRecord]]:
    """한 문장의 gold·pred Claim들을 짝짓는다 → (pairs, 미검출 gold, 과잉 pred)."""
    g_left = list(gold)
    p_left = list(pred)
    pairs: list[tuple[ClaimRecord, ClaimRecord]] = []

    # 1차: value+unit 정확 키. 같은 키가 여럿이면 metric 유사도 높은 짝부터.
    def key(c: ClaimRecord) -> str:
        return _norm(c.value) + _norm(c.unit)

    keys = {key(g) for g in g_left} & {key(p) for p in p_left}
    for k in keys:
        gs = [g for g in g_left if key(g) == k]
        ps = [p for p in p_left if key(p) == k]
        cand = sorted(((_sim(g.metric, p.metric), id(g), id(p), g, p) for g in gs for p in ps),
                      key=lambda t: -t[0])
        used_g, used_p = set(), set()
        for sim, gi, pi, g, p in cand:
            if sim < STAGE1_MIN_METRIC_SIM:
                break  # 정렬돼 있으므로 이후는 전부 미달 — 2차로
            if gi in used_g or pi in used_p:
                continue
            used_g.add(gi); used_p.add(pi)
            pairs.append((g, p))
            g_left.remove(g); p_left.remove(p)

    # 2차: 잔여 그리디 — value가 틀린 Claim을 '필드 오류'로 회수.
    # 통과 조건: 수치 코어 일치(같은 숫자, 단위·형식만 다름) 또는 metric 고유사(값 오염 케이스)
    def stage2_ok(g: ClaimRecord, p: ClaimRecord) -> bool:
        core_g, core_p = _num_core(g.value), _num_core(p.value)
        core_match = bool(core_g) and core_g == core_p
        return core_match or _sim(g.metric, p.metric) >= STAGE2_MIN_METRIC_SIM

    cand = sorted(((_pair_score(g, p), id(g), id(p), g, p) for g in g_left for p in p_left
                   if stage2_ok(g, p)),
                  key=lambda t: -t[0])
    used_g, used_p = set(), set()
    for score, gi, pi, g, p in cand:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi); used_p.add(pi)
        pairs.append((g, p))
    g_left = [g for g in g_left if id(g) not in used_g]
    p_left = [p for p in p_left if id(p) not in used_p]
    return pairs, g_left, p_left


@dataclass
class EvalReport:
    golden_version: str = ""
    prompt_version: str = ""
    pipeline_version: str = PIPELINE_VERSION
    # ⑴ 검출
    tp: int = 0
    fn: int = 0
    fp: int = 0
    # ⑵ 필드별 {field: [correct, total]}
    field_acc: dict = field(default_factory=lambda: {f: [0, 0] for f in FIELDS})
    # ⑶ 인수인계 품질
    handoff_full: int = 0            # 매칭 쌍 중 HANDOFF_FIELDS 완전 일치
    risk_matched: int = 0            # pred eligible=true ∧ 핵심 필드 오류
    risk_fp: int = 0                 # eligible=true인 과잉 Claim(FP)
    normalized_missing: int = 0      # metric_normalized 미제공(null) — 계약상 합법(2번 verbatim 폴백)
    # 제외 채점
    excl_tp: int = 0
    excl_fn: int = 0
    excl_fp: int = 0
    # 커버리지(전수 회계) — 양방향: 골든 문장 누락 + 골든 밖 문장 생성(가짜 Claim 계통 신호)
    missing_sentences: list = field(default_factory=list)
    extra_sentences: list = field(default_factory=list)
    mismatches: list = field(default_factory=list)   # (claim_id, field, gold, pred) 표본

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def acc(self, f: str) -> float:
        c, t = self.field_acc[f]
        return c / t if t else 1.0

    def to_markdown(self) -> str:
        lines = [
            f"# P3 채점 리포트  (골든 {self.golden_version} · 프롬프트 {self.prompt_version or '-'} · {self.pipeline_version})",
            "",
            f"## ⑴ Claim 검출 — P {self.precision:.3f} / R {self.recall:.3f} / F1 {self.f1:.3f}"
            f"   (TP {self.tp} · FN {self.fn} · FP {self.fp})",
            "",
            "## ⑵ 필드별 정확도 (매칭 쌍 한정)",
            "| 필드 | 정확도 | support |", "|---|---|---|",
        ]
        for f in FIELDS:
            c, t = self.field_acc[f]
            lines.append(f"| {f} | {self.acc(f):.3f} | {c}/{t} |")
        total_pairs = self.tp
        full_rate = self.handoff_full / total_pairs if total_pairs else 0.0
        lines += [
            "",
            f"## ⑶ 인수인계 품질 — 계약 필드 완전 일치 {self.handoff_full}/{total_pairs} ({full_rate:.3f})",
            f"- ⚠ 위험(eligible=true ∧ 필드 오류): 매칭 {self.risk_matched}건 + 과잉 {self.risk_fp}건",
            "- ※ metric_normalized는 채점 제외(55차) — 골든값이 KOSIS 미검증 합성값이고 "
            "파이프라인은 verbatim을 쓴다. 3번 검증분이 들어오면 채점 대상으로 복귀",
            "",
            f"## 제외(excluded) — 일치 {self.excl_tp} · 누락 {self.excl_fn} · 과잉 {self.excl_fp}",
            f"## 커버리지 — 미처리 문장 {len(self.missing_sentences)}건 · 골든 밖 문장 {len(self.extra_sentences)}건",
        ]
        if self.extra_sentences:
            lines.append(f"  ⚠ 골든 밖 문장(가짜 Claim 의심 — P1/P2 회귀 신호): {self.extra_sentences[:10]}")
        if self.mismatches:
            lines += ["", "## 불일치 표본 (최대 30)"]
            for cid, f, g, p in self.mismatches[:30]:
                lines.append(f"- {cid} · {f}: 골든 {g!r} ↔ 파이프라인 {p!r}")
        return "\n".join(lines)


def _field_val(c: ClaimRecord, f: str):
    v = getattr(c, f)
    if f == "forecast":
        return (v or "N").upper() or "N"
    return v


def evaluate(gold: DocumentSet, pred: DocumentSet, prompt_version: str = "") -> EvalReport:
    # pred 입구 가드 — eligible 미파생(None) 묵살·문자열 'FALSE' truthy 함정 차단(리뷰 지적)
    for c in pred.claims:
        c.finalize()
        if not isinstance(c.kosis_eligible, bool):
            raise TypeError(f"{c.claim_id}: kosis_eligible이 bool이 아님({type(c.kosis_eligible).__name__})")
    rep = EvalReport(golden_version=gold.version, prompt_version=prompt_version)

    # 문장 키 그룹
    def group(claims: list[ClaimRecord]) -> dict:
        d: dict[tuple[str, str], list[ClaimRecord]] = {}
        for c in claims:
            d.setdefault((c.article_id, c.sent_id), []).append(c)
        return d

    g_by, p_by = group(gold.claims), group(pred.claims)
    for key in sorted(set(g_by) | set(p_by)):
        pairs, fns, fps = match_sentence(g_by.get(key, []), p_by.get(key, []))
        rep.tp += len(pairs)
        rep.fn += len(fns)
        rep.fp += len(fps)
        rep.risk_fp += sum(1 for p in fps if p.kosis_eligible)
        for g, p in pairs:
            handoff_ok = True
            for f in FIELDS:
                gv, pv = _norm(_field_val(g, f)), _norm(_field_val(p, f))
                if not gv and not pv:
                    continue  # 양쪽 빈값 = 자명 일치 — 분모 제외(희소 필드 정확도 인플레이션 방지)
                if f == "metric_normalized" and not pv and gv:
                    # 미제공 null은 계약상 합법(모호 4종·미승인 — 2번 verbatim 폴백)이라
                    # 오류·위험으로 계상하면 완벽 추출도 상한에 갇힌다(리뷰 실측 96.1%) — 별도 집계
                    rep.normalized_missing += 1
                    continue
                rep.field_acc[f][1] += 1
                if gv == pv:
                    rep.field_acc[f][0] += 1
                else:
                    if len(rep.mismatches) < 200:
                        rep.mismatches.append((g.claim_id, f, _field_val(g, f), _field_val(p, f)))
                    if f in HANDOFF_FIELDS:
                        handoff_ok = False
            if handoff_ok:
                rep.handoff_full += 1
            elif p.kosis_eligible:
                rep.risk_matched += 1

    # 제외 채점 — 문장 단위 코드 다중집합 비교
    def egroup(ds: DocumentSet) -> dict:
        d: dict[tuple[str, str], list[str]] = {}
        for e in ds.excluded:
            d.setdefault((e.article_id, e.sent_id), []).append(e.exclusion_code)
        return d

    ge, pe = egroup(gold), egroup(pred)
    for key in set(ge) | set(pe):
        gc, pc = sorted(ge.get(key, [])), sorted(pe.get(key, []))
        matched = 0
        pc_left = list(pc)
        for code in gc:
            if code in pc_left:
                pc_left.remove(code)
                matched += 1
        rep.excl_tp += matched
        rep.excl_fn += len(gc) - matched
        rep.excl_fp += len(pc_left)

    # 커버리지 — 양방향(전수 회계 + 가짜 Claim 계통 신호)
    rep.missing_sentences = sorted(gold.sentence_keys() - pred.sentence_keys())
    rep.extra_sentences = sorted(pred.sentence_keys() - gold.sentence_keys())
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description="P3 채점기 — 골든 자가 채점(self) 또는 산출물 채점")
    ap.add_argument("--golden", type=Path, default=None, help="골든 xlsx 경로")
    ap.add_argument("--self-check", action="store_true", help="골든 vs 골든 자가 채점(100%% 검증)")
    ap.add_argument("--report", type=Path, default=None, help="리포트 md 저장 경로")
    args = ap.parse_args()

    from src.p3_golden import load_golden, GOLDEN_DEFAULT
    gold = load_golden(args.golden or GOLDEN_DEFAULT)
    if args.self_check:
        rep = evaluate(gold, gold, prompt_version="self")
        md = rep.to_markdown()
        print(md)
        ok = (rep.f1 == 1.0 and rep.fn == 0 and rep.fp == 0
              and all(rep.acc(f) == 1.0 for f in FIELDS)
              and rep.handoff_full == rep.tp
              and not rep.missing_sentences and not rep.extra_sentences
              and rep.excl_fn == 0 and rep.excl_fp == 0)
        print(f"\n자가 채점 {'통과' if ok else '실패'}")
        if args.report:
            args.report.write_text(md, encoding="utf-8")
        raise SystemExit(0 if ok else 1)
    ap.error("--self-check를 지정하세요 (산출물 채점: src.p3_emit.load_documents_jsonl로 적재 후 evaluate())")


if __name__ == "__main__":
    main()
